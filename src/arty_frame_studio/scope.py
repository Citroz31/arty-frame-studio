"""Oscilloscope Keysight InfiniiVision (DSOX1202A et voisins) piloté en SCPI.

Deux transports sont proposés :

* **LAN** : socket SCPI brute, port 5025, sans dépendance supplémentaire ;
* **VISA** : USB ou LAN par PyVISA et une bibliothèque VISA installée, par
  exemple Keysight IO Libraries Suite sous Windows.

``scope_sim.SimulatedKeysight`` répond aux mêmes commandes pour la démo et les
tests. Les mesures locales et les curseurs travaillent sur les points lus ; les
mesures de l'oscilloscope portent sur son enregistrement complet.
"""

from __future__ import annotations

import csv
import importlib
import math
import re
import socket
import statistics
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .model import FrameConfig

SCPI_PORT = 5025
CHANNELS = (1, 2)
# Valeur renvoyée par les InfiniiVision pour une mesure impossible.
INVALID_MEASUREMENT = 9.9e37
# Calibres 1-2-5 proposés ; l'oscilloscope refuse ceux hors de sa plage.
VOLT_SCALES = tuple(
    round(mantissa * 10.0**exponent, 12) for exponent in range(-3, 2) for mantissa in (1, 2, 5)
) + (100.0,)
TIME_SCALES = tuple(
    round(mantissa * 10.0**exponent, 15)
    for exponent in range(-9, 1)
    for mantissa in (1, 2, 5)
    if 2e-9 <= mantissa * 10.0**exponent <= 5.0
)
PROBES = (1.0, 10.0, 100.0)
# Bit « Run » du registre d'opération : acquisition en cours ou en attente.
_RUN_BIT = 8
# Niveau haut attendu des sorties LVCMOS33 de l'Arty.
LOGIC_HIGH = 3.3
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,252}")


class ScopeError(RuntimeError):
    """L'oscilloscope a refusé une commande ou la liaison a échoué."""


class ScopeTimeout(ScopeError):
    """Aucune réponse de l'oscilloscope dans le délai."""


class TriggerTimeout(ScopeTimeout):
    """Mode Normal : aucun front de déclenchement pendant l'attente."""


class ScpiTransport(Protocol):
    def write(self, command: str) -> None: ...

    def query(self, command: str) -> str: ...

    def query_block(self, command: str) -> bytes: ...

    def close(self) -> None: ...


def parse_ieee_block(data: bytes) -> bytes:
    """Charge utile d'un bloc binaire IEEE 488.2 de longueur définie (``#8000001000…``)."""
    if len(data) < 2 or data[:1] != b"#" or not data[1:2].isdigit():
        raise ScopeError("Bloc binaire IEEE 488.2 attendu.")
    digits = int(data[1:2])
    if digits == 0:
        raise ScopeError("Bloc binaire de longueur indéfinie non pris en charge.")
    header = data[2 : 2 + digits]
    if len(header) != digits or not header.isdigit():
        raise ScopeError("En-tête de bloc binaire invalide.")
    length = int(header)
    payload = data[2 + digits : 2 + digits + length]
    if len(payload) != length:
        raise ScopeError(f"Bloc binaire incomplet : {len(payload)} octets sur {length}.")
    return payload


