"""Instrument SCPI générique (analyseur de réseau vectoriel, multimètre…) et sa simulation.

Le mode mesure envoie un mot, déclenche l'instrument puis lit des valeurs
numériques. Les commandes sont celles de l'utilisateur : chaque appareil a ses
propres noms SCPI, à vérifier dans son guide de programmation. Les préréglages
ne sont que des points de départ. Les liaisons LAN (port 5025) et VISA sont celles
de l'oscilloscope.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from .scope import (
    ScopeError,
    ScpiTransport,
    SocketTransport,
    VisaTransport,
)

MAX_COMMAND_LENGTH = 256
_PRINTABLE = re.compile(r"[\x20-\x7e]+")
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class InstrumentError(ScopeError):
    """Une commande a été refusée ou une réponse n'est pas numérique."""


def commands_from_text(text: str) -> list[str]:
    """Une commande par ligne ; lignes vides et commentaires (``#``) ignorés."""
    commands = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if len(line) > MAX_COMMAND_LENGTH or not _PRINTABLE.fullmatch(line):
            raise ValueError(
                f"Ligne {number} : commande SCPI en ASCII imprimable, "
                f"{MAX_COMMAND_LENGTH} caractères au plus."
            )
        commands.append(line)
    return commands


def parse_numbers(reply: str) -> list[float]:
    """Nombres d'une réponse SCPI : ``-1.5E+00,+0.0E+00`` → ``[-1.5, 0.0]``."""
    values = [float(match.group()) for match in _NUMBER.finditer(reply)]
    if not values:
        raise InstrumentError(f"Réponse non numérique : {reply!r}.")
    return values


def is_query(command: str) -> bool:
    return command.rstrip().endswith("?")


class ScpiInstrument:
    """Écrit des commandes, lit des réponses, vide la file d'erreurs SCPI.

    Les accès sont sérialisés : l'interface peut rester réactive pendant qu'un
    fil de travail interroge l'appareil.
    """

    def __init__(self, transport: ScpiTransport) -> None:
        self._transport = transport
        self._lock = threading.RLock()
        self.identity = ""

    def identify(self) -> str:
        with self._lock:
            self.identity = self._transport.query("*IDN?")
            if not self.identity.strip():
                raise InstrumentError("L'instrument n'a pas répondu à *IDN?.")
            return self.identity

    def errors(self) -> list[str]:
        found = []
        with self._lock:
            for _ in range(20):
                reply = self._transport.query(":SYSTem:ERRor?")
                code = reply.split(",", 1)[0].strip().lstrip("+")
                if code in ("0", "-0", ""):
                    break
                found.append(reply)
        return found

    def check(self, action: str) -> None:
        problems = self.errors()
        if problems:
            raise InstrumentError(f"{action} refusé par l'instrument : {'; '.join(problems)}")

    def run(self, commands: Sequence[str]) -> list[str]:
        """Envoie chaque commande ; les requêtes (``?``) rendent leur réponse."""
        replies = []
        with self._lock:
            for command in commands:
                if is_query(command):
                    replies.append(self._transport.query(command))
                else:
                    self._transport.write(command)
        return replies

    def measure(
        self, trigger: Sequence[str], reads: Sequence[str], *, check_errors: bool = False
    ) -> list[float]:
        """Déclenche, puis lit toutes les valeurs numériques des requêtes ``reads``."""
        if not reads:
            raise InstrumentError("Aucune commande de lecture : indiquer au moins une requête.")
        for read in reads:
            if not is_query(read):
                raise InstrumentError(f"Commande de lecture sans « ? » : {read!r}.")
        with self._lock:
            self.run(trigger)
            values: list[float] = []
            for reply in self.run(reads):
                values.extend(parse_numbers(reply))
            if check_errors:
                self.check("Mesure")
            return values

    def close(self) -> None:
        self._transport.close()


@dataclass(frozen=True)
class InstrumentPreset:
    name: str
    setup: str
    trigger: str
    reads: str
    labels: str
    note: str


