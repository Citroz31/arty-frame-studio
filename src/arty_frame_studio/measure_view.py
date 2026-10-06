"""Onglet Mesure : envoyer une suite de mots et mesurer après chacun.

``MeasurePanel`` prépare la liste de mots (liste saisie, compteur, bit isolé),
choisit ce qui valide chaque mot (opérateur, oscilloscope, instrument SCPI), puis
déroule le balayage avec ``sweep.SweepRunner``. Les résultats s'affichent au fil
de l'eau, avec un graphique de la première valeur, et s'exportent en CSV.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import flet as ft  # type: ignore[import-untyped]
import flet.canvas as cv  # type: ignore[import-untyped]

from .instruments import (
    PRESETS,
    InstrumentPreset,
    ScpiInstrument,
    SimulatedVna,
    commands_from_text,
    open_transport,
)
from .model import MAX_BITS, FrameConfig
from .pna import FoundInstrument, PnaDriver, SimulatedPna, discover_instruments
from .probes import (
    SCOPE_QUANTITIES,
    InstrumentProbe,
    ManualProbe,
    ScopeProbe,
    ScopeSpec,
    VnaProbe,
)
from .scope import Acquisition, ScopeConnectionError, ScopeError, parse_si
from .states import MAX_STATES, describe_states, infer_states_width, load_states_text, parse_states
from .sweep import (
    FAIL,
    OK,
    SKIPPED,
    Limit,
    MeasureFunction,
    StepResult,
    SweepRunner,
    SweepState,
    SweepSummary,
    counter_words,
    describe_words,
    format_duration,
    infer_width,
    parse_words,
    result_line,
    value_statistics,
    walking_words,
    word_text,
    write_results_csv,
)
from .ui_layout import AMBER, BLUE, GREEN, LINE, MUTED, PANEL, RED, TEXT

PREFERENCES = Path("profiles") / "mesure.json"
CHART_HEIGHT = 220
CHART_MARGIN = (58.0, 26.0, 16.0, 28.0)  # gauche, haut, droite, bas
VISIBLE_RESULTS = 300
STATUS_COLORS = {OK: GREEN, FAIL: RED, SKIPPED: MUTED, "error": RED}
WORD_MODES = {
    "list": "Liste de mots",
    "states": "Liste d'états : mot ; TR ; nom (fichier)",
    "counter": "Compteur (début, fin, pas)",
    "walk1": "Un seul bit à 1 qui parcourt le mot",
    "walk0": "Un seul bit à 0 qui parcourt le mot",
}
PROBE_KINDS = {
    "manual": "Validation manuelle (opérateur)",
    "scope": "Oscilloscope (onglet Oscilloscope)",
    "vna": "VNA Keysight : paramètres S de chaque état (.sNp)",
    "instrument": "Instrument SCPI (autre appareil, commandes libres)",
    "none": "Aucune mesure : envoi seul",
}
TX_LEVELS = {"1": "TX = niveau haut (3,3 V)", "0": "TX = niveau bas (0 V)"}
_S_PARAMETER = re.compile(r"[Ss]([1-8])([1-8])")


class Bench(Protocol):
    """Ce que le mode mesure demande à l'application : carte, oscilloscope, verrous."""

    def device_ready(self) -> bool: ...

    def scope_ready(self) -> bool: ...

    async def send_word(self, config: FrameConfig) -> None: ...

    async def acquire_scope(self) -> Acquisition: ...

    def set_busy(self, busy: bool) -> None: ...

    def tr_supported(self) -> bool: ...

    async def set_tr(self, level: int) -> None: ...


def results_chart_shapes(
    results: list[StepResult],
    width: float,
    height: float = CHART_HEIGHT,
    *,
    limit: Limit | None = None,
    label: str = "valeur 1",
) -> list[Any]:
    """Première valeur mesurée en fonction du pas ; échecs en rouge, bornes en pointillés."""
    left, top, right_margin, bottom_margin = CHART_MARGIN
    right, bottom = width - right_margin, height - bottom_margin
    plot_width, plot_height = right - left, bottom - top
    shapes: list[Any] = [
        cv.Rect(0, 0, width, height, border_radius=12, paint=ft.Paint(color="#05080F"))
    ]
    grid = ft.Paint(color="#1C2940", stroke_width=1, style=ft.PaintingStyle.STROKE)
    for index in range(5):
        y = top + index * plot_height / 4
        shapes.append(cv.Line(left, y, right, y, paint=grid))
    points = [
        (result.index, result.values[0], result.status)
        for result in results
        if result.values and math.isfinite(result.values[0])
    ]
    small = ft.TextStyle(size=11, color=MUTED)
    if not points:
        shapes.append(
            cv.Text(
                left + plot_width / 2,
                top + plot_height / 2,
                "Le graphique apparaît avec les premières valeurs mesurées.",
                style=ft.TextStyle(size=12, color=MUTED),
                alignment=ft.alignment.center,
            )
        )
        return shapes
    values = [value for _, value, _ in points]
    low, high = min(values), max(values)
    # Au-delà de 1500 pas, un point sur n : le tracé reste fluide, les extrêmes comptent.
    stride = max(1, math.ceil(len(points) / 1500))
    if stride > 1:
        points = [point for number, point in enumerate(points) if number % stride == 0]
    if limit is not None and limit.active:
        low = min(low, limit.low if limit.low is not None else low)
        high = max(high, limit.high if limit.high is not None else high)
    if high - low < 1e-12:
        low, high = low - 0.5, high + 0.5
    pad = (high - low) * 0.06
    low, high = low - pad, high + pad
    first, last = results[0].index, results[-1].index
    span = max(1, last - first)

    def x_of(index: int) -> float:
        return left + (index - first) / span * plot_width

    def y_of(value: float) -> float:
        return bottom - (value - low) / (high - low) * plot_height

    for index in range(5):
        value = high - index * (high - low) / 4
        shapes.append(
            cv.Text(
                left - 6,
                top + index * plot_height / 4,
                f"{value:.4g}",
                style=small,
                alignment=ft.alignment.center_right,
            )
        )
    shapes.append(cv.Text(left, bottom + 6, f"pas {first + 1}", style=small))
    shapes.append(
        cv.Text(right, bottom + 6, f"pas {last + 1}", style=small, alignment=ft.alignment.top_right)
    )
    shapes.append(cv.Text(left + 4, 4, label, style=ft.TextStyle(size=11, color=TEXT)))
    if limit is not None and limit.active:
        dashed = ft.Paint(
            color=AMBER, stroke_width=1.2, style=ft.PaintingStyle.STROKE, stroke_dash_pattern=[5, 4]
        )
        for bound in (limit.low, limit.high):
            if bound is not None:
                y = y_of(bound)
                shapes.append(cv.Line(left, y, right, y, paint=dashed))
    elements = [cv.Path.MoveTo(x_of(points[0][0]), y_of(points[0][1]))]
    elements.extend(cv.Path.LineTo(x_of(index), y_of(value)) for index, value, _ in points[1:])
    shapes.append(
        cv.Path(
            elements=elements,
            paint=ft.Paint(color=BLUE, stroke_width=1.4, style=ft.PaintingStyle.STROKE),
            data="chart:line",
        )
    )
    if len(points) <= 400:
        for index, value, status in points:
            shapes.append(
                cv.Circle(
                    x_of(index),
                    y_of(value),
                    3.2,
                    paint=ft.Paint(
                        color=STATUS_COLORS.get(status, MUTED), style=ft.PaintingStyle.FILL
                    ),
                    data=f"chart:point:{index}",
                )
            )
    return shapes


