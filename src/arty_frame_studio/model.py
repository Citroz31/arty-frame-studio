"""Configuration validée, commune au firmware et à la simulation."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .firmware_config import MAX_CORE_HZ, MIN_CORE_HZ, REFERENCE_CORE_HZ

# Horloge de cœur du firmware de référence ; un firmware personnalisé annonce
# la sienne par INFO. Un tick vaut un demi-cycle de cœur (2,5 ns à 200 MHz).
REFERENCE_HZ = REFERENCE_CORE_HZ
TICK_NS = 2.5
MAX_BITS = 26
MAX_COUNTER = 65_535
# repeat_count 0 : la trame se répète sans fin jusqu'à STOP (firmware révision 3).
CONTINUOUS = 0
# Schéma 3 : champ free_clock (CLK libre, firmware révision 4).
PROFILE_SCHEMA = 3


@dataclass(frozen=True, slots=True)
class FrameConfig:
    word: int = 0x2AAAAAA
    bit_count: int = 26
    divider: int = 20
    latch_ticks: int = 8
    gap_ticks: int = 40
    repeat_count: int = 1
    lsb_first: bool = False
    latch_active_low: bool = False
    # Les ticks et le diviseur n'ont de sens qu'avec l'horloge du firmware.
    core_hz: int = REFERENCE_HZ
    # CLK libre : CLK garde sa période pendant LATCH et la pause. LATCH et
    # pause durent alors des périodes entières de CLK (voir free_clock_ticks).
    free_clock: bool = False

    def __post_init__(self) -> None:
        limits = {
            "bit_count": (1, MAX_BITS),
            "divider": (1, MAX_COUNTER),
            "latch_ticks": (1, MAX_COUNTER),
            "gap_ticks": (0, MAX_COUNTER),
            "repeat_count": (CONTINUOUS, MAX_COUNTER),
        }
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                hint = " (0 : continu jusqu'à STOP)" if name == "repeat_count" else ""
                raise ValueError(f"{name} doit être un entier entre {low} et {high}{hint}.")
        if type(self.word) is not int or not 0 <= self.word < 1 << self.bit_count:
            raise ValueError(f"La valeur doit tenir sur {self.bit_count} bits non signés.")
        if type(self.lsb_first) is not bool or type(self.latch_active_low) is not bool:
            raise ValueError("Les options d’ordre et de polarité doivent être des booléens.")
        check_core_hz(self.core_hz)
        if type(self.free_clock) is not bool:
            raise ValueError("L’option CLK libre doit être un booléen.")
        if self.free_clock:
            # Chaque trame doit durer un nombre entier de périodes de CLK
            # (2 × divider ticks) pour que la suivante démarre en phase.
            if self.latch_ticks % self.divider or (self.latch_ticks // self.divider) % 2 == 0:
                raise ValueError(
                    "CLK libre : latch_ticks doit valoir (2k − 1) × divider, "
                    "soit un LATCH de k périodes de CLK."
                )
            if self.gap_ticks % (2 * self.divider):
                raise ValueError(
                    "CLK libre : gap_ticks doit être un multiple de 2 × divider, "
                    "soit une pause d’un nombre entier de périodes de CLK."
                )

    @property
    def continuous(self) -> bool:
        """Émission répétée jusqu'à STOP, sans nombre de trames fixé."""
        return self.repeat_count == CONTINUOUS

    @property
    def bits(self) -> tuple[int, ...]:
        indices = range(self.bit_count)
        if not self.lsb_first:
            indices = range(self.bit_count - 1, -1, -1)
        return tuple((self.word >> index) & 1 for index in indices)

    @property
    def frequency_hz(self) -> float:
        return self.core_hz / self.divider

    @property
    def tick_ns(self) -> float:
        return tick_ns(self.core_hz)

    @property
    def latch_active_ticks(self) -> int:
        """Durée visible de LATCH ; en CLK libre il commence au dernier front descendant."""
        return self.latch_ticks + (self.divider if self.free_clock else 0)

    @property
    def latch_periods(self) -> int:
        """Périodes de CLK couvertes par LATCH (CLK libre)."""
        return self.latch_active_ticks // (2 * self.divider)

    @property
    def gap_periods(self) -> int:
        """Périodes de CLK de la pause (CLK libre)."""
        return self.gap_ticks // (2 * self.divider)

    @property
    def frame_duration_ticks(self) -> int:
        return (2 * self.bit_count + 1) * self.divider + self.latch_ticks + self.gap_ticks

    @property
    def frame_duration_ns(self) -> float:
        return self.frame_duration_ticks * self.tick_ns

    @property
    def total_duration_ns(self) -> float:
        """Durée de la séquence ; ``math.inf`` en émission continue."""
        if self.continuous:
            return math.inf
        return self.frame_duration_ns * self.repeat_count


def check_core_hz(core_hz: int) -> int:
    if type(core_hz) is not int or not MIN_CORE_HZ <= core_hz <= MAX_CORE_HZ:
        raise ValueError(
            f"Horloge de cœur entière entre {MIN_CORE_HZ // 10**6} et "
            f"{MAX_CORE_HZ // 10**6} MHz requise."
        )
    return core_hz


def tick_ns(core_hz: int = REFERENCE_HZ) -> float:
    """Durée d'un tick matériel : un demi-cycle de l'horloge de cœur."""
    return 1e9 / (2 * check_core_hz(core_hz))


