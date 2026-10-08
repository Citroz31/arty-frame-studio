"""Mode mesure : envoyer une suite de mots, mesurer après chacun, noter les résultats.

Ce module ne dépend ni de Flet ni du matériel. ``SweepRunner`` pilote trois
fonctions fournies par l'appelant : envoyer un mot et attendre la fin de la
trame, mesurer (oscilloscope, VNA, validation manuelle…) et enregistrer le
résultat. Les mots viennent d'une liste saisie, d'un compteur ou d'un bit
isolé qui parcourt le mot.
"""

from __future__ import annotations

import asyncio
import csv
import math
import re
import statistics
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .model import MAX_BITS, FrameConfig
from .scope import format_si

# Garde-fou : 2^24 mots d'un compteur ne se mesurent pas ; un balayage plus long
# doit être découpé ou échantillonné avec un pas.
MAX_STEPS = 100_000

OK, FAIL, SKIPPED, ERROR = "ok", "fail", "skipped", "error"
STATUS_LABELS = {OK: "OK", FAIL: "Échec", SKIPPED: "Sauté", ERROR: "Erreur"}

_SEPARATORS = re.compile(r"[\s,;]+")


# -- mots -------------------------------------------------------------------
def _token_value(token: str, base: str) -> tuple[int, int | None]:
    """Valeur d'un mot saisi et, en binaire, son nombre de chiffres."""
    text = token.replace("_", "")
    lowered = text.lower()
    if lowered.startswith("0x"):
        digits, radix, kind = lowered[2:], 16, "hex"
    elif lowered.startswith("0b"):
        digits, radix, kind = lowered[2:], 2, "bin"
    else:
        radix = {"bin": 2, "hex": 16, "dec": 10}[base]
        digits, kind = lowered, base
    allowed = {2: "01", 10: "0123456789", 16: "0123456789abcdef"}[radix]
    if not digits or any(char not in allowed for char in digits):
        names = {2: "binaire (0 et 1)", 10: "décimale", 16: "hexadécimale"}
        raise ValueError(
            f"Mot invalide : « {token} » en base {names[radix]} ; "
            "préfixer 0x ou 0b, ou changer de base."
        )
    return int(digits, radix), len(digits) if kind == "bin" else None


def parse_words(text: str, bit_count: int, base: str = "bin") -> list[int]:
    """Mots séparés par des retours à la ligne, espaces, virgules ou points-virgules.

    Un mot sans préfixe suit ``base`` (binaire par défaut) ; ``0x`` et ``0b``
    forcent la base. Les zéros de tête n'ont pas d'effet sur la valeur : la
    largeur de la trame vient de ``bit_count`` (voir ``infer_width``).
    """
    if base not in ("bin", "hex", "dec"):
        raise ValueError("Base attendue : bin, hex ou dec.")
    check_width(bit_count)
    tokens = [token for token in _SEPARATORS.split(text.strip()) if token]
    if not tokens:
        raise ValueError("Saisir au moins un mot.")
    if len(tokens) > MAX_STEPS:
        raise ValueError(f"{len(tokens)} mots : {MAX_STEPS} au maximum par balayage.")
    words = []
    for token in tokens:
        value, _ = _token_value(token, base)
        if value >= 1 << bit_count:
            raise ValueError(f"« {token} » dépasse la largeur de {bit_count} bits.")
        words.append(value)
    return words


def infer_width(text: str, base: str = "bin") -> int | None:
    """Largeur commune si tous les mots sont binaires de même longueur, sinon ``None``."""
    widths = set()
    for token in (token for token in _SEPARATORS.split(text.strip()) if token):
        try:
            _, digits = _token_value(token, base)
        except ValueError:
            return None
        if digits is None:
            return None
        widths.add(digits)
    return widths.pop() if len(widths) == 1 else None


def check_width(bit_count: int) -> int:
    if type(bit_count) is not int or not 1 <= bit_count <= MAX_BITS:
        raise ValueError(f"Largeur de mot entre 1 et {MAX_BITS} bits requise.")
    return bit_count