GENERIC = InstrumentPreset(
    "Instrument SCPI générique",
    setup="",
    trigger="",
    reads="",
    labels="",
    note="Saisir ses propres commandes (voir le guide de programmation de l'appareil).",
)
KEYSIGHT_PNA = InstrumentPreset(
    "VNA Keysight PNA / Streamline : marqueur 1 (à vérifier)",
    setup="",
    trigger="INITiate:IMMediate;*OPC?",
    reads="CALCulate:MARKer1:Y?",
    labels="Marqueur 1",
    note="Point de départ : balayage unique puis valeur du marqueur 1 de la trace "
    "sélectionnée. Configurer au préalable la mesure (S21), la plage et le marqueur sur "
    "l'appareil, et vérifier ces commandes dans son guide : elles varient d'une famille à "
    "l'autre.",
)
PRESETS = (GENERIC, KEYSIGHT_PNA)


class SimulatedVna:
    """Transport SCPI simulé : une réponse fictive qui dépend du mot envoyé.

    Il sert à découvrir le mode mesure sans appareil. La valeur n'a aucun sens
    physique : ``-1,5 dB - 0,25 dB × (mot modulo 64)``.
    """

    IDENTITY = "KEYSIGHT TECHNOLOGIES,SIMULATED VNA (Arty Frame Studio, démo),SIM,1.0"

    def __init__(self) -> None:
        self.word: int | None = None
        self.commands: list[str] = []
        self.errors: list[str] = []
        self.closed = False

    def set_word(self, word: int) -> None:
        self.word = word

    @property
    def level(self) -> float:
        return -80.0 if self.word is None else -1.5 - 0.25 * (self.word % 64)

    def _execute(self, command: str) -> str | None:
        text = command.strip()
        upper = text.upper()
        if upper == "*IDN?":
            return self.IDENTITY
        if upper == "*OPC?":
            return "1"
        if re.fullmatch(r":?SYST\w*:ERR\w*\?", upper):
            return self.errors.pop(0) if self.errors else '+0,"No error"'
        if re.fullmatch(r":?CALC\w*:(?:MEAS\w*:)?MARK\w*:Y\?", upper):
            return f"{self.level:+.6E},+0.000000E+00"
        if upper in ("*CLS", "*RST") or upper.startswith(("INIT", ":INIT", "SENS", ":SENS")):
            return None
        self.errors.append(f'-113,"Undefined header: {text}"')
        return None

    def write(self, command: str) -> None:
        if self.closed:
            raise ScopeError("Instrument simulé fermé.")
        self.commands.append(command)
        for part in command.split(";"):
            if part.strip().endswith("?"):
                self.errors.append('-410,"Query INTERRUPTED"')
            else:
                self._execute(part)

    def query(self, command: str) -> str:
        if self.closed:
            raise ScopeError("Instrument simulé fermé.")
        self.commands.append(command)
        reply = ""
        for part in command.split(";"):
            result = self._execute(part)
            if result is not None:
                reply = result
        return reply

    def query_block(self, command: str) -> bytes:
        raise ScopeError("Bloc binaire indisponible en simulation.")

    def close(self) -> None:
        self.closed = True


def open_transport(
    kind: str, address: str, *, timeout: float = 10.0, simulated: SimulatedVna | None = None
) -> ScpiTransport:
    """Liaison vers l'instrument : ``lan`` (SCPI brut), ``visa`` ou ``demo``."""
    if kind == "lan":
        return SocketTransport(address, timeout=timeout)
    if kind == "visa":
        return VisaTransport(address, timeout=timeout)
    if kind == "demo":
        return simulated if simulated is not None else SimulatedVna()
    raise ValueError("Liaison attendue : lan, visa ou demo.")


__all__ = [
    "GENERIC",
    "KEYSIGHT_PNA",
    "PRESETS",
    "InstrumentError",
    "InstrumentPreset",
    "ScpiInstrument",
    "SimulatedVna",
    "commands_from_text",
    "is_query",
    "open_transport",
    "parse_numbers",
]
