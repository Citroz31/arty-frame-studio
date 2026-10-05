"""Oscilloscope simulé : répond au sous-ensemble SCPI utilisé par ``KeysightScope``.

Les voies observent les signaux de la trame courante (DATA, CLK ou LATCH),
répétés à chaque période de trame comme en émission continue, avec un modèle
électrique simple : niveaux LVCMOS 0–3,3 V, fronts d'environ 1,2 ns, léger
dépassement amorti et bruit. Il sert à la démonstration sans oscilloscope et
aux tests ; il ne prédit pas le signal réel d'un montage.
"""

from __future__ import annotations

import bisect
import math
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .model import FrameConfig
from .scope import (
    CHANNELS,
    INVALID_MEASUREMENT,
    TIME_SCALES,
    VOLT_SCALES,
    ScopeError,
    Trace,
    measure_trace,
    nice_ceiling,
)
from .simulation import simulate

SIGNALS = ("data", "clk", "latch")
IDENTITY = "KEYSIGHT TECHNOLOGIES,DSO-X 1202A,SIMULATION,02.12 (Arty Frame Studio, démo)"
_VOWELS = set("AEIOU")
_SLOPES = {"POS": "POS", "POSITIVE": "POS", "NEG": "NEG", "NEGATIVE": "NEG"}


@dataclass(frozen=True)
class SignalSource:
    """Transitions d'une période de trame par signal : (instants en s, niveau après)."""

    period: float
    edges: dict[str, tuple[tuple[float, ...], tuple[int, ...]]]
    high: float = 3.3
    low: float = 0.0

    def level(self, signal: str, time: float) -> int:
        times, levels = self.edges[signal]
        index = bisect.bisect_right(times, time % self.period) - 1
        return levels[index]

    def rising_edges(self, signal: str) -> list[float]:
        times, levels = self.edges[signal]
        return [t for i, t in enumerate(times) if levels[i] == 1 and levels[i - 1] == 0]

    def falling_edges(self, signal: str) -> list[float]:
        times, levels = self.edges[signal]
        return [t for i, t in enumerate(times) if levels[i] == 0 and levels[i - 1] == 1]


def signal_source(config: FrameConfig, *, high: float = 3.3) -> SignalSource:
    """Une période de la trame, d'après le simulateur idéal du projet."""
    waveform = simulate(config, max_frames=1)
    if waveform.frames_simulated:
        period = config.frame_duration_ns * 1e-9
    else:  # very long free-CLK frame cropped by the preview budget
        period = waveform.duration_ns * 1e-9
    edges = {}
    for signal in SIGNALS:
        times: list[float] = []
        levels: list[int] = []
        for transition in waveform.transitions:
            at = transition.time_ns * 1e-9
            level = int(getattr(transition, signal))
            if at >= period - 1e-15:
                break
            if not levels or level != levels[-1]:
                times.append(at)
                levels.append(level)
        if not times or times[0] > 0:
            times.insert(0, 0.0)
            levels.insert(0, levels[-1] if levels else 0)
        edges[signal] = (tuple(times), tuple(levels))
    return SignalSource(period, edges, high=high)


@dataclass
class _Channel:
    display: bool = True
    scale: float = 1.0
    offset: float = 0.0
    probe: float = 1.0
    coupling: str = "DC"


@dataclass
class _Trigger:
    source: int = 1
    slope: str = "POS"
    level: float = 1.65
    sweep: str = "AUTO"


@dataclass
class _Record:
    offset: float
    increment: float
    codes: list[int] = field(default_factory=list)


def _short(mnemonic: str) -> str:
    """Forme courte SCPI : un mot de plus de 4 lettres garde ses 4 premières, ou
    3 si la 4e est une voyelle (``CHANnel1`` → ``CHAN1``, ``LEVel`` → ``LEV``)."""
    match = re.fullmatch(r"([A-Z*]+)(\d*)", mnemonic)
    if not match:
        return mnemonic
    word, suffix = match.groups()
    if len(word) > 4:
        word = word[:3] if word[3] in _VOWELS else word[:4]
    return word + suffix


def _header(text: str) -> tuple[str, bool]:
    query = text.endswith("?")
    parts = text.rstrip("?").lstrip(":").upper().split(":")
    return ":".join(_short(part) for part in parts), query


