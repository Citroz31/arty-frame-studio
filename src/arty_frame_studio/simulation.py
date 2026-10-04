"""Chronogrammes numériques idéaux, exprimés exactement en ticks matériels.

Un tick vaut un demi-cycle de l'horloge de cœur : 2,5 ns pour le firmware de
référence à 200 MHz, ``FrameConfig.tick_ns`` pour un firmware personnalisé.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from html import escape
from pathlib import Path

from .model import TICK_NS, FrameConfig


@dataclass(frozen=True, slots=True)
class Transition:
    time_ticks: int
    data: int
    clk: int
    latch: int
    tick_ns: float = TICK_NS

    @property
    def time_ns(self) -> float:
        return self.time_ticks * self.tick_ns


@dataclass(frozen=True, slots=True)
class Waveform:
    config: FrameConfig
    transitions: tuple[Transition, ...]
    duration_ticks: int
    frames_simulated: int
    truncated: bool

    @property
    def duration_ns(self) -> float:
        return self.duration_ticks * self.config.tick_ns


# En CLK libre, une trame peut compter jusqu'à ~131 000 fronts de CLK : l'aperçu
# réduit alors le nombre de trames (au moins une) pour rester interactif.
MAX_FREE_CLOCK_TRANSITIONS = 50_000


def simulate(config: FrameConfig, max_frames: int = 4) -> Waveform:
    if type(max_frames) is not int or not 1 <= max_frames <= 256:
        raise ValueError("Nombre de trames à simuler entre 1 et 256.")
    # Une émission continue n'a pas de fin : la fenêtre montre max_frames trames.
    total = math.inf if config.continuous else config.repeat_count
    frames = max_frames if config.continuous else min(config.repeat_count, max_frames)
    if config.free_clock:
        per_frame = 2 * (config.bit_count + config.latch_periods + config.gap_periods)
        frames = min(frames, max(1, MAX_FREE_CLOCK_TRANSITIONS // per_frame))
    idle = int(config.latch_active_low)
    points: list[Transition] = []
    time = 0

    def record(data: int, clk: int, latch: int) -> None:
        point = Transition(time, data, clk, latch, config.tick_ns)
        if points and points[-1].time_ticks == time:
            points[-1] = point
        else:
            points.append(point)

    bits = config.bits
    divider = config.divider
    for _ in range(frames if config.free_clock else 0):
        # CLK libre : une période de 2N ticks sans interruption ; LATCH part du
        # dernier front descendant ; DATA et LATCH changent aux fronts descendants.
        record(bits[0], 0, idle)
        for index, bit in enumerate(bits):
            time += divider
            record(bit, 1, idle)
            time += divider
            last = index + 1 == len(bits)
            record(0 if last else bits[index + 1], 0, 1 - idle if last else idle)
        for period in range(config.latch_periods):
            time += divider
            record(0, 1, 1 - idle)
            time += divider
            record(0, 0, 1 - idle if period + 1 < config.latch_periods else idle)
        for _ in range(config.gap_periods):
            time += divider
            record(0, 1, idle)
            time += divider
            record(0, 0, idle)
    for _ in range(0 if config.free_clock else frames):
        record(bits[0], 0, idle)
        for index, bit in enumerate(bits):
            time += config.divider
            record(bit, 1, idle)
            time += config.divider
            record(bits[index + 1] if index + 1 < len(bits) else 0, 0, idle)
        time += config.divider
        record(0, 0, 1 - idle)
        time += config.latch_ticks
        record(0, 0, idle)
        time += config.gap_ticks
        record(0, 0, idle)
    if frames < total:
        # La fenêtre se termine au début de la répétition suivante : conserver
        # ce niveau réel, plutôt que simuler un arrêt qui n'a pas été demandé.
        record(bits[0], 0, idle)
    return Waveform(config, tuple(points), time, frames, frames < total)


def export_csv(waveform: Waveform, path: str | Path) -> None:
    """Une ligne par instant de transition ; maintien des niveaux entre lignes."""
    with Path(path).open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["time_ns", "DATA", "CLK", "LATCH"])
        for transition in waveform.transitions:
            writer.writerow(
                [f"{transition.time_ns:.1f}", transition.data, transition.clk, transition.latch]
            )


def export_vcd(waveform: Waveform, path: str | Path) -> None:
    lines = [
        "$version Arty Frame Studio, simulation numérique idéale $end",
        "$timescale 1ps $end",
        "$scope module frame $end",
        "$var wire 1 ! DATA $end",
        '$var wire 1 " CLK $end',
        "$var wire 1 # LATCH $end",
        "$upscope $end",
        "$enddefinitions $end",
    ]
    previous: tuple[int, int, int] | None = None
    for transition in waveform.transitions:
        # Picosecond timestamps are exact at 200 MHz (2500 ps per tick).
        lines.append(f"#{round(transition.time_ns * 1000)}")
        levels = (transition.data, transition.clk, transition.latch)
        for index, symbol in enumerate(("!", '"', "#")):
            if previous is None or levels[index] != previous[index]:
                lines.append(f"{levels[index]}{symbol}")
        previous = levels
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def waveform_svg(waveform: Waveform, width: int = 1200) -> str:
    if type(width) is not int or not 400 <= width <= 8000:
        raise ValueError("Largeur du chronogramme entre 400 et 8000 pixels.")
    left, right, height = 92, width - 28, 332
    duration = waveform.duration_ticks

    def x(tick: int) -> float:
        return left + tick / duration * (right - left)

    unit, scale = ("µs", 1000) if waveform.duration_ns >= 1000 else ("ns", 1)
    config = waveform.config
    label = (
        f"{config.bit_count} bits · {config.frequency_hz / 1e6:.6g} MHz · "
        f"{'LSB' if config.lsb_first else 'MSB'} d’abord"
    )
    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        'aria-label="Chronogramme idéal DATA CLK LATCH">',
        '<rect width="100%" height="100%" rx="12" fill="#f8fafc"/>',
        '<g font-family="Arial, sans-serif" fill="#0f172a">',
        '<text x="22" y="28" font-size="16" font-weight="bold">Chronogramme numérique idéal</text>',
        f'<text x="22" y="49" font-size="12" fill="#475569">{escape(label)}</text>',
    ]
    for index in range(11):
        tick = duration * index / 10
        xpos = left + (right - left) * index / 10
        pieces.append(f'<path d="M{xpos:.2f} 66 V266" stroke="#e2e8f0" stroke-dasharray="3 4"/>')
        anchor = "start" if index == 0 else "end" if index == 10 else "middle"
        pieces.append(
            f'<text x="{xpos:.2f}" y="287" text-anchor="{anchor}" font-size="11">'
            f"{tick * config.tick_ns / scale:.3g}</text>"
        )
    for row, (name, color) in enumerate(
        (("DATA", "#2563eb"), ("CLK", "#0891b2"), ("LATCH", "#d97706"))
    ):
        baseline = 112 + row * 68
        pieces.append(f'<text x="18" y="{baseline - 8}" font-size="13">{name}</text>')
        pieces.append(f'<path d="M{left} {baseline} H{right}" stroke="#cbd5e1" stroke-width="1"/>')
        coords: list[str] = []
        previous_y: int | None = None
        for transition in waveform.transitions:
            level = (transition.data, transition.clk, transition.latch)[row]
            ypos = baseline - level * 28
            xpos = x(transition.time_ticks)
            if previous_y is None:
                coords.append(f"M{xpos:.2f} {ypos}")
            else:
                coords.append(f"H{xpos:.2f}")
                if ypos != previous_y:
                    coords.append(f"V{ypos}")
            previous_y = ypos
        coords.append(f"H{right}")
        pieces.append(
            f'<path d="{" ".join(coords)}" fill="none" stroke="{color}" '
            'stroke-width="2" stroke-linejoin="round"/>'
        )
    # Valeur du bit échantillonné au front montant de CLK, dans la fenêtre des
    # bits de chaque trame (en CLK libre, CLK monte aussi pendant LATCH/pause).
    bit_window = 2 * config.divider * config.bit_count
    if config.bit_count * waveform.frames_simulated <= 104 and width >= 800:
        previous_clk = 0
        for transition in waveform.transitions:
            in_bits = transition.time_ticks % config.frame_duration_ticks < bit_window
            if transition.clk and not previous_clk and in_bits:
                pieces.append(
                    f'<text x="{x(transition.time_ticks):.2f}" y="75" font-size="10" '
                    f'text-anchor="middle" fill="#2563eb">{transition.data}</text>'
                )
            previous_clk = transition.clk
    footer = f"Temps ({unit}) · {waveform.frames_simulated} trame(s)"
    if waveform.truncated:
        total = "une émission continue" if config.continuous else str(config.repeat_count)
        footer += f" affichée(s) sur {total} — exports limités à cette fenêtre"
    pieces.append(f'<text x="{left}" y="316" font-size="11" fill="#475569">{escape(footer)}</text>')
    pieces.append("</g></svg>")
    return "\n".join(pieces)
