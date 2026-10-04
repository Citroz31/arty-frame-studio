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
from .firmware_config import FirmwareBuildConfig
from .model import CONTINUOUS, FrameConfig, load_profile, save_profile, with_free_clock
from .prebuilt import validate_programming_image, verify_prebuilt_firmware
from .remote_build import GitHubBuildClient, RemoteBuildTarget
from .simulation import export_csv, export_vcd, simulate, waveform_svg
from .toolchain import Toolchain, ToolchainConfig
from .transport import DemoDevice, SerialDevice, TransportError, list_ports, run_led_test
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
    for name in ("jtag-devices", "jtag-diagnose", "jtag-program"):
        item = commands.add_parser(
            name, help="Windows : FTDI D2XX natif, sans changement de pilote"
        )
        item.add_argument("--ftdi-dll", type=Path, help="DLL D2XX FTDI ; défaut : pilote installé")
        if name != "jtag-devices":
            item.add_argument(
                "--serial", help="Numéro de série du canal JTAG A, si plusieurs cartes"
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
    settings = commands.add_parser(
        "firmware-config", help="Créer ou vérifier une configuration de firmware personnalisé"
    )
    settings.add_argument("--input", type=Path, help="Configuration JSON à compléter/vérifier")
    settings.add_argument("--core-mhz", type=float, help="Horloge du cœur, ex. 150")
    for pin in ("data", "clock", "latch"):
        settings.add_argument(f"--{pin}", help=f"Broche {pin.upper()} : JA1..JD10")
    settings.add_argument("--drive", type=int, choices=(4, 8, 12, 16))
    settings.add_argument("--slew", choices=("SLOW", "FAST"))
    settings.add_argument("--output", type=Path, help="Fichier JSON à écrire")
    settings.add_argument("--xdc", type=Path, help="Écrire aussi les contraintes générées")
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
    for option, field in (("data", "data_pin"), ("clock", "clock_pin"), ("latch", "latch_pin")):
        if getattr(args, option):
            changes[field] = getattr(args, option).upper()
    if args.drive is not None:
        changes["drive_ma"] = args.drive
    if args.slew is not None:
        changes["slew"] = args.slew
    return replace(firmware, **changes)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ports":
            for port in list_ports():
                print(f"{port.device}\t{port.description}\t{port.hwid}")
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
                status = probe.connect()
                print("Firmware Arty Frame Studio : réponse PING valide.")
                print(json.dumps(asdict(status), ensure_ascii=False))
            finally:
                probe.close()
        elif args.command == "jtag-devices":
            for ftdi_device in list_ftdi_devices(dll_path=args.ftdi_dll):
                print(json.dumps(asdict(ftdi_device), ensure_ascii=False))
        elif args.command == "jtag-diagnose":
            jtag_probe = probe_arty(serial=args.serial, dll_path=args.ftdi_dll)
            print(json.dumps(asdict(jtag_probe), ensure_ascii=False))
            print("Artix-7 100T détecté par JTAG. Le firmware UART reste à vérifier par PING.")
        elif args.command == "jtag-program":
            image = read_bitstream(args.bitstream)
            validate_programming_image(image, args.project_root)
            print(f"Bitstream : {image.path}\nPart : {image.part}\nSHA256 : {image.sha256}")
            print("Chargement SRAM par FTDI D2XX Windows ; backend expérimental.")
            jtag_program = program_arty(image.payload, serial=args.serial, dll_path=args.ftdi_dll)
            print(json.dumps(asdict(jtag_program), ensure_ascii=False))
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
            print(
                f"Simulation idéale : {waveform.frames_simulated}/{total} trame(s), "
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
            device = DemoDevice() if getattr(args, "demo", False) else SerialDevice(args.port)
            device.connect()
            try:
                if args.command == "send":
                    # SEND refuse une trame calculée pour une autre horloge de
                    # cœur, ou continue pour un firmware qui ne la gère pas.
                    device.identify()
                    status = device.send(config)
                    print(json.dumps(asdict(status), ensure_ascii=False))
                    if args.duration is not None:
                        deadline = time.monotonic() + args.duration
                        while status.busy and time.monotonic() < deadline:
                            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                            status = device.status()
                        if status.busy:
                            status = device.stop()
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
                        print(f"Terminé : {status.completed} trame(s).")
                    elif config.continuous:
                        print(
                            "Émission continue en cours sur la carte ; elle s'arrête avec la "
                            "commande stop (ou une coupure/un reset).",
                            file=sys.stderr,
                        )
                else:
                    status = device.stop() if args.command == "stop" else device.status()
                    print(json.dumps(asdict(status), ensure_ascii=False))
            except KeyboardInterrupt:
                device.stop()
                print("Émission interrompue par STOP.", file=sys.stderr)
                return 130
            finally:
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
