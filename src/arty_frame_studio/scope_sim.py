"""Oscilloscope simulé : répond au sous-ensemble SCPI utilisé par ``KeysightScope``.

Les voies observent une émission de la trame courante (DATA, CLK ou LATCH),
avec son nombre de répétitions, puis le repos, ou sans fin en mode continu. Un modèle
électrique simple ajoute des niveaux LVCMOS 0–3,3 V, des fronts d'environ 1,2 ns, un léger
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
    duration: float | None = None
    idle_latch: int = 0
    free_clock_period: float | None = None

    def transition(self, signal: str, time: float) -> tuple[float, int, int]:
        """Dernier front, niveaux avant/après ; CLK libre est calculée sans aperçu."""
        idle = self.idle_latch if signal == "latch" else 0
        if time < 0:
            return time, idle, idle
        if self.duration is not None and time >= self.duration:
            previous = self.level(signal, math.nextafter(self.duration, -math.inf))
            return self.duration, previous, idle
        if signal == "clk" and self.free_clock_period is not None:
            half = self.free_clock_period / 2
            index = math.floor(time / half)
            level = index % 2
            return index * half, 1 - level if index else 0, level
        times, levels = self.edges[signal]
        phase = time % self.period
        index = bisect.bisect_right(times, phase) - 1
        start = time - phase + times[index]
        previous = levels[index - 1] if start > 0 else idle
        if index == 0 and start > 0 and times[-1] >= self.period:
            previous = levels[-2]
        return start, previous, levels[index]

    def level(self, signal: str, time: float) -> int:
        if time < 0 or (self.duration is not None and time >= self.duration):
            return self.idle_latch if signal == "latch" else 0
        if signal == "clk" and self.free_clock_period is not None:
            return math.floor(time / (self.free_clock_period / 2)) % 2
        times, levels = self.edges[signal]
        index = bisect.bisect_right(times, time % self.period) - 1
        return levels[index]

    def rising_edges(self, signal: str) -> list[float]:
        times, levels = self.edges[signal]
        idle = self.idle_latch if signal == "latch" else 0
        return [
            t for i, t in enumerate(times) if levels[i] == 1 and (levels[i - 1] if i else idle) == 0
        ]

    def falling_edges(self, signal: str) -> list[float]:
        if signal == "clk" and self.free_clock_period is not None:
            return [self.free_clock_period]
        times, levels = self.edges[signal]
        idle = self.idle_latch if signal == "latch" else 0
        return [
            t for i, t in enumerate(times) if levels[i] == 0 and (levels[i - 1] if i else idle) == 1
        ]


def signal_source(config: FrameConfig, *, high: float = 3.3) -> SignalSource:
    """Source exacte et bornée en mémoire, indépendante du budget de l'aperçu."""
    tick = config.tick_ns * 1e-9
    half = config.divider * tick
    period = config.frame_duration_ticks * tick
    idle = int(config.latch_active_low)
    edges: dict[str, tuple[tuple[float, ...], tuple[int, ...]]] = {}

    def compact(transitions: list[tuple[float, int]]) -> tuple[tuple[float, ...], tuple[int, ...]]:
        times: list[float] = []
        levels: list[int] = []
        for at, level in transitions:
            if not levels or level != levels[-1]:
                times.append(at)
                levels.append(level)
        return tuple(times), tuple(levels)

    data = [(2 * index * half, bit) for index, bit in enumerate(config.bits)]
    data.append((2 * config.bit_count * half, 0))
    edges["data"] = compact(data)
    if config.free_clock:
        edges["clk"] = ((0.0, half), (0, 1))
        latch_start = 2 * config.bit_count * half
    else:
        clock = [(0.0, 0)]
        for index in range(config.bit_count):
            clock.extend((((2 * index + 1) * half, 1), ((2 * index + 2) * half, 0)))
        edges["clk"] = compact(clock)
        latch_start = (2 * config.bit_count + 1) * half
    edges["latch"] = compact(
        [
            (0.0, idle),
            (latch_start, 1 - idle),
            (latch_start + config.latch_active_ticks * tick, idle),
        ]
    )
    return SignalSource(
        period,
        edges,
        high=high,
        duration=None if config.continuous else config.repeat_count * period,
        idle_latch=idle,
        free_clock_period=2 * half if config.free_clock else None,
    )


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
    start: float
    span: float
    dense: list[float] = field(default_factory=list)
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
        self.time_reference = "CENT"
        self.time_mode = "MAIN"
        self.trigger_mode = "EDGE"
        self.trigger = _Trigger()
        self.wave_source = 1
        self.points = 1000
        self.errors: list[str] = []
        self.running = False
        self.waiting = False
        self.trigger_event = False
        self.single_armed = False
        self.captured_mapping: dict[int, str | None] = {}
        self.records: dict[int, _Record] = {}
        self.analysis: dict[int, Trace] = {}

    def _error(self, text: str) -> None:
        self.errors.append(text)

    # -- signaux --------------------------------------------------------------
    def _voltage(self, source: SignalSource, signal: str | None, time: float) -> float:
        if signal is None:
            return 0.0
        start, previous, new = source.transition(signal, time)
        span = source.high - source.low
        value = source.low + span * new
        # Superpose recent step responses. Starting every falling edge at the
        # high rail would invent a voltage jump when a short pulse has not settled.
        earliest = time - max(12 * self._tau, 24e-9)
        while start >= earliest:
            elapsed = time - start
            delta = span * (new - previous)
            value -= delta * math.exp(-elapsed / self._tau)
            value += (
                0.08 * delta * math.exp(-elapsed / 2e-9) * math.sin(2 * math.pi * 3.5e8 * elapsed)
            )
            if start <= 0:
                break
            earlier = source.transition(signal, start - max(1e-15, abs(start) * 1e-14))
            if earlier[0] >= start:
                break
            start, previous, new = earlier
        return value

    def _trigger_time(
        self, source: SignalSource | None, mapping: dict[int, str | None]
    ) -> float | None:
        signal = mapping.get(self.trigger.source)
        level = self.trigger.level
        if source is None or signal is None or not source.low < level < source.high:
            return None
        rising = self.trigger.slope == "POS"
        edges = source.rising_edges(signal) if rising else source.falling_edges(signal)
        if not edges:
            return None
        changes = sorted(set(source.rising_edges(signal) + source.falling_edges(signal)))
        for start in edges:
            following = next(
                (value for value in changes if value > start), source.period + changes[0]
            )
            end = min(following, start + 12 * self._tau)
            previous_time, previous = start, self._voltage(source, signal, start)
            # Find the first actual crossing, including the damped overshoot. A
            # narrow pulse need not reach an arbitrarily high trigger threshold.
            for index in range(1, 193):
                time = start + index * (end - start) / 192
                value = self._voltage(source, signal, time)
                crossed = previous <= level <= value if rising else value <= level <= previous
                if crossed:
                    left, right = previous_time, time
                    for _ in range(24):
                        middle = (left + right) / 2
                        above = self._voltage(source, signal, middle) >= level
                        if above == rising:
                            right = middle
                        else:
                            left = middle
                    return (left + right) / 2
                previous_time, previous = time, value
        return None

    def _record(
        self,
        source: SignalSource | None,
        channel: int,
        origin: float,
        points: int,
        mapping: dict[int, str | None],
    ) -> list[float]:
        signal = mapping.get(channel)
        step = 10 * self.time_scale / points
        start = self._record_start()
        values = [
            (self._voltage(source, signal, origin + start + index * step) if source else 0.0)
            + self._random.gauss(0.0, self._noise)
            for index in range(points)
        ]
        if self.channels[channel].coupling == "AC" and values:
            mean = sum(values) / len(values)
            values = [value - mean for value in values]
        return values

    def _record_start(self) -> float:
        divisions = {"LEFT": 1, "CENT": 5, "RIGHT": 9}[self.time_reference]
        return self.time_position - divisions * self.time_scale

    def _acquire(self, *, single: bool = False, forced: bool = False) -> None:
        source = self._source()
        mapping = self._mapping()
        trigger = None if forced else self._trigger_time(source, mapping)
        if trigger is None and not forced and (single or self.trigger.sweep == "NORM"):
            self.waiting = True
            return
        origin = trigger if trigger is not None else 0.0
        self.trigger_event = trigger is not None
        self.waiting = False
        self.single_armed = False
        self.captured_mapping = mapping
        self.records.clear()
        self.analysis.clear()
        start = self._record_start()
        for channel in CHANNELS:
            settings = self.channels[channel]
            increment = settings.scale * 10 / 256
            # Mesures « de l'oscilloscope » : enregistrement plus fin, comme sa mémoire.
            fine = 4 * self.points
            dense = self._record(source, channel, origin, fine, mapping)
            self.records[channel] = _Record(
                settings.offset, increment, start, 10 * self.time_scale, dense
            )
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
            if signal == "clk" and source.free_clock_period is not None:
                period = source.free_clock_period
            elif len(edges) >= 2:
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
            self.time_reference = {
                "CENT": "CENT",
                "CENTER": "CENT",
                "LEFT": "LEFT",
                "RIGHT": "RIGHT",
            }[argument]
        elif key == "TIM:MODE":
            self.time_mode = {
                "MAIN": "MAIN",
                "WIND": "WIND",
                "WINDOW": "WIND",
                "XY": "XY",
                "ROLL": "ROLL",
            }[argument]
        elif key == "TRIG:MODE":
            if argument != "EDGE":
                raise ValueError(argument)
            self.trigger_mode = argument
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
            self.single_armed = False
            self.running = True
            self._acquire()
        elif key == "STOP":
            self.single_armed = False
            self.running = False
            self.waiting = False
        elif key == "SING":
            self.running = False
            self.single_armed = True
            self._acquire(single=True)
        elif key == "TRIG:FORC":
            if self.running or self.waiting:
                self._acquire(forced=True)
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
        if key == "TIM:REF":
            return self.time_reference
        if key == "TIM:MODE":
            return self.time_mode
        if key == "TRIG:MODE":
            return self.trigger_mode
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
                self._acquire(single=self.single_armed)
            return "+8" if self.running or self.waiting else "+0"
        if key == "TER":
            event, self.trigger_event = self.trigger_event, False
            return "+1" if event else "+0"
        if key == "WAV:PRE":
            record = self._wave_record()
            start = record.start
            step = record.span / len(record.codes)
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
            self._acquire(single=self.single_armed)
        if self.wave_source not in self.records:
            raise ScopeError("Aucune acquisition disponible : déclenchement en attente.")
        captured = self.records[self.wave_source]
        count = min(self.points, len(captured.dense))
        codes = [
            max(
                0,
                min(
                    255,
                    round(
                        (captured.dense[index * len(captured.dense) // count] - captured.offset)
                        / captured.increment
                        + 128
                    ),
                ),
            )
            for index in range(count)
        ]
        return _Record(
            captured.offset, captured.increment, captured.start, captured.span, codes=codes
        )

    def _measure(self, channel: int, item: str) -> str:
        trace = self.analysis.get(channel)
        record = self.records.get(channel)
        floor = 0.5 * record.increment * 256 / 10 if record else 0.5 * self.channels[channel].scale
        values = (
            measure_trace(
                trace, min_amplitude=floor, clock=self.captured_mapping.get(channel) == "clk"
            )
            if trace is not None
            else None
        )
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