class SimulatedKeysight:
    """Transport SCPI simulé d'un DSOX1202A (deux voies, déclenchement sur front).

    ``source`` fournit les signaux à observer ; ``mapping`` associe chaque voie à
    ``data``, ``clk``, ``latch`` ou ``None`` (sonde non raccordée).
    """

    def __init__(
        self,
        source: Callable[[], SignalSource | None],
        mapping: Callable[[], dict[int, str | None]] | None = None,
        *,
        seed: int = 1,
        noise: float = 0.012,
        rise_time: float = 1.2e-9,
    ) -> None:
        self._source = source
        self._mapping = mapping or (lambda: {1: "data", 2: "clk"})
        self._random = random.Random(seed)
        self._noise = noise
        self._tau = rise_time / 2.2
        self.commands: list[str] = []
        self.closed = False
        self._reset()

    # -- état -----------------------------------------------------------------
    def _reset(self) -> None:
        self.channels = {1: _Channel(offset=-0.5), 2: _Channel(offset=3.8)}
        self.time_scale = 50e-9
        self.time_position = 0.0
        self.trigger = _Trigger()
        self.wave_source = 1
        self.points = 1000
        self.errors: list[str] = []
        self.running = False
        self.waiting = False
        self.trigger_event = False
        self.records: dict[int, _Record] = {}
        self.analysis: dict[int, Trace] = {}

    def _error(self, text: str) -> None:
        self.errors.append(text)

    # -- signaux --------------------------------------------------------------
    def _voltage(self, source: SignalSource, signal: str | None, time: float) -> float:
        if signal is None:
            return 0.0
        times, levels = source.edges[signal]
        phase = time % source.period
        index = bisect.bisect_right(times, phase) - 1
        start = times[index]
        new, previous = levels[index], levels[index - 1]
        span = source.high - source.low
        value = source.low + span * new
        if new != previous:
            elapsed = phase - start
            delta = span * (new - previous)
            # First-order edge plus a small damped overshoot (probe and wiring).
            value -= delta * math.exp(-elapsed / self._tau)
            value += (
                0.08 * delta * math.exp(-elapsed / 2e-9) * math.sin(2 * math.pi * 3.5e8 * elapsed)
            )
        return value

    def _trigger_time(self, source: SignalSource | None) -> float | None:
        signal = self._mapping().get(self.trigger.source)
        level = self.trigger.level
        if source is None or signal is None or not source.low < level < source.high:
            return None
        rising = self.trigger.slope == "POS"
        edges = source.rising_edges(signal) if rising else source.falling_edges(signal)
        if not edges:
            return None
        # Instant où le front exponentiel franchit le niveau choisi.
        fraction = (level - source.low) / (source.high - source.low)
        fraction = fraction if rising else 1 - fraction
        delay = -self._tau * math.log(1 - fraction)
        return edges[0] + delay + source.period  # une période d'historique avant

    def _record(
        self, source: SignalSource | None, channel: int, origin: float, points: int
    ) -> list[float]:
        signal = self._mapping().get(channel)
        step = 10 * self.time_scale / points
        start = self.time_position - 5 * self.time_scale
        values = [
            (self._voltage(source, signal, origin + start + index * step) if source else 0.0)
            + self._random.gauss(0.0, self._noise)
            for index in range(points)
        ]
        if self.channels[channel].coupling == "AC" and values:
            mean = sum(values) / len(values)
            values = [value - mean for value in values]
        return values

    def _acquire(self) -> None:
        source = self._source()
        trigger = self._trigger_time(source)
        if trigger is None and self.trigger.sweep == "NORM":
            self.waiting = True
            return
        origin = trigger if trigger is not None else 0.0
        self.trigger_event = trigger is not None
        self.waiting = False
        self.records.clear()
        self.analysis.clear()
        start = self.time_position - 5 * self.time_scale
        for channel in CHANNELS:
            settings = self.channels[channel]
            increment = settings.scale * 10 / 256
            values = self._record(source, channel, origin, self.points)
            codes = [
                max(0, min(255, round((value - settings.offset) / increment + 128)))
                for value in values
            ]
            self.records[channel] = _Record(settings.offset, increment, codes)
            # Mesures « de l'oscilloscope » : enregistrement plus fin, comme sa mémoire.
            fine = 4 * self.points
            dense = self._record(source, channel, origin, fine)
            step = 10 * self.time_scale / fine
            self.analysis[channel] = Trace(
                channel, tuple(start + index * step for index in range(fine)), tuple(dense)
            )

    def _autoscale(self) -> None:
        source = self._source()
        mapping = self._mapping()
        fastest: tuple[float, int] | None = None
        for channel in CHANNELS:
            settings = self.channels[channel]
            signal = mapping.get(channel)
            settings.display = True
            settings.coupling = "DC"
            if source is None or signal is None:
                continue
            scale = nice_ceiling((source.high - source.low) / 3.5, VOLT_SCALES)
            settings.scale = scale
            # CH1 dans la moitié haute, CH2 dans la moitié basse de l'écran.
            settings.offset = (
                source.low - 0.5 * scale if channel == 1 else source.high + 0.5 * scale
            )
            edges = source.rising_edges(signal)
            if len(edges) >= 2:
                gaps = sorted(b - a for a, b in zip(edges, edges[1:], strict=False))
                period = gaps[len(gaps) // 2]
            elif edges:
                period = source.period
            else:
                continue
            if fastest is None or period < fastest[0]:
                fastest = (period, channel)
        if fastest is not None and source is not None:
            self.time_scale = nice_ceiling(fastest[0] / 2, TIME_SCALES)
            self.time_position = 0.0
            self.trigger = _Trigger(fastest[1], "POS", (source.high + source.low) / 2, "AUTO")
        self.running = True
        self._acquire()

    # -- transport ------------------------------------------------------------
    def write(self, command: str) -> None:
        if self.closed:
            raise ScopeError("Oscilloscope simulé fermé.")
        self.commands.append(command)
        text = command.strip()
        head, _, argument = text.partition(" ")
        key, query = _header(head)
        if query:
            self._error(f'-420,"Query UNTERMINATED {head}"')
            return
        argument = argument.strip().upper()
        try:
            self._set(key, argument)
        except (ValueError, KeyError):
            self._error(f'-224,"Illegal parameter value {text}"')

    def _set(self, key: str, argument: str) -> None:
        channel = re.fullmatch(r"CHAN([12]):(DISP|SCAL|OFFS|PROB|COUP)", key)
        if channel:
            settings, item = self.channels[int(channel.group(1))], channel.group(2)
            if item == "DISP":
                settings.display = argument in ("1", "ON")
            elif item == "COUP":
                if argument not in ("DC", "AC"):
                    raise ValueError(argument)
                settings.coupling = argument
            elif item == "SCAL":
                value = float(argument)
                if not 1e-3 <= value <= 100:
                    raise ValueError(argument)
                settings.scale = value
            elif item == "PROB":
                value = float(argument)
                if value <= 0:
                    raise ValueError(argument)
                settings.probe = value
            else:
                settings.offset = float(argument)
            return
        if key == "TIM:SCAL":
            value = float(argument)
            if not 2e-9 <= value <= 50:
                raise ValueError(argument)
            self.time_scale = value
        elif key == "TIM:POS":
            self.time_position = float(argument)
        elif key == "TIM:REF":
            if argument not in ("CENT", "CENTER"):
                raise ValueError(argument)
        elif key == "TRIG:MODE":
            if argument != "EDGE":
                raise ValueError(argument)
        elif key == "TRIG:EDGE:SOUR":
            match = re.fullmatch(r"CHAN(?:NEL)?([12])", argument)
            if not match:
                raise ValueError(argument)
            self.trigger.source = int(match.group(1))
        elif key == "TRIG:EDGE:SLOP":
            self.trigger.slope = _SLOPES[argument]
        elif key in ("TRIG:EDGE:LEV", "TRIG:LEV"):
            self.trigger.level = float(argument)
        elif key == "TRIG:SWE":
            self.trigger.sweep = {"AUTO": "AUTO", "NORM": "NORM", "NORMAL": "NORM"}[argument]
        elif key == "RUN":
            self.running = True
            self._acquire()
        elif key == "STOP":
            self.running = False
            self.waiting = False
        elif key == "SING":
            self.running = False
            self._acquire()
        elif key == "AUT":
            self._autoscale()
        elif key == "WAV:SOUR":
            match = re.fullmatch(r"CHAN(?:NEL)?([12])", argument)
            if not match:
                raise ValueError(argument)
            self.wave_source = int(match.group(1))
        elif key == "WAV:POIN":
            value = int(float(argument))
            if not 100 <= value <= 10_000:
                raise ValueError(argument)
            if value != self.points:
                self.points = value
                self.records.clear()
        elif key in ("WAV:FORM", "WAV:UNS", "WAV:POIN:MODE", "*CLS", "*RST"):
            if key == "*RST":
                self._reset()
        else:
            self._error(f'-113,"Undefined header {key}"')

    def query(self, command: str) -> str:
        if self.closed:
            raise ScopeError("Oscilloscope simulé fermé.")
        self.commands.append(command)
        head, _, argument = command.strip().partition(" ")
        key, query = _header(head)
        if not query:
            self._error(f'-410,"Query INTERRUPTED {head}"')
            return ""
        return self._get(key, argument.strip().upper())

    def _get(self, key: str, argument: str) -> str:
        channel = re.fullmatch(r"CHAN([12]):(DISP|SCAL|OFFS|PROB|COUP)", key)
        if channel:
            settings, item = self.channels[int(channel.group(1))], channel.group(2)
            if item == "DISP":
                return str(int(settings.display))
            if item == "COUP":
                return settings.coupling
            value = {"SCAL": settings.scale, "OFFS": settings.offset, "PROB": settings.probe}
            return f"{value[item]:+.6E}"
        if key == "*IDN":
            return IDENTITY
        if key == "*OPC":
            return "1"
        if key == "SYST:ERR":
            return self.errors.pop(0) if self.errors else '+0,"No error"'
        if key == "TIM:SCAL":
            return f"{self.time_scale:+.6E}"
        if key == "TIM:POS":
            return f"{self.time_position:+.6E}"
        if key == "TRIG:EDGE:SOUR":
            return f"CHAN{self.trigger.source}"
        if key == "TRIG:EDGE:SLOP":
            return self.trigger.slope
        if key in ("TRIG:EDGE:LEV", "TRIG:LEV"):
            return f"{self.trigger.level:+.6E}"
        if key == "TRIG:SWE":
            return self.trigger.sweep
        if key == "OPER:COND":
            if self.waiting:
                # Normal sweep: keep waiting, unless the settings now allow a trigger.
                self._acquire()
            return "+8" if self.running or self.waiting else "+0"
        if key == "TER":
            event, self.trigger_event = self.trigger_event, False
            return "+1" if event else "+0"
        if key == "WAV:PRE":
            record = self._wave_record()
            start = self.time_position - 5 * self.time_scale
            step = 10 * self.time_scale / len(record.codes)
            return (
                f"+0,+0,+{len(record.codes)},+1,{step:+.6E},{start:+.6E},+0,"
                f"{record.increment:+.6E},{record.offset:+.6E},+128"
            )
        measure = re.fullmatch(r"MEAS:(FREQ|PER|VPP|VMAX|VMIN|DUTY)", key)
        if measure:
            match = re.fullmatch(r"CHAN(?:NEL)?([12])", argument)
            number = int(match.group(1)) if match else 1
            return self._measure(number, measure.group(1))
        self._error(f'-113,"Undefined header {key}"')
        return ""

    def _wave_record(self) -> _Record:
        if self.wave_source not in self.records:
            self._acquire()
        if self.wave_source not in self.records:
            raise ScopeError("Aucune acquisition disponible : déclenchement en attente.")
        return self.records[self.wave_source]

    def _measure(self, channel: int, item: str) -> str:
        trace = self.analysis.get(channel)
        floor = 0.5 * self.channels[channel].scale
        values = measure_trace(trace, min_amplitude=floor) if trace is not None else None
        result = {
            "FREQ": values.frequency if values else None,
            "PER": values.period if values else None,
            "VPP": values.vpp if values else None,
            "VMAX": values.vmax if values else None,
            "VMIN": values.vmin if values else None,
            "DUTY": values.duty * 100 if values and values.duty is not None else None,
        }[item]
        return f"{INVALID_MEASUREMENT if result is None else result:+.6E}"

    def query_block(self, command: str) -> bytes:
        key, _ = _header(command.strip().partition(" ")[0])
        self.commands.append(command)
        if key == "WAV:DATA":
            return bytes(self._wave_record().codes)
        if key == "DISP:DATA":
            raise ScopeError("Copie d'écran indisponible en simulation.")
        self._error(f'-113,"Undefined header {key}"')
        raise ScopeError(f"Bloc inconnu : {command}")

    def close(self) -> None:
        self.closed = True
