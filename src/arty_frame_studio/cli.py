"""Commandes reproductibles utilisables sans interface graphique."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .model import FrameConfig, load_profile, save_profile
from .simulation import export_csv, export_vcd, simulate, waveform_svg
from .toolchain import Toolchain, ToolchainConfig
from .transport import DemoDevice, SerialDevice, TransportError, list_ports


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Arty A7-100T : trames et FPGA sans Vivado")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ports", help="Lister les ports USB/UART")
    profile = commands.add_parser("profile", help="Créer un profil JSON d’exemple")
    profile.add_argument("path", type=Path)
    simulation = commands.add_parser("simulate", help="Exporter le chronogramme idéal")
    simulation.add_argument("--profile", type=Path)
    simulation.add_argument("--output", type=Path, default=Path("exports/chronogramme"))
    simulation.add_argument("--max-frames", type=int, default=4)
    send = commands.add_parser("send", help="Transmettre un profil à la carte ou à la démo")
    send.add_argument("--profile", type=Path, required=True)
    connection = send.add_mutually_exclusive_group(required=True)
    connection.add_argument("--port", help="COM3, /dev/ttyUSB1, etc.")
    connection.add_argument("--demo", action="store_true")
    send.add_argument("--wait", action="store_true", help="Attendre la fin ; Ctrl+C envoie STOP")
    for name in ("status", "stop"):
        item = commands.add_parser(name, help="Lire l’état" if name == "status" else "Arrêter")
        item.add_argument("--port", required=True)
    for name in ("doctor", "build", "program"):
        item = commands.add_parser(name, help="Chaîne FPGA : " + name)
        item.add_argument("--toolchain", type=Path, default=Path("toolchain.json"))
        item.add_argument("--project-root", type=Path, default=Path.cwd())
        if name == "program":
            item.add_argument("--bitstream", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "ports":
            for port in list_ports():
                print(f"{port.device}\t{port.description}")
        elif args.command == "profile":
            save_profile(FrameConfig(), args.path)
            print(f"Profil créé : {args.path.resolve()}")
        elif args.command == "simulate":
            config = load_profile(args.profile) if args.profile else FrameConfig()
            waveform = simulate(config, max_frames=args.max_frames)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            svg = args.output.with_suffix(".svg")
            svg.write_text(waveform_svg(waveform), encoding="utf-8")
            export_csv(waveform, args.output.with_suffix(".csv"))
            export_vcd(waveform, args.output.with_suffix(".vcd"))
            print(
                f"Simulation idéale : {waveform.frames_simulated}/{config.repeat_count} trame(s), "
                f"{waveform.duration_ns:g} ns. Exports : {args.output.resolve()}.[svg,csv,vcd]"
            )
            if waveform.truncated:
                print("Les exports sont limités à la fenêtre simulée.")
        elif args.command in ("send", "status", "stop"):
            device = DemoDevice() if getattr(args, "demo", False) else SerialDevice(args.port)
            device.connect()
            try:
                if args.command == "send":
                    status = device.send(load_profile(args.profile))
                    print(json.dumps(asdict(status), ensure_ascii=False))
                    if args.wait:
                        while status.busy:
                            time.sleep(0.05)
                            status = device.status()
                        print(f"Terminé : {status.completed} trame(s).")
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
                print(f"Bitstream : {chain.build(log=print)}")
            else:
                chain.program(args.bitstream or chain.bitstream, log=print)
                print("FPGA configuré en SRAM. Les commandes passent maintenant par USB/UART.")
    except (OSError, ValueError, RuntimeError, TransportError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
