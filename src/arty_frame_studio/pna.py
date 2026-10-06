"""VNA Keysight PNA / PNA-X / Streamline : mesure des paramètres S, simulation, détection.

``PnaDriver`` déclenche un balayage unique d'un canal puis relit les paramètres S
de tous les ports choisis (S11, S21… SNN) : un état = un balayage = une matrice.
Les mesures nécessaires sont créées sur le canal si elles n'y sont pas déjà, puis
supprimées à la fin ; le mode de balayage et la source de déclenchement d'origine
sont rétablis.

Les commandes suivent le jeu SCPI des analyseurs PNA (N5245B…) et des VNA USB
Streamline (P9374A…). Elles n'ont été vérifiées que contre ``SimulatedPna`` : la
première mesure sur un appareil réel est à surveiller (voir docs/mode-vna.md).
"""

from __future__ import annotations

import ipaddress
import math
import re
import socket
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .instruments import InstrumentError, ScpiInstrument, open_transport, parse_numbers
from .scope import SCPI_PORT, ScopeError, format_si, list_visa_resources
from .touchstone import MAX_PORTS, SParameters

MEASUREMENT_PREFIX = "AFS"
MAX_STATE_ERRORS = 5
_VNA_MODEL = re.compile(r"^(?:N5\d{3}|E83\d\d|E50\d\d|P5\d{3}|P9\d{3}|M9\d{3}|N9\d{3})", re.I)
LOCAL_HOSTS = ("127.0.0.1",)


class VnaError(InstrumentError):
    """Le VNA n'a pas pu être préparé ou mesuré comme demandé."""


def _quoted_list(reply: str) -> list[str]:
    text = reply.strip().strip('"').strip("'")
    if not text or text.upper() == "NO CATALOG":
        return []
    return [item.strip().strip('"').strip("'") for item in text.split(",") if item.strip()]


def _single_number(reply: str) -> float:
    return parse_numbers(reply)[0]


def parameter_name(i: int, j: int) -> str:
    return f"S{i}{j}"


