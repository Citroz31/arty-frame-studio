"""Commandes reproductibles utilisables sans interface graphique."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .bitstream import read_bitstream
from .firmware_config import (
    MAX_FRAME_CLOCK_HZ,
    REFERENCE_CORE_HZ,
    FirmwareBuildConfig,
    FrameClockPlan,
    format_hz,
    frame_clock_neighbors,
    plan_frame_clock,
)
from .local_tools import ensure_local_toolchain
from .model import CONTINUOUS, FrameConfig, load_profile, save_profile, with_free_clock
from .prebuilt import validate_programming_image, verify_prebuilt_firmware
from .remote_build import GitHubBuildClient, RemoteBuildTarget
from .scope import (
    KeysightScope,
    SocketTransport,
    VisaTransport,
    describe,
    frame_preset,
    list_visa_resources,
    measurement_warnings,
    short_si,
    write_acquisition_csv,
)
from .scope_sim import SimulatedKeysight, signal_source
from .simulation import export_csv, export_vcd, simulate, waveform_svg
from .sweep_cli import add_sweep_parser, add_vna_list_parser, run_sweep, run_vna_list
from .toolchain import Toolchain, ToolchainConfig
from .transport import (
    DemoDevice,
    DeviceError,
    SerialDevice,
    TransportError,
    check_firmware_accepts,
    list_ports,
    run_led_test,
)
from .windows_jtag import list_ftdi_devices, probe_arty, program_arty

FREE_CLOCK_HELP = (
    "CLK libre pendant LATCH et pause (firmware révision 4) ; LATCH et pause du "
    "profil arrondis à des périodes entières de CLK"
)


def _free_clock(config: FrameConfig) -> FrameConfig:
    result = with_free_clock(config)
    if result != config:
        print(
            f"CLK libre : LATCH {result.latch_periods} période(s), pause "
            f"{result.gap_periods} période(s) de CLK (latch_ticks={result.latch_ticks}, "
            f"gap_ticks={result.gap_ticks}).",
            file=sys.stderr,
        )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Arty A7-100T : trames et FPGA sans Vivado")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ports", help="Lister les ports USB/UART")
    firmware = commands.add_parser(
        "firmware-check", help="Vérifier le firmware fourni et ses sources"
    )
    firmware.add_argument("--project-root", type=Path, default=Path.cwd())
    diagnose = commands.add_parser("diagnose", help="Vérifier le port et le PING du firmware UART")
    diagnose.add_argument("--port", required=True, help="COM7, /dev/ttyUSB1, etc.")
    diagnose.add_argument(
        "--timeout", type=float, default=2, help="Délai PING en secondes (défaut : 2)"
    )
    diagnose.add_argument(
        "--reset-board",
        action="store_true",
        help="Impulsion DTR avant PING (cavalier JP2) : remet à zéro la logique du FPGA",
    )
    for name in ("jtag-devices", "jtag-diagnose", "jtag-program"):
        item = commands.add_parser(
            name, help="Windows : FTDI D2XX natif, sans changement de pilote"
        )
        item.add_argument("--ftdi-dll", type=Path, help="DLL D2XX FTDI ; défaut : pilote installé")
        if name != "jtag-devices":
            item.add_argument(
                "--serial", help="Numéro de série du canal JTAG A, si plusieurs cartes"
            )
        if name in ("jtag-diagnose", "jtag-program"):
            item.add_argument(
                "--tck-mhz",
                type=float,
                default=6 if name == "jtag-program" else 1,
                help="Fréquence JTAG : 1, 2, 3, 5, 6, 10, 15 ou 30 MHz (défaut : %(default)g)",
            )
        if name == "jtag-program":
            item.add_argument("--project-root", type=Path, default=Path.cwd())
            item.add_argument(
                "--bitstream",
                required=True,
                type=Path,
                help="Fichier .bit existant pour xc7a100tcsg324 ; SRAM, backend expérimental",
            )
    for name in ("info", "led-test"):
        item = commands.add_parser(
            name,
            help="Lire l'identité du firmware"
            if name == "info"
            else "Faire défiler un motif sur LD4-LD7 pour tester la liaison",
        )
        item.add_argument("--port", required=True, help="COM7, /dev/ttyUSB1, etc.")
    tr = commands.add_parser(
        "tr", help="Fixer la broche TR à 3,3 V (1) ou 0 V (0) ; elle garde ce niveau"
    )
    tr.add_argument("--port", required=True, help="COM7, /dev/ttyUSB1, etc.")
    tr.add_argument("level", type=int, choices=(0, 1), help="1 = 3,3 V, 0 = 0 V")
    settings = commands.add_parser(
        "firmware-config", help="Créer ou vérifier une configuration de firmware personnalisé"
    )
    settings.add_argument("--input", type=Path, help="Configuration JSON à compléter/vérifier")
    core = settings.add_mutually_exclusive_group()
    core.add_argument("--core-mhz", type=float, help="Horloge du cœur, ex. 150")
    core.add_argument(
        "--clk-mhz",
        type=float,
        help="Fréquence CLK voulue : le cœur réalisable le plus proche est choisi",
    )
    settings.add_argument(
        "--below", action="store_true", help="Avec --clk-mhz : ne jamais dépasser la demande"
    )
    for pin in ("data", "clock", "latch", "tr"):
        settings.add_argument(f"--{pin}", help=f"Broche {pin.upper()} : JA1..JD10")
    settings.add_argument("--drive", type=int, choices=(4, 8, 12, 16))
    settings.add_argument("--slew", choices=("SLOW", "FAST"))
    settings.add_argument("--output", type=Path, help="Fichier JSON à écrire")
    settings.add_argument("--xdc", type=Path, help="Écrire aussi les contraintes générées")
    plan = commands.add_parser(
        "clock-plan",
        help="Fréquence CLK réalisable la plus proche (PLL et N) et firmware nécessaire",
        description=(
            "Cherche, parmi toutes les horloges de cœur du PLL et tous les diviseurs N, "
            "la fréquence CLK la plus proche de la demande ; "
            f"limite absolue : {format_hz(MAX_FRAME_CLOCK_HZ)}."
        ),
    )
    plan.add_argument("mhz", type=float, help="Fréquence CLK voulue en MHz, ex. 150")
    plan.add_argument("--below", action="store_true", help="Ne jamais dépasser la demande")
    plan.add_argument(
        "--current-core-mhz",
        type=float,
        help="Cœur du firmware chargé : préféré à écart égal (aucune recompilation)",
    )
    plan.add_argument(
        "--input", type=Path, help="Configuration firmware de départ (broches, courant)"
    )
    plan.add_argument(
        "--output", type=Path, help="Écrire la configuration firmware de ce cœur (JSON)"
    )
    plan.add_argument(
        "--profile",
        type=Path,
        help="Écrire aussi un profil de trame à cette fréquence (base : --base-profile)",
    )
    plan.add_argument("--base-profile", type=Path, help="Profil de trame à adapter")
    remote = commands.add_parser(
        "remote-build", help="Compiler un firmware sur GitHub Actions et le télécharger"
    )
    remote.add_argument("--firmware-config", type=Path, help="Défaut : firmware de référence")
    remote.add_argument("--repository", default=RemoteBuildTarget().repository)
    remote.add_argument("--ref", default="main", help="Branche contenant firmware.yml")
    remote.add_argument("--output-dir", type=Path, default=Path("builds"))
    remote.add_argument(
        "--token-env",
        default="ARTY_GITHUB_TOKEN",
        help="Variable d'environnement contenant le jeton (Actions : lecture/écriture)",
    )
    profile = commands.add_parser("profile", help="Créer un profil JSON d’exemple")
    profile.add_argument("path", type=Path)
    simulation = commands.add_parser("simulate", help="Exporter le chronogramme idéal")
    simulation.add_argument("--profile", type=Path)
    simulation.add_argument("--output", type=Path, default=Path("exports/chronogramme"))
    simulation.add_argument("--max-frames", type=int, default=4)
    simulation.add_argument("--free-clock", action="store_true", help=FREE_CLOCK_HELP)
    send = commands.add_parser("send", help="Transmettre un profil à la carte ou à la démo")
    send.add_argument("--profile", type=Path, required=True)
    connection = send.add_mutually_exclusive_group(required=True)
    connection.add_argument("--port", help="COM3, /dev/ttyUSB1, etc.")
    connection.add_argument("--demo", action="store_true")
    send.add_argument("--wait", action="store_true", help="Attendre la fin ; Ctrl+C envoie STOP")
    send.add_argument(
        "--continuous",
        action="store_true",
        help="Répéter la trame sans fin jusqu'à STOP (repeat_count 0, firmware révision 3)",
    )
    send.add_argument("--free-clock", action="store_true", help=FREE_CLOCK_HELP)
    send.add_argument(
        "--duration",
        type=float,
        metavar="SECONDES",
        help="Envoyer STOP après cette durée (utile en émission continue)",
    )
    scope = commands.add_parser(
        "scope",
        help="Lire l'écran courant de l'oscilloscope Keysight et les mesures des voies",
        description=(
            "Lit l'écran courant sans réarmer le déclenchement. "
            "--single demande une nouvelle acquisition ; --demo simule une acquisition."
        ),
    )
    link = scope.add_mutually_exclusive_group(required=True)
    link.add_argument("--lan", metavar="ADRESSE", help="Adresse IP (SCPI, port 5025)")
    link.add_argument("--visa", metavar="RESSOURCE", help="USB0::0x2A8D::…::INSTR (PyVISA)")
    link.add_argument(
        "--demo", action="store_true", help="Oscilloscope simulé selon le profil et --ch1/--ch2"
    )
    scope.add_argument("--profile", type=Path, help="Trame observée en démonstration")
    for channel, signal in ((1, "data"), (2, "clk")):
        scope.add_argument(
            f"--ch{channel}",
            choices=("data", "clk", "latch", "other"),
            default=signal,
            help=f"Signal câblé sur CH{channel} (défaut : {signal}) ; ne change pas le câblage",
        )
    scope.add_argument(
        "--preset",
        action="store_true",
        help="Régler pour la trame (1 V/div, 5 périodes de CLK, déclenchement sur la voie CLK)",
    )
    scope.add_argument("--autoscale", action="store_true", help="Lancer Auto scale avant")
    scope.add_argument(
        "--single", action="store_true", help="Armer une nouvelle acquisition unique"
    )
    scope.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Attente du déclenchement pour --single ou --demo (s)",
    )
    scope.add_argument("--csv", type=Path, help="Exporter les points de l'acquisition")
    scope.add_argument("--png", type=Path, help="Copie d'écran de l'oscilloscope réel")
    commands.add_parser("scope-list", help="Lister les instruments VISA (USB et LAN)")
    add_sweep_parser(commands)
    add_vna_list_parser(commands)
    install = commands.add_parser(
        "install-fpga-tools", help="Installer les outils FPGA portables Windows sans WSL"
    )
    install.add_argument("--project-root", type=Path, default=Path.cwd())
    install.add_argument("--tools-dir", type=Path, help="Dossier des outils portables")
    install.add_argument(
        "--config", type=Path, help="JSON à écrire ; défaut : projet/toolchain.json"
    )
    for name in ("status", "stop"):
        item = commands.add_parser(name, help="Lire l’état" if name == "status" else "Arrêter")
        item.add_argument("--port", required=True)
    for name in ("doctor", "build", "program"):
        item = commands.add_parser(name, help="Chaîne FPGA : " + name)
        item.add_argument("--toolchain", type=Path, default=Path("toolchain.json"))
        item.add_argument("--project-root", type=Path, default=Path.cwd())
        if name == "program":
            item.add_argument("--bitstream", type=Path)
        if name == "build":
            item.add_argument(
                "--firmware-config", type=Path, help="Horloge et broches ; défaut : référence"
            )
    return parser


def _firmware_settings(args: argparse.Namespace) -> FirmwareBuildConfig:
    firmware = FirmwareBuildConfig.load(args.input) if args.input else FirmwareBuildConfig()
    changes: dict[str, Any] = {}
    if args.core_mhz is not None:
        changes["core_hz"] = round(args.core_mhz * 1e6)
    if args.clk_mhz is not None:
        plan = plan_frame_clock(_megahertz(args.clk_mhz), never_above=args.below)
        print(plan.summary())
        changes["core_hz"] = plan.core_hz
    elif args.below:
        raise ValueError("--below s'utilise avec --clk-mhz.")
    for option, field in (
        ("data", "data_pin"),
        ("clock", "clock_pin"),
        ("latch", "latch_pin"),
        ("tr", "tr_pin"),
    ):
        if getattr(args, option):
            changes[field] = getattr(args, option).upper()
    if args.drive is not None:
        changes["drive_ma"] = args.drive
    if args.slew is not None:
        changes["slew"] = args.slew
    return replace(firmware, **changes)


def _megahertz(value: float) -> str:
    """MHz saisis → Hz décimaux exacts (150.1 → 150100000)."""
    if not math.isfinite(value):
        raise ValueError("Fréquence : nombre fini attendu.")
    return f"{value!r}e6"


def _plan_lines(plan: FrameClockPlan, current_core_hz: int | None) -> list[str]:
    lines = [f"Demande : {format_hz(plan.requested_hz)}", f"Réalisable : {plan.summary()}"]
    below, above = frame_clock_neighbors(plan.requested_hz)
    neighbors = [
        f"{label} {format_hz(item.achieved_hz)} (cœur {format_hz(item.setting.exact_hz)}, "
        f"N={item.divider})"
        for label, item in (("inférieure", below), ("supérieure", above))
        if item is not None and item.achieved_hz != plan.achieved_hz
    ]
    if neighbors:
        lines.append("Fréquences voisines : " + " ; ".join(neighbors))
    if plan.core_hz == current_core_hz:
        lines.append("Le firmware chargé convient : régler seulement N.")
    elif plan.core_hz == REFERENCE_CORE_HZ:
        lines.append("Le firmware de référence (fourni) convient : aucune compilation.")
    else:
        lines.append(
            f"Nouveau firmware nécessaire : cœur {plan.core_hz} Hz ({plan.setting.describe()})."
        )
    return lines


def _clock_plan(args: argparse.Namespace) -> int:
    current = None if args.current_core_mhz is None else round(args.current_core_mhz * 1e6)
    plan = plan_frame_clock(_megahertz(args.mhz), never_above=args.below, current_core_hz=current)
    for line in _plan_lines(plan, current):
        print(line)
    if args.output:
        base = FirmwareBuildConfig.load(args.input) if args.input else FirmwareBuildConfig()
        firmware = replace(base, core_hz=plan.core_hz)
        firmware.save(args.output)
        print(f"Configuration firmware enregistrée : {args.output} ({firmware.summary()})")
        for note in firmware.warnings():
            print(f"Attention : {note}")
        if not firmware.is_reference:
            print(
                "Compiler puis charger : arty-frame build --firmware-config "
                f"{args.output} (local) ou arty-frame remote-build --firmware-config "
                f"{args.output} (GitHub), puis arty-frame program ou jtag-program."
            )
    if args.profile:
        frame = load_profile(args.base_profile) if args.base_profile else FrameConfig()
        latch_ns = frame.latch_ticks * frame.tick_ns
        gap_ns = frame.gap_ticks * frame.tick_ns
        tick = plan.setting.tick_ns
        frame = replace(
            frame,
            core_hz=plan.core_hz,
            divider=plan.divider,
            latch_ticks=max(1, min(65_535, round(latch_ns / tick))),
            gap_ticks=max(0, min(65_535, round(gap_ns / tick))),
        )
        if frame.free_clock:
            frame = with_free_clock(replace(frame, free_clock=False))
        save_profile(frame, args.profile)
        print(
            f"Profil de trame enregistré : {args.profile} (CLK "
            f"{format_hz(plan.achieved_hz)}, N={plan.divider}, LATCH "
            f"{frame.latch_ticks * frame.tick_ns:.6g} ns, pause "
            f"{frame.gap_ticks * frame.tick_ns:.6g} ns)"
        )
    return 0


def _scope(args: argparse.Namespace) -> int:
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
        raise ValueError("Le délai de déclenchement doit être fini, supérieur à 0 et au plus 60 s.")
    if args.demo and args.png:
        raise ValueError("Copie d'écran indisponible en démonstration ; utilisez l'export CSV.")
    config = load_profile(args.profile) if args.profile else FrameConfig()
    mapping: dict[int, str | None] = {
        channel: None if signal == "other" else signal
        for channel, signal in ((1, args.ch1), (2, args.ch2))
    }
    transport: Any
    if args.lan:
        transport = SocketTransport(args.lan)
    elif args.visa:
        transport = VisaTransport(args.visa)
    else:
        source = signal_source(config)
        transport = SimulatedKeysight(lambda: source, lambda: mapping)
    scope = KeysightScope(transport)
    try:
        print(f"Oscilloscope : {scope.identify()}")
        if args.preset or args.demo:
            scope.apply_settings(frame_preset(config, mapping, scope.read_settings()))
        if args.autoscale:
            scope.autoscale()
        acquisition = (
            scope.capture(timeout=args.timeout, mapping=mapping)
            if args.demo or args.single
            else scope.read_display(mapping=mapping)
        )
        settings = acquisition.settings
        trigger = settings.trigger
        acquisition_state = (
            "écran existant (origine du déclenchement inconnue)"
            if acquisition.from_display
            else "déclenchée"
            if acquisition.triggered
            else "sans front"
        )
        print(
            f"Base de temps {short_si(settings.time_scale, 's')}/div · déclenchement configuré "
            f"CH{trigger.source} {'montant' if trigger.slope == 'POS' else 'descendant'} "
            f"{trigger.level:.3g} V · {acquisition_state}"
        )
        for channel, values in sorted(acquisition.measurements.items()):
            print(f"CH{channel} : {describe(values)}")
        for warning in measurement_warnings(acquisition, mapping):
            print(f"Attention : {warning}", file=sys.stderr)
        if args.csv:
            print(f"Points : {write_acquisition_csv(acquisition, args.csv)}")
        if args.png:
            args.png.parent.mkdir(parents=True, exist_ok=True)
            args.png.write_bytes(scope.screenshot())
            print(f"Copie d'écran : {args.png}")
    finally:
        scope.close(resume=args.demo or args.single)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ports":
            for port in list_ports():
                print(f"{port.device}\t{port.description}\t{port.hwid}")
        elif args.command == "scope":
            return _scope(args)
        elif args.command == "sweep":
            return run_sweep(args)
        elif args.command == "vna-list":
            return run_vna_list(args)
        elif args.command == "scope-list":
            resources = list_visa_resources()
            for resource in resources:
                print(resource)
            if not resources:
                print(
                    "Aucun instrument VISA : vérifier le câble USB arrière et Keysight IO "
                    "Libraries Suite, ou utiliser scope --lan ADRESSE si l'appareil dispose "
                    "d'une prise LAN.",
                    file=sys.stderr,
                )
                return 1
        elif args.command == "firmware-check":
            manifest = verify_prebuilt_firmware(args.project_root)
            print("Firmware fourni : SHA256, sources RTL/XDC et timing du cœur vérifiés.")
            print(
                json.dumps(
                    {
                        key: manifest[key]
                        for key in (
                            "source_commit",
                            "sha256",
                            "routed_core_fmax_mhz",
                            "hardware_validated",
                        )
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "diagnose":
            probe = SerialDevice(args.port, timeout=args.timeout)
            print(f"Diagnostic {probe.port} : 115200 bauds, 8N1, aucun contrôle de flux.")
            print("Test PING uniquement : aucune commande SEND ou STOP n'est envoyée.")
            try:
                ports = list_ports()
                matching = [p for p in ports if p.device == probe.port]
                if not matching and probe.port.upper().startswith("COM"):
                    matching = [p for p in ports if p.device.upper() == probe.port.upper()]
                if matching:
                    for port in matching:
                        print(
                            f"Port USB : {port.description}\nIdentifiant : {port.hwid or 'inconnu'}"
                        )
                else:
                    print(
                        "Port absent de la liste USB/UART ; "
                        "tentative sur le port demandé uniquement."
                    )
            except TransportError as exc:
                print(f"Liste des ports indisponible : {exc}", file=sys.stderr)
            try:
                if args.reset_board:
                    print("Impulsion DTR : remise à zéro de la logique du FPGA (cavalier JP2).")
                    status = probe.connect(reset_board=True)
                else:
                    status = probe.connect()
                print("Firmware Arty Frame Studio : réponse PING valide.")
                round_trip = getattr(probe, "last_round_trip", None)
                if round_trip is not None:
                    print(f"Temps de réponse PING : {round_trip * 1000:.1f} ms.")
                print(json.dumps(asdict(status), ensure_ascii=False))
            finally:
                # Retards et paquets étrangers : utiles même après un échec.
                for note in getattr(probe, "link_notes", list)():
                    print(f"Liaison : {note}", file=sys.stderr)
                probe.close()
        elif args.command == "jtag-devices":
            for ftdi_device in list_ftdi_devices(dll_path=args.ftdi_dll):
                print(json.dumps(asdict(ftdi_device), ensure_ascii=False))
        elif args.command == "jtag-diagnose":
            jtag_probe = probe_arty(
                serial=args.serial, dll_path=args.ftdi_dll, tck_hz=round(args.tck_mhz * 1e6)
            )
            print(json.dumps(asdict(jtag_probe), ensure_ascii=False))
            print("Artix-7 100T détecté par JTAG. Le firmware UART reste à vérifier par PING.")
        elif args.command == "jtag-program":
            image = read_bitstream(args.bitstream)
            validate_programming_image(image, args.project_root)
            print(f"Bitstream : {image.path}\nPart : {image.part}\nSHA256 : {image.sha256}")
            print("Chargement SRAM par FTDI D2XX Windows ; backend expérimental.")
            jtag_program = program_arty(
                image.payload,
                serial=args.serial,
                dll_path=args.ftdi_dll,
                tck_hz=round(args.tck_mhz * 1e6),
            )
            print(json.dumps(asdict(jtag_program), ensure_ascii=False))
            print(
                f"Chargement en {jtag_program.seconds:.1f} s à {jtag_program.tck_hz / 1e6:g} MHz."
            )
            print(
                "Configuration SRAM terminée. Vérifiez le firmware UART avec "
                "diagnose --port COM7 avant d'envoyer une trame."
            )
        elif args.command in ("info", "led-test"):
            board = SerialDevice(args.port)
            board.connect()
            try:
                identity = board.identify()
                print(json.dumps(asdict(identity), ensure_ascii=False))
                if args.command == "led-test":
                    print("Observer LD4 à LD7 : chenillard, toutes allumées, puis état normal.")
                    led_result = run_led_test(board)
                    print(
                        f"Test LED : {led_result.commands} commandes confirmées, aller-retour "
                        f"moyen {led_result.mean_ms:.1f} ms, maximum {led_result.max_ms:.1f} ms."
                    )
            finally:
                board.close()
        elif args.command == "tr":
            board = SerialDevice(args.port)
            board.connect()
            try:
                identity = board.identify()
                if not identity.tr:
                    raise ValueError(
                        f"Le firmware (révision {identity.revision}) n'a pas de broche TR : "
                        "charger le firmware de révision 5 ou plus."
                    )
                status = board.tr(args.level)
                print(
                    f"TR : {'3,3 V' if args.level else '0 V'} (commande confirmée, "
                    f"{status.completed} trame(s) terminée(s)). La broche garde ce niveau "
                    "jusqu'à la prochaine commande ou un reset de la carte."
                )
            finally:
                board.close()
        elif args.command == "clock-plan":
            return _clock_plan(args)
        elif args.command == "firmware-config":
            firmware = _firmware_settings(args)
            print(f"Configuration : {firmware.summary()}")
            for note in firmware.warnings():
                print(f"Attention : {note}")
            if args.output:
                firmware.save(args.output)
                print(f"Configuration enregistrée : {args.output}")
            if args.xdc:
                args.xdc.write_text(firmware.xdc(), encoding="utf-8")
                print(f"Contraintes générées : {args.xdc}")
        elif args.command == "remote-build":
            firmware = (
                FirmwareBuildConfig.load(args.firmware_config)
                if args.firmware_config
                else FirmwareBuildConfig()
            )
            token = os.environ.get(args.token_env, "")
            if not token:
                raise ValueError(
                    f"Définir {args.token_env} avec un jeton GitHub (Actions : lecture/écriture)."
                )
            client = GitHubBuildClient(RemoteBuildTarget(args.repository, args.ref), token)
            remote_result = client.build(firmware, args.output_dir, progress=print)
            print(f"Firmware téléchargé et vérifié : {remote_result.bitstream}")
        elif args.command == "install-fpga-tools":
            ensure_local_toolchain(
                args.project_root,
                log=print,
                tools_dir=args.tools_dir,
                config_path=args.config,
            )
            config_path = args.config or args.project_root / "toolchain.json"
            print(f"Configuration locale prête : {config_path.expanduser().resolve()}")
        elif args.command == "profile":
            save_profile(FrameConfig(), args.path)
            print(f"Profil créé : {args.path.resolve()}")
        elif args.command == "simulate":
            config = load_profile(args.profile) if args.profile else FrameConfig()
            if args.free_clock:
                config = _free_clock(config)
            waveform = simulate(config, max_frames=args.max_frames)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            svg = args.output.with_suffix(".svg")
            svg.write_text(waveform_svg(waveform), encoding="utf-8")
            export_csv(waveform, args.output.with_suffix(".csv"))
            export_vcd(waveform, args.output.with_suffix(".vcd"))
            total = "continu" if config.continuous else str(config.repeat_count)
            shown = f"{waveform.frames_simulated} trame(s) complète(s)"
            if waveform.partial_last_frame:
                shown += " + 1 partielle"
            print(
                f"Simulation idéale : {shown} sur {total}, "
                f"{waveform.duration_ns:g} ns. Exports : {args.output.resolve()}.[svg,csv,vcd]"
            )
            if waveform.truncated:
                print("Les exports sont limités à la fenêtre simulée.")
        elif args.command in ("send", "status", "stop"):
            if args.command == "send":
                # Refuser les options incohérentes avant d'ouvrir le port.
                config = load_profile(args.profile)
                if args.continuous:
                    config = replace(config, repeat_count=CONTINUOUS)
                if args.free_clock:
                    config = _free_clock(config)
                if args.duration is not None and not (
                    math.isfinite(args.duration) and args.duration > 0
                ):
                    raise ValueError("--duration attend un nombre de secondes positif.")
            device = (
                DemoDevice(core_hz=config.core_hz)
                if getattr(args, "demo", False)
                else SerialDevice(args.port)
            )
            device.connect()
            must_stop = False
            stop_attempted = False
            stop_confirmed = False
            try:
                if args.command == "send":
                    # SEND refuse une trame calculée pour une autre horloge de
                    # cœur, ou continue pour un firmware qui ne la gère pas.
                    check_firmware_accepts(config, device.identify())
                    # Une absence de réponse à SEND laisse l'exécution inconnue.
                    # Ne jamais répéter SEND ; demander une seule fois STOP lors
                    # d'une erreur ou interruption avant de fermer le port.
                    must_stop = True
                    try:
                        status = device.send(config)
                    except DeviceError:
                        # Un refus explicite n'a pas démarré notre séquence.
                        must_stop = False
                        raise
                    must_stop = status.busy
                    print(json.dumps(asdict(status), ensure_ascii=False))
                    if args.duration is not None:
                        deadline = time.monotonic() + args.duration
                        while status.busy and time.monotonic() < deadline:
                            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                            if time.monotonic() >= deadline:
                                # Ne pas lancer STATUS à l'échéance : un timeout
                                # UART supplémentaire retarderait inutilement STOP.
                                break
                            status = device.status()
                            must_stop = status.busy
                        if status.busy:
                            stop_attempted = True
                            must_stop = False
                            status = device.stop()
                            stop_confirmed = True
                            print(
                                f"STOP après {args.duration:g} s : {status.completed} trame(s)"
                                + (" (compteur modulo 65536)." if config.continuous else ".")
                            )
                        else:
                            print(f"Terminé : {status.completed} trame(s).")
                    elif args.wait:
                        if config.continuous:
                            print("Émission continue : Ctrl+C envoie STOP.", file=sys.stderr)
                        while status.busy:
                            time.sleep(0.05)
                            status = device.status()
                            must_stop = status.busy
                        print(f"Terminé : {status.completed} trame(s).")
                    elif config.continuous:
                        if args.demo:
                            print(
                                "Démo locale : la simulation sera fermée à la fin de cette "
                                "commande. Utiliser --wait ou --duration pour la maintenir.",
                                file=sys.stderr,
                            )
                        else:
                            print(
                                "Émission continue en cours sur la carte ; elle s'arrête avec "
                                "la commande stop (ou une coupure/un reset).",
                                file=sys.stderr,
                            )
                        must_stop = False
                    else:
                        # Sans attente, l'émission finie continue sur le FPGA.
                        must_stop = False
                else:
                    status = device.stop() if args.command == "stop" else device.status()
                    print(json.dumps(asdict(status), ensure_ascii=False))
            except KeyboardInterrupt:
                if must_stop and not stop_attempted:
                    stop_attempted = True
                    must_stop = False
                    try:
                        device.stop()
                        stop_confirmed = True
                    except (OSError, ValueError, RuntimeError) as exc:
                        print(f"Confirmation STOP non reçue : {exc}", file=sys.stderr)
                print(
                    "Émission interrompue par STOP."
                    if stop_confirmed
                    else "Commande interrompue ; vérifier l'état de la carte si nécessaire.",
                    file=sys.stderr,
                )
                return 130
            finally:
                if must_stop and not stop_attempted:
                    stop_attempted = True
                    try:
                        device.stop()
                        print("STOP confirmé après l'échec de la commande.", file=sys.stderr)
                    except (OSError, ValueError, RuntimeError) as exc:
                        print(
                            f"Confirmation STOP non reçue : {exc}. "
                            "L'émission peut encore être active ; vérifier la carte.",
                            file=sys.stderr,
                        )
                device.close()
        else:
            chain = Toolchain(ToolchainConfig.from_json(args.toolchain), args.project_root)
            if args.command == "doctor":
                results = chain.doctor()
                for result in results:
                    print(f"{'OK' if result.ok else 'ABSENT'}\t{result.name}\t{result.detail}")
                return 0 if all(result.ok for result in results) else 1
            if args.command == "build":
                firmware = (
                    FirmwareBuildConfig.load(args.firmware_config)
                    if args.firmware_config
                    else FirmwareBuildConfig()
                )
                print(f"Configuration : {firmware.summary()}")
                print(f"Bitstream : {chain.build(log=print, firmware=firmware)}")
            else:
                chain.program(args.bitstream or chain.bitstream, log=print)
                print("FPGA configuré en SRAM. Les commandes passent maintenant par USB/UART.")
    except (OSError, ValueError, RuntimeError, TransportError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
