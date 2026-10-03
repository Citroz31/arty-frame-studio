"""Configuration validée, commune au firmware et à la simulation."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path

REFERENCE_HZ = 200_000_000
TICK_NS = 2.5
MAX_BITS = 26
MAX_COUNTER = 65_535


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

    @property
    def bits(self) -> tuple[int, ...]:
        indices = range(self.bit_count)
        if not self.lsb_first:
            indices = range(self.bit_count - 1, -1, -1)
        return tuple((self.word >> index) & 1 for index in indices)

    @property
    def frequency_hz(self) -> float:
        return REFERENCE_HZ / self.divider

    @property
    def frame_duration_ticks(self) -> int:
        return (2 * self.bit_count + 1) * self.divider + self.latch_ticks + self.gap_ticks

    @property
    def frame_duration_ns(self) -> float:
        return self.frame_duration_ticks * TICK_NS

    @property
    def total_duration_ns(self) -> float:
        return self.frame_duration_ns * self.repeat_count


def divider_for_frequency(hz: float) -> int:
    """Choisit la fréquence réalisable la plus élevée sans dépasser la demande.

    Un récepteur dimensionné pour ``hz`` n'est ainsi jamais surcadencé : 150 MHz
    donne 100 MHz (N=2), pas 200 MHz. Un écart relatif de 1 ppm, très inférieur
    à la tolérance de l'oscillateur, absorbe l'arrondi d'une saisie décimale.
    """
    if not math.isfinite(hz) or not REFERENCE_HZ / MAX_COUNTER <= hz <= REFERENCE_HZ:
        raise ValueError(f"Fréquence entre {REFERENCE_HZ / MAX_COUNTER:.3f} Hz et 200 MHz requise.")
    ideal = REFERENCE_HZ / hz
    nearest = round(ideal)
    divider = nearest if math.isclose(ideal, nearest, rel_tol=1e-6) else math.ceil(ideal)
    return min(MAX_COUNTER, max(1, divider))


def ticks_for_ns(ns: float, *, allow_zero: bool = False) -> int:
    if not math.isfinite(ns) or ns < 0:
        raise ValueError("La durée doit être finie et positive.")
    ticks = math.floor(ns / TICK_NS + 0.5)
    minimum = 0 if allow_zero else 1
    if not minimum <= ticks <= MAX_COUNTER:
        raise ValueError(f"Durée réalisable entre {minimum * TICK_NS:g} et 163837,5 ns.")
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
    payload = {"schema_version": 1, "frame": asdict(config)}
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
        or payload["schema_version"] != 1
    ):
        raise ValueError("Format de profil invalide : schema_version=1 et frame requis.")
    frame = payload["frame"]
    if not isinstance(frame, dict) or set(frame) != {field.name for field in fields(FrameConfig)}:
        raise ValueError("Les champs du profil ne correspondent pas à FrameConfig.")
    return FrameConfig(**frame)
