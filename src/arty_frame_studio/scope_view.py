"""Onglet Oscilloscope : visualiser et mesurer DATA/CLK/LATCH depuis l'application.

``ScopePanel`` relie l'interface à un Keysight InfiniiVision (LAN ou USB/VISA) ou
à l'oscilloscope simulé de la démo. ``scope_canvas_shapes`` dessine l'écran de
10 × 8 divisions à partir d'une acquisition ; il ne dépend d'aucun état Flet.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import flet as ft  # type: ignore[import-untyped]
import flet.canvas as cv  # type: ignore[import-untyped]

from .model import FrameConfig
from .scope import (
    CHANNELS,
    LOGIC_HIGH,
    PROBES,
    TIME_SCALES,
    VOLT_SCALES,
    Acquisition,
    ChannelSettings,
    KeysightScope,
    ScopeError,
    ScopeSettings,
    SocketTransport,
    TriggerSettings,
    TriggerTimeout,
    VisaTransport,
    format_si,
    frame_preset,
    list_visa_resources,
    measurement_warnings,
    parse_si,
    period_cursors,
    short_si,
    write_acquisition_csv,
)
from .scope_sim import SignalSource, SimulatedKeysight, signal_source
from .ui_layout import AMBER, BLUE, GREEN, LINE, MUTED, PANEL, RED, TEXT

CHANNEL_COLORS = {1: "#FACC15", 2: "#4ADE80"}  # jaune et vert, comme sur l'oscilloscope
SCREEN_BG = "#05080F"
GRID = "#1C2940"
GRID_AXIS = "#35507A"
SCREEN_HEIGHT = 420
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 46.0, 30.0, 26.0
REFRESH_RATES = (0.2, 0.5, 1.0, 2.0, 5.0)
SIGNAL_NAMES = {"data": "DATA", "clk": "CLK", "latch": "LATCH", "": "Autre / libre"}
PINOUT_HINT = (
    "Firmware de référence : DATA sur JB1, CLK sur JB2, LATCH sur JB3, masse sur JB5 "
    "ou JB11. Un firmware personnalisé utilise les broches de sa configuration."
)
PREFERENCES = Path("profiles") / "oscilloscope.json"


@dataclass(frozen=True)
class Cursors:
    """Positions en divisions depuis le centre : X de −5 à 5, Y de −4 à 4."""

    mode: str = "off"  # off, time, volt, both
    x1: float = -1.0
    x2: float = 1.0
    y1: float = 1.0
    y2: float = -1.0
    channel: int = 1

    @property
    def time(self) -> bool:
        return self.mode in ("time", "both")

    @property
    def volt(self) -> bool:
        return self.mode in ("volt", "both")


def cursor_readout(cursors: Cursors, settings: ScopeSettings) -> str:
    """Valeurs des curseurs : instants relatifs au déclenchement, tensions de la voie."""
    parts = []
    if cursors.time:
        t1 = settings.time_position + cursors.x1 * settings.time_scale
        t2 = settings.time_position + cursors.x2 * settings.time_scale
        delta = t2 - t1
        inverse = format_si(1 / abs(delta), "Hz") if delta else "—"
        parts.append(
            f"X1 {format_si(t1, 's')} · X2 {format_si(t2, 's')} · "
            f"ΔX {format_si(delta, 's')} · 1/ΔX {inverse}"
        )
    if cursors.volt:
        channel = settings.channel(cursors.channel)
        v1 = channel.offset + cursors.y1 * channel.scale
        v2 = channel.offset + cursors.y2 * channel.scale
        parts.append(
            f"CH{cursors.channel} · Y1 {format_si(v1, 'V')} · Y2 {format_si(v2, 'V')} · "
            f"ΔY {format_si(v2 - v1, 'V')}"
        )
    return "\n".join(parts) if parts else "Curseurs désactivés."


def _decimate(points: list[tuple[float, float]], width: float) -> list[tuple[float, float]]:
    """Au plus un trait vertical (min, max) par pixel pour les traces denses."""
    if len(points) <= 3 * width:
        return points
    columns: dict[int, list[float]] = {}
    for x, y in points:
        columns.setdefault(round(x), []).append(y)
    result = []
    for column in sorted(columns):
        values = columns[column]
        result.append((float(column), values[0]))
        if len(values) > 1:
            result.append((float(column), min(values)))
            result.append((float(column), max(values)))
            result.append((float(column), values[-1]))
    return result


def scope_canvas_shapes(
    acquisition: Acquisition | None,
    settings: ScopeSettings,
    cursors: Cursors,
    width: float,
    height: float = SCREEN_HEIGHT,
    labels: dict[int, str] | None = None,
) -> list[Any]:
    """Écran complet : ``screen_shapes`` puis ``cursor_shapes`` par-dessus."""
    return screen_shapes(acquisition, settings, width, height, labels) + cursor_shapes(
        cursors, width, height
    )


def screen_shapes(
    acquisition: Acquisition | None,
    settings: ScopeSettings,
    width: float,
    height: float = SCREEN_HEIGHT,
    labels: dict[int, str] | None = None,
) -> list[Any]:
    """Grille, traces, repères de masse et de déclenchement, sans les curseurs."""
    labels = labels or {}
    left, right = MARGIN_X, width - MARGIN_X
    top, bottom = MARGIN_TOP, height - MARGIN_BOTTOM
    plot_width, plot_height = right - left, bottom - top
    step_x, step_y = plot_width / 10, plot_height / 8
    start = settings.window[0]

    def x_of(time: float) -> float:
        return left + (time - start) / (10 * settings.time_scale) * plot_width

    def y_of(volts: float, channel: int) -> float:
        channel_settings = settings.channel(channel)
        return (
            top
            + plot_height / 2
            - (volts - channel_settings.offset) / channel_settings.scale * step_y
        )

    def clamp(value: float, low: float, high: float) -> float:
        return min(high, max(low, value))

    shapes: list[Any] = [
        cv.Rect(0, 0, width, height, border_radius=12, paint=ft.Paint(color=SCREEN_BG))
    ]
    minor = ft.Paint(color=GRID, stroke_width=1, style=ft.PaintingStyle.STROKE)
    axis = ft.Paint(color=GRID_AXIS, stroke_width=1, style=ft.PaintingStyle.STROKE)
    for index in range(11):
        x = left + index * step_x
        shapes.append(cv.Line(x, top, x, bottom, paint=axis if index == 5 else minor))
    for index in range(9):
        y = top + index * step_y
        shapes.append(cv.Line(left, y, right, y, paint=axis if index == 4 else minor))
    # Graduations fines sur les axes centraux, cinq par division.
    for index in range(51):
        x = left + index * step_x / 5
        shapes.append(
            cv.Line(x, top + plot_height / 2 - 3, x, top + plot_height / 2 + 3, paint=axis)
        )
    for index in range(41):
        y = top + index * step_y / 5
        shapes.append(
            cv.Line(left + plot_width / 2 - 3, y, left + plot_width / 2 + 3, y, paint=axis)
        )

    small = ft.TextStyle(size=11, color=MUTED)
    offset_x = left
    for channel in CHANNELS:
        channel_settings = settings.channel(channel)
        if not channel_settings.enabled:
            continue
        name = labels.get(channel)
        text = (
            f"CH{channel}{' ' + name if name else ''} · {short_si(channel_settings.scale, 'V')}/div"
        )
        shapes.append(
            cv.Text(
                offset_x,
                8,
                text,
                style=ft.TextStyle(
                    size=12, color=CHANNEL_COLORS[channel], weight=ft.FontWeight.W_600
                ),
            )
        )
        offset_x += 220
    trigger = settings.trigger
    arrow = "↑" if trigger.slope == "POS" else "↓"
    sweep = "Auto" if trigger.sweep == "AUTO" else "Normal"
    shapes.append(
        cv.Text(
            right,
            8,
            f"{short_si(settings.time_scale, 's')}/div · T CH{trigger.source} {arrow} "
            f"{format_si(trigger.level, 'V', 3)} · {sweep}",
            style=ft.TextStyle(size=12, color=TEXT),
            alignment=ft.alignment.top_right,
        )
    )
    if settings.time_position:
        shapes.append(
            cv.Text(
                left,
                bottom + 6,
                f"Retard {format_si(settings.time_position, 's')}",
                style=small,
            )
        )

    if acquisition is None:
        shapes.append(
            cv.Text(
                left + plot_width / 2,
                top + plot_height / 2 - 30,
                "Aucune acquisition · Connecter, puis Single ou Run",
                style=ft.TextStyle(size=14, color=MUTED),
                alignment=ft.alignment.center,
            )
        )
    else:
        for trace in acquisition.traces:
            if not settings.channel(trace.channel).enabled or not trace.times:
                continue
            points = [
                (
                    clamp(x_of(time), left, right),
                    clamp(y_of(volts, trace.channel), top, bottom),
                )
                for time, volts in zip(trace.times, trace.volts, strict=False)
            ]
            points = _decimate(points, plot_width)
            elements = [cv.Path.MoveTo(*points[0])]
            elements.extend(cv.Path.LineTo(*point) for point in points[1:])
            shapes.append(
                cv.Path(
                    elements=elements,
                    paint=ft.Paint(
                        color=CHANNEL_COLORS[trace.channel],
                        stroke_width=1.6,
                        style=ft.PaintingStyle.STROKE,
                    ),
                    data=f"trace:{trace.channel}",
                )
            )

    # Repères : masse de chaque voie à gauche, niveau et instant de déclenchement.
    for channel in CHANNELS:
        if not settings.channel(channel).enabled:
            continue
        y = clamp(y_of(0.0, channel), top, bottom)
        shapes.append(
            cv.Text(
                left - 6,
                y,
                f"{channel}▶",
                style=ft.TextStyle(
                    size=12, color=CHANNEL_COLORS[channel], weight=ft.FontWeight.W_700
                ),
                alignment=ft.alignment.center_right,
            )
        )
    trigger_y = clamp(y_of(trigger.level, trigger.source), top, bottom)
    shapes.append(
        cv.Text(
            right + 6,
            trigger_y,
            "◀T",
            style=ft.TextStyle(size=12, color=AMBER, weight=ft.FontWeight.W_700),
            alignment=ft.alignment.center_left,
        )
    )
    trigger_x = x_of(0.0)
    if left <= trigger_x <= right:
        shapes.append(
            cv.Text(
                trigger_x,
                top - 2,
                "▼",
                style=ft.TextStyle(size=12, color=AMBER),
                alignment=ft.alignment.bottom_center,
            )
        )

    return shapes


def cursor_shapes(cursors: Cursors, width: float, height: float = SCREEN_HEIGHT) -> list[Any]:
    """Curseurs en pointillés ; redessinés seuls quand ils bougent."""
    left, right = MARGIN_X, width - MARGIN_X
    top, bottom = MARGIN_TOP, height - MARGIN_BOTTOM
    step_x, step_y = (right - left) / 10, (bottom - top) / 8
    shapes: list[Any] = []
    dashed = [5, 4]
    if cursors.time:
        cursor_paint = ft.Paint(
            color=AMBER, stroke_width=1.2, style=ft.PaintingStyle.STROKE, stroke_dash_pattern=dashed
        )
        for name, position in (("X1", cursors.x1), ("X2", cursors.x2)):
            x = left + (position + 5) * step_x
            shapes.append(cv.Line(x, top, x, bottom, paint=cursor_paint, data=f"cursor:{name}"))
            shapes.append(
                cv.Text(x + 3, bottom - 16, name, style=ft.TextStyle(size=11, color=AMBER))
            )
    if cursors.volt:
        color = CHANNEL_COLORS[cursors.channel]
        cursor_paint = ft.Paint(
            color=color, stroke_width=1.2, style=ft.PaintingStyle.STROKE, stroke_dash_pattern=dashed
        )
        for name, position in (("Y1", cursors.y1), ("Y2", cursors.y2)):
            y = top + 4 * step_y - position * step_y
            shapes.append(cv.Line(left, y, right, y, paint=cursor_paint, data=f"cursor:{name}"))
            shapes.append(
                cv.Text(right - 22, y - 15, name, style=ft.TextStyle(size=11, color=color))
            )
    return shapes


def _options(values: tuple[float, ...], unit: str) -> list[Any]:
    return [ft.dropdown.Option(repr(value), f"{short_si(value, unit)}/div") for value in values]


def _closest(value: float, values: tuple[float, ...]) -> str:
    return repr(min(values, key=lambda choice: abs(math.log(choice / value))))


class ScopePanel:
    """État et commandes de l'onglet Oscilloscope.

    Les échanges avec l'instrument passent dans un fil séparé et sont sérialisés
    par ``io_lock`` : un réglage n'interrompt jamais une acquisition en cours.
    """

    def __init__(
        self,
        page: Any,
        *,
        project_root: Path,
        log: Callable[[str, str], None],
        frame_source: Callable[[], FrameConfig | None],
    ) -> None:
        self.page = page
        self.project_root = project_root
        self._log = log
        self._frame_source = frame_source
        self.scope: KeysightScope | None = None
        self.settings = ScopeSettings()
        self.acquisition: Acquisition | None = None
        self.cursors = Cursors()
        self.running = False
        self.closing = False
        self.pending = False
        # Attente maximale d'un front en mode Normal pour une acquisition.
        self.trigger_wait = 2.0
        # Réglages en cours d'envoi : une acquisition lancée avant eux ne les écrase pas.
        self.applying = 0
        self.count = 0
        self.width = 900.0
        self.io_lock = asyncio.Lock()
        self.run_task: asyncio.Task[None] | None = None
        self._cached_source: tuple[FrameConfig, SignalSource] | None = None
        # Grille et traces gardées entre deux déplacements de curseur.
        self._screen: list[Any] = []
        self._create_controls()
        self._load_preferences()
        self._show_settings(self.settings)
        self._sync()

    # -- contrôles --------------------------------------------------------------
    def _create_controls(self) -> None:
        self.source = ft.Dropdown(
            label="Oscilloscope",
            value="demo",
            width=270,
            options=[
                ft.dropdown.Option("demo", "Simulation (démo)"),
                ft.dropdown.Option("lan", "Keysight · réseau LAN"),
                ft.dropdown.Option("visa", "Keysight · USB / VISA"),
            ],
            on_change=self._source_changed,
        )
        self.address = ft.TextField(
            label="Adresse",
            width=360,
            dense=True,
            border_color=LINE,
            on_submit=self._toggle_connection,  # Entrée connecte
        )
        self.resources = ft.Dropdown(
            label="Instruments trouvés",
            width=420,
            visible=False,
            options=[],
            on_change=self._resource_chosen,
        )
        self.search_button = ft.IconButton(
            ft.Icons.SEARCH,
            tooltip="Rechercher les oscilloscopes USB/LAN visibles par VISA",
            on_click=self._search,
        )
        self.connect_button = ft.ElevatedButton(
            "Connecter", icon=ft.Icons.LINK, on_click=self._toggle_connection
        )
        self.identity = ft.Text("Non connecté", size=12, color=MUTED, selectable=True)
        self.source_hint = ft.Text(size=12, color=MUTED)
        self.mapping = {
            channel: ft.Dropdown(
                label=f"Sonde CH{channel} sur",
                value=default,
                width=200,
                options=[ft.dropdown.Option(key, name) for key, name in SIGNAL_NAMES.items()],
                on_change=self._mapping_changed,
            )
            for channel, default in ((1, "data"), (2, "clk"))
        }

        self.run_button = ft.ElevatedButton(
            "Run", icon=ft.Icons.PLAY_ARROW, on_click=self._toggle_run
        )
        self.single_button = ft.OutlinedButton(
            "Single", icon=ft.Icons.LOOKS_ONE, on_click=self._single
        )
        self.autoscale_button = ft.OutlinedButton(
            "Auto scale", icon=ft.Icons.AUTO_FIX_HIGH, on_click=self._autoscale
        )
        self.preset_button = ft.TextButton(
            "Préréglage de la trame",
            icon=ft.Icons.TUNE,
            tooltip="1 V/div, voies empilées, 5 périodes de CLK et déclenchement sur CLK, "
            "d'après la trame du Pilotage",
            on_click=self._preset,
        )
        self.refresh = ft.Dropdown(
            label="Rafraîchissement",
            value="0.5",
            width=170,
            options=[ft.dropdown.Option(repr(rate), f"{rate:g} s") for rate in REFRESH_RATES],
        )
        self.status = ft.Text("Arrêté", size=12, color=MUTED)
        # Sans largeur fixe : le canevas suit son cadre et signale sa taille (on_resize).
        self.canvas = cv.Canvas(
            shapes=[],
            height=SCREEN_HEIGHT,
            on_resize=self._resized,
            resize_interval=100,
        )
        self.screen = ft.GestureDetector(
            content=self.canvas,
            on_tap_down=self._pointer,
            on_pan_update=self._pointer,
            drag_interval=30,
        )

        self.frequency_text = {
            channel: ft.Text(
                "—", size=24, weight=ft.FontWeight.W_700, color=CHANNEL_COLORS[channel]
            )
            for channel in CHANNELS
        }
        self.period_text = {
            channel: ft.Text("", size=16, weight=ft.FontWeight.W_500, color=CHANNEL_COLORS[channel])
            for channel in CHANNELS
        }
        self.channel_title = {
            channel: ft.Text(f"CH{channel}", size=12, color=CHANNEL_COLORS[channel])
            for channel in CHANNELS
        }
        self.detail_text = {
            channel: ft.Text("", size=12, color=TEXT, selectable=True) for channel in CHANNELS
        }
        self.warning = ft.Text(size=12, color=AMBER, visible=False, selectable=True)

        self.enabled = {
            channel: ft.Switch(
                label=f"CH{channel}",
                value=True,
                active_color=CHANNEL_COLORS[channel],
                on_change=self._settings_changed,
            )
            for channel in CHANNELS
        }
        self.vscale = {
            channel: ft.Dropdown(
                label="Calibre",
                width=120,
                options=_options(VOLT_SCALES, "V"),
                on_change=self._settings_changed,
            )
            for channel in CHANNELS
        }
        self.offset = {
            channel: ft.TextField(
                label="Décalage (V)",
                width=120,
                dense=True,
                border_color=LINE,
                on_submit=self._settings_changed,
                on_blur=self._settings_changed,
            )
            for channel in CHANNELS
        }
        self.probe = {
            channel: ft.Dropdown(
                label="Sonde",
                width=110,
                options=[ft.dropdown.Option(repr(value), f"{value:g}:1") for value in PROBES],
                on_change=self._settings_changed,
            )
            for channel in CHANNELS
        }
        self.coupling = {
            channel: ft.Dropdown(
                label="Couplage",
                width=110,
                options=[ft.dropdown.Option("DC"), ft.dropdown.Option("AC")],
                on_change=self._settings_changed,
            )
            for channel in CHANNELS
        }
        self.time_scale = ft.Dropdown(
            label="Base de temps",
            width=150,
            options=_options(TIME_SCALES, "s"),
            on_change=self._settings_changed,
        )
        self.time_position = ft.TextField(
            label="Position (s)",
            hint_text="0, 100n, -2u…",
            width=150,
            dense=True,
            border_color=LINE,
            on_submit=self._settings_changed,
            on_blur=self._settings_changed,
        )
        self.trigger_source = ft.Dropdown(
            label="Source",
            width=110,
            options=[ft.dropdown.Option(str(channel), f"CH{channel}") for channel in CHANNELS],
            on_change=self._settings_changed,
        )
        self.trigger_slope = ft.Dropdown(
            label="Front",
            width=150,
            options=[
                ft.dropdown.Option("POS", "Montant ↑"),
                ft.dropdown.Option("NEG", "Descendant ↓"),
            ],
            on_change=self._settings_changed,
        )
        self.trigger_level = ft.TextField(
            label="Niveau (V)",
            width=120,
            dense=True,
            border_color=LINE,
            on_submit=self._settings_changed,
            on_blur=self._settings_changed,
        )
        self.trigger_half = ft.TextButton(
            "50 %",
            tooltip="Niveau au milieu du signal de la source",
            on_click=self._trigger_middle,
        )
        self.trigger_sweep = ft.Dropdown(
            label="Mode",
            width=140,
            options=[ft.dropdown.Option("AUTO", "Auto"), ft.dropdown.Option("NORM", "Normal")],
            on_change=self._settings_changed,
        )

        self.cursor_mode = ft.Dropdown(
            label="Curseurs",
            value="off",
            width=200,
            options=[
                ft.dropdown.Option("off", "Désactivés"),
                ft.dropdown.Option("time", "Temps (X)"),
                ft.dropdown.Option("volt", "Tension (Y)"),
                ft.dropdown.Option("both", "Temps et tension"),
            ],
            on_change=self._cursors_changed,
        )
        self.cursor_channel = ft.Dropdown(
            label="Voie Y",
            value="1",
            width=110,
            options=[ft.dropdown.Option(str(channel), f"CH{channel}") for channel in CHANNELS],
            on_change=self._cursors_changed,
        )
        self.sliders = {
            name: ft.Slider(
                min=-5 if name.startswith("x") else -4,
                max=5 if name.startswith("x") else 4,
                divisions=200 if name.startswith("x") else 160,
                value=getattr(self.cursors, name),
                label=name.upper(),
                expand=True,
                on_change=self._cursors_changed,
            )
            for name in ("x1", "x2", "y1", "y2")
        }
        self.snap_button = ft.OutlinedButton(
            "Mesurer une période",
            icon=ft.Icons.STRAIGHTEN,
            tooltip="Place X1 et X2 sur deux fronts montants consécutifs",
            on_click=self._snap_period,
        )
        self.cursor_text = ft.Text("Curseurs désactivés.", size=12, color=TEXT, selectable=True)
        self.export_button = ft.OutlinedButton(
            "Exporter CSV", icon=ft.Icons.DOWNLOAD, on_click=self._export_csv
        )
        self.screenshot_button = ft.OutlinedButton(
            "Copie d'écran PNG", icon=ft.Icons.CAMERA_ALT, on_click=self._screenshot
        )
        self.export_note = ft.Text(size=12, color=MUTED, selectable=True)
        self._source_changed()

    # -- disposition ------------------------------------------------------------
    def _stepper(self, target: str | int, dropdown: ft.Dropdown) -> ft.Row:
        """Calibre entouré de loupes : le cran voisin, comme un bouton rotatif."""
        unit = "s/div" if target == "time" else "V/div"
        return ft.Row(
            [
                ft.IconButton(
                    ft.Icons.ZOOM_OUT,
                    tooltip=f"Dézoomer : {unit} supérieur",
                    data=(target, 1),
                    on_click=self._step,
                ),
                dropdown,
                ft.IconButton(
                    ft.Icons.ZOOM_IN,
                    tooltip=f"Zoomer : {unit} inférieur",
                    data=(target, -1),
                    on_click=self._step,
                ),
            ],
            spacing=0,
            tight=True,  # largeur du contenu : le champ voisin reste sur la même ligne
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    @staticmethod
    def _card(*controls: Any) -> ft.Container:
        # Mêmes cartes que les autres onglets, un peu plus compactes.
        return ft.Container(
            ft.Column(
                list(controls), spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH
            ),
            padding=16,
            bgcolor=PANEL,
            border_radius=16,
            border=ft.border.all(1, LINE),
        )

    @staticmethod
    def _title(text: str, note: str | None = None, size: int = 16) -> ft.Control:
        controls = [ft.Text(text, size=size, weight=ft.FontWeight.W_600)]
        if note:
            controls.append(ft.Text(note, size=12, color=MUTED))
        return ft.Column(controls, spacing=4)

    def build(self) -> ft.Control:
        connection = self._card(
            self._title(
                "Oscilloscope",
                "Keysight InfiniiVision (DSOX1202A) en LAN ou USB/VISA, ou signaux simulés "
                "de la trame. Les commandes passent par le PC ; l'écran se met à jour à "
                "chaque acquisition.",
                size=18,
            ),
            ft.Row(
                [self.source, self.address, self.search_button, self.connect_button],
                wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            self.resources,
            self.source_hint,
            ft.Row([self.mapping[1], self.mapping[2], self.identity], wrap=True, spacing=12),
            ft.Text(PINOUT_HINT, size=12, color=MUTED),
        )
        toolbar = ft.Row(
            [
                self.run_button,
                self.single_button,
                self.autoscale_button,
                self.preset_button,
                self.refresh,
                self.status,
            ],
            wrap=True,
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        measures = ft.ResponsiveRow(
            [
                ft.Container(
                    ft.Column(
                        [
                            self.channel_title[channel],
                            ft.Row(
                                [self.frequency_text[channel], self.period_text[channel]],
                                spacing=14,
                                wrap=True,
                                vertical_alignment=ft.CrossAxisAlignment.END,
                            ),
                            self.detail_text[channel],
                        ],
                        spacing=2,
                    ),
                    padding=12,
                    bgcolor=PANEL,
                    border_radius=12,
                    border=ft.border.all(1, LINE),
                    col={"xs": 12, "md": 6},
                )
                for channel in CHANNELS
            ],
            spacing=12,
        )
        display = ft.Column(
            [
                toolbar,
                ft.Container(
                    self.screen,
                    height=SCREEN_HEIGHT,
                    border_radius=12,
                    border=ft.border.all(1, LINE),
                    bgcolor=SCREEN_BG,
                ),
                measures,
                self.warning,
            ],
            spacing=12,
            # Largeur imposée au cadre de l'écran : le canevas la remplit.
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        channels = self._card(
            self._title("Voies", "Calibre, décalage du centre, sonde, couplage"),
            *[
                ft.Column(
                    [
                        self.enabled[channel],
                        ft.Row(
                            [self._stepper(channel, self.vscale[channel]), self.offset[channel]],
                            wrap=True,
                            spacing=8,
                        ),
                        ft.Row([self.probe[channel], self.coupling[channel]], wrap=True, spacing=8),
                    ],
                    spacing=6,
                )
                for channel in CHANNELS
            ],
        )
        timebase = self._card(
            self._title("Base de temps", "Position : retard du centre de l'écran sur le front"),
            ft.Row(
                [self._stepper("time", self.time_scale), self.time_position], wrap=True, spacing=8
            ),
        )
        trigger = self._card(
            self._title("Déclenchement", "Front sur CH1 ou CH2"),
            ft.Row([self.trigger_source, self.trigger_slope], wrap=True, spacing=8),
            ft.Row(
                [self.trigger_level, self.trigger_half, self.trigger_sweep],
                wrap=True,
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        cursors = self._card(
            self._title("Curseurs", "Glisser ou cliquer sur l'écran pour placer le plus proche"),
            ft.Row([self.cursor_mode, self.cursor_channel], wrap=True, spacing=8),
            *[
                ft.Row([ft.Text(name.upper(), width=26), slider])
                for name, slider in self.sliders.items()
            ],
            ft.Row([self.snap_button]),
            self.cursor_text,
        )
        export = self._card(
            self._title("Exporter", "Points de la dernière acquisition ou écran de l'appareil"),
            ft.Row([self.export_button, self.screenshot_button], wrap=True),
            self.export_note,
        )
        controls = ft.Column([channels, timebase, trigger, cursors, export], spacing=12)
        return ft.Column(
            [
                connection,
                ft.ResponsiveRow(
                    [
                        ft.Container(display, col={"xs": 12, "lg": 8}),
                        ft.Container(controls, col={"xs": 12, "lg": 4}),
                    ],
                    spacing=14,
                    run_spacing=14,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
            ],
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            spacing=14,
        )

    # -- état affiché -------------------------------------------------------------
    def _update(self) -> None:
        if not self.closing:
            self.page.update()

    def _labels(self) -> dict[int, str]:
        return {
            channel: SIGNAL_NAMES[value].split(" ")[0]
            for channel, value in self._mapping_values().items()
            if value
        }

    def _mapping_values(self) -> dict[int, str | None]:
        return {channel: (self.mapping[channel].value or None) for channel in CHANNELS}

    def _sync(self) -> None:
        connected = self.scope is not None
        demo = self.source.value == "demo"
        self.connect_button.text = "Déconnecter" if connected else "Connecter"
        self.connect_button.icon = ft.Icons.LINK_OFF if connected else ft.Icons.LINK
        self.connect_button.disabled = self.pending
        self.source.disabled = connected or self.pending
        self.address.disabled = connected or demo or self.pending
        self.search_button.disabled = connected or self.source.value != "visa" or self.pending
        for button in (self.single_button, self.autoscale_button, self.preset_button):
            button.disabled = not connected or self.pending
        self.run_button.disabled = not connected
        self.run_button.text = "Stop" if self.running else "Run"
        self.run_button.icon = ft.Icons.STOP if self.running else ft.Icons.PLAY_ARROW
        # Vert pour lancer, rouge pendant l'acquisition continue, comme la touche de l'appareil.
        self.run_button.style = (
            ft.ButtonStyle(bgcolor=RED if self.running else GREEN, color="#0C1423")
            if connected
            else None
        )
        self.screenshot_button.disabled = not connected or demo or self.pending
        self.export_button.disabled = self.acquisition is None
        self.cursor_channel.disabled = not self.cursors.volt
        for name, slider in self.sliders.items():
            slider.disabled = not (self.cursors.time if name.startswith("x") else self.cursors.volt)
        self.snap_button.disabled = self.acquisition is None

    def _show_settings(self, settings: ScopeSettings) -> None:
        """Recopie les réglages (relus de l'appareil) dans les contrôles."""
        for channel in CHANNELS:
            channel_settings = settings.channel(channel)
            self.enabled[channel].value = channel_settings.enabled
            self.vscale[channel].value = _closest(channel_settings.scale, VOLT_SCALES)
            self.offset[channel].value = f"{channel_settings.offset:.6g}"
            self.probe[channel].value = _closest(channel_settings.probe, PROBES)
            self.coupling[channel].value = channel_settings.coupling
        self.time_scale.value = _closest(settings.time_scale, TIME_SCALES)
        self.time_position.value = (
            "0" if settings.time_position == 0 else format_si(settings.time_position, "s")
        )
        trigger = settings.trigger
        self.trigger_source.value = str(trigger.source)
        self.trigger_slope.value = trigger.slope
        self.trigger_level.value = f"{trigger.level:.4g}"
        self.trigger_sweep.value = trigger.sweep

    @staticmethod
    def _value(name: str, text: str | None, unit: str) -> float:
        try:
            return parse_si(text or "0", unit)
        except ValueError as exc:
            raise ValueError(f"{name} : {exc}") from exc

    def _settings_from_controls(self) -> ScopeSettings:
        channels = []
        for channel in CHANNELS:
            channels.append(
                ChannelSettings(
                    enabled=bool(self.enabled[channel].value),
                    scale=float(self.vscale[channel].value or "1"),
                    offset=self._value(f"Décalage CH{channel}", self.offset[channel].value, "V"),
                    probe=float(self.probe[channel].value or "1"),
                    coupling=self.coupling[channel].value or "DC",
                )
            )
        return ScopeSettings(
            (channels[0], channels[1]),
            time_scale=float(self.time_scale.value or "5e-08"),
            time_position=self._value("Position", self.time_position.value, "s"),
            trigger=TriggerSettings(
                source=int(self.trigger_source.value or "1"),
                slope=self.trigger_slope.value or "POS",
                level=self._value("Niveau", self.trigger_level.value, "V"),
                sweep=self.trigger_sweep.value or "AUTO",
            ),
            points=self.settings.points,
        )

    def _draw(self, *, cursors_only: bool = False) -> None:
        # Les mêmes objets pour la grille et les traces : Flet n'envoie alors que
        # les curseurs modifiés, ce qui garde le glisser fluide.
        if not cursors_only or not self._screen:
            self._screen = screen_shapes(
                self.acquisition, self.settings, self.width, labels=self._labels()
            )
        self.canvas.shapes = self._screen + cursor_shapes(self.cursors, self.width)
        self.cursor_text.value = cursor_readout(self.cursors, self.settings)

    def _show_measurements(self) -> None:
        acquisition = self.acquisition
        mapping = self._mapping_values()
        for channel in CHANNELS:
            signal = mapping.get(channel)
            title = f"CH{channel}" + (f" · {SIGNAL_NAMES[signal]}" if signal else "")
            self.channel_title[channel].value = title
            values = acquisition.measurements.get(channel) if acquisition else None
            if values is None:
                self.frequency_text[channel].value = "—"
                self.period_text[channel].value = ""
                self.detail_text[channel].value = (
                    "Voie désactivée" if acquisition else "En attente d'une acquisition"
                )
                continue
            self.frequency_text[channel].value = format_si(values.frequency, "Hz")
            self.period_text[channel].value = (
                f"T = {format_si(values.period, 's')}" if values.period else "pas de front"
            )
            duty = "—" if values.duty is None else f"{values.duty * 100:.1f} %"
            lines = [
                f"Rapport cyclique {duty}",
                f"Vpp {format_si(values.vpp, 'V')} · Min {format_si(values.vmin, 'V')} · "
                f"Max {format_si(values.vmax, 'V')}",
                "Mesuré par l'oscilloscope"
                if values.source == "oscilloscope"
                else "Calculé sur les points affichés",
            ]
            local = acquisition.local.get(channel) if acquisition else None
            if (
                local is not None
                and local.frequency
                and values.frequency
                and abs(values.frequency / local.frequency - 1) > 0.02
            ):
                lines.append(
                    f"Fronts à l'écran : {format_si(local.frequency, 'Hz')} "
                    "(l'appareil mesure le premier cycle, CLK en salves possible)"
                )
            self.detail_text[channel].value = "\n".join(lines)
        warnings = measurement_warnings(acquisition, mapping) if acquisition else []
        self.warning.value = "\n".join(warnings)
        self.warning.visible = bool(warnings)

    # -- préférences ----------------------------------------------------------------
    def _load_preferences(self) -> None:
        """Dernier oscilloscope utilisé : type de liaison, adresse, sondes, cadence."""
        try:
            data = json.loads((self.project_root / PREFERENCES).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if data.get("source") in ("demo", "lan", "visa"):
            self.source.value = data["source"]
        if isinstance(data.get("address"), str):
            self.address.value = data["address"]
        mapping = data.get("mapping")
        if isinstance(mapping, dict):
            for channel in CHANNELS:
                if mapping.get(str(channel)) in SIGNAL_NAMES:
                    self.mapping[channel].value = mapping[str(channel)]
        if data.get("refresh") in [repr(rate) for rate in REFRESH_RATES]:
            self.refresh.value = data["refresh"]
        self._source_changed()

    def _save_preferences(self) -> None:
        data = {
            "source": self.source.value,
            "address": (self.address.value or "").strip(),
            "mapping": {str(channel): self.mapping[channel].value or "" for channel in CHANNELS},
            "refresh": self.refresh.value,
        }
        path = self.project_root / PREFERENCES
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError:
            pass  # simple confort : la connexion fonctionne sans

    # -- simulation ---------------------------------------------------------------
    def _signal_source(self) -> SignalSource | None:
        config = self._frame_source()
        if config is None:
            return None
        if self._cached_source is None or self._cached_source[0] != config:
            self._cached_source = (config, signal_source(config))
        return self._cached_source[1]

    # -- événements -----------------------------------------------------------------
    def _source_changed(self, _: Any = None) -> None:
        kind = self.source.value
        self.resources.visible = False
        if kind == "lan":
            self.address.label = "Adresse IP de l'oscilloscope"
            self.address.hint_text = "192.168.1.50"
            self.source_hint.value = (
                "SCPI sur le port 5025, sans logiciel supplémentaire. L'adresse se lit sur "
                "l'oscilloscope : Utility → I/O → LAN."
            )
        elif kind == "visa":
            self.address.label = "Ressource VISA"
            self.address.hint_text = "USB0::0x2A8D::…::INSTR"
            self.source_hint.value = (
                "USB : installer Keysight IO Libraries Suite (gratuit), relier le port USB "
                "arrière de l'oscilloscope, puis Rechercher."
            )
        else:
            self.address.label = "Adresse"
            self.address.hint_text = "Inutile en simulation"
            self.source_hint.value = (
                "Simulation : les voies montrent les signaux de la trame du Pilotage, avec "
                "des fronts LVCMOS 3,3 V réalistes. Aucun appareil n'est piloté."
            )
        self._sync()

    def _resource_chosen(self, _: Any = None) -> None:
        if self.resources.value:
            self.address.value = self.resources.value
            self._update()

    async def _search(self, _: Any = None) -> None:
        self.pending = True
        self._sync()
        self._update()
        try:
            found = await asyncio.to_thread(list_visa_resources)
        finally:
            self.pending = False
        self.resources.options = [ft.dropdown.Option(resource) for resource in found]
        self.resources.visible = bool(found)
        if found:
            scopes = [resource for resource in found if resource.startswith("USB")] or found
            self.resources.value = scopes[0]
            self.address.value = scopes[0]
            self._log(f"Instruments VISA : {', '.join(found)}", BLUE)
        else:
            self._log(
                "Aucun instrument VISA trouvé : vérifier le câble USB arrière de l'oscilloscope "
                "et Keysight IO Libraries Suite, ou utiliser la connexion LAN.",
                AMBER,
            )
        self._sync()
        self._update()

    def _open(self) -> tuple[KeysightScope, str, ScopeSettings]:
        kind = self.source.value
        address = (self.address.value or "").strip()
        transport: Any
        if kind == "lan":
            transport = SocketTransport(address)
        elif kind == "visa":
            transport = VisaTransport(address)
        else:
            transport = SimulatedKeysight(self._signal_source, self._mapping_values)
        scope = KeysightScope(transport)
        try:
            identity = scope.identify()
            settings = scope.read_settings()
            if kind == "demo":
                settings = frame_preset(self._frame_source(), self._mapping_values(), settings)
                scope.apply_settings(settings)
        except Exception:
            scope.close(resume=False)
            raise
        return scope, identity, settings

    async def _toggle_connection(self, _: Any = None) -> None:
        if self.pending:
            return
        if self.scope is not None:
            await self.disconnect()
            return
        self.pending = True
        self.status.value = "Connexion…"
        self._sync()
        self._update()
        try:
            async with self.io_lock:
                scope, identity, settings = await asyncio.to_thread(self._open)
        except (ScopeError, ValueError, OSError) as exc:
            self.status.value = "Non connecté"
            self._log(f"Oscilloscope : {exc}", RED)
            self._report(str(exc))
            return
        finally:
            self.pending = False
            self._sync()
            self._update()
        self.scope = scope
        self.settings = settings
        fields = [part.strip() for part in identity.split(",")]
        self.identity.value = " · ".join(fields[:3]) if len(fields) >= 3 else identity
        self.identity.color = BLUE if self.source.value == "demo" else GREEN
        self._log(f"Oscilloscope connecté : {identity}", GREEN)
        self._save_preferences()
        self._show_settings(settings)
        self._sync()
        await self._acquire()

    async def disconnect(self) -> None:
        self.running = False
        if self.run_task is not None:
            await asyncio.gather(self.run_task, return_exceptions=True)
            self.run_task = None
        scope, self.scope = self.scope, None
        if scope is not None:
            async with self.io_lock:
                await asyncio.to_thread(scope.close)
            self._log("Oscilloscope déconnecté ; il reprend son acquisition (Run).", MUTED)
        self.identity.value = "Non connecté"
        self.identity.color = MUTED
        self.status.value = "Arrêté"
        self._sync()
        self._update()

    def _report(self, message: str) -> None:
        self.status.value = message
        self.status.color = RED

    async def _acquire(self, *, continuous: bool = False) -> None:
        scope = self.scope
        if scope is None:
            return
        try:
            async with self.io_lock:
                acquisition = await asyncio.to_thread(
                    scope.capture, timeout=self.trigger_wait, points=self.settings.points
                )
        except TriggerTimeout as exc:
            # Normal sans front : comme l'appareil, garder l'écran et attendre.
            self.status.value = "En attente de déclenchement (mode Normal)"
            self.status.color = AMBER
            if not continuous:
                self._log(f"Oscilloscope : {exc}", AMBER)
            self._update()
            return
        except (ScopeError, OSError, ValueError) as exc:
            self.running = False
            self._report(f"Acquisition impossible : {exc}")
            self._log(f"Oscilloscope : {exc}", RED)
            self._sync()
            self._update()
            return
        self.acquisition = acquisition
        self.count += 1
        if acquisition.settings != self.settings and not self.applying:
            # Réglages modifiés sur la face avant : l'écran suit l'appareil.
            self.settings = acquisition.settings
            self._show_settings(self.settings)
        self.status.value = (
            f"Acquisition {self.count} · "
            + ("déclenchée" if acquisition.triggered else "sans front (Auto)")
            + f" · {acquisition.elapsed * 1000:.0f} ms"
        )
        self.status.color = GREEN if acquisition.triggered else AMBER
        self._draw()
        self._show_measurements()
        self._sync()
        self._update()

    async def _single(self, _: Any = None) -> None:
        if self.running:
            self.running = False
        await self._acquire()

    async def _toggle_run(self, _: Any = None) -> None:
        if self.scope is None:
            return
        if self.running:
            self.running = False
            self._sync()
            self._update()
            return
        self.running = True
        self._sync()
        self._update()
        self.run_task = asyncio.create_task(self._run_loop())

    def _interval(self) -> float:
        try:
            return float(self.refresh.value or "0.5")
        except ValueError:
            return 0.5

    async def _run_loop(self) -> None:
        try:
            while self.running and not self.closing and self.scope is not None:
                await self._acquire(continuous=True)
                await asyncio.sleep(self._interval())
        finally:
            self.running = False
            self._sync()
            self._update()

    async def _settings_changed(self, _: Any = None) -> None:
        try:
            settings = self._settings_from_controls()
        except ValueError as exc:
            self._report(str(exc))
            self._update()
            return
        if settings == self.settings:
            return
        self.settings = settings
        self._draw()
        self._update()
        await self._apply(settings)

    async def _apply(self, settings: ScopeSettings) -> None:
        scope = self.scope
        if scope is None:
            return
        self.applying += 1
        try:
            async with self.io_lock:
                await asyncio.to_thread(scope.apply_settings, settings)
        except (ScopeError, OSError) as exc:
            self._report(str(exc))
            self._log(f"Oscilloscope : {exc}", RED)
            self._update()
            return
        finally:
            self.applying -= 1
        if not self.running:
            await self._acquire()

    async def _step(self, event: Any) -> None:
        target, direction = event.control.data
        if target == "time":
            dropdown, values = self.time_scale, TIME_SCALES
        else:
            dropdown, values = self.vscale[target], VOLT_SCALES
        index = values.index(float(_closest(float(dropdown.value or values[0]), values)))
        dropdown.value = repr(values[max(0, min(len(values) - 1, index + direction))])
        await self._settings_changed()

    async def _autoscale(self, _: Any = None) -> None:
        scope = self.scope
        if scope is None or self.pending:
            return
        self.pending = True
        self.status.value = "Auto scale en cours…"
        self.status.color = AMBER
        self._sync()
        self._update()
        try:
            async with self.io_lock:
                settings = await asyncio.to_thread(scope.autoscale)
        except (ScopeError, OSError) as exc:
            self._report(f"Auto scale : {exc}")
            self._log(f"Oscilloscope : {exc}", RED)
            return
        finally:
            self.pending = False
            self._sync()
            self._update()
        self.settings = settings
        self._show_settings(settings)
        self._log("Oscilloscope : Auto scale appliqué.", BLUE)
        await self._acquire()

    async def _preset(self, _: Any = None) -> None:
        settings = frame_preset(self._frame_source(), self._mapping_values(), self.settings)
        self.settings = settings
        self._show_settings(settings)
        self._draw()
        self._update()
        await self._apply(settings)

    async def _trigger_middle(self, _: Any = None) -> None:
        source = int(self.trigger_source.value or "1")
        values = self.acquisition.local.get(source) if self.acquisition else None
        if values is not None and values.high is not None and values.low is not None:
            level = (values.high + values.low) / 2
        else:
            level = LOGIC_HIGH / 2
        self.trigger_level.value = f"{level:.4g}"
        await self._settings_changed()

    async def _mapping_changed(self, _: Any = None) -> None:
        self._show_measurements()
        self._draw()
        self._update()
        if self.scope is not None and self.source.value == "demo" and not self.running:
            await self._acquire()

    # -- curseurs -------------------------------------------------------------------
    def _cursors_changed(self, _: Any = None) -> None:
        self.cursors = Cursors(
            mode=self.cursor_mode.value or "off",
            x1=float(self.sliders["x1"].value),
            x2=float(self.sliders["x2"].value),
            y1=float(self.sliders["y1"].value),
            y2=float(self.sliders["y2"].value),
            channel=int(self.cursor_channel.value or "1"),
        )
        self._sync()
        self._draw(cursors_only=True)
        self._update()

    def _set_cursors(self, **changes: Any) -> None:
        self.cursors = replace(self.cursors, **changes)
        for name in ("x1", "x2", "y1", "y2"):
            self.sliders[name].value = getattr(self.cursors, name)
        self.cursor_mode.value = self.cursors.mode
        self._sync()
        self._draw(cursors_only=True)
        self._update()

    def _pointer(self, event: Any) -> None:
        """Clic ou glisser : déplace le curseur le plus proche du point visé."""
        x, y = getattr(event, "local_x", None), getattr(event, "local_y", None)
        if x is None or y is None or self.cursors.mode == "off":
            return
        plot_width = self.width - 2 * MARGIN_X
        plot_height = SCREEN_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
        division_x = min(5.0, max(-5.0, (float(x) - MARGIN_X) / plot_width * 10 - 5))
        division_y = min(4.0, max(-4.0, 4 - (float(y) - MARGIN_TOP) / plot_height * 8))
        candidates = []
        if self.cursors.time:
            candidates += [("x1", self.cursors.x1, division_x), ("x2", self.cursors.x2, division_x)]
        if self.cursors.volt:
            candidates += [("y1", self.cursors.y1, division_y), ("y2", self.cursors.y2, division_y)]
        name, _, target = min(candidates, key=lambda item: abs(item[1] - item[2]))
        self._set_cursors(**{name: round(target, 3)})

    def _snap_period(self, _: Any = None) -> None:
        acquisition = self.acquisition
        if acquisition is None:
            return
        mapping = self._mapping_values()
        preferred = [channel for channel, signal in mapping.items() if signal == "clk"]
        order = preferred + [self.settings.trigger.source, 1, 2]
        found = None
        for channel in order:
            trace = acquisition.trace(channel)
            if trace is None:
                continue
            floor = 0.5 * self.settings.channel(channel).scale
            found = period_cursors(trace, self.settings.time_position, min_amplitude=floor)
            if found:
                break
        if found is None:
            self._report("Pas deux fronts montants à l'écran : élargir la base de temps.")
            self._update()
            return
        scale, position = self.settings.time_scale, self.settings.time_position
        mode = "both" if self.cursors.volt else "time"
        self._set_cursors(
            mode=mode,
            x1=round((found[0] - position) / scale, 4),
            x2=round((found[1] - position) / scale, 4),
        )

    # -- redimensionnement et export -------------------------------------------------------
    def _resized(self, event: Any) -> None:
        width = float(getattr(event, "width", 0) or 0)
        if not math.isfinite(width) or width < 300 or abs(width - self.width) < 0.5:
            return
        self.width = width
        self._draw()
        self._update()

    def _export_csv(self, _: Any = None) -> None:
        acquisition = self.acquisition
        if acquisition is None:
            return
        path = self.project_root / "exports" / f"oscilloscope-{datetime.now():%Y%m%d-%H%M%S}.csv"
        try:
            write_acquisition_csv(acquisition, path)
        except OSError as exc:
            self._report(f"Export CSV : {exc}")
            self._update()
            return
        self.export_note.value = f"Points exportés : {path}"
        self._log(self.export_note.value, GREEN)
        self._update()

    async def _screenshot(self, _: Any = None) -> None:
        scope = self.scope
        if scope is None:
            return
        try:
            async with self.io_lock:
                image = await asyncio.to_thread(scope.screenshot)
        except (ScopeError, OSError) as exc:
            self._report(f"Copie d'écran : {exc}")
            self._update()
            return
        folder = self.project_root / "exports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"oscilloscope-{datetime.now():%Y%m%d-%H%M%S}.png"
        path.write_bytes(image)
        self.export_note.value = f"Copie d'écran : {path}"
        self._log(self.export_note.value, GREEN)
        self._update()

    async def shutdown(self) -> None:
        self.closing = True
        self.running = False
        if self.run_task is not None:
            self.run_task.cancel()
            await asyncio.gather(self.run_task, return_exceptions=True)
        scope, self.scope = self.scope, None
        if scope is not None:
            await asyncio.to_thread(scope.close)