class PnaDriver:
    """Un canal, ``ports`` ports : balayage unique puis lecture des paramètres S."""

    def __init__(self, instrument: ScpiInstrument, channel: int, ports: int) -> None:
        if type(channel) is not int or not 1 <= channel <= 200:
            raise ValueError("Canal entre 1 et 200 requis.")
        if type(ports) is not int or not 1 <= ports <= MAX_PORTS:
            raise ValueError(f"Nombre de ports entre 1 et {MAX_PORTS} requis.")
        self.instrument = instrument
        self.channel = channel
        self.ports = ports
        self.frequencies: tuple[float, ...] = ()
        self.names: dict[tuple[int, int], str] = {}
        self.created: list[str] = []
        self.averages = 1
        self._saved_mode = ""
        self._saved_trigger = ""
        self.prepared = False

    # -- préparation ------------------------------------------------------------
    def _run(self, *commands: str) -> list[str]:
        return self.instrument.run(commands)

    def available_channels(self) -> list[int]:
        reply = self._run("SYST:CHAN:CAT?")[0]
        return [int(value) for value in _quoted_list(reply) if value.lstrip("+").isdigit()]

    def _catalog(self) -> dict[str, str]:
        """Paramètre (``S21``) → nom de la première mesure qui le porte sur le canal."""
        items = _quoted_list(self._run(f"CALC{self.channel}:PAR:CAT?")[0])
        found: dict[str, str] = {}
        for name, parameter in zip(items[0::2], items[1::2], strict=False):
            found.setdefault(parameter.upper(), name)
        return found

    def prepare(self) -> None:
        """Vérifie le canal, crée les mesures manquantes, lit l'axe de fréquence."""
        c = self.channel
        channels = self.available_channels()
        if c not in channels:
            known = ", ".join(str(number) for number in channels) or "aucun"
            raise VnaError(
                f"Le canal {c} n'existe pas sur l'instrument (canaux présents : {known}). "
                "Le créer sur le VNA (Trace/Chan → New Channel) ou choisir un autre numéro."
            )
        self._run("FORM:DATA ASCII,0")
        self._saved_mode = self._run(f"SENS{c}:SWE:MODE?")[0].strip().strip('"')
        self._saved_trigger = self._run("TRIG:SOUR?")[0].strip().strip('"')
        existing = self._catalog()
        self.names, self.created = {}, []
        for i in range(1, self.ports + 1):
            for j in range(1, self.ports + 1):
                parameter = parameter_name(i, j)
                if parameter in existing:
                    self.names[(i, j)] = existing[parameter]
                    continue
                name = f"{MEASUREMENT_PREFIX}_{parameter}"
                self._run(f"CALC{c}:PAR:DEF:EXT '{name}','{parameter}'")
                self.created.append(name)
                self.names[(i, j)] = name
        self.instrument.check(
            f"Création des mesures S sur le canal {c} "
            f"(le VNA a-t-il {self.ports} port(s) et une calibration compatible ?)"
        )
        averaging = int(_single_number(self._run(f"SENS{c}:AVER:STAT?")[0]))
        count = int(_single_number(self._run(f"SENS{c}:AVER:COUN?")[0])) if averaging else 1
        self.averages = max(1, count)
        self._run(f"CALC{c}:PAR:SEL '{self.names[(1, 1)]}'")
        axis = parse_numbers(self._run(f"CALC{c}:X?")[0])
        if len(axis) < 1:
            raise VnaError("Le VNA n'a donné aucun point de fréquence.")
        self.frequencies = tuple(axis)
        self._run("TRIG:SOUR IMM")
        self.instrument.check("Préparation du balayage")
        self.prepared = True

    def describe(self) -> str:
        if not self.frequencies:
            return f"Canal {self.channel}, {self.ports} port(s)."
        first, last = self.frequencies[0], self.frequencies[-1]
        average = f", moyennage ×{self.averages}" if self.averages > 1 else ""
        created = f" · {len(self.created)} mesure(s) créée(s)" if self.created else ""
        return (
            f"Canal {self.channel} · {self.ports} port(s) · {len(self.frequencies)} point(s) "
            f"de {format_si(first, 'Hz', 4).strip()} à {format_si(last, 'Hz', 4).strip()}"
            f"{average}{created}"
        )

    # -- mesure -------------------------------------------------------------------
    def acquire(self) -> tuple[SParameters, list[str]]:
        """Un balayage complet puis la matrice S ; le second résultat liste les erreurs SCPI."""
        if not self.prepared:
            raise VnaError("Le VNA n'est pas préparé.")
        c = self.channel
        if self.averages > 1:
            self._run(
                f"SENS{c}:AVER:CLE",
                f"SENS{c}:SWE:GRO:COUN {self.averages}",
                f"SENS{c}:SWE:MODE GRO;*OPC?",
            )
        else:
            self._run(f"SENS{c}:SWE:MODE SING;*OPC?")
        points = len(self.frequencies)
        matrix: dict[tuple[int, int], tuple[complex, ...]] = {}
        for (i, j), name in self.names.items():
            reply = self._run(f"CALC{c}:PAR:SEL '{name}'", f"CALC{c}:DATA? SDATA")[0]
            numbers = parse_numbers(reply)
            if len(numbers) != 2 * points:
                raise VnaError(
                    f"{parameter_name(i, j)} : {len(numbers)} valeur(s) reçue(s) pour "
                    f"{points} point(s) ; le nombre de points a-t-il changé sur le VNA ?"
                )
            matrix[(i, j)] = tuple(
                complex(numbers[index], numbers[index + 1]) for index in range(0, len(numbers), 2)
            )
        problems = self.instrument.errors()[:MAX_STATE_ERRORS]
        return SParameters(self.ports, self.frequencies, matrix), problems

    def restore(self) -> None:
        """Supprime les mesures créées, rétablit déclenchement et mode de balayage."""
        if not self.prepared:
            return
        c = self.channel
        commands = [f"CALC{c}:PAR:DEL '{name}'" for name in self.created]
        if self._saved_mode:
            mode = "CONT" if self._saved_mode.upper().startswith("CONT") else "HOLD"
            commands.append(f"SENS{c}:SWE:MODE {mode}")
        if self._saved_trigger:
            commands.append(f"TRIG:SOUR {self._saved_trigger}")
        self.prepared = False
        self.created = []
        self._run(*commands)