def counter_words(start: int, stop: int, step: int, bit_count: int) -> list[int]:
    """``start``, ``start + step``… jusqu'à ``stop`` inclus (compteur binaire)."""
    check_width(bit_count)
    if type(step) is not int or step < 1:
        raise ValueError("Le pas doit être un entier positif.")
    limit = (1 << bit_count) - 1
    for name, value in (("début", start), ("fin", stop)):
        if type(value) is not int or not 0 <= value <= limit:
            raise ValueError(f"La valeur de {name} doit tenir sur {bit_count} bits (0 à {limit}).")
    if stop < start:
        raise ValueError("La fin doit être supérieure ou égale au début.")
    count = (stop - start) // step + 1
    if count > MAX_STEPS:
        raise ValueError(
            f"{count} mots : {MAX_STEPS} au maximum. Augmenter le pas ou réduire la plage."
        )
    return list(range(start, stop + 1, step))


def walking_words(bit_count: int, *, ones: bool = True) -> list[int]:
    """Un seul bit à 1 (ou à 0) qui parcourt le mot, du bit de poids faible au fort."""
    check_width(bit_count)
    full = (1 << bit_count) - 1
    return [(1 << index) if ones else full ^ (1 << index) for index in range(bit_count)]


def word_text(word: int, bit_count: int) -> str:
    return f"{word:0{bit_count}b}"


def describe_words(words: Sequence[int], bit_count: int) -> str:
    """« 256 mots · 00000000 → 11111111 » pour l'aperçu de la liste."""
    if not words:
        return "Aucun mot."
    first, last = word_text(words[0], bit_count), word_text(words[-1], bit_count)
    return f"{len(words)} mot(s) de {bit_count} bits · {first} → {last}"