def divider_for_frequency(hz: float, core_hz: int = REFERENCE_HZ) -> int:
    """Choisit la fréquence réalisable la plus élevée sans dépasser la demande.

    Un récepteur dimensionné pour ``hz`` n'est ainsi jamais surcadencé : 150 MHz
    donne 100 MHz (N=2), pas 200 MHz. Les comparaisons finales corrigent
    l'arrondi flottant du quotient, sans tolérance autorisant un dépassement.
    """
    check_core_hz(core_hz)
    if not math.isfinite(hz) or not core_hz / MAX_COUNTER <= hz <= core_hz:
        raise ValueError(
            f"Fréquence entre {core_hz / MAX_COUNTER:.3f} Hz et {core_hz / 1e6:g} MHz requise."
        )
    divider = math.ceil(core_hz / hz)
    if divider > 1 and core_hz / (divider - 1) <= hz:
        divider -= 1
    if core_hz / divider > hz:
        divider += 1
    return min(MAX_COUNTER, max(1, divider))


def ticks_for_ns(ns: float, *, allow_zero: bool = False, core_hz: int = REFERENCE_HZ) -> int:
    if not math.isfinite(ns) or ns < 0:
        raise ValueError("La durée doit être finie et positive.")
    tick = tick_ns(core_hz)
    ticks = math.floor(ns / tick + 0.5)
    minimum = 0 if allow_zero else 1
    if not minimum <= ticks <= MAX_COUNTER:
        raise ValueError(
            f"Durée réalisable entre {minimum * tick:.9g} et {MAX_COUNTER * tick:.9g} ns "
            f"(pas de {tick:.6g} ns)."
        )
    return ticks


def free_clock_ticks(
    latch_ns: float, gap_ns: float, divider: int, core_hz: int = REFERENCE_HZ
) -> tuple[int, int]:
    """LATCH (au moins une période) et pause arrondis à des périodes entières de CLK.

    Renvoie ``(latch_ticks, gap_ticks)`` pour SEND : LATCH visible de k périodes
    → ``(2k − 1) × divider`` (il commence une demi-période plus tôt qu'en mode
    normal), pause de m périodes → ``2m × divider``.
    """
    if type(divider) is not int or not 1 <= divider <= MAX_COUNTER:
        raise ValueError(f"divider doit être un entier entre 1 et {MAX_COUNTER}.")
    period_ns = 2 * divider * tick_ns(core_hz)
    for value in (latch_ns, gap_ns):
        if not math.isfinite(value) or value < 0:
            raise ValueError("La durée doit être finie et positive.")
    latch_periods = max(1, math.floor(latch_ns / period_ns + 0.5))
    gap_periods = math.floor(gap_ns / period_ns + 0.5)
    latch_ticks = (2 * latch_periods - 1) * divider
    gap_ticks = 2 * gap_periods * divider
    if latch_ticks > MAX_COUNTER:
        raise ValueError(
            f"CLK libre : LATCH limité à {(MAX_COUNTER // divider + 1) // 2} période(s) "
            f"de CLK avec N = {divider}."
        )
    if gap_ticks > MAX_COUNTER:
        raise ValueError(
            f"CLK libre : pause limitée à {MAX_COUNTER // (2 * divider)} période(s) "
            f"de CLK avec N = {divider}."
        )
    return latch_ticks, gap_ticks


def with_free_clock(config: FrameConfig) -> FrameConfig:
    """Même trame en CLK libre, LATCH et pause arrondis à des périodes entières."""
    if config.free_clock:
        return config
    latch, gap = free_clock_ticks(
        config.latch_ticks * config.tick_ns,
        config.gap_ticks * config.tick_ns,
        config.divider,
        config.core_hz,
    )
    return replace(config, free_clock=True, latch_ticks=latch, gap_ticks=gap)


def parse_word(text: str, base: str, bit_count: int) -> int:
    bases = {"bin": 2, "hex": 16, "dec": 10}
    if base not in bases:
        raise ValueError("Base attendue : bin, hex ou dec.")
    if type(bit_count) is not int or not 1 <= bit_count <= MAX_BITS:
        raise ValueError("Longueur entre 1 et 26 bits requise.")
    normalized = text.strip().replace("_", "").replace(" ", "")
    prefixes = {"bin": "0b", "hex": "0x"}
    prefix = prefixes.get(base)
    if prefix and normalized.lower().startswith(prefix):
        normalized = normalized[2:]
    digits = {"bin": "01", "hex": "0123456789abcdef", "dec": "0123456789"}[base]
    if not normalized or any(char.lower() not in digits for char in normalized):
        raise ValueError("Valeur invalide pour la base sélectionnée.")
    value = int(normalized, bases[base])
    if value >= 1 << bit_count:
        raise ValueError(f"La valeur dépasse la capacité de {bit_count} bits.")
    return value


def save_profile(config: FrameConfig, path: str | Path) -> None:
    payload = {"schema_version": PROFILE_SCHEMA, "frame": asdict(config)}
    Path(path).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def load_profile(path: str | Path) -> FrameConfig:
    profile_path = Path(path)
    if profile_path.stat().st_size > 65_536:
        raise ValueError("Profil trop volumineux (maximum 64 Kio).")
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "frame"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] not in (1, 2, PROFILE_SCHEMA)
    ):
        raise ValueError("Format de profil invalide : schema_version 1 à 3 et frame requis.")
    frame = payload["frame"]
    expected = {field.name for field in fields(FrameConfig)}
    if payload["schema_version"] == 1:
        # Version 1 : firmware de référence à 200 MHz, sans champ core_hz.
        expected.discard("core_hz")
    if payload["schema_version"] < 3:
        # Versions 1 et 2 : CLK toujours interrompue entre les trames.
        expected.discard("free_clock")
    if not isinstance(frame, dict) or set(frame) != expected:
        raise ValueError("Les champs du profil ne correspondent pas à FrameConfig.")
    return FrameConfig(**frame)