class MeasurePanel:
    """État et commandes de l'onglet Mesure."""

    def __init__(
        self,
        page: Any,
        *,
        project_root: Path,
        log: Callable[[str, str], None],
        frame_source: Callable[[], FrameConfig | None],
        bench: Bench,
    ) -> None:
        self.page = page
        self.project_root = project_root
        self._log = log
        self._frame_source = frame_source
        self.bench = bench
        self.runner: SweepRunner | None = None
        self.task: asyncio.Task[None] | None = None
        self.summary: SweepSummary | None = None
        self.results: list[StepResult] = []
        self.limit = Limit()
        self.running = False
        self.closing = False
        self.manual = ManualProbe(self._manual_wait, self._manual_done)
        self.instrument: ScpiInstrument | None = None
        self.simulated: SimulatedVna | SimulatedPna | None = None
        self.instrument_pending = False
        self.detect_pending = False
        self.found: list[FoundInstrument] = []
        self.vna_probe: VnaProbe | None = None
        self.vna_folder: Path | None = None
        self.vna_driver_note = ""
        self.width = 900.0
        self._last_render = 0.0
        self._states: list[SweepState] = []
        self._create_controls()
        self._load_preferences()
        self._mode_changed()

    # -- contrôles ------------------------------------------------------------------
    def _field(self, label: str, value: str = "", *, width: int = 140, **extra: Any) -> Any:
        return ft.TextField(
            label=label,
            value=value,
            width=width,
            dense=True,
            border_color=LINE,
            on_change=self._words_changed,
            **extra,
        )

    def _create_controls(self) -> None:
        self.word_mode = ft.Dropdown(
            label="Mots à envoyer",
            value="list",
            width=330,
            options=[ft.dropdown.Option(key, text) for key, text in WORD_MODES.items()],
            on_change=self._mode_changed,
        )
        self.word_base = ft.Dropdown(
            label="Base des mots",
            value="bin",
            width=150,
            options=[
                ft.dropdown.Option("bin", "Binaire"),
                ft.dropdown.Option("hex", "Hexadécimal"),
                ft.dropdown.Option("dec", "Décimal"),
            ],
            on_change=self._words_changed,
        )
        self.word_list = self._field(
            "Un mot par ligne (zéros de tête inclus)",
            "00000000\n00000001\n00000010\n00000011",
            width=520,
            multiline=True,
            min_lines=4,
            max_lines=8,
        )
        self.counter_start = self._field("Début", "0")
        self.counter_stop = self._field("Fin", "11111111")
        self.counter_step = self._field("Pas (décimal)", "1")
        self.width_field = self._field(
            "Largeur (bits)",
            "",
            width=130,
            hint_text="auto",
            tooltip="Vide : largeur commune des mots binaires saisis, sinon celle du Pilotage.",
        )
        self.repeats = self._field(
            "Répétitions par mot",
            "1",
            width=170,
            tooltip="Chaque mot est envoyé ce nombre de fois de suite avant la mesure.",
        )
        self.words_note = ft.Text(size=12, color=MUTED, selectable=True)
        self.state_list = self._field(
            "Un état par ligne : mot ; TR ; nom",
            "000000000000 ; TX ; référence\n000000000001 ; TX ; bit 0\n000000000010 ; RX ; bit 1",
            width=520,
            multiline=True,
            min_lines=5,
            max_lines=10,
            tooltip="Le mot est obligatoire ; TR (TX, RX, 1 ou 0) et le nom sont facultatifs.",
        )
        self.tx_level = ft.Dropdown(
            label="Niveau de la broche TR",
            value="1",
            width=250,
            options=[ft.dropdown.Option(key, text) for key, text in TX_LEVELS.items()],
            on_change=self._words_changed,
            tooltip="Dans le fichier, TX et RX deviennent ce niveau (1 = 3,3 V, 0 = 0 V).",
        )
        self.load_states_button = ft.OutlinedButton(
            "Charger un fichier d'états…", icon=ft.Icons.FOLDER_OPEN, on_click=self._browse_states
        )
        self.states_picker = ft.FilePicker(on_result=self._states_file_chosen)
        if hasattr(self.page, "overlay"):
            self.page.overlay.append(self.states_picker)

        self.probe_kind = ft.Dropdown(
            label="Après chaque mot",
            value="manual",
            width=330,
            options=[ft.dropdown.Option(key, text) for key, text in PROBE_KINDS.items()],
            on_change=self._mode_changed,
        )
        channels = [ft.dropdown.Option("1", "CH1"), ft.dropdown.Option("2", "CH2")]
        quantities = [
            ft.dropdown.Option(key, f"{name} ({unit})")
            for key, (name, unit) in SCOPE_QUANTITIES.items()
        ]
        self.scope_channel = {
            slot: ft.Dropdown(label=f"Voie {slot}", value="1", width=100, options=channels)
            for slot in ("A", "B")
        }
        self.scope_quantity = {
            "A": ft.Dropdown(label="Mesure A", value="frequency", width=240, options=quantities),
            "B": ft.Dropdown(
                label="Mesure B (facultative)",
                value="",
                width=240,
                options=[ft.dropdown.Option("", "—"), *quantities],
            ),
        }
        self.scope_note = ft.Text(
            "Une nouvelle acquisition est lancée après chaque mot. Connecter l'oscilloscope "
            "et régler ses voies dans l'onglet Oscilloscope.",
            size=12,
            color=MUTED,
        )

        self.instrument_kind = ft.Dropdown(
            label="Liaison",
            value="demo",
            width=220,
            options=[
                ft.dropdown.Option("demo", "Simulation (démo)"),
                ft.dropdown.Option("lan", "Keysight · réseau LAN"),
                ft.dropdown.Option("visa", "USB / VISA"),
            ],
            on_change=self._instrument_kind_changed,
        )
        self.instrument_address = ft.TextField(
            label="Adresse", width=360, dense=True, border_color=LINE, disabled=True
        )
        self.instrument_button = ft.ElevatedButton(
            "Connecter", icon=ft.Icons.LINK, on_click=self._toggle_instrument
        )
        self.instrument_status = ft.Text("Instrument non connecté", size=12, color=MUTED)
        self.preset = ft.Dropdown(
            label="Préréglage de commandes",
            value="0",
            width=420,
            options=[ft.dropdown.Option(str(i), preset.name) for i, preset in enumerate(PRESETS)],
            on_change=self._preset_chosen,
        )
        self.preset_note = ft.Text(size=12, color=MUTED)
        commands: dict[str, Any] = {"multiline": True, "min_lines": 2, "max_lines": 5, "width": 420}
        self.instrument_setup = self._field("Réglages, une fois avant le balayage", **commands)
        self.instrument_trigger = self._field("Déclencher après chaque mot", **commands)
        self.instrument_reads = self._field("Lire (requêtes « ? »)", **commands)
        self.instrument_labels = self._field(
            "Noms des colonnes (séparés par des virgules)", width=420
        )
        self.instrument_timeout = self._field("Délai max. (s)", "10", width=140)
        self.detect_button = ft.OutlinedButton(
            "Détecter le VNA", icon=ft.Icons.SEARCH, on_click=self._detect
        )
        self.scan_button = ft.TextButton(
            "Chercher sur le réseau",
            icon=ft.Icons.LAN,
            on_click=self._scan,
            tooltip="Essaie le port SCPI 5025 des 254 adresses du réseau privé de ce PC.",
        )
        self.detected = ft.Dropdown(
            label="Instruments trouvés",
            width=560,
            options=[],
            visible=False,
            on_change=self._detected_chosen,
        )
        self.vna_channel = self._field(
            "Canal du VNA",
            "1",
            width=140,
            tooltip="Numéro du canal (Trace/Chan) dont la calibration et la plage sont utilisées.",
        )
        self.vna_ports = ft.Dropdown(
            label="Nombre de ports",
            value="2",
            width=170,
            options=[ft.dropdown.Option(str(count), f"{count} port(s)") for count in range(1, 5)],
            on_change=self._words_changed,
        )
        self.vna_track = self._field(
            "Paramètre suivi",
            "S21",
            width=150,
            tooltip="Module (dB) et phase de ce paramètre S : critère, statistiques, graphique.",
        )
        self.vna_freq = self._field(
            "Fréquence suivie",
            "",
            width=180,
            hint_text="milieu de bande",
            tooltip="Le point de mesure le plus proche est utilisé. Exemples : 5G, 2.4e9.",
        )
        self.vna_folder_input = self._field(
            "Dossier des fichiers .sNp",
            "",
            width=440,
            hint_text="exports/vna-AAAAMMJJ-HHMMSS (un nouveau par campagne)",
            tooltip="Reprendre une campagne : indiquer son dossier et cocher « ne pas remesurer ».",
        )
        self.vna_skip = ft.Switch(
            label="Ne pas remesurer les états déjà enregistrés (reprise)", value=False
        )
        self.vna_timeout = self._field(
            "Délai max. par balayage (s)",
            "60",
            width=230,
            tooltip="Doit dépasser la durée d'un balayage (avec moyennage) du VNA.",
        )
        self.vna_note = ft.Text(size=12, color=MUTED, selectable=True)

        self.limit_low = self._field(
            "Valeur 1 ≥", "", width=130, tooltip="Critère facultatif : en dessous, le mot échoue."
        )
        self.limit_high = self._field(
            "Valeur 1 ≤", "", width=130, tooltip="Critère facultatif : au-dessus, le mot échoue."
        )
        self.stop_on_fail = ft.Switch(label="S'arrêter au premier échec", value=False)
        self.settle = self._field(
            "Attente après l'envoi (ms)",
            "20",
            width=200,
            tooltip="Laisse le composant se stabiliser avant la mesure.",
        )

        self.start_button = ft.ElevatedButton(
            "Démarrer", icon=ft.Icons.PLAY_ARROW, on_click=self._start
        )
        self.continue_button = ft.OutlinedButton(
            "Reprendre", icon=ft.Icons.SKIP_NEXT, on_click=self._continue
        )
        self.pause_button = ft.OutlinedButton("Pause", icon=ft.Icons.PAUSE, on_click=self._pause)
        self.stop_button = ft.OutlinedButton("Arrêter", icon=ft.Icons.STOP, on_click=self._stop)
        self.skip_button = ft.TextButton("Sauter ce mot", icon=ft.Icons.REDO, on_click=self._skip)
        self.status = ft.Text("Prêt", size=13, color=MUTED)
        self.progress = ft.ProgressBar(value=0, visible=False)
        self.current_word = ft.Text("", size=20, weight=ft.FontWeight.W_700, color=AMBER)
        self.eta = ft.Text("", size=12, color=MUTED)

        self.manual_value = ft.TextField(
            label="Valeur relevée (facultatif)", width=220, dense=True, border_color=LINE
        )
        self.manual_note = ft.TextField(
            label="Remarque (facultatif)", width=320, dense=True, border_color=LINE
        )
        self.manual_buttons = [
            ft.ElevatedButton(
                "Valider",
                icon=ft.Icons.CHECK,
                data="ok",
                on_click=self._decide,
                style=ft.ButtonStyle(bgcolor=GREEN, color="#0C1423"),
            ),
            ft.OutlinedButton("Rejeter", icon=ft.Icons.CLOSE, data="fail", on_click=self._decide),
            ft.OutlinedButton(
                "Renvoyer le mot", icon=ft.Icons.REPLAY, data="retry", on_click=self._decide
            ),
            ft.TextButton("Sauter", icon=ft.Icons.REDO, data="skip", on_click=self._decide),
        ]
        self.manual_title = ft.Text("", size=14, weight=ft.FontWeight.W_600)
        self.manual_card = ft.Container(
            ft.Column(
                [
                    self.manual_title,
                    ft.Text(
                        "Le mot est envoyé. Relever la mesure sur l'instrument, puis décider.",
                        size=12,
                        color=MUTED,
                    ),
                    ft.Row([self.manual_value, self.manual_note], wrap=True, spacing=10),
                    ft.Row(self.manual_buttons, wrap=True, spacing=10),
                ],
                spacing=8,
            ),
            padding=14,
            bgcolor=PANEL,
            border=ft.border.all(1.5, AMBER),
            border_radius=16,
            visible=False,
        )

        self.results_list = ft.ListView(height=260, spacing=1, auto_scroll=True)
        self.stats = ft.Text(size=12, color=TEXT, selectable=True)
        self.chart = cv.Canvas(
            shapes=[], height=CHART_HEIGHT, on_resize=self._resized, resize_interval=100
        )
        self.export_button = ft.OutlinedButton(
            "Exporter CSV", icon=ft.Icons.DOWNLOAD, on_click=self._export_csv
        )
        self.clear_button = ft.TextButton(
            "Effacer les résultats", icon=ft.Icons.DELETE_SWEEP, on_click=self._clear
        )
        self.export_note = ft.Text(size=12, color=MUTED, selectable=True)
        self._preset_chosen()
        self.counter_row = ft.Row(
            [self.counter_start, self.counter_stop, self.counter_step], wrap=True, spacing=10
        )
        self.scope_row = ft.Column(
            [
                ft.Row(
                    [
                        self.scope_channel["A"],
                        self.scope_quantity["A"],
                        self.scope_channel["B"],
                        self.scope_quantity["B"],
                    ],
                    wrap=True,
                    spacing=10,
                ),
                self.scope_note,
            ],
            spacing=6,
        )
        self.states_box = ft.Column(
            [
                ft.Row([self.load_states_button, self.tx_level], wrap=True, spacing=10),
                self.state_list,
            ],
            spacing=8,
        )
        self.connection_box = ft.Column(
            [
                ft.Row(
                    [self.instrument_kind, self.instrument_address, self.instrument_button],
                    wrap=True,
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row([self.detect_button, self.scan_button], wrap=True, spacing=10),
                self.detected,
                self.instrument_status,
            ],
            spacing=8,
        )
        self.vna_box = ft.Column(
            [
                ft.Row(
                    [self.vna_channel, self.vna_ports, self.vna_track, self.vna_freq],
                    wrap=True,
                    spacing=10,
                ),
                self.vna_folder_input,
                ft.Row(
                    [self.vna_timeout, self.vna_skip],
                    wrap=True,
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.vna_note,
            ],
            spacing=8,
        )
        self.instrument_box = ft.Column(
            [
                self.preset,
                self.preset_note,
                self.instrument_setup,
                self.instrument_trigger,
                self.instrument_reads,
                ft.Row([self.instrument_labels, self.instrument_timeout], wrap=True, spacing=10),
            ],
            spacing=8,
        )
        self.manual_note_box = ft.Text(
            "Après chaque mot, la mesure se fait sur votre instrument (VNA, oscilloscope, "
            "multimètre…) : relever la valeur puis Valider, Rejeter, Sauter ou Renvoyer le mot.",
            size=12,
            color=MUTED,
        )

    # -- disposition --------------------------------------------------------------------
    @staticmethod
    def _card(*controls: Any) -> ft.Container:
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
    def _title(text: str, note: str | None = None) -> ft.Control:
        controls = [ft.Text(text, size=16, weight=ft.FontWeight.W_600)]
        if note:
            controls.append(ft.Text(note, size=12, color=MUTED))
        return ft.Column(controls, spacing=4)

    def build(self) -> ft.Control:
        words = self._card(
            self._title(
                "1 · Mots à envoyer",
                "Chaque mot est envoyé avec les réglages du Pilotage (fréquence, ordre des "
                "bits, LATCH) ; seule la valeur change.",
            ),
            ft.Row([self.word_mode, self.word_base, self.width_field, self.repeats], wrap=True),
            self.word_list,
            self.states_box,
            self.counter_row,
            self.words_note,
        )
        probe = self._card(
            self._title("2 · Validation de chaque mot"),
            ft.Row([self.probe_kind, self.settle], wrap=True, spacing=10),
            self.manual_note_box,
            self.scope_row,
            self.connection_box,
            self.vna_box,
            self.instrument_box,
            ft.Row(
                [self.limit_low, self.limit_high, self.stop_on_fail],
                wrap=True,
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        controls = self._card(
            self._title("3 · Balayage"),
            ft.Row(
                [
                    self.start_button,
                    self.continue_button,
                    self.pause_button,
                    self.stop_button,
                    self.skip_button,
                ],
                wrap=True,
                spacing=10,
            ),
            self.progress,
            ft.Row([self.status, self.eta], wrap=True, spacing=16),
            self.current_word,
        )
        results = self._card(
            self._title("Résultats", "Les 300 derniers pas ; le CSV contient tous les pas."),
            ft.Container(
                self.chart,
                height=CHART_HEIGHT,
                border_radius=12,
                border=ft.border.all(1, LINE),
                bgcolor="#05080F",
            ),
            self.stats,
            ft.Container(
                self.results_list,
                height=260,
                border_radius=12,
                border=ft.border.all(1, LINE),
                padding=8,
            ),
            ft.Row([self.export_button, self.clear_button], wrap=True),
            self.export_note,
        )
        return ft.Column(
            [
                ft.ResponsiveRow(
                    [
                        ft.Container(
                            ft.Column([words, probe], spacing=14),
                            col={"xs": 12, "lg": 6},
                        ),
                        ft.Container(
                            ft.Column([controls, self.manual_card, results], spacing=14),
                            col={"xs": 12, "lg": 6},
                        ),
                    ],
                    spacing=14,
                    run_spacing=14,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                )
            ],
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            spacing=14,
        )

    # -- préparation des mots ------------------------------------------------------------
    def _update(self) -> None:
        if not self.closing:
            self.page.update()

    def _pilotage_width(self) -> int:
        frame = self._frame_source()
        return frame.bit_count if frame is not None else 24

    def _width(self) -> int:
        text = (self.width_field.value or "").strip()
        if text:
            try:
                width = int(text)
            except ValueError as exc:
                raise ValueError("La largeur doit être un entier de bits.") from exc
            if not 1 <= width <= MAX_BITS:
                raise ValueError(f"Largeur entre 1 et {MAX_BITS} bits.")
            return width
        if self.word_mode.value == "list":
            inferred = infer_width(self.word_list.value or "", self.word_base.value or "bin")
            if inferred is not None:
                return inferred
        if self.word_mode.value == "states":
            inferred = infer_states_width(
                self.state_list.value or "", self.word_base.value or "bin"
            )
            if inferred is not None:
                return inferred
        return self._pilotage_width()

    def _build_words(self) -> tuple[list[int], int]:
        width = self._width()
        mode = self.word_mode.value
        base = self.word_base.value or "bin"
        if mode == "list":
            return parse_words(self.word_list.value or "", width, base), width
        if mode == "counter":
            try:
                step = int((self.counter_step.value or "1").strip())
            except ValueError as exc:
                raise ValueError("Le pas doit être un entier décimal.") from exc
            start = parse_words(self.counter_start.value or "", width, base)
            stop = parse_words(self.counter_stop.value or "", width, base)
            if len(start) != 1 or len(stop) != 1:
                raise ValueError("Début et fin : un seul mot chacun.")
            return counter_words(start[0], stop[0], step, width), width
        return walking_words(width, ones=mode == "walk1"), width

    def _tx_level(self) -> int:
        return int(self.tx_level.value or "1")

    def _build_states(self) -> tuple[list[SweepState], int]:
        """États à envoyer : ceux du fichier, ou les mots des autres modes sans TR."""
        if self.word_mode.value == "states":
            width = self._width()
            states = parse_states(
                self.state_list.value or "",
                width,
                self.word_base.value or "bin",
                tx_level=self._tx_level(),
            )
            return states, width
        words, width = self._build_words()
        return [SweepState(word) for word in words], width

    def _preview(self) -> None:
        try:
            states, width = self._build_states()
            if self.probe_kind.value == "vna" and len(states) > MAX_STATES:
                raise ValueError(f"Mode VNA : {MAX_STATES} états au maximum ({len(states)}).")
        except ValueError as exc:
            self._states = []
            self.words_note.value = str(exc)
            self.words_note.color = RED
            return
        self._states = states
        if self.word_mode.value == "states":
            note = describe_states(states, width, self._tx_level())
        else:
            note = describe_words([state.word for state in states], width)
        if width != self._pilotage_width():
            note += f" · largeur imposée (le Pilotage est sur {self._pilotage_width()} bits)"
        if len(states) > 5000:
            note += f" · {len(states)} mots : prévoir plusieurs heures avec une validation manuelle"
        self.words_note.value = note
        self.words_note.color = MUTED
        if (
            any(state.tr is not None for state in states)
            and self.bench.device_ready()
            and not self.bench.tr_supported()
        ):
            self.words_note.value += (
                " · ⚠ le firmware chargé n'a pas de broche TR (révision 5 requise, onglet FPGA)"
            )
            self.words_note.color = AMBER

    def _words_changed(self, _: Any = None) -> None:
        self._preview()
        self._update()

    def _mode_changed(self, _: Any = None) -> None:
        mode = self.word_mode.value
        self.word_list.visible = mode == "list"
        self.states_box.visible = mode == "states"
        self.counter_row.visible = mode == "counter"
        kind = self.probe_kind.value
        self.scope_row.visible = kind == "scope"
        self.connection_box.visible = kind in ("instrument", "vna")
        self.vna_box.visible = kind == "vna"
        self.instrument_box.visible = kind == "instrument"
        self.manual_note_box.visible = kind == "manual"
        if kind == "vna" and not self.vna_note.value:
            self.vna_note.value = (
                "Un balayage du VNA par état : la matrice S de tous les ports est enregistrée "
                "dans un fichier Touchstone par état, avec le paramètre suivi dans le tableau."
            )
        self._preview()
        self._sync()
        if _ is not None:
            self._update()

    # -- instrument SCPI --------------------------------------------------------------------
    def _preset_chosen(self, _: Any = None) -> None:
        try:
            preset: InstrumentPreset = PRESETS[int(self.preset.value or "0")]
        except (ValueError, IndexError):
            preset = PRESETS[0]
        self.preset_note.value = preset.note
        # Au démarrage (``_`` vide) seule la note s'affiche ; choisir un préréglage
        # dans la liste remplace les champs de commandes.
        if _ is not None:
            self.instrument_setup.value = preset.setup
            self.instrument_trigger.value = preset.trigger
            self.instrument_reads.value = preset.reads
            self.instrument_labels.value = preset.labels
            self._update()

    def _instrument_kind_changed(self, _: Any = None) -> None:
        self.instrument_address.disabled = self.instrument_kind.value == "demo"
        hint = {
            "lan": "192.168.1.60",
            "visa": "USB0::0x2A8D::…::INSTR ou TCPIP0::…::INSTR",
        }.get(self.instrument_kind.value or "", "")
        self.instrument_address.hint_text = hint
        self._update()

    # -- détection du VNA ------------------------------------------------------------------------
    async def _detect(self, _: Any = None) -> None:
        await self._discover(scan=False)

    async def _scan(self, _: Any = None) -> None:
        await self._discover(scan=True)

    async def _discover(self, *, scan: bool) -> None:
        if self.detect_pending or self.running or self.instrument_pending:
            return
        if self.instrument is not None:
            return
        self.detect_pending = True
        self.instrument_status.value = (
            "Recherche sur le réseau (quelques secondes)…"
            if scan
            else "Recherche des instruments (VISA, ce PC)…"
        )
        self.instrument_status.color = AMBER
        self._sync()
        self._update()
        address = (self.instrument_address.value or "").strip()
        hosts = [address] if address and self.instrument_kind.value == "lan" else []
        try:
            found = await asyncio.to_thread(discover_instruments, scan_network=scan, hosts=hosts)
        except Exception as exc:  # une recherche ne doit jamais bloquer l'interface
            found = []
            self._log(f"Détection : {exc}", RED)
        finally:
            self.detect_pending = False
        self.found = found
        self._show_found(scan)
        self._sync()
        self._update()

    def _show_found(self, scanned: bool) -> None:
        self.detected.options = [
            ft.dropdown.Option(str(index), item.label) for index, item in enumerate(self.found)
        ]
        self.detected.visible = len(self.found) > 1
        if not self.found:
            self.detected.value = None
            self.instrument_status.value = (
                "Aucun instrument trouvé. VNA USB (P9374A) : lancer l'application du VNA et "
                "installer Keysight IO Libraries. VNA en LAN : saisir son adresse IP "
                + ("(lue sur l'appareil)." if scanned else "ou « Chercher sur le réseau ».")
            )
            self.instrument_status.color = AMBER
            return
        self._select_found(0)
        self.detected.value = "0"
        count = len(self.found)
        self.instrument_status.value = (
            f"{count} instrument(s) trouvé(s) : {self.found[0].label} — cliquer sur Connecter."
        )
        self.instrument_status.color = GREEN
        self._log(f"Détection : {count} instrument(s) trouvé(s).", BLUE)

    def _select_found(self, index: int) -> None:
        if not 0 <= index < len(self.found):
            return
        item = self.found[index]
        self.instrument_kind.value = item.kind
        self.instrument_address.value = item.address
        self.instrument_address.disabled = False

    def _detected_chosen(self, _: Any = None) -> None:
        try:
            self._select_found(int(self.detected.value or "0"))
        except ValueError:
            return
        self._update()

    # -- fichier d'états ---------------------------------------------------------------------------
    def _browse_states(self, _: Any = None) -> None:
        self.states_picker.pick_files(
            dialog_title="Choisir un fichier d'états",
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["csv", "txt"],
            allow_multiple=False,
        )

    def _states_file_chosen(self, event: Any) -> None:
        if event.files and event.files[0].path:
            self.load_states_file(Path(event.files[0].path))

    def load_states_file(self, path: Path) -> None:
        """Charge un fichier d'états dans la liste et passe en mode « Liste d'états »."""
        try:
            text = load_states_text(path)
        except (OSError, ValueError) as exc:
            self.words_note.value = f"Fichier d'états illisible : {exc}"
            self.words_note.color = RED
            self._update()
            return
        self.state_list.value = text
        self.word_mode.value = "states"
        self._mode_changed()
        self.words_note.value = f"{path.name} · {self.words_note.value}"
        self._log(f"Fichier d'états chargé : {path}", BLUE)
        self._update()

    async def _toggle_instrument(self, _: Any = None) -> None:
        if self.instrument_pending or self.running:
            return
        if self.instrument is not None:
            instrument, self.instrument, self.simulated = self.instrument, None, None
            await asyncio.to_thread(instrument.close)
            self.instrument_status.value = "Instrument non connecté"
            self.instrument_status.color = MUTED
            self._sync()
            self._update()
            return
        self.instrument_pending = True
        self.instrument_status.value = "Connexion…"
        self._sync()
        self._update()
        try:
            is_vna = self.probe_kind.value == "vna"
            timeout = self._number(
                (self.vna_timeout if is_vna else self.instrument_timeout).value,
                "Délai",
                default=60.0 if is_vna else 10.0,
            )
            kind = self.instrument_kind.value or "demo"
            address = (self.instrument_address.value or "").strip()
            simulated: SimulatedVna | SimulatedPna | None = None
            if kind == "demo":
                simulated = SimulatedPna() if is_vna else SimulatedVna()

            def open_and_identify() -> ScpiInstrument:
                instrument = ScpiInstrument(
                    open_transport(kind, address, timeout=timeout, simulated=simulated)
                )
                try:
                    instrument.identify()
                except Exception:
                    instrument.close()
                    raise
                return instrument

            self.instrument = await asyncio.to_thread(open_and_identify)
            self.simulated = simulated
            self.instrument_status.value = f"Connecté · {self.instrument.identity}"
            self.instrument_status.color = GREEN
            self._log(f"Instrument connecté : {self.instrument.identity}", GREEN)
        except (ScopeError, ValueError, OSError) as exc:
            self.instrument_status.value = f"Connexion impossible : {exc}"
            self.instrument_status.color = RED
            self._log(f"Instrument : {exc}", RED)
        finally:
            self.instrument_pending = False
            self._sync()
            self._update()

    @staticmethod
    def _number(text: str | None, name: str, *, default: float | None = None) -> float:
        if not (text or "").strip():
            if default is None:
                raise ValueError(f"{name} : valeur requise.")
            return default
        try:
            return parse_si(text or "")
        except ValueError as exc:
            raise ValueError(f"{name} : {exc}") from exc

    # -- sonde et paramètres du balayage ------------------------------------------------------
    def _limit(self) -> Limit:
        low = (self.limit_low.value or "").strip()
        high = (self.limit_high.value or "").strip()
        return Limit(
            self._number(low, "Borne basse") if low else None,
            self._number(high, "Borne haute") if high else None,
        )

    @staticmethod
    def _integer(text: str | None, name: str, low: int, high: int) -> int:
        try:
            value = int((text or "").strip())
        except ValueError as exc:
            raise ValueError(f"{name} : entier de {low} à {high} attendu.") from exc
        if not low <= value <= high:
            raise ValueError(f"{name} : entier de {low} à {high} attendu.")
        return value

    def _tracked_parameter(self, ports: int) -> tuple[int, int]:
        match = _S_PARAMETER.fullmatch((self.vna_track.value or "").strip())
        if match is None:
            raise ValueError("Paramètre suivi : S11, S21, S12… (la lettre S et deux chiffres).")
        i, j = int(match.group(1)), int(match.group(2))
        if max(i, j) > ports:
            raise ValueError(f"Paramètre suivi S{i}{j} : le VNA est réglé sur {ports} port(s).")
        return i, j

    def _campaign_folder(self, *, resume: bool) -> Path:
        if resume and self.vna_folder is not None:
            return self.vna_folder
        text = (self.vna_folder_input.value or "").strip()
        folder = Path(text) if text else Path("exports") / f"vna-{datetime.now():%Y%m%d-%H%M%S}"
        return folder if folder.is_absolute() else self.project_root / folder

    async def _drop_instrument(self) -> None:
        instrument, self.instrument, self.simulated = self.instrument, None, None
        self.instrument_status.value = "Instrument non connecté"
        self.instrument_status.color = MUTED
        if instrument is not None:
            await asyncio.to_thread(instrument.close)

    async def _make_vna_probe(self, states: Sequence[SweepState], *, resume: bool) -> VnaProbe:
        if self.instrument is None:
            raise ValueError("VNA non connecté : « Détecter le VNA » puis Connecter.")
        if isinstance(self.simulated, SimulatedVna):
            raise ValueError(
                "La simulation connectée est celle du mode SCPI : la déconnecter puis la "
                "reconnecter avec « VNA Keysight » choisi."
            )
        channel = self._integer(self.vna_channel.value, "Canal du VNA", 1, 200)
        ports = int(self.vna_ports.value or "2")
        track = self._tracked_parameter(ports)
        frequency_text = (self.vna_freq.value or "").strip()
        frequency = self._number(frequency_text, "Fréquence suivie") if frequency_text else None
        folder = self._campaign_folder(resume=resume)
        driver = PnaDriver(self.instrument, channel, ports)
        probe = VnaProbe(
            driver,
            states,
            folder,
            track=track,
            track_frequency=frequency,
            skip_existing=bool(self.vna_skip.value),
            on_word=self.simulated.set_word if self.simulated is not None else None,
        )
        try:
            await probe.prepare()
        except ScopeConnectionError as exc:
            await self._drop_instrument()
            raise ValueError(f"Liaison avec le VNA perdue ({exc}) : le reconnecter.") from exc
        self.vna_folder = folder
        self.vna_probe = probe
        self.vna_note.value = f"{driver.describe()} · fichiers dans {folder}"
        self.vna_note.color = MUTED
        return probe

    async def _make_probe(
        self, states: Sequence[SweepState], *, resume: bool = False
    ) -> MeasureFunction | None:
        kind = self.probe_kind.value
        if kind == "none":
            return None
        if kind == "vna":
            return await self._make_vna_probe(states, resume=resume)
        if kind == "manual":
            return self.manual
        if kind == "scope":
            if not self.bench.scope_ready():
                raise ValueError(
                    "Oscilloscope non connecté : le connecter dans l'onglet Oscilloscope."
                )
            specs = [
                ScopeSpec(int(self.scope_channel["A"].value or "1"), self.scope_quantity["A"].value)
            ]
            if self.scope_quantity["B"].value:
                specs.append(
                    ScopeSpec(
                        int(self.scope_channel["B"].value or "1"), self.scope_quantity["B"].value
                    )
                )
            return ScopeProbe(self.bench.acquire_scope, specs)
        if self.instrument is None:
            raise ValueError("Instrument non connecté : cliquer sur Connecter dans la section 2.")
        if isinstance(self.simulated, SimulatedPna):
            raise ValueError(
                "La simulation connectée est celle du mode VNA : la déconnecter puis la "
                "reconnecter avec « Instrument SCPI » choisi."
            )
        reads = commands_from_text(self.instrument_reads.value or "")
        trigger = commands_from_text(self.instrument_trigger.value or "")
        setup = commands_from_text(self.instrument_setup.value or "")
        labels = [
            label.strip()
            for label in (self.instrument_labels.value or "").split(",")
            if label.strip()
        ]
        probe = InstrumentProbe(
            self.instrument,
            trigger,
            reads,
            labels,
            on_word=self.simulated.set_word if self.simulated is not None else None,
        )
        if setup:
            instrument = self.instrument

            def apply_setup() -> None:
                instrument.run(setup)
                instrument.check("Réglages")

            await asyncio.to_thread(apply_setup)
        return probe

    def _plan(self) -> tuple[FrameConfig, list[SweepState]]:
        frame = self._frame_source()
        if frame is None:
            raise ValueError("Corriger d'abord les paramètres de la trame dans Pilotage.")
        states, width = self._build_states()
        if self.probe_kind.value == "vna" and len(states) > MAX_STATES:
            raise ValueError(f"Mode VNA : {MAX_STATES} états au maximum ({len(states)}).")
        if any(state.tr is not None for state in states) and not self.bench.tr_supported():
            raise ValueError(
                "Des états fixent la broche TR, mais le firmware chargé n'en a pas : charger "
                "le firmware de révision 5 ou plus (onglet FPGA), ou retirer la colonne TR."
            )
        try:
            repeats = int((self.repeats.value or "1").strip())
        except ValueError as exc:
            raise ValueError("Répétitions : entier de 1 à 65535.") from exc
        if not 1 <= repeats <= 65535:
            raise ValueError("Répétitions : entier de 1 à 65535.")
        # L'émission continue et le nombre de répétitions du Pilotage ne s'appliquent
        # pas : chaque mot est une trame finie, dont la fin est attendue.
        return replace(frame, bit_count=width, word=0, repeat_count=repeats), states

    # -- exécution -----------------------------------------------------------------------------
    def _sync(self) -> None:
        ready = self.bench.device_ready()
        running = self.running
        resumable = (
            not running
            and self.runner is not None
            and self.summary is not None
            and self.summary.next_index < len(self.runner.words)
            and self.summary.reason in ("stopped", "error", "fail")
        )
        self.start_button.disabled = running or not ready
        self.continue_button.visible = resumable
        self.continue_button.disabled = not ready
        self.pause_button.disabled = not running
        paused = self.runner is not None and self.runner.state == "paused"
        self.pause_button.text = "Continuer" if paused else "Pause"
        self.pause_button.icon = ft.Icons.PLAY_ARROW if paused else ft.Icons.PAUSE
        self.stop_button.disabled = not running
        self.skip_button.disabled = not running
        self.progress.visible = running or (self.summary is not None and bool(self.results))
        for control in (
            self.word_mode,
            self.word_base,
            self.word_list,
            self.state_list,
            self.tx_level,
            self.load_states_button,
            self.vna_channel,
            self.vna_ports,
            self.vna_track,
            self.vna_freq,
            self.vna_folder_input,
            self.vna_skip,
            self.vna_timeout,
            self.counter_start,
            self.counter_stop,
            self.counter_step,
            self.width_field,
            self.repeats,
            self.probe_kind,
            self.settle,
            self.limit_low,
            self.limit_high,
            self.stop_on_fail,
            *self.scope_channel.values(),
            *self.scope_quantity.values(),
            self.preset,
            self.instrument_setup,
            self.instrument_trigger,
            self.instrument_reads,
            self.instrument_labels,
            self.instrument_timeout,
        ):
            control.disabled = running
        connected = self.instrument is not None
        self.instrument_button.text = "Déconnecter" if connected else "Connecter"
        self.instrument_button.icon = ft.Icons.LINK_OFF if connected else ft.Icons.LINK
        self.instrument_button.disabled = running or self.instrument_pending or self.detect_pending
        searching = running or connected or self.instrument_pending or self.detect_pending
        self.detect_button.disabled = searching
        self.scan_button.disabled = searching
        self.detected.disabled = searching
        self.instrument_kind.disabled = (
            running or connected or self.instrument_pending or self.detect_pending
        )
        self.instrument_address.disabled = (
            running or connected or self.instrument_kind.value == "demo" or self.instrument_pending
        )
        self.export_button.disabled = not self.results
        self.clear_button.disabled = running or not self.results
        if not ready and not running and self.summary is None:
            self.status.value = "Connecter la carte (ou la démo) dans Pilotage pour démarrer."
            self.status.color = AMBER

    async def _start(self, _: Any = None) -> None:
        if self.running or self.closing:
            return
        try:
            base, states = self._plan()
            self.limit = self._limit()
            settle = self._number(self.settle.value, "Attente", default=0.0) / 1000
            self.vna_folder = None
            probe = await self._make_probe(states)
        except (ValueError, ScopeError, OSError) as exc:
            self.status.value = str(exc)
            self.status.color = RED
            self._sync()
            self._update()
            return
        self.results_list.controls = []
        self.summary = None
        self._states = states
        self.runner = SweepRunner(
            base,
            states,
            send=self.bench.send_word,
            before_word=self._before_word,
            measure=probe,
            settle=settle,
            limit=self.limit,
            stop_on_fail=bool(self.stop_on_fail.value),
            on_result=self._on_result,
            on_step=self._on_step,
        )
        self.results = self.runner.results  # la même liste : mise à jour à chaque pas
        self._save_preferences()
        self._log(
            f"Mesure : {describe_states(states, base.bit_count, self._tx_level())} · "
            f"{PROBE_KINDS[self.probe_kind.value or 'none']}.",
            BLUE,
        )
        self._launch(0)

    async def _before_word(self, state: SweepState) -> None:
        if state.tr is not None:
            await self.bench.set_tr(state.tr)

    async def _continue(self, _: Any = None) -> None:
        if self.running or self.runner is None or self.summary is None:
            return
        # Le matériel peut avoir changé depuis l'arrêt : la sonde est recréée.
        try:
            self.runner.measure = await self._make_probe(self.runner.states, resume=True)
            self.runner.limit = self._limit()
        except (ValueError, ScopeError, OSError) as exc:
            self.status.value = str(exc)
            self.status.color = RED
            self._update()
            return
        self._launch(self.summary.next_index)

    def _launch(self, start: int) -> None:
        assert self.runner is not None
        self.running = True
        self.bench.set_busy(True)
        self.status.value = "Balayage en cours…"
        self.status.color = AMBER
        self._sync()
        self._update()
        self.task = asyncio.create_task(self._execute(self.runner, start))

    async def _execute(self, runner: SweepRunner, start: int) -> None:
        try:
            summary = await runner.run(start)
        except Exception as exc:  # défaut inattendu : jamais silencieux
            self._log(f"Mesure : erreur interne : {exc}", RED)
            summary = SweepSummary(
                len(runner.words), {}, 0.0, "error", start, f"Erreur interne : {exc}"
            )
        await self._finish_vna(runner)
        self.summary = summary
        self.running = False
        self.bench.set_busy(False)
        self.manual_card.visible = False
        counts = summary.counts
        detail = (
            f"{counts.get(OK, 0)} OK · {counts.get(FAIL, 0)} échec(s) · "
            f"{counts.get(SKIPPED, 0)} sauté(s)"
        )
        if summary.reason == "finished":
            self.status.value = f"Terminé · {detail} · {format_duration(summary.elapsed)}"
            self.status.color = GREEN if not counts.get(FAIL) else AMBER
            self._log(f"Mesure terminée · {detail}.", GREEN)
        elif summary.reason == "stopped":
            self.status.value = f"Arrêté au pas {summary.next_index + 1} · {detail}"
            self.status.color = MUTED
        else:
            self.status.value = summary.message or "Balayage interrompu"
            self.status.color = RED
            self._log(f"Mesure interrompue : {summary.message}", RED)
        self.current_word.value = ""
        self.progress.value = 1.0 if summary.reason == "finished" else self.progress.value
        self._render(force=True)
        self._sync()
        self._update()

    async def _finish_vna(self, runner: SweepRunner) -> None:
        """Rend le VNA dans son état d'origine et écrit le récapitulatif de la campagne."""
        probe, self.vna_probe = self.vna_probe, None
        if probe is None:
            return
        try:
            await probe.finish()
        except (ScopeError, OSError) as exc:
            self._log(f"VNA : état d'origine non rétabli ({exc}).", AMBER)
        if runner.results:
            try:
                path = write_results_csv(runner.results, probe.folder / "resultats.csv")
            except OSError as exc:
                self._log(f"Récapitulatif non écrit : {exc}", RED)
            else:
                self.export_note.value = f"Campagne enregistrée : {probe.folder} ({path.name})"
                self.export_note.color = GREEN
                self._log(self.export_note.value, GREEN)

    def _on_step(self, index: int, word: int) -> None:
        runner = self.runner
        width = runner.base.bit_count if runner is not None else 0
        total = len(runner.words) if runner is not None else 0
        extra = ""
        if runner is not None and index < len(runner.states):
            state = runner.states[index]
            extra = (f" · TR {state.tr}" if state.tr is not None else "") + (
                f" · {state.name}" if state.name else ""
            )
        self.current_word.value = f"{word_text(word, width)}  ({index + 1}/{total}){extra}"
        self.progress.value = index / total if total else 0
        if runner is not None:
            recent = [result.duration for result in self.results[-20:]]
            if recent and total:
                remaining = (total - index) * (sum(recent) / len(recent))
                self.eta.value = f"Reste environ {format_duration(remaining)}"
        self._render()

    def _on_result(self, result: StepResult) -> None:
        self._render()

    def _render(self, *, force: bool = False) -> None:
        """Liste, statistiques et graphique ; au plus dix fois par seconde pendant un balayage."""
        now = time.monotonic()
        if not force and now - self._last_render < 0.1:
            return
        self._last_render = now
        visible = self.results[-VISIBLE_RESULTS:]
        self.results_list.controls = [
            ft.Text(
                result_line(result),
                size=12,
                color=STATUS_COLORS.get(result.status, TEXT),
                selectable=True,
            )
            for result in visible
        ]
        stats = value_statistics(self.results)
        if stats:
            label = next((r.names[0] for r in self.results if r.names), "valeur 1")
            self.stats.value = (
                f"{label} · {int(stats['count'])} mesure(s) · min {stats['min']:.6g} · "
                f"max {stats['max']:.6g} · moyenne {stats['mean']:.6g}"
            )
        else:
            self.stats.value = ""
        label = next((r.names[0] for r in self.results if r.names), "valeur 1")
        self.chart.shapes = results_chart_shapes(
            self.results, self.width, limit=self.limit, label=label
        )
        self._sync()
        self._update()

    # -- boutons -----------------------------------------------------------------------------
    async def _pause(self, _: Any = None) -> None:
        runner = self.runner
        if runner is None or not self.running:
            return
        if runner.state == "paused":
            runner.resume()
            self.status.value = "Balayage en cours…"
        else:
            runner.pause()
            self.status.value = "Pause après ce mot…"
        self.status.color = AMBER
        self._sync()
        self._update()

    async def _stop(self, _: Any = None) -> None:
        if self.runner is not None and self.running:
            self.runner.stop()

    async def _skip(self, _: Any = None) -> None:
        if self.runner is not None and self.running:
            self.runner.skip()

    # -- validation manuelle ----------------------------------------------------------------
    def _manual_wait(self, config: FrameConfig, index: int) -> None:
        total = len(self.runner.words) if self.runner is not None else 0
        self.manual_title.value = (
            f"Mot {index + 1}/{total} envoyé : {word_text(config.word, config.bit_count)} "
            f"(0x{config.word:X})"
        )
        self.manual_value.value = ""
        self.manual_note.value = ""
        self.manual_card.visible = True
        self._update()

    def _manual_done(self) -> None:
        self.manual_card.visible = False

    async def _decide(self, event: Any) -> None:
        action = event.control.data
        value = None
        text = (self.manual_value.value or "").strip()
        if text:
            try:
                value = parse_si(text)
            except ValueError as exc:
                self.status.value = f"Valeur relevée : {exc}"
                self.status.color = RED
                self._update()
                return
        self.manual.decide(action, (self.manual_note.value or "").strip(), value)

    # -- export, effacement, préférences -----------------------------------------------------------
    def _export_csv(self, _: Any = None) -> None:
        if not self.results:
            return
        path = self.project_root / "exports" / f"mesure-{datetime.now():%Y%m%d-%H%M%S}.csv"
        try:
            write_results_csv(self.results, path)
        except OSError as exc:
            self.export_note.value = f"Export CSV impossible : {exc}"
            self.export_note.color = RED
            self._update()
            return
        self.export_note.value = f"Résultats exportés : {path}"
        self.export_note.color = GREEN
        self._log(self.export_note.value, GREEN)
        self._update()

    def _clear(self, _: Any = None) -> None:
        if self.running:
            return
        self.results = []
        self.runner = None
        self.summary = None
        self.progress.value = 0
        self.eta.value = ""
        self.status.value = "Prêt"
        self.status.color = MUTED
        self._render(force=True)

    def _resized(self, event: Any) -> None:
        width = float(getattr(event, "width", 0) or 0)
        if not math.isfinite(width) or width < 300 or abs(width - self.width) < 0.5:
            return
        self.width = width
        self._render(force=True)

    def _preference_controls(self) -> dict[str, Any]:
        return {
            "word_mode": self.word_mode,
            "word_base": self.word_base,
            "word_list": self.word_list,
            "state_list": self.state_list,
            "tx_level": self.tx_level,
            "vna_channel": self.vna_channel,
            "vna_ports": self.vna_ports,
            "vna_track": self.vna_track,
            "vna_freq": self.vna_freq,
            "vna_folder": self.vna_folder_input,
            "vna_timeout": self.vna_timeout,
            "counter_start": self.counter_start,
            "counter_stop": self.counter_stop,
            "counter_step": self.counter_step,
            "width": self.width_field,
            "repeats": self.repeats,
            "probe": self.probe_kind,
            "settle": self.settle,
            "limit_low": self.limit_low,
            "limit_high": self.limit_high,
            "scope_a_channel": self.scope_channel["A"],
            "scope_a_quantity": self.scope_quantity["A"],
            "scope_b_channel": self.scope_channel["B"],
            "scope_b_quantity": self.scope_quantity["B"],
            "instrument_kind": self.instrument_kind,
            "instrument_address": self.instrument_address,
            "instrument_setup": self.instrument_setup,
            "instrument_trigger": self.instrument_trigger,
            "instrument_reads": self.instrument_reads,
            "instrument_labels": self.instrument_labels,
            "instrument_timeout": self.instrument_timeout,
        }

    def _load_preferences(self) -> None:
        try:
            data = json.loads((self.project_root / PREFERENCES).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for name, control in self._preference_controls().items():
            value = data.get(name)
            if not isinstance(value, str):
                continue
            options = getattr(control, "options", None)
            if options and value not in [
                option.key if option.key is not None else option.text for option in options
            ]:
                continue  # option disparue : garder la valeur par défaut
            control.value = value
        if isinstance(data.get("stop_on_fail"), bool):
            self.stop_on_fail.value = data["stop_on_fail"]
        if isinstance(data.get("vna_skip"), bool):
            self.vna_skip.value = data["vna_skip"]
        self.instrument_address.disabled = self.instrument_kind.value == "demo"

    def _save_preferences(self) -> None:
        data: dict[str, Any] = {
            name: control.value or "" for name, control in self._preference_controls().items()
        }
        data["stop_on_fail"] = bool(self.stop_on_fail.value)
        data["vna_skip"] = bool(self.vna_skip.value)
        path = self.project_root / PREFERENCES
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError:
            pass  # simple confort : le balayage fonctionne sans

    async def shutdown(self) -> None:
        self.closing = True
        if self.runner is not None:
            self.runner.stop()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
        instrument, self.instrument = self.instrument, None
        if instrument is not None:
            await asyncio.to_thread(instrument.close)
