"""Responsive Flet views; hardware and transport actions stay in Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import flet as ft  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from .app import Studio

BG = "#0C1423"
PANEL = "#142136"
LINE = "#273A54"
TEXT = "#E7EDF5"
MUTED = "#A5B4C9"
AMBER = "#F4B759"
BLUE = "#75B9F4"
GREEN = "#79CFB0"
RED = "#FF8E91"
LED_ON = "#4ADE80"
LED_OFF = "#1E2B3F"


def _grid(*items: tuple[Any, int]) -> ft.Control:
    controls = []
    for control, columns in items:
        control.width = None
        control.expand = True
        controls.append(
            ft.Container(
                ft.Row([control], vertical_alignment=ft.CrossAxisAlignment.START),
                col={"xs": 12, "sm": columns},
            )
        )
    return ft.ResponsiveRow(controls, spacing=12, run_spacing=14)


def _section(title: str, subtitle: str, controls: list[Any]) -> ft.Control:
    return ft.ExpansionTile(
        title=ft.Text(title, weight=ft.FontWeight.W_600),
        subtitle=ft.Text(subtitle, size=12, color=MUTED),
        maintain_state=True,
        controls=[ft.Container(ft.Column(controls, spacing=14), padding=ft.padding.all(16))],
        collapsed_shape=ft.RoundedRectangleBorder(radius=12),
        shape=ft.RoundedRectangleBorder(radius=12),
        bgcolor=PANEL,
        collapsed_bgcolor=PANEL,
    )


def _path(studio: Studio, field: Any, extensions: list[str]) -> ft.Control:
    field.width = None
    field.expand = True
    return ft.Row(
        [
            field,
            ft.IconButton(
                ft.Icons.FOLDER_OPEN,
                tooltip="Parcourir (application de bureau)",
                disabled=bool(getattr(studio.page, "web", False)),
                on_click=lambda _: studio._browse_path(field, extensions),
            ),
        ],
        vertical_alignment=ft.CrossAxisAlignment.START,
    )


def build_layout(s: Studio) -> ft.Control:
    """Build the four existing views around prepare/connect/test/send."""
    s.setup_steps = ft.Container(
        ft.Row(
            [
                ft.Text("1  Charger le FPGA", size=12, color=BLUE),
                ft.Text("→", color=MUTED),
                ft.Text("2  Connecter le port", size=12, color=BLUE),
                ft.Text("→", color=MUTED),
                ft.Text("3  Tester les LED", size=12, color=BLUE),
                ft.Text("→", color=MUTED),
                ft.Text("4  Envoyer", size=12, color=BLUE),
            ],
            wrap=True,
            spacing=10,
        ),
        padding=ft.padding.symmetric(horizontal=16, vertical=10),
        bgcolor=PANEL,
        border_radius=12,
        visible=False,
    )
    connection = s._card(
        s._heading("Connexion", "Simulation locale ou carte Arty sur USB/UART"),
        ft.Row([s.mode, s.port, s.refresh_button, s.connect_button], wrap=True, spacing=12),
        ft.Row([s.connection_status, s.prepare_button], wrap=True, spacing=16),
        s.connection_hint,
        s.firmware_status,
        s.hardware_pinout,
        ft.Row(
            [
                s.led_test_button,
                *[
                    ft.Column(
                        [lamp, ft.Text(f"LD{index + 4}", size=10, color=MUTED)],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=4,
                    )
                    for index, lamp in enumerate(s.led_lamps)
                ],
                s.led_note,
            ],
            wrap=True,
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )
    s.lsb.width = 210
    s.latch_low.width = 210
    frame = s._card(
        s._heading("Trame série", "De 1 à 26 bits, transmis sur DATA"),
        _grid((s.word, 12), (s.base, 6), (s.bit_count, 6)),
        s.binary_preview,
        s.order_preview,
        ft.Row([s.lsb, s.latch_low], wrap=True, spacing=8),
        ft.Row(
            [
                ft.TextButton("Exemple SIPO · 10 MHz", on_click=s._sipo_example),
                ft.TextButton(
                    "Guide utilisateur", icon=ft.Icons.HELP_OUTLINE, on_click=s._open_guide
                ),
            ],
            wrap=True,
        ),
    )
    frame.col = {"xs": 12, "lg": 6}
    timing = s._card(
        s._heading("Horloge et séquence", "Lecture de DATA au front montant de CLK"),
        _grid((s.frequency, 7), (s.divider, 5)),
        s.frequency_actual,
        _grid((s.latch_ns, 6), (s.gap_ns, 6), (s.repeat, 6)),
        ft.Column([s.continuous, s.free_clock], spacing=4),
        s.emission_note,
        ft.TextButton("Exemple CLK seule · 10 MHz", on_click=s._clock_example),
        s.timing_summary,
        s.quantization_note,
        _section(
            "Base de temps",
            "Lue automatiquement sur une carte connectée",
            [
                _grid((s.core_clock, 12)),
                ft.Text(
                    "Ce choix règle la simulation. Changer le cœur de la carte exige de "
                    "compiler puis charger un firmware depuis l'onglet FPGA.",
                    size=12,
                    color=MUTED,
                ),
            ],
        ),
    )
    timing.col = {"xs": 12, "lg": 6}
    profiles = _section(
        "Profils de trame",
        "Charger ou enregistrer les paramètres d'une séquence",
        [
            _path(s, s.profile_path, ["json"]),
            ft.Row(
                [
                    ft.OutlinedButton(
                        "Charger le profil", icon=ft.Icons.FOLDER_OPEN, on_click=s._load_profile
                    ),
                    ft.OutlinedButton(
                        "Enregistrer le profil", icon=ft.Icons.SAVE, on_click=s._save_profile
                    ),
                ],
                wrap=True,
            ),
            s.profile_message,
        ],
    )
    # Actions stay outside the scrollable parameter area, including at 760×680.
    control_tab = ft.Column(
        [
            ft.Column(
                [
                    connection,
                    ft.ResponsiveRow([frame, timing], spacing=14, run_spacing=14),
                    profiles,
                ],
                expand=True,
                scroll=ft.ScrollMode.AUTO,
                spacing=14,
            ),
            ft.Container(
                ft.Column(
                    [
                        s.validation,
                        s.compatibility_note,
                        ft.Row(
                            [s.send_button, s.stop_button, s.simulate_button], wrap=True, spacing=12
                        ),
                        s.hardware_status,
                    ],
                    spacing=8,
                ),
                padding=14,
                bgcolor=PANEL,
                border_radius=12,
                border=ft.border.all(1, LINE),
            ),
        ],
        spacing=12,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    s.wave_canvas.width = None
    waveform_tab = ft.Column(
        [
            s._card(
                s._heading("Chronogramme idéal", "Calculé avec les paramètres du panneau Pilotage"),
                s.wave_note,
                ft.Container(s.wave_canvas, height=s.wave_canvas.height),
                ft.Text(
                    "Simulation des fronts attendus, sans mesure des broches ni modèle "
                    "d'intégrité électrique.",
                    size=12,
                    color=MUTED,
                ),
                ft.TextButton("Modifier la trame", icon=ft.Icons.TUNE, on_click=s._open_control),
            ),
            _section(
                "Exporter le chronogramme",
                "SVG · CSV des transitions · VCD pour GTKWave",
                [
                    _grid((s.export_path, 12)),
                    ft.Row(
                        [
                            ft.OutlinedButton("SVG", on_click=s._export_svg),
                            ft.OutlinedButton("CSV", on_click=s._export_csv),
                            ft.OutlinedButton("VCD", on_click=s._export_vcd),
                        ],
                        wrap=True,
                    ),
                    s.export_message,
                ],
            ),
        ],
        expand=True,
        scroll=ft.ScrollMode.AUTO,
        spacing=14,
    )

    native = s._card(
        s._heading("1 · Charger le firmware", "Windows natif · pilote Adept / FTDI existant"),
        ft.Text(
            "Le firmware fourni est prêt à charger. Aucun outil de compilation "
            "FPGA n'est nécessaire sur ce PC.",
            size=12,
            color=MUTED,
        ),
        _path(s, s.windows_bitstream_path, ["bit"]),
        ft.Row(
            [
                s.jtag_program_button,
                s.jtag_probe_button,
                ft.TextButton("Utiliser le firmware fourni", on_click=s._use_reference),
            ],
            wrap=True,
        ),
        s.selected_firmware_note,
        _section(
            "Options JTAG",
            "Facultatif : seulement avec plusieurs cartes ou une DLL spécifique",
            [
                _path(s, s.ftdi_dll_path, ["dll"]),
                _grid((s.ftdi_serial, 12)),
            ],
        ),
        ft.Text(
            "Chargement SRAM temporaire : recharger après une coupure d'alimentation. "
            "La détection JTAG identifie la puce ; la connexion UART vérifie le firmware.",
            size=12,
            color=MUTED,
        ),
    )
    native.visible = s.is_windows
    next_step = s._card(
        s._heading("2 · Connecter et vérifier", "Après le chargement, revenir dans Pilotage"),
        ft.Text(
            "Vérifier LD4 (PLL), sélectionner Carte · USB/UART et le port de l'Arty, "
            "puis Connecter. INFO identifie le cœur ; le test LED confirme le dialogue.",
            size=12,
            color=MUTED,
        ),
        ft.TextButton("Aller à Pilotage", icon=ft.Icons.USB, on_click=s._open_control),
        ft.Text(
            "Référence : DATA JB1/E15 · CLK JB2/E16 · LATCH JB3/D15 · "
            "masse JB5 ou JB11. Sorties 3,3 V ; commencer à fréquence réduite.",
            size=12,
            color=BLUE,
            selectable=True,
        ),
    )
    custom = _section(
        "Personnaliser le firmware",
        "Optionnel : changer le cœur ou le brochage",
        [
            ft.Text(
                "Ces réglages décrivent un prochain firmware ; ils ne modifient pas "
                "la carte connectée. Compiler, charger le résultat puis reconnecter.",
                size=12,
                color=MUTED,
            ),
            _grid((s.fw_core, 6), (s.fw_drive, 3), (s.fw_slew, 3)),
            _grid(*[(control, 4) for control in s.fw_pins.values()]),
            s.fw_summary,
            s.fw_warnings,
            _section(
                "Configuration JSON",
                "Charger ou enregistrer le choix d'horloge et de broches",
                [
                    _path(s, s.fw_config_path, ["json"]),
                    ft.Row(
                        [
                            ft.OutlinedButton(
                                "Charger la configuration", on_click=s._load_firmware
                            ),
                            ft.OutlinedButton(
                                "Enregistrer la configuration", on_click=s._save_firmware
                            ),
                        ],
                        wrap=True,
                    ),
                ],
            ),
            s._heading("Compiler sur GitHub", "Recommandé sous Windows sans chaîne FPGA locale"),
            _grid((s.gh_repository, 8), (s.gh_ref, 4), (s.gh_token, 12)),
            s.remote_build_button,
            ft.Text(
                "Le résultat est téléchargé et vérifié, puis sélectionné au-dessus pour "
                "le chargement. La compilation ne programme pas automatiquement la carte.",
                size=12,
                color=MUTED,
            ),
        ],
    )
    local = _section(
        "Outils locaux (Linux / WSL)",
        "Facultatif : chaîne open source configurée par toolchain.json",
        [
            ft.Text(
                "Yosys → nextpnr/openXC7 → Project X-Ray. Ce parcours est réservé à "
                "une chaîne de compilation installée ; le firmware fourni suffit pour démarrer.",
                size=12,
                color=MUTED,
            ),
            s.toolchain_note,
            _path(s, s.toolchain_path, ["json"]),
            ft.Row([s.doctor_button, s.build_button], wrap=True),
            s.doctor_results,
            _path(s, s.bitstream_path, ["bit"]),
            s.program_button,
        ],
    )
    fpga_tab = ft.Column(
        [native, next_step, custom, local], expand=True, scroll=ft.ScrollMode.AUTO, spacing=14
    )
    journal_tab = ft.Column(
        [
            s._heading("Journal de session", "Commandes, réponses et progression des opérations"),
            ft.Container(
                s.journal,
                expand=True,
                bgcolor="#080E19",
                padding=12,
                border_radius=12,
                border=ft.border.all(1, LINE),
            ),
            _grid((s.journal_path, 12)),
            ft.Row(
                [
                    ft.OutlinedButton("Exporter le journal", on_click=s._export_journal),
                    ft.TextButton("Effacer", on_click=s._clear_journal),
                ],
                wrap=True,
            ),
        ],
        expand=True,
        spacing=12,
    )
    s.tabs = ft.Tabs(
        tabs=[
            ft.Tab(text="Pilotage", icon=ft.Icons.TUNE, content=control_tab),
            ft.Tab(text="Chronogramme", icon=ft.Icons.SHOW_CHART, content=waveform_tab),
            ft.Tab(text="FPGA", icon=ft.Icons.MEMORY, content=fpga_tab),
            ft.Tab(text="Journal", icon=ft.Icons.TERMINAL, content=journal_tab),
        ],
        selected_index=0,
        expand=True,
        animation_duration=150,
    )
    s.tool_panel = ft.Container(
        ft.Row(
            [
                ft.Column([s.tool_progress, s.tool_message], spacing=8, expand=True),
                ft.IconButton(
                    ft.Icons.CLOSE,
                    tooltip="Masquer ce message (le journal le conserve)",
                    on_click=s._dismiss_tool_panel,
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.START,
        ),
        padding=12,
        bgcolor=PANEL,
        border_radius=12,
        visible=False,
    )
    return ft.Column(
        [
            ft.Row(
                [
                    ft.Icon(ft.Icons.MEMORY, size=30, color=AMBER),
                    ft.Column(
                        [
                            ft.Text("ARTY FRAME STUDIO", size=22, weight=ft.FontWeight.W_700),
                            ft.Text(
                                "Trames série · Horloge · LATCH · Simulation", size=12, color=MUTED
                            ),
                        ],
                        spacing=3,
                        expand=True,
                    ),
                    ft.Container(s.mode_badge, padding=10, bgcolor=PANEL, border_radius=12),
                ],
                spacing=14,
            ),
            s.setup_steps,
            s.tool_panel,
            s.tabs,
        ],
        expand=True,
        spacing=12,
    )
