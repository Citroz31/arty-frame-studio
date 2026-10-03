"""Configuration validée, commune au firmware et à la simulation."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .firmware_config import MAX_CORE_HZ, MIN_CORE_HZ, REFERENCE_CORE_HZ

# Horloge de cœur du firmware de référence ; un firmware personnalisé annonce
# la sienne par INFO. Un tick vaut un demi-cycle de cœur (2,5 ns à 200 MHz).
REFERENCE_HZ = REFERENCE_CORE_HZ
TICK_NS = 2.5
MAX_BITS = 26
MAX_COUNTER = 65_535
PROFILE_SCHEMA = 2


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

    def __post_init__(self) -> None:
        limits = {
            "bit_count": (1, MAX_BITS),
            "divider": (1, MAX_COUNTER),
            "latch_ticks": (1, MAX_COUNTER),
            "gap_ticks": (0, MAX_COUNTER),
            "repeat_count": (1, MAX_COUNTER),
        }
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} doit être un entier entre {low} et {high}.")
        if type(self.word) is not int or not 0 <= self.word < 1 << self.bit_count:
            raise ValueError(f"La valeur doit tenir sur {self.bit_count} bits non signés.")
        if type(self.lsb_first) is not bool or type(self.latch_active_low) is not bool:
            raise ValueError("Les options d’ordre et de polarité doivent être des booléens.")
        check_core_hz(self.core_hz)

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
    def frame_duration_ticks(self) -> int:
        return (2 * self.bit_count + 1) * self.divider + self.latch_ticks + self.gap_ticks

    @property
    def frame_duration_ns(self) -> float:
        return self.frame_duration_ticks * self.tick_ns

    @property
    def total_duration_ns(self) -> float:
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
        or payload["schema_version"] not in (1, PROFILE_SCHEMA)
    ):
        raise ValueError("Format de profil invalide : schema_version 1 ou 2 et frame requis.")
    frame = payload["frame"]
    expected = {field.name for field in fields(FrameConfig)}
    if payload["schema_version"] == 1:
        # Version 1 : firmware de référence à 200 MHz, sans champ core_hz.
        expected.discard("core_hz")
    if not isinstance(frame, dict) or set(frame) != expected:
        raise ValueError("Les champs du profil ne correspondent pas à FrameConfig.")
    return FrameConfig(**frame)
