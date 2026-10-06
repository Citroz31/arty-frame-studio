"""Paramètres S d'un multipôle et fichiers Touchstone (``.s1p``, ``.s2p``, ``.s4p``…).

Le mode VNA écrit un fichier par état au format Touchstone 1.0, partie réelle et
imaginaire (``RI``), en hertz, sous une impédance de référence de 50 Ω : les
logiciels de simulation (ADS, AWR, QUCS, scikit-rf) le lisent directement.
"""

from __future__ import annotations

import cmath
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_PORTS = 8
_UNITS = {"HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}
_EXTENSION = re.compile(r"\.s(\d+)p$", re.IGNORECASE)


@dataclass(frozen=True)
class SParameters:
    """Paramètres S mesurés : ``matrix[(i, j)]`` est S_ij (ports numérotés depuis 1)."""

    ports: int
    frequencies: tuple[float, ...]
    matrix: Mapping[tuple[int, int], tuple[complex, ...]]

    def __post_init__(self) -> None:
        if type(self.ports) is not int or not 1 <= self.ports <= MAX_PORTS:
            raise ValueError(f"Nombre de ports entre 1 et {MAX_PORTS} requis.")
        if not self.frequencies:
            raise ValueError("Aucune fréquence dans les paramètres S.")
        if any(not math.isfinite(f) for f in self.frequencies):
            raise ValueError("Fréquences non finies.")
        for i in range(1, self.ports + 1):
            for j in range(1, self.ports + 1):
                trace = self.matrix.get((i, j))
                if trace is None:
                    raise ValueError(f"S{i}{j} manquant dans les paramètres S.")
                if len(trace) != len(self.frequencies):
                    raise ValueError(
                        f"S{i}{j} : {len(trace)} point(s) pour "
                        f"{len(self.frequencies)} fréquence(s)."
                    )

    def nearest_index(self, frequency: float) -> int:
        """Indice du point de mesure le plus proche de ``frequency`` (Hz)."""
        return min(
            range(len(self.frequencies)), key=lambda index: abs(self.frequencies[index] - frequency)
        )

    def at(self, i: int, j: int, frequency: float | None = None) -> tuple[float, complex]:
        """(fréquence réelle, S_ij) au point le plus proche ; milieu de bande par défaut."""
        index = len(self.frequencies) // 2 if frequency is None else self.nearest_index(frequency)
        return self.frequencies[index], self.matrix[(i, j)][index]


def magnitude_db(value: complex) -> float:
    """Module en dB ; un zéro exact vaut -400 dB plutôt que moins l'infini."""
    magnitude = abs(value)
    return 20 * math.log10(magnitude) if magnitude > 1e-20 else -400.0


def phase_degrees(value: complex) -> float:
    return math.degrees(cmath.phase(value))


def touchstone_name(stem: str, ports: int) -> str:
    return f"{stem}.s{ports}p"


def _pair(value: complex) -> str:
    return f"{value.real:.9g} {value.imag:.9g}"


def _order(ports: int) -> list[list[tuple[int, int]]]:
    """Lignes de données d'un point : Touchstone 1.0 écrit S11 S21 S12 S22 pour 2 ports."""
    if ports == 1:
        return [[(1, 1)]]
    if ports == 2:
        return [[(1, 1), (2, 1), (1, 2), (2, 2)]]
    rows: list[list[tuple[int, int]]] = []
    for i in range(1, ports + 1):
        row = [(i, j) for j in range(1, ports + 1)]
        rows.extend(row[start : start + 4] for start in range(0, ports, 4))
    return rows


def write_touchstone(
    path: Path,
    data: SParameters,
    *,
    z0: float = 50.0,
    comments: Sequence[str] = (),
) -> Path:
    """Écrit ``data`` en Touchstone 1.0 (hertz, RI) ; crée le dossier au besoin."""
    path = Path(path)
    match = _EXTENSION.search(path.name)
    if match is None or int(match.group(1)) != data.ports:
        raise ValueError(f"L'extension du fichier doit être .s{data.ports}p.")
    lines = [f"! {comment}" for comment in comments]
    lines.append(f"# HZ S RI R {z0:g}")
    layout = _order(data.ports)
    for index, frequency in enumerate(data.frequencies):
        for number, row in enumerate(layout):
            cells = [_pair(data.matrix[key][index]) for key in row]
            prefix = [f"{frequency:.12g}"] if number == 0 else []
            lines.append(" ".join([*prefix, *cells]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)  # un fichier présent est toujours complet
    return path


def read_touchstone(path: Path) -> SParameters:
    """Relit un fichier Touchstone 1.0 (unités Hz à GHz, formats RI, MA et DB)."""
    path = Path(path)
    match = _EXTENSION.search(path.name)
    if match is None:
        raise ValueError("Extension .sNp attendue.")
    ports = int(match.group(1))
    unit, kind = 1e9, "MA"  # valeurs par défaut de la norme
    numbers: list[float] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("!", 1)[0].strip()
        if not line:
            continue
        if line.startswith("#"):
            options = line[1:].upper().split()
            for option in options:
                if option in _UNITS:
                    unit = _UNITS[option]
                elif option in ("RI", "MA", "DB"):
                    kind = option
                elif option in ("Y", "Z", "G", "H"):
                    raise ValueError("Seuls les paramètres S sont pris en charge.")
            continue
        numbers.extend(float(token) for token in line.split())
    per_point = 1 + 2 * ports * ports
    if not numbers or len(numbers) % per_point:
        raise ValueError("Nombre de valeurs incohérent avec le nombre de ports.")

    def complex_of(first: float, second: float) -> complex:
        if kind == "RI":
            return complex(first, second)
        magnitude = 10 ** (first / 20) if kind == "DB" else first
        return cmath.rect(magnitude, math.radians(second))

    layout = [key for row in _order(ports) for key in row]
    frequencies: list[float] = []
    traces: dict[tuple[int, int], list[complex]] = {key: [] for key in layout}
    for start in range(0, len(numbers), per_point):
        chunk = numbers[start : start + per_point]
        frequencies.append(chunk[0] * unit)
        for number, key in enumerate(layout):
            traces[key].append(complex_of(chunk[1 + 2 * number], chunk[2 + 2 * number]))
    return SParameters(
        ports, tuple(frequencies), {key: tuple(values) for key, values in traces.items()}
    )
