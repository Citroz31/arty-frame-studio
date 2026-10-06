"""Sondes du mode mesure : ce qui est mesuré après chaque mot envoyé.

Quatre sondes rendent une ``Measurement`` : la validation manuelle de l'opérateur,
une lecture de l'oscilloscope (déjà connecté dans l'onglet Oscilloscope), une
lecture SCPI d'un instrument et la matrice S complète d'un VNA Keysight, écrite en
Touchstone pour chaque état.
"""

from __future__ import annotations

import asyncio
import math
import statistics
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .instruments import InstrumentError, ScpiInstrument
from .model import FrameConfig
from .pna import PnaDriver
from .scope import Acquisition
from .sweep import Measurement, SweepState, word_text
from .touchstone import (
    SParameters,
    magnitude_db,
    phase_degrees,
    read_touchstone,
    touchstone_name,
    write_touchstone,
)

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


def state_filename(index: int, state: SweepState, bit_count: int, ports: int) -> str:
    """``0007_000000000000000000000110_tr1.s2p`` : rang, mot, niveau de TR s'il y en a un."""
    tr = "" if state.tr is None else f"_tr{state.tr}"
    return touchstone_name(f"{index + 1:04d}_{word_text(state.word, bit_count)}{tr}", ports)


class VnaProbe:
    """Un balayage du VNA par état, la matrice S écrite en Touchstone, une valeur suivie.

    La valeur suivie (module en dB et phase d'un paramètre S à une fréquence) sert
    de critère, de statistique et de graphique ; les fichiers ``.sNp`` gardent toute
    la mesure. Avec ``skip_existing``, un état dont le fichier est déjà là n'est pas
    remesuré (reprise d'une campagne interrompue).
    """

    def __init__(
        self,
        driver: PnaDriver,
        states: Sequence[SweepState],
        folder: Path,
        *,
        track: tuple[int, int] = (2, 1),
        track_frequency: float | None = None,
        skip_existing: bool = False,
        on_word: Callable[[int], None] | None = None,
    ) -> None:
        if not states:
            raise ValueError("Aucun état à mesurer.")
        for port in track:
            if not 1 <= port <= driver.ports:
                raise ValueError(
                    f"Paramètre suivi S{track[0]}{track[1]} : le VNA est réglé sur "
                    f"{driver.ports} port(s)."
                )
        if track_frequency is not None and not math.isfinite(track_frequency):
            raise ValueError("La fréquence suivie doit être un nombre fini.")
        self.driver = driver
        self.states = tuple(states)
        self.folder = Path(folder)
        self.track = track
        self.track_frequency = track_frequency
        self.skip_existing = skip_existing
        self._on_word = on_word
        self._inflight: asyncio.Future[tuple[SParameters, list[str]]] | None = None
        label = f"S{track[0]}{track[1]}"
        self.names = (f"{label} (dB)", f"{label} phase (°)")

    async def prepare(self) -> None:
        await asyncio.to_thread(self.driver.prepare)

    async def _settle_previous(self) -> None:
        """Attend la fin d'un balayage abandonné (Arrêter, Sauter) avant tout autre échange.

        Un fil de lecture ne s'interrompt pas : deux séquences SCPI entrelacées
        laisseraient le VNA avec une sélection de mesure fausse, ou une requête sans
        réponse qui ferme la liaison.
        """
        pending, self._inflight = self._inflight, None
        if pending is not None:
            await asyncio.wait({pending})
            if not pending.cancelled():
                pending.exception()  # lu : rien à signaler, le pas a été abandonné

    async def finish(self) -> None:
        await self._settle_previous()
        await asyncio.to_thread(self.driver.restore)

    def _existing(self, path: Path) -> SParameters | None:
        if not self.skip_existing or not path.exists():
            return None
        try:
            data = read_touchstone(path)
        except (OSError, ValueError):
            return None
        same_axis = data.frequencies == self.driver.frequencies or (
            len(data.frequencies) == len(self.driver.frequencies)
            and all(
                math.isclose(a, b, rel_tol=1e-6)
                for a, b in zip(data.frequencies, self.driver.frequencies, strict=True)
            )
        )
        return data if data.ports == self.driver.ports and same_axis else None

    def _tracked(self, data: SParameters) -> tuple[float, ...]:
        _, value = data.at(self.track[0], self.track[1], self.track_frequency)
        return magnitude_db(value), phase_degrees(value)

    async def __call__(self, config: FrameConfig, index: int) -> Measurement:
        await self._settle_previous()
        state = self.states[index]
        path = self.folder / state_filename(index, state, config.bit_count, self.driver.ports)
        existing = await asyncio.to_thread(self._existing, path)
        if existing is not None:
            note = "Déjà mesuré (fichier conservé)"
            return Measurement(self._tracked(existing), self.names, note=note, file=path.name)
        if self._on_word is not None:
            self._on_word(config.word)
        self._inflight = asyncio.get_running_loop().run_in_executor(None, self.driver.acquire)
        data, problems = await asyncio.shield(self._inflight)
        self._inflight = None
        comments = [
            "Arty Frame Studio : mesure d'un état",
            f"état {index + 1}/{len(self.states)} · mot {word_text(state.word, config.bit_count)} "
            f"(0x{state.word:X})",
            *([f"TR {state.tr}"] if state.tr is not None else []),
            *([f"nom {state.name}"] if state.name else []),
            f"instrument {self.driver.instrument.identity} · canal {self.driver.channel}",
            f"date {datetime.now().isoformat(timespec='seconds')}",
        ]
        await asyncio.to_thread(write_touchstone, path, data, comments=comments)
        if problems:
            return Measurement(
                self._tracked(data),
                self.names,
                action="fail",
                note="Erreur du VNA : " + "; ".join(problems),
                file=path.name,
            )
        return Measurement(self._tracked(data), self.names, file=path.name)