class SocketTransport:
    """SCPI brut sur TCP (port 5025), lignes terminées par ``\\n``."""

    def __init__(
        self,
        host: str,
        port: int = SCPI_PORT,
        *,
        timeout: float = 5.0,
        connect: Callable[..., Any] = socket.create_connection,
    ) -> None:
        host = host.strip() if isinstance(host, str) else ""
        if not _HOST.fullmatch(host):
            raise ValueError("Adresse IP ou nom d'hôte de l'oscilloscope invalide.")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Port TCP entre 1 et 65535 requis.")
        self.host, self.port = host, port
        try:
            self._socket = connect((host, port), timeout)
        except OSError as exc:
            raise ScopeError(
                f"Oscilloscope injoignable sur {host}:{port} : {exc}. Vérifier l'adresse IP "
                "(Utility → I/O → LAN sur l'oscilloscope) et le réseau."
            ) from exc
        self._socket.settimeout(timeout)
        self._timeout = timeout
        self._buffer = bytearray()
        self._stale = False

    def _receive(self) -> None:
        try:
            chunk = self._socket.recv(65536)
        except TimeoutError as exc:
            # Une réponse tardive décalerait toutes les suivantes : elle sera jetée.
            self._stale = True
            raise ScopeTimeout("Délai dépassé en attendant l'oscilloscope.") from exc
        except OSError as exc:
            raise ScopeError(f"Liaison LAN interrompue : {exc}") from exc
        if not chunk:
            raise ScopeError("L'oscilloscope a fermé la connexion LAN.")
        self._buffer.extend(chunk)

    def _read_exact(self, count: int) -> bytes:
        while len(self._buffer) < count:
            self._receive()
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def _read_line(self) -> bytes:
        while b"\n" not in self._buffer:
            self._receive()
        end = self._buffer.index(b"\n")
        line = bytes(self._buffer[:end])
        del self._buffer[: end + 1]
        return line

    def _resync(self) -> None:
        """Jette ce qui reste d'une réponse arrivée après un délai dépassé."""
        self._buffer.clear()
        self._stale = False
        self._socket.settimeout(0.3)
        try:
            while self._socket.recv(65536):
                pass
        except OSError:
            pass
        finally:
            self._socket.settimeout(self._timeout)

    def write(self, command: str) -> None:
        if self._stale:
            self._resync()
        try:
            self._socket.sendall(command.encode("ascii") + b"\n")
        except OSError as exc:
            raise ScopeError(f"Envoi impossible vers l'oscilloscope : {exc}") from exc

    def query(self, command: str) -> str:
        self.write(command)
        return self._read_line().decode("ascii", "replace").strip()

    def query_block(self, command: str) -> bytes:
        self.write(command)
        start = self._read_exact(2)
        if start[:1] != b"#" or not start[1:2].isdigit():
            raise ScopeError("Bloc binaire IEEE 488.2 attendu.")
        header = self._read_exact(int(start[1:2]))
        payload = self._read_exact(int(header))
        # The instrument ends the block with a newline.
        self._read_line()
        return payload

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


def open_resource_manager() -> Any:
    """VISA du système (Keysight, NI), sinon pyvisa-py s'il est installé."""
    try:
        pyvisa = importlib.import_module("pyvisa")
    except ImportError as exc:
        raise ScopeError(
            "PyVISA est absent : relancer start-windows.cmd --setup-only pour installer "
            "les dépendances."
        ) from exc
    try:
        return pyvisa.ResourceManager()
    except Exception as system_error:
        try:
            return pyvisa.ResourceManager("@py")
        except Exception:
            raise ScopeError(
                "Aucune bibliothèque VISA trouvée. Pour l'USB, installer Keysight IO "
                f"Libraries Suite, ou utiliser la connexion LAN. Détail : {system_error}"
            ) from system_error


def list_visa_resources() -> list[str]:
    """Instruments VISA visibles en USB, LAN ou GPIB ; liste vide sans VISA.

    Les ports série (ASRL) sont exclus : l'un d'eux est l'UART de l'Arty, qui ne
    doit pas recevoir de commandes SCPI.
    """
    try:
        manager = open_resource_manager()
        resources = manager.list_resources()
    except Exception:
        return []
    return sorted(
        str(resource)
        for resource in resources
        if str(resource).upper().startswith(("USB", "TCPIP", "GPIB"))
        and str(resource).upper().endswith("::INSTR")
    )