def format_duration(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        return "—"
    seconds = round(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


# -- mesures et résultats -------------------------------------------------------
@dataclass(frozen=True)
class SweepState:
    """Un état à mesurer : le mot, le niveau de la broche TR (3,3 V ou 0 V) et un nom.

    ``tr`` vaut ``None`` quand la broche TR n'est pas pilotée par cet état, 1 pour le
    niveau haut (3,3 V) et 0 pour le niveau bas (0 V).
    """

    word: int
    tr: int | None = None
    name: str = ""

    def __post_init__(self) -> None:
        if type(self.word) is not int or self.word < 0:
            raise ValueError("Le mot d'un état est un entier positif.")
        if self.tr not in (None, 0, 1) or isinstance(self.tr, bool):
            raise ValueError("TR : 0, 1 ou aucun.")


def as_states(items: Iterable[int | SweepState]) -> tuple[SweepState, ...]:
    return tuple(item if isinstance(item, SweepState) else SweepState(item) for item in items)


@dataclass(frozen=True)
class Limit:
    """Critère de réussite sur la première valeur mesurée (bornes incluses)."""

    low: float | None = None
    high: float | None = None

    def __post_init__(self) -> None:
        for value in (self.low, self.high):
            if value is not None and not math.isfinite(value):
                raise ValueError("Les bornes doivent être des nombres finis.")
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError("La borne basse dépasse la borne haute.")

    @property
    def active(self) -> bool:
        return self.low is not None or self.high is not None

    def check(self, value: float) -> bool:
        if not math.isfinite(value):
            return False
        return (self.low is None or value >= self.low) and (self.high is None or value <= self.high)


@dataclass(frozen=True)
class Measurement:
    """Ce que rend une mesure : valeurs numériques et décision éventuelle.

    ``action`` : ``ok`` (garder, juger sur les bornes), ``fail`` (rejeter),
    ``skip`` (sauter ce mot) ou ``retry`` (renvoyer le même mot).
    """

    values: tuple[float, ...] = ()
    names: tuple[str, ...] = ()
    action: str = "ok"
    note: str = ""
    file: str = ""  # fichier écrit pour ce pas (Touchstone…)

    def __post_init__(self) -> None:
        if self.action not in ("ok", "fail", "skip", "retry"):
            raise ValueError("Action de mesure : ok, fail, skip ou retry.")
        if self.names and len(self.names) != len(self.values):
            raise ValueError("Un nom par valeur mesurée est requis.")


@dataclass(frozen=True)
class StepResult:
    index: int
    word: int
    bit_count: int
    status: str
    values: tuple[float, ...] = ()
    names: tuple[str, ...] = ()
    note: str = ""
    started: float = 0.0  # secondes depuis l'epoch
    duration: float = 0.0
    tr: int | None = None
    name: str = ""
    file: str = ""

    @property
    def word_bin(self) -> str:
        return word_text(self.word, self.bit_count)


@dataclass(frozen=True)
class SweepSummary:
    total: int
    counts: dict[str, int]
    elapsed: float
    reason: str  # finished, stopped, fail, error
    next_index: int
    message: str = ""


def value_statistics(results: Iterable[StepResult], index: int = 0) -> dict[str, float]:
    """Minimum, maximum, moyenne et nombre de la valeur ``index`` des pas mesurés."""
    values = [
        result.values[index]
        for result in results
        if result.status in (OK, FAIL) and len(result.values) > index
    ]
    if not values:
        return {}
    return {
        "count": float(len(values)),
        "min": min(values),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def result_line(result: StepResult) -> str:
    values = "  ".join(
        f"{name or 'valeur'} {format_si(value, '', 5).strip()}"
        if math.isfinite(value)
        else f"{name or 'valeur'} —"
        for name, value in zip(
            result.names or ("",) * len(result.values), result.values, strict=False
        )
    )
    note = f" · {result.note}" if result.note else ""
    state = (f" TR{result.tr}" if result.tr is not None else "") + (
        f" « {result.name} »" if result.name else ""
    )
    return (
        f"{result.index + 1:>5}  {result.word_bin}{state}  {STATUS_LABELS[result.status]:<6} "
        f"{values}{note}"
    )


# -- exécution ----------------------------------------------------------------
SendFunction = Callable[[FrameConfig], Awaitable[None]]
BeforeWordFunction = Callable[[SweepState], Awaitable[None]]
MeasureFunction = Callable[[FrameConfig, int], Awaitable[Measurement]]


class SweepRunner:
    """Envoie chaque mot, attend la fin de la trame, mesure, puis passe au suivant.

    ``send`` rend la main une fois la trame terminée. Une erreur d'envoi ou de
    mesure arrête le balayage sans masquer le problème : ``summary.next_index``
    permet de le reprendre au même pas. ``stop()`` s'applique dès que possible,
    même pendant une attente de validation ; ``pause()`` après le pas en cours.

    Chaque élément de ``words`` est un mot ou un ``SweepState`` ; ``before_word``
    est attendu avant l'envoi d'un état qui fixe le niveau de TR.
    """

    def __init__(
        self,
        base: FrameConfig,
        words: Sequence[int | SweepState],
        *,
        send: SendFunction,
        before_word: BeforeWordFunction | None = None,
        measure: MeasureFunction | None = None,
        settle: float = 0.0,
        limit: Limit | None = None,
        stop_on_fail: bool = False,
        max_retries: int = 20,
        on_result: Callable[[StepResult], None] | None = None,
        on_step: Callable[[int, int], None] | None = None,
        on_state: Callable[[str], None] | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not words:
            raise ValueError("Aucun mot à envoyer.")
        if len(words) > MAX_STEPS:
            raise ValueError(f"{len(words)} mots : {MAX_STEPS} au maximum par balayage.")
        if not math.isfinite(settle) or settle < 0:
            raise ValueError("L'attente après l'envoi doit être un nombre positif.")
        self.base = base
        self.states = as_states(words)
        self.words = tuple(state.word for state in self.states)
        if before_word is None and any(state.tr is not None for state in self.states):
            raise ValueError("Des états fixent TR mais rien ne pilote la broche TR.")
        self._send = send
        self._before_word = before_word
        self.measure = measure
        self.settle = settle
        self.limit = limit or Limit()
        self.stop_on_fail = stop_on_fail
        self.max_retries = max_retries
        self._on_result = on_result
        self._on_step = on_step
        self._on_state = on_state
        self._sleep = sleep
        self._clock = clock
        self._wall = wall_clock
        self.state = "idle"
        self.results: list[StepResult] = []
        self._stop = asyncio.Event()
        self._skip = asyncio.Event()
        self._resume = asyncio.Event()
        self._resume.set()
        self._pause_requested = False

    # -- commandes ---------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        self._resume.set()

    def skip(self) -> None:
        self._skip.set()

    def pause(self) -> None:
        if self.state == "running":
            self._pause_requested = True

    def resume(self) -> None:
        self._pause_requested = False
        self._resume.set()

    # -- interne -----------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        self.state = state
        if self._on_state is not None:
            self._on_state(state)

    async def _interruptible(self, awaitable: Awaitable[Any]) -> tuple[str, Any]:
        """Attend ``awaitable`` : (« done », valeur), ou (« stop »/« skip ») si demandé avant."""
        task = asyncio.ensure_future(awaitable)
        stopper = asyncio.ensure_future(self._stop.wait())
        skipper = asyncio.ensure_future(self._skip.wait())
        try:
            await asyncio.wait({task, stopper, skipper}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                return "done", task.result()
            kind = "stop" if self._stop.is_set() else "skip"
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return kind, None
        finally:
            for pending in (task, stopper, skipper):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, stopper, skipper, return_exceptions=True)

    def _record(self, result: StepResult) -> StepResult:
        self.results.append(result)
        if self._on_result is not None:
            self._on_result(result)
        return result

    def _judge(self, measurement: Measurement) -> tuple[str, str]:
        if measurement.action == "fail":
            return FAIL, measurement.note
        if measurement.action == "skip":
            return SKIPPED, measurement.note
        if self.limit.active and measurement.values:
            value = measurement.values[0]
            if not self.limit.check(value):
                bounds = (
                    f"{'' if self.limit.low is None else f'{self.limit.low:g}'} … "
                    f"{'' if self.limit.high is None else f'{self.limit.high:g}'}"
                )
                reason = f"{value:g} hors de [{bounds}]"
                return FAIL, f"{measurement.note} · {reason}" if measurement.note else reason
        elif self.limit.active:
            return FAIL, "Aucune valeur mesurée pour appliquer le critère"
        return OK, measurement.note

    def _summary(
        self, started: float, reason: str, next_index: int, message: str = ""
    ) -> SweepSummary:
        counts = {status: 0 for status in (OK, FAIL, SKIPPED, ERROR)}
        for result in self.results:
            counts[result.status] += 1
        return SweepSummary(
            total=len(self.words),
            counts=counts,
            elapsed=self._clock() - started,
            reason=reason,
            next_index=next_index,
            message=message,
        )

    def _result(
        self,
        index: int,
        config: FrameConfig,
        status: str,
        *,
        wall: float,
        step_started: float,
        measurement: Measurement | None = None,
        note: str = "",
    ) -> StepResult:
        state = self.states[index]
        values = measurement.values if measurement is not None else ()
        names = measurement.names if measurement is not None else ()
        return StepResult(
            index,
            state.word,
            config.bit_count,
            status,
            values,
            names,
            note,
            wall,
            self._clock() - step_started,
            tr=state.tr,
            name=state.name,
            file=measurement.file if measurement is not None else "",
        )

    async def _emit(self, state: SweepState, config: FrameConfig) -> None:
        """Niveau de TR d'abord, puis la trame : TR est stable pendant tout l'envoi."""
        if state.tr is not None and self._before_word is not None:
            await self._before_word(state)
        await self._send(config)

    async def run(self, start: int = 0) -> SweepSummary:
        """Exécute les pas ``start`` à la fin ; le résumé donne le pas à reprendre."""
        if not 0 <= start < len(self.words):
            raise ValueError("Pas de départ hors de la liste.")
        started = self._clock()
        self._stop.clear()
        self._skip.clear()
        self._set_state("running")
        index = start
        while index < len(self.words):
            if self._pause_requested:
                self._resume.clear()
                self._set_state("paused")
                await self._resume.wait()
                if not self._stop.is_set():
                    self._set_state("running")
            if self._stop.is_set():
                self._set_state("stopped")
                return self._summary(started, "stopped", index)
            state = self.states[index]
            word = state.word
            config = replace(self.base, word=word)
            if self._on_step is not None:
                self._on_step(index, word)
            step_started = self._clock()
            wall = self._wall()
            self._skip.clear()
            retries = 0
            outcome: StepResult | None = None
            while outcome is None:
                try:
                    kind, _ = await self._interruptible(self._emit(state, config))
                except Exception as exc:  # matériel : arrêter, ne rien deviner
                    self._record(
                        self._result(
                            index,
                            config,
                            ERROR,
                            wall=wall,
                            step_started=step_started,
                            note=str(exc),
                        )
                    )
                    self._set_state("error")
                    return self._summary(
                        started, "error", index, f"Envoi du mot {index + 1} : {exc}"
                    )
                if kind == "stop":
                    self._set_state("stopped")
                    return self._summary(started, "stopped", index)
                if kind == "skip":
                    outcome = self._result(
                        index, config, SKIPPED, wall=wall, step_started=step_started, note="Sauté"
                    )
                    break
                if self.settle:
                    kind, _ = await self._interruptible(self._sleep(self.settle))
                    if kind == "stop":
                        self._set_state("stopped")
                        return self._summary(started, "stopped", index)
                    if kind == "skip":
                        outcome = self._result(
                            index,
                            config,
                            SKIPPED,
                            wall=wall,
                            step_started=step_started,
                            note="Sauté",
                        )
                        break
                if self.measure is None:
                    measurement = Measurement()
                else:
                    try:
                        kind, measurement = await self._interruptible(self.measure(config, index))
                    except Exception as exc:
                        self._record(
                            self._result(
                                index,
                                config,
                                ERROR,
                                wall=wall,
                                step_started=step_started,
                                note=str(exc),
                            )
                        )
                        self._set_state("error")
                        return self._summary(
                            started, "error", index, f"Mesure du mot {index + 1} : {exc}"
                        )
                    if kind == "stop":
                        self._set_state("stopped")
                        return self._summary(started, "stopped", index)
                    if kind == "skip":
                        measurement = Measurement(action="skip", note="Sauté")
                if measurement.action == "retry":
                    retries += 1
                    if retries > self.max_retries:
                        measurement = Measurement(
                            action="fail", note=f"Abandon après {self.max_retries} renvois"
                        )
                    else:
                        continue
                status, note = self._judge(measurement)
                outcome = self._result(
                    index,
                    config,
                    status,
                    wall=wall,
                    step_started=step_started,
                    measurement=measurement,
                    note=note,
                )
            self._record(outcome)
            index += 1
            if outcome.status == FAIL and self.stop_on_fail:
                self._set_state("stopped")
                return self._summary(
                    started, "fail", index, f"Mot {outcome.index + 1} en échec : {outcome.note}"
                )
        self._set_state("finished")
        return self._summary(started, "finished", len(self.words))


# -- export ---------------------------------------------------------------------
def result_columns(results: Sequence[StepResult]) -> list[str]:
    """Noms des colonnes de mesure, dans l'ordre d'apparition."""
    columns: list[str] = []
    width = 0
    for result in results:
        width = max(width, len(result.values))
        for name in result.names:
            if name not in columns:
                columns.append(name)
    for number in range(len(columns), width):
        columns.append(f"valeur{number + 1}")
    return columns


def write_results_csv(results: Sequence[StepResult], path: Path) -> Path:
    """Un pas par ligne : mot en binaire, hexadécimal, décimal, statut et mesures.

    Les colonnes ``tr``, ``nom`` et ``fichier`` n'existent que si un pas les renseigne.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = result_columns(results)
    with_tr = any(result.tr is not None for result in results)
    with_name = any(result.name for result in results)
    with_file = any(result.file for result in results)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "pas",
                "mot_bin",
                "mot_hex",
                "mot_dec",
                *(["tr"] if with_tr else []),
                *(["nom"] if with_name else []),
                "statut",
                *columns,
                *(["fichier"] if with_file else []),
                "note",
                "horodatage",
            ]
        )
        for result in results:
            by_name = dict(zip(result.names, result.values, strict=False))
            cells = []
            for number, column in enumerate(columns):
                if column in by_name:
                    cells.append(f"{by_name[column]:.9g}")
                elif not result.names and number < len(result.values):
                    cells.append(f"{result.values[number]:.9g}")
                else:
                    cells.append("")
            stamp = datetime.fromtimestamp(result.started).isoformat(timespec="milliseconds")
            writer.writerow(
                [
                    result.index + 1,
                    result.word_bin,
                    f"0x{result.word:X}",
                    result.word,
                    *([("" if result.tr is None else result.tr)] if with_tr else []),
                    *([result.name] if with_name else []),
                    STATUS_LABELS[result.status],
                    *cells,
                    *([result.file] if with_file else []),
                    result.note,
                    stamp,
                ]
            )
    return path
