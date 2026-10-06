"""Sondes du mode mesure : ce qui est mesuré après chaque mot envoyé.

Trois sondes rendent une ``Measurement`` : la validation manuelle de l'opérateur,
une lecture de l'oscilloscope (déjà connecté dans l'onglet Oscilloscope) et une
lecture SCPI d'un instrument (analyseur de réseau…).
"""

from __future__ import annotations

import asyncio
import math
import statistics
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from .instruments import InstrumentError, ScpiInstrument
from .model import FrameConfig
from .scope import Acquisition
from .sweep import Measurement

# Grandeurs lues sur l'oscilloscope : nom affiché et unité.
SCOPE_QUANTITIES = {
    "frequency": ("fréquence", "Hz"),
    "period": ("période", "s"),
    "vpp": ("Vpp", "V"),
    "vmax": ("Vmax", "V"),
    "vmin": ("Vmin", "V"),
    "vavg": ("moyenne", "V"),
    "duty": ("rapport cyclique", "%"),
}


@dataclass(frozen=True)
class ScopeSpec:
    channel: int
    quantity: str

    def __post_init__(self) -> None:
        if type(self.channel) is not int or self.channel not in (1, 2):
            raise ValueError("Voie CH1 ou CH2 attendue.")
        if self.quantity not in SCOPE_QUANTITIES:
            raise ValueError(f"Grandeur inconnue : {self.quantity!r}.")

    @property
    def label(self) -> str:
        name, unit = SCOPE_QUANTITIES[self.quantity]
        return f"CH{self.channel} {name} ({unit})"


def scope_value(acquisition: Acquisition, spec: ScopeSpec) -> float | None:
    """Valeur d'une grandeur, ou ``None`` si elle n'est pas mesurable (pas de front…)."""
    measures = acquisition.measurements.get(spec.channel)
    trace = acquisition.trace(spec.channel)
    if measures is None or trace is None:
        raise InstrumentError(
            f"CH{spec.channel} n'est pas affichée : activer la voie dans l'onglet Oscilloscope."
        )
    if spec.quantity == "vavg":
        return statistics.fmean(trace.volts) if trace.volts else None
    if spec.quantity == "duty":
        return None if measures.duty is None else measures.duty * 100
    return getattr(measures, spec.quantity)


class ScopeProbe:
    """Une acquisition de l'oscilloscope après le mot, puis les grandeurs choisies."""

    def __init__(
        self, acquire: Callable[[], Awaitable[Acquisition]], specs: Sequence[ScopeSpec]
    ) -> None:
        if not specs:
            raise ValueError("Choisir au moins une grandeur à mesurer.")
        self._acquire = acquire
        self.specs = tuple(specs)

    async def __call__(self, config: FrameConfig, index: int) -> Measurement:
        acquisition = await self._acquire()
        values = []
        missing = []
        for spec in self.specs:
            value = scope_value(acquisition, spec)
            if value is None or not math.isfinite(value):
                values.append(math.nan)
                missing.append(spec.label)
            else:
                values.append(value)
        note = f"Non mesurable : {', '.join(missing)}" if missing else ""
        return Measurement(tuple(values), tuple(spec.label for spec in self.specs), note=note)


class InstrumentProbe:
    """Déclenchement et lecture SCPI d'un instrument, dans un fil de travail."""

    def __init__(
        self,
        instrument: ScpiInstrument,
        trigger: Sequence[str],
        reads: Sequence[str],
        labels: Sequence[str] = (),
        *,
        on_word: Callable[[int], None] | None = None,
    ) -> None:
        if not reads:
            raise ValueError("Indiquer au moins une commande de lecture (requête avec « ? »).")
        self.instrument = instrument
        self.trigger = tuple(trigger)
        self.reads = tuple(reads)
        self.labels = tuple(labels)
        self._on_word = on_word
        self._first = True

    def _names(self, count: int) -> tuple[str, ...]:
        """Les noms donnés, dans l'ordre ; les valeurs en plus s'appellent valeur2, valeur3…"""
        named = list(self.labels[:count])
        named += [f"valeur{number + 1}" for number in range(len(named), count)]
        return tuple(named)

    async def __call__(self, config: FrameConfig, index: int) -> Measurement:
        if self._on_word is not None:
            self._on_word(config.word)
        # La file d'erreurs n'est vidée qu'au premier mot : une commande mal écrite
        # est signalée tout de suite, sans doubler les échanges ensuite.
        check = self._first
        values = await asyncio.to_thread(
            self.instrument.measure, self.trigger, self.reads, check_errors=check
        )
        self._first = False
        return Measurement(tuple(values), self._names(len(values)))


class ManualProbe:
    """Attend la décision de l'opérateur : valider, rejeter, sauter ou renvoyer."""

    def __init__(
        self,
        on_wait: Callable[[FrameConfig, int], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        self._on_wait = on_wait
        self._on_done = on_done
        self._future: asyncio.Future[Measurement] | None = None

    @property
    def waiting(self) -> bool:
        return self._future is not None and not self._future.done()

    async def __call__(self, config: FrameConfig, index: int) -> Measurement:
        future: asyncio.Future[Measurement] = asyncio.get_running_loop().create_future()
        self._future = future
        if self._on_wait is not None:
            self._on_wait(config, index)
        try:
            return await future
        finally:
            self._future = None
            if self._on_done is not None:
                self._on_done()

    def decide(self, action: str, note: str = "", value: float | None = None) -> bool:
        """Rend la décision ; ``False`` si aucun mot n'attend de validation."""
        future = self._future
        if future is None or future.done():
            return False
        values = (value,) if value is not None and math.isfinite(value) else ()
        future.set_result(
            Measurement(values, ("valeur relevée",) if values else (), action=action, note=note)
        )
        return True