class VisaTransport:
    """USB-TMC ou LAN par PyVISA ; les blocs binaires sont lus par PyVISA."""

    def __init__(self, resource: str, *, timeout: float = 5.0, manager: Any = None) -> None:
        resource = resource.strip() if isinstance(resource, str) else ""
        if not resource or any(char in resource for char in "\r\n\0"):
            raise ValueError("Ressource VISA attendue, par exemple USB0::0x2A8D::…::INSTR.")
        manager = manager if manager is not None else open_resource_manager()
        try:
            self._instrument = manager.open_resource(resource)
        except Exception as exc:
            raise ScopeError(f"Ouverture VISA impossible ({resource}) : {exc}") from exc
        self._instrument.timeout = int(timeout * 1000)
        self._instrument.read_termination = "\n"
        self._instrument.write_termination = "\n"
        self.resource = resource

    def _wrap(self, exc: Exception, command: str) -> ScopeError:
        text = str(exc)
        if "TMO" in text or "imeout" in text:
            # Device clear : vide les tampons pour ne pas lire une réponse tardive.
            try:
                self._instrument.clear()
            except Exception:
                pass
            return ScopeTimeout(f"Délai dépassé pour {command}.")
        return ScopeError(f"Erreur VISA pour {command} : {text}")

    def write(self, command: str) -> None:
        try:
            self._instrument.write(command)
        except Exception as exc:
            raise self._wrap(exc, command) from exc

    def query(self, command: str) -> str:
        try:
            return str(self._instrument.query(command)).strip()
        except Exception as exc:
            raise self._wrap(exc, command) from exc

    def query_block(self, command: str) -> bytes:
        try:
            values = self._instrument.query_binary_values(
                command, datatype="B", is_big_endian=False, container=bytes
            )
        except Exception as exc:
            raise self._wrap(exc, command) from exc
        return bytes(values)

    def close(self) -> None:
        try:
            self._instrument.close()
        except Exception:
            pass


@dataclass(frozen=True)
class ChannelSettings:
    enabled: bool = True
    scale: float = 1.0  # V/div, à la pointe de la sonde
    offset: float = 0.0  # V au centre de l'écran
    probe: float = 1.0  # atténuation de la sonde
    coupling: str = "DC"

    def __post_init__(self) -> None:
        if not (math.isfinite(self.scale) and self.scale > 0):
            raise ValueError("Calibre vertical positif requis (V/div).")
        if not math.isfinite(self.offset):
            raise ValueError("Décalage vertical fini requis.")
        if not (math.isfinite(self.probe) and self.probe > 0):
            raise ValueError("Facteur de sonde positif requis.")
        if self.coupling not in ("DC", "AC"):
            raise ValueError("Couplage DC ou AC.")


@dataclass(frozen=True)
class TriggerSettings:
    source: int = 1
    slope: str = "POS"  # POS (montant) ou NEG (descendant)
    level: float = 1.65  # V
    sweep: str = "AUTO"  # AUTO ou NORM

    def __post_init__(self) -> None:
        if self.source not in CHANNELS:
            raise ValueError("Source de déclenchement CH1 ou CH2.")
        if self.slope not in ("POS", "NEG"):
            raise ValueError("Front de déclenchement montant ou descendant.")
        if not math.isfinite(self.level):
            raise ValueError("Niveau de déclenchement fini requis.")
        if self.sweep not in ("AUTO", "NORM"):
            raise ValueError("Mode de déclenchement Auto ou Normal.")


@dataclass(frozen=True)
class ScopeSettings:
    channels: tuple[ChannelSettings, ChannelSettings] = (
        ChannelSettings(offset=-0.5),
        ChannelSettings(offset=3.8),
    )
    time_scale: float = 50e-9  # s/div
    time_position: float = 0.0  # s, position du déclenchement
    trigger: TriggerSettings = field(default_factory=TriggerSettings)
    points: int = 1000

    def __post_init__(self) -> None:
        if len(self.channels) != len(CHANNELS):
            raise ValueError("Deux voies attendues.")
        if not (math.isfinite(self.time_scale) and self.time_scale > 0):
            raise ValueError("Base de temps positive requise (s/div).")
        if not math.isfinite(self.time_position):
            raise ValueError("Position horizontale finie requise.")
        if type(self.points) is not int or not 100 <= self.points <= 10_000:
            raise ValueError("Nombre de points entre 100 et 10 000.")

    def channel(self, number: int) -> ChannelSettings:
        return self.channels[number - 1]

    def with_channel(self, number: int, settings: ChannelSettings) -> ScopeSettings:
        channels = list(self.channels)
        channels[number - 1] = settings
        return ScopeSettings(
            (channels[0], channels[1]),
            self.time_scale,
            self.time_position,
            self.trigger,
            self.points,
        )

    @property
    def window(self) -> tuple[float, float]:
        """Temps affichés aux bords gauche et droit (10 divisions)."""
        return self.time_position - 5 * self.time_scale, self.time_position + 5 * self.time_scale