# -- simulation -----------------------------------------------------------------------
def _normalize(header: str) -> tuple[str, int | None]:
    """``CALCulate1:PARameter:CATalog?`` → (``CAL:PAR:CAT?``, 1) : trois lettres par mot."""
    channel: int | None = None
    parts = []
    for part in header.strip().upper().split(":"):
        if not part:
            continue
        question = part.endswith("?")
        part = part.rstrip("?")
        match = re.fullmatch(r"([A-Z*]+)(\d*)", part)
        if match is None:
            parts.append(part)
            continue
        word, digits = match.groups()
        if digits and word[:3] in ("CAL", "SEN"):
            channel = int(digits)
        parts.append(word if word.startswith("*") else word[:3])
        if question:
            parts[-1] += "?"
    return ":".join(parts), channel


class SimulatedPna:
    """VNA simulé : mêmes commandes que ``PnaDriver``, matrice S qui dépend du mot.

    S21 = -(0,5 dB × bits 5..0 du mot) à 1 GHz et sa phase augmente de 5,625° par
    pas des bits 11..6 ; les réflexions valent -20 dB. Rien de physique : c'est un
    moyen de découvrir le mode VNA, ou de le tester, sans appareil.
    """

    IDENTITY = "Keysight Technologies,N5245B-SIM,SIM00001,A.00.00 (Arty Frame Studio, démo)"

    def __init__(self, *, max_ports: int = 4, points: int = 51, averaging: int = 0) -> None:
        self.max_ports = max_ports
        self.frequencies = [1e9 + index * 1e8 for index in range(points)]
        self.averaging = averaging
        self.word = 0
        self.channels = [1, 2]
        self.measurements: dict[str, str] = {"CH1_S11_1": "S11"}
        self.selected: str | None = None
        self.commands: list[str] = []
        self.errors: list[str] = []
        self.sweeps: list[int] = []  # mot présent à chaque balayage
        self.mode = "CONT"
        self.trigger = "INT"
        self.closed = False
        self._lock = threading.Lock()

    def set_word(self, word: int) -> None:
        self.word = word

    # -- modèle ------------------------------------------------------------------------
    def parameter(self, i: int, j: int, index: int) -> complex:
        ghz = self.frequencies[index] / 1e9
        if i == j:
            return 0.1 * complex(math.cos(0.3 * ghz * i), math.sin(0.3 * ghz * i))
        if {i, j} == {1, 2}:
            attenuation = 0.5 * (self.word & 0x3F) + 0.1 * ghz
            phase = math.radians(5.625 * ((self.word >> 6) & 0x3F) + 3.6 * ghz)
            return 10 ** (-attenuation / 20) * complex(math.cos(phase), math.sin(phase))
        return 0.01 * complex(math.cos(ghz * (i + j)), math.sin(ghz * (i + j)))

    # -- commandes ------------------------------------------------------------------------
    def _error(self, code: int, text: str) -> None:
        self.errors.append(f'{code},"{text}"')

    def _execute(self, command: str) -> str | None:
        text = command.strip()
        header, _, argument = text.partition(" ")
        key, channel = _normalize(header)
        argument = argument.strip()
        if key == "*IDN?":
            return self.IDENTITY
        if key == "*OPC?":
            return "1"
        if key in ("*CLS", "*RST", "FOR:DAT"):
            return None
        if key == "SYS:ERR?":
            return self.errors.pop(0) if self.errors else '+0,"No error"'
        if key == "SYS:CHA:CAT?":
            return '"' + ",".join(str(number) for number in self.channels) + '"'
        if key == "TRI:SOU?":
            return self.trigger
        if key == "TRI:SOU":
            self.trigger = argument.upper()
            return None
        if channel is not None and channel not in self.channels:
            self._error(-224, f"Illegal parameter value: channel {channel}")
            return None if not key.endswith("?") else ""
        if key == "CAL:PAR:CAT?":
            if channel != 1 or not self.measurements:
                return '"NO CATALOG"'
            return '"' + ",".join(f"{n},{p}" for n, p in self.measurements.items()) + '"'
        if key == "CAL:PAR:DEF:EXT":
            match = re.fullmatch(r"'([^']+)'\s*,\s*'?(S\d\d)'?", argument, re.IGNORECASE)
            if match is None:
                self._error(-102, f"Syntax error: {text}")
                return None
            name, parameter = match.group(1), match.group(2).upper()
            i, j = int(parameter[1]), int(parameter[2])
            if max(i, j) > self.max_ports:
                self._error(-224, f"Illegal parameter value: {parameter}")
            else:
                self.measurements[name] = parameter
            return None
        if key == "CAL:PAR:SEL":
            name = argument.split(",")[0].strip().strip("'")
            if name not in self.measurements:
                self._error(-224, f"Illegal parameter value: no measurement {name}")
            else:
                self.selected = name
            return None
        if key == "CAL:PAR:DEL":
            self.measurements.pop(argument.strip().strip("'"), None)
            return None
        if key == "CAL:X?":
            return ",".join(f"{value:.9E}" for value in self.frequencies)
        if key == "CAL:DAT?":
            if self.selected is None or self.selected not in self.measurements:
                self._error(-221, "Settings conflict: no selected measurement")
                return ""
            parameter = self.measurements[self.selected]
            i, j = int(parameter[1]), int(parameter[2])
            values: list[float] = []
            for index in range(len(self.frequencies)):
                value = self.parameter(i, j, index)
                values.extend((value.real, value.imag))
            return ",".join(f"{value:.9E}" for value in values)
        if key == "SEN:SWE:MOD?":
            return self.mode
        if key == "SEN:SWE:MOD":
            mode = argument.upper()
            if mode.startswith(("SING", "GRO")):
                self.sweeps.append(self.word)
                self.mode = "HOLD"
            else:
                self.mode = mode
            return None
        if key == "SEN:AVE:STA?":
            return "1" if self.averaging else "0"
        if key == "SEN:AVE:COU?":
            return str(self.averaging or 1)
        if key in ("SEN:AVE:CLE", "SEN:SWE:GRO:COU"):
            return None
        self._error(-113, f"Undefined header: {text}")
        return None

    def write(self, command: str) -> None:
        if self.closed:
            raise ScopeError("VNA simulé fermé.")
        with self._lock:
            self.commands.append(command)
            for part in command.split(";"):
                if part.strip().endswith("?"):
                    self._error(-410, "Query INTERRUPTED")
                elif part.strip():
                    self._execute(part)

    def query(self, command: str) -> str:
        if self.closed:
            raise ScopeError("VNA simulé fermé.")
        with self._lock:
            self.commands.append(command)
            reply = ""
            for part in command.split(";"):
                if not part.strip():
                    continue
                result = self._execute(part)
                if result is not None:
                    reply = result
            return reply

    def query_block(self, command: str) -> bytes:
        raise ScopeError("Bloc binaire indisponible en simulation.")

    def close(self) -> None:
        self.closed = True