@dataclass(frozen=True)
class Trace:
    channel: int
    times: tuple[float, ...]  # s, relativement au déclenchement
    volts: tuple[float, ...]


@dataclass(frozen=True)
class Measurements:
    frequency: float | None = None
    period: float | None = None
    vpp: float | None = None
    vmax: float | None = None
    vmin: float | None = None
    duty: float | None = None  # fraction de la période à l'état haut
    high: float | None = None
    low: float | None = None
    source: str = "local"  # "oscilloscope" ou "local"


@dataclass(frozen=True)
class Acquisition:
    settings: ScopeSettings
    traces: tuple[Trace, ...]
    # Mesures de l'oscilloscope complétées par le calcul local ; ``local`` garde
    # le calcul sur les points lus, pour signaler un écart.
    measurements: dict[int, Measurements]
    triggered: bool
    elapsed: float = 0.0
    local: dict[int, Measurements] = field(default_factory=dict)

    def trace(self, channel: int) -> Trace | None:
        return next((trace for trace in self.traces if trace.channel == channel), None)


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    index = min(len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def edge_times(trace: Trace, *, rising: bool = True, min_amplitude: float = 0.0) -> list[float]:
    """Instants des fronts au niveau médian, avec hystérésis et interpolation.

    Sous ``min_amplitude`` (écart entre niveaux bas et haut), le signal est
    considéré comme sans front : bruit d'une sonde non raccordée, par exemple.
    """
    volts, times = trace.volts, trace.times
    if len(volts) < 3:
        return []
    ordered = sorted(volts)
    low, high = _percentile(ordered, 0.05), _percentile(ordered, 0.95)
    amplitude = high - low
    if amplitude <= max(1e-3, min_amplitude, 0.02 * (ordered[-1] - ordered[0])):
        return []
    middle, hysteresis = (low + high) / 2, 0.1 * amplitude
    state_high = volts[0] > middle
    last_other = 0  # dernier échantillon du côté opposé au niveau médian
    edges: list[float] = []
    for index in range(1, len(volts)):
        value = volts[index]
        if state_high:
            if value >= middle:
                last_other = index
            if value < middle - hysteresis:
                state_high = False
                if not rising:
                    edges.append(_crossing(times, volts, last_other, index, middle))
                last_other = index
        else:
            if value <= middle:
                last_other = index
            if value > middle + hysteresis:
                state_high = True
                if rising:
                    edges.append(_crossing(times, volts, last_other, index, middle))
                last_other = index
    return edges


def _crossing(
    times: Sequence[float], volts: Sequence[float], start: int, end: int, level: float
) -> float:
    """Interpolation linéaire du passage au niveau entre deux échantillons."""
    for index in range(max(start, 0), end):
        first, second = volts[index], volts[index + 1]
        if (first - level) * (second - level) <= 0 and first != second:
            fraction = (level - first) / (second - first)
            return times[index] + fraction * (times[index + 1] - times[index])
    return times[end]


def clock_period(rising: Sequence[float]) -> float | None:
    """Période d'horloge : moyenne des intervalles proches du plus court.

    Une CLK en salves alterne des intervalles d'une période et des trous
    (LATCH, pause) : ces derniers sont écartés au lieu de fausser la moyenne.
    """
    intervals = [b - a for a, b in zip(rising, rising[1:], strict=False) if b > a]
    if not intervals:
        return None
    shortest = min(intervals)
    cluster = [value for value in intervals if value <= 1.1 * shortest]
    return sum(cluster) / len(cluster)


def measure_trace(trace: Trace, *, min_amplitude: float = 0.0) -> Measurements:
    """Fréquence, période, niveaux et rapport cyclique calculés sur les points lus.

    La période vient des fronts montants (voir ``clock_period``) ; le rapport
    cyclique est la médiane des durées à l'état haut de ces périodes.
    """
    if len(trace.volts) < 3:
        return Measurements()
    ordered = sorted(trace.volts)
    vmin, vmax = ordered[0], ordered[-1]
    low, high = _percentile(ordered, 0.05), _percentile(ordered, 0.95)
    rising = edge_times(trace, rising=True, min_amplitude=min_amplitude)
    falling = edge_times(trace, rising=False, min_amplitude=min_amplitude)
    period = clock_period(rising)
    duty = None
    if period is not None:
        fractions = []
        for start, stop in zip(rising, rising[1:], strict=False):
            if stop - start > 1.1 * period:
                continue  # trou entre deux salves
            fall = next((value for value in falling if start < value < stop), None)
            if fall is not None:
                fractions.append((fall - start) / (stop - start))
        duty = statistics.median(fractions) if fractions else None
    return Measurements(
        frequency=1 / period if period else None,
        period=period,
        vpp=vmax - vmin,
        vmax=vmax,
        vmin=vmin,
        duty=duty,
        high=high,
        low=low,
    )


def period_cursors(
    trace: Trace, around: float, *, min_amplitude: float = 0.0
) -> tuple[float, float] | None:
    """Deux fronts montants consécutifs, le premier le plus proche de ``around``."""
    edges = edge_times(trace, rising=True, min_amplitude=min_amplitude)
    pairs = list(zip(edges, edges[1:], strict=False))
    if not pairs:
        return None
    return min(pairs, key=lambda pair: abs(pair[0] - around))


def nice_ceiling(value: float, choices: Sequence[float]) -> float:
    """Plus petit calibre de la liste supérieur ou égal à ``value``."""
    return next((choice for choice in choices if choice >= value * (1 - 1e-9)), choices[-1])


def format_si(value: float | None, unit: str, digits: int = 4) -> str:
    """Valeur avec préfixe SI : 1e7 Hz → « 10.00 MHz » ; ``None`` → « — »."""
    if value is None or not math.isfinite(value):
        return "—"
    if value == 0:
        return f"0 {unit}"
    prefixes = (
        (1e9, "G"),
        (1e6, "M"),
        (1e3, "k"),
        (1.0, ""),
        (1e-3, "m"),
        (1e-6, "µ"),
        (1e-9, "n"),
        (1e-12, "p"),
    )
    magnitude = abs(value)
    factor, prefix = next(
        ((size, name) for size, name in prefixes if magnitude >= size * 0.9995), (1e-12, "p")
    )
    scaled = value / factor
    decimals = max(0, digits - 1 - int(math.floor(math.log10(abs(scaled)))))
    if abs(round(scaled, decimals)) >= 10 ** (digits - decimals):
        decimals = max(0, decimals - 1)  # 9.99995 s'arrondit à 10.00, pas 10.000
    return f"{scaled:.{decimals}f} {prefix}{unit}"


def short_si(value: float, unit: str) -> str:
    """Calibre compact : 5e-4 V → « 500 mV », 5e-8 s → « 50 ns »."""
    text = format_si(value, unit, digits=3)
    number, _, rest = text.partition(" ")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return f"{number} {rest}" if rest else number


_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9}


def parse_si(text: str, unit: str = "") -> float:
    """« 1,65 », « 100n », « 100 ns », « -2.5u », « 2e-6 s » → valeur en unités SI."""
    cleaned = (text or "").strip().replace(",", ".").replace(" ", "")
    if unit and cleaned.endswith(unit):
        cleaned = cleaned[: -len(unit)]
    factor = 1.0
    if cleaned and cleaned[-1] in _SI and not re.fullmatch(r"[-+]?\d*\.?\d+e", cleaned):
        factor = _SI[cleaned[-1]]
        cleaned = cleaned[:-1]
    try:
        value = float(cleaned) * factor
    except ValueError as exc:
        raise ValueError(f"Valeur numérique attendue (ex. 1.65, 100n) : {text!r}.") from exc
    if not math.isfinite(value):
        raise ValueError("Valeur finie attendue.")
    return value


def _number(text: str) -> float:
    try:
        value = float(text.strip().split(",")[0])
    except ValueError as exc:
        raise ScopeError(f"Réponse numérique attendue, reçu {text!r}.") from exc
    return value


class KeysightScope:
    """Pilote SCPI d'un InfiniiVision (1000/2000/3000/4000 X) : 2 voies, front.

    Toutes les commandes passent par un verrou : un rafraîchissement et un
    réglage lancé depuis l'interface ne s'entremêlent pas sur la liaison.
    """

    def __init__(
        self,
        transport: ScpiTransport,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._lock = threading.RLock()
        self._sleep = sleep
        self._clock = clock
        self.identity = ""

    # -- échanges de base -------------------------------------------------
    def write(self, command: str) -> None:
        with self._lock:
            self._transport.write(command)

    def query(self, command: str) -> str:
        with self._lock:
            return self._transport.query(command)

    def identify(self) -> str:
        with self._lock:
            self.identity = self.query("*IDN?")
            return self.identity

    def errors(self) -> list[str]:
        """Vide la file d'erreurs SCPI ; liste vide si tout est accepté."""
        found = []
        with self._lock:
            for _ in range(20):
                reply = self.query(":SYSTem:ERRor?")
                code = reply.split(",", 1)[0].strip().lstrip("+")
                if code in ("0", "-0", ""):
                    break
                found.append(reply)
        return found

    def _check(self, action: str) -> None:
        problems = self.errors()
        if problems:
            raise ScopeError(f"{action} refusé par l'oscilloscope : {'; '.join(problems)}")

    # -- réglages -----------------------------------------------------------
    def read_settings(self) -> ScopeSettings:
        with self._lock:
            channels = []
            for number in CHANNELS:
                prefix = f":CHANnel{number}"
                channels.append(
                    ChannelSettings(
                        enabled=self.query(f"{prefix}:DISPlay?").strip() in ("1", "ON"),
                        scale=_number(self.query(f"{prefix}:SCALe?")),
                        offset=_number(self.query(f"{prefix}:OFFSet?")),
                        probe=_number(self.query(f"{prefix}:PROBe?")),
                        coupling=self.query(f"{prefix}:COUPling?").strip().upper()[:2],
                    )
                )
            source = self.query(":TRIGger:EDGE:SOURce?").strip().upper()
            match = re.search(r"CHAN(?:NEL)?([12])", source)
            slope = self.query(":TRIGger:EDGE:SLOPe?").strip().upper()
            sweep = self.query(":TRIGger:SWEep?").strip().upper()
            trigger = TriggerSettings(
                source=int(match.group(1)) if match else 1,
                slope="NEG" if slope.startswith("NEG") else "POS",
                level=_number(self.query(":TRIGger:EDGE:LEVel?")),
                sweep="NORM" if sweep.startswith("NORM") else "AUTO",
            )
            return ScopeSettings(
                (channels[0], channels[1]),
                time_scale=_number(self.query(":TIMebase:SCALe?")),
                time_position=_number(self.query(":TIMebase:POSition?")),
                trigger=trigger,
            )

    def apply_settings(self, settings: ScopeSettings) -> None:
        with self._lock:
            for number in CHANNELS:
                channel = settings.channel(number)
                prefix = f":CHANnel{number}"
                self.write(f"{prefix}:DISPlay {int(channel.enabled)}")
                # The scale is expressed at the probe tip: set the probe first.
                self.write(f"{prefix}:PROBe {channel.probe:g}")
                self.write(f"{prefix}:COUPling {channel.coupling}")
                self.write(f"{prefix}:SCALe {channel.scale:.6E}")
                self.write(f"{prefix}:OFFSet {channel.offset:.6E}")
            # Position comptée depuis le centre de l'écran, comme l'affichage de l'application.
            self.write(":TIMebase:REFerence CENTer")
            self.write(f":TIMebase:SCALe {settings.time_scale:.6E}")
            self.write(f":TIMebase:POSition {settings.time_position:.6E}")
            trigger = settings.trigger
            self.write(":TRIGger:MODE EDGE")
            self.write(f":TRIGger:EDGE:SOURce CHANnel{trigger.source}")
            self.write(
                f":TRIGger:EDGE:SLOPe {'POSitive' if trigger.slope == 'POS' else 'NEGative'}"
            )
            self.write(f":TRIGger:EDGE:LEVel {trigger.level:.6E}")
            self.write(f":TRIGger:SWEep {'AUTO' if trigger.sweep == 'AUTO' else 'NORMal'}")
            self._check("Réglage")

    def autoscale(self) -> ScopeSettings:
        with self._lock:
            self.write(":AUToscale")
            self.query("*OPC?")
            self._check("Auto scale")
            return self.read_settings()

    def run(self) -> None:
        self.write(":RUN")

    def stop(self) -> None:
        self.write(":STOP")

    # -- acquisition --------------------------------------------------------
    def acquire(self, timeout: float = 2.0) -> bool:
        """Une acquisition unique ; ``True`` si elle a été déclenchée par un front.

        En mode Auto, l'oscilloscope acquiert même sans front (``False``). En
        mode Normal sans front, l'attente s'arrête au délai par STOP.
        """
        with self._lock:
            self.query(":TER?")  # efface l'indicateur de déclenchement
            self.write(":SINGle")
            self.query("*OPC?")
            deadline = self._clock() + timeout
            while int(_number(self.query(":OPERegister:CONDition?"))) & _RUN_BIT:
                if self._clock() >= deadline:
                    self.write(":STOP")
                    raise TriggerTimeout(
                        "Aucun déclenchement dans le délai : vérifier la source, le niveau "
                        "et le front, ou passer le déclenchement en mode Auto."
                    )
                self._sleep(0.02)
            return int(_number(self.query(":TER?"))) == 1

    def read_trace(self, channel: int, points: int = 1000) -> Trace:
        with self._lock:
            self.write(f":WAVeform:SOURce CHANnel{channel}")
            self.write(":WAVeform:FORMat BYTE")
            self.write(":WAVeform:UNSigned 1")
            self.write(":WAVeform:POINts:MODE NORMal")
            self.write(f":WAVeform:POINts {points}")
            preamble = [value.strip() for value in self.query(":WAVeform:PREamble?").split(",")]
            if len(preamble) < 10:
                raise ScopeError("Préambule de forme d'onde incomplet.")
            x_increment, x_origin, x_reference = (float(value) for value in preamble[4:7])
            y_increment, y_origin, y_reference = (float(value) for value in preamble[7:10])
            codes = self._transport.query_block(":WAVeform:DATA?")
        times = tuple((index - x_reference) * x_increment + x_origin for index in range(len(codes)))
        volts = tuple((code - y_reference) * y_increment + y_origin for code in codes)
        return Trace(channel, times, volts)

    def measure(self, channel: int) -> Measurements:
        """Mesures calculées par l'oscilloscope sur tout son enregistrement."""

        def value(item: str) -> float | None:
            number = _number(self.query(f":MEASure:{item}? CHANnel{channel}"))
            return None if abs(number) >= INVALID_MEASUREMENT / 10 else number

        with self._lock:
            frequency = value("FREQuency")
            period = value("PERiod")
            duty = value("DUTYcycle")
            return Measurements(
                frequency=frequency,
                period=period,
                vpp=value("VPP"),
                vmax=value("VMAX"),
                vmin=value("VMIN"),
                duty=None if duty is None else duty / 100,
                source="oscilloscope",
            )

    def capture(self, *, timeout: float = 2.0, points: int = 1000) -> Acquisition:
        """Acquisition unique, réglages relus, traces des voies affichées et mesures."""
        start = self._clock()
        with self._lock:
            triggered = self.acquire(timeout)
            settings = self.read_settings()
            traces = tuple(
                self.read_trace(number, points)
                for number in CHANNELS
                if settings.channel(number).enabled
            )
            measurements = {}
            locals_ = {}
            for trace in traces:
                # Moins d'une demi-division d'écart : pas de front à mesurer.
                floor = 0.5 * settings.channel(trace.channel).scale
                local = measure_trace(trace, min_amplitude=floor)
                locals_[trace.channel] = local
                remote = self.measure(trace.channel)
                # Scope values first; local estimates fill what it could not measure.
                measurements[trace.channel] = Measurements(
                    frequency=remote.frequency or local.frequency,
                    period=remote.period or local.period,
                    vpp=remote.vpp if remote.vpp is not None else local.vpp,
                    vmax=remote.vmax if remote.vmax is not None else local.vmax,
                    vmin=remote.vmin if remote.vmin is not None else local.vmin,
                    duty=remote.duty if remote.duty is not None else local.duty,
                    high=local.high,
                    low=local.low,
                    source="oscilloscope" if remote.frequency is not None else "local",
                )
        return Acquisition(
            settings, traces, measurements, triggered, self._clock() - start, locals_
        )

    def screenshot(self) -> bytes:
        """Copie d'écran PNG de l'oscilloscope."""
        with self._lock:
            return self._transport.query_block(":DISPlay:DATA? PNG, COLor")

    def close(self, *, resume: bool = True) -> None:
        """Rend la main à l'oscilloscope (RUN) puis ferme la liaison."""
        with self._lock:
            try:
                if resume:
                    self._transport.write(":RUN")
            except ScopeError:
                pass
            finally:
                self._transport.close()


# -- aides propres aux sorties de l'Arty ------------------------------------
def frame_preset(
    config: FrameConfig | None, mapping: dict[int, str | None], current: ScopeSettings
) -> ScopeSettings:
    """Réglages pour observer la trame : 1 V/div, voies empilées, 5 périodes de CLK.

    Le déclenchement se fait sur la voie qui observe CLK (CH2 à défaut), au front
    montant, à mi-niveau LVCMOS. Le facteur de sonde de chaque voie est conservé.
    """
    clock = next((number for number, signal in mapping.items() if signal == "clk"), 2)
    period = 1 / config.frequency_hz if config is not None else 100e-9
    channels = (
        replace(current.channel(1), enabled=True, scale=1.0, offset=-0.5, coupling="DC"),
        replace(current.channel(2), enabled=True, scale=1.0, offset=3.8, coupling="DC"),
    )
    return ScopeSettings(
        channels,
        time_scale=nice_ceiling(period / 2, TIME_SCALES),
        time_position=0.0,
        trigger=TriggerSettings(source=clock, slope="POS", level=LOGIC_HIGH / 2, sweep="AUTO"),
        points=current.points,
    )


def measurement_warnings(acquisition: Acquisition, mapping: dict[int, str | None]) -> list[str]:
    """Indices de mesure suspecte sur les voies reliées aux sorties 3,3 V de l'Arty."""
    warnings = []
    for channel, values in acquisition.measurements.items():
        if not mapping.get(channel):
            continue
        local = acquisition.local.get(channel, values)
        high, low = local.high, local.low
        if values.vmax is not None and values.vmax > 2 * LOGIC_HIGH:
            warnings.append(
                f"CH{channel} : maximum {format_si(values.vmax, 'V')} pour une sortie de "
                "3,3 V. Vérifier le facteur de sonde (1:1 ou 10:1) sur l'oscilloscope et "
                "dans le réglage Sonde de la voie."
            )
        elif high is not None and low is not None and 0.15 < high - low < 1.0:
            warnings.append(
                f"CH{channel} : amplitude {format_si(high - low, 'V')} au lieu d'environ "
                "3,3 V. Une sonde 10:1 réglée en 1:1 divise les tensions par 10."
            )
        if (
            values.vmax is not None
            and high is not None
            and low is not None
            and high - low > 0.5
            and values.vmax - high > 0.3 * (high - low)
        ):
            warnings.append(
                f"CH{channel} : fort dépassement aux fronts ({format_si(values.vmax, 'V')}). "
                "Utiliser le ressort de masse court de la sonde plutôt que le long fil."
            )
    return warnings


def describe(values: Measurements) -> str:
    """Résumé texte d'une mesure, pour la ligne de commande."""
    duty = "—" if values.duty is None else f"{values.duty * 100:.1f} %"
    return (
        f"fréquence {format_si(values.frequency, 'Hz')} · période {format_si(values.period, 's')} "
        f"· Vpp {format_si(values.vpp, 'V')} · rapport cyclique {duty}"
    )


def write_acquisition_csv(acquisition: Acquisition, path: Path) -> Path:
    """Points de chaque voie affichée : colonnes temps (s) et tension (V) par voie."""
    path.parent.mkdir(parents=True, exist_ok=True)
    traces = acquisition.traces
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        header = []
        for trace in traces:
            header += [f"temps_CH{trace.channel}_s", f"CH{trace.channel}_V"]
        writer.writerow(header)
        for index in range(max((len(trace.times) for trace in traces), default=0)):
            row: list[str] = []
            for trace in traces:
                if index < len(trace.times):
                    row += [f"{trace.times[index]:.12g}", f"{trace.volts[index]:.6g}"]
                else:
                    row += ["", ""]
            writer.writerow(row)
    return path