# -- détection -----------------------------------------------------------------------------
@dataclass(frozen=True)
class FoundInstrument:
    """Un instrument SCPI joint pendant la détection."""

    kind: str  # lan | visa
    address: str
    identity: str

    @property
    def is_vna(self) -> bool:
        parts = [part.strip() for part in self.identity.split(",")]
        return len(parts) > 1 and bool(_VNA_MODEL.match(parts[1]))

    @property
    def label(self) -> str:
        where = "VISA" if self.kind == "visa" else "LAN"
        kind = "VNA" if self.is_vna else "Instrument"
        return f"{kind} · {self.identity} · {where} {self.address}"


def identify(kind: str, address: str, timeout: float) -> str | None:
    """``*IDN?`` de l'instrument, ou ``None`` s'il ne répond pas (rien n'est modifié)."""
    try:
        instrument = ScpiInstrument(open_transport(kind, address, timeout=timeout))
    except (ScopeError, ValueError, OSError):
        return None
    try:
        return instrument.identify()
    except (ScopeError, ValueError, OSError):
        return None
    finally:
        try:
            instrument.close()
        except (ScopeError, OSError):
            pass


def local_networks() -> list[ipaddress.IPv4Network]:
    """Réseaux privés /24 des interfaces de ce PC (aucun paquet n'est envoyé)."""
    addresses: set[str] = set()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))
            addresses.add(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    try:
        addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    networks = []
    for text in sorted(addresses):
        try:
            address = ipaddress.IPv4Address(text)
        except ValueError:
            continue
        if address.is_private and not address.is_loopback and not address.is_link_local:
            network = ipaddress.IPv4Network(f"{address}/24", strict=False)
            if network not in networks:
                networks.append(network)
    return networks


def _port_open(host: str, timeout: float) -> bool:
    try:
        with socket.create_connection((host, SCPI_PORT), timeout):
            return True
    except OSError:
        return False


def discover_instruments(
    *,
    scan_network: bool = False,
    hosts: Sequence[str] = (),
    timeout: float = 1.5,
    progress: Callable[[str], None] | None = None,
    visa_resources: Callable[[], list[str]] = list_visa_resources,
    networks: Callable[[], list[ipaddress.IPv4Network]] = local_networks,
    port_open: Callable[[str, float], bool] = _port_open,
    identify_instrument: Callable[[str, str, float], str | None] = identify,
) -> list[FoundInstrument]:
    """Cherche des instruments SCPI : VISA (USB, LAN), application du PC, adresses données.

    * **VISA** : tout ce que Keysight IO Libraries ou NI-VISA voit, c'est-à-dire un
      VNA USB (P9374A…) et les appareils LAN annoncés par VXI-11 ;
    * **Ce PC** (127.0.0.1:5025) : l'application d'un VNA USB peut y servir le SCPI ;
    * ``hosts`` : adresses déjà connues (dernière utilisée…) ;
    * ``scan_network`` : essaie le port 5025 des 254 adresses du réseau privé de ce PC.
      À demander explicitement : les réseaux d'entreprise n'apprécient pas toujours.

    Les appareils reconnus comme VNA passent en premier.
    """

    def say(text: str) -> None:
        if progress is not None:
            progress(text)

    found: list[FoundInstrument] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, address: str) -> None:
        if (kind, address) in seen:
            return
        seen.add((kind, address))
        identity = identify_instrument(kind, address, timeout)
        if identity:
            found.append(FoundInstrument(kind, address, identity))

    say("Recherche VISA (USB, LAN)…")
    for resource in visa_resources():
        add("visa", resource)
    candidates = [*LOCAL_HOSTS, *hosts]
    say("Recherche sur ce PC et aux adresses connues…")
    for host in dict.fromkeys(candidates):
        if port_open(host, min(timeout, 0.5)):
            add("lan", host)
    if scan_network:
        targets: list[str] = []
        for network in networks():
            say(f"Recherche sur {network} (port {SCPI_PORT})…")
            targets.extend(str(address) for address in network.hosts())
        targets = [host for host in dict.fromkeys(targets) if host not in candidates]
        with ThreadPoolExecutor(max_workers=64) as pool:
            opened = list(pool.map(lambda host: port_open(host, 0.3), targets))
        for host, is_open in zip(targets, opened, strict=True):
            if is_open:
                add("lan", host)
    found.sort(key=lambda item: (not item.is_vna, item.kind != "visa", item.address))
    return found


__all__ = [
    "LOCAL_HOSTS",
    "FoundInstrument",
    "PnaDriver",
    "SimulatedPna",
    "VnaError",
    "discover_instruments",
    "identify",
    "local_networks",
    "parameter_name",
]
