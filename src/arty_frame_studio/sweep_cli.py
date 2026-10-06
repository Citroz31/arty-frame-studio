"""Commande ``arty-frame sweep`` : le mode mesure sans interface graphique."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .instruments import (
    InstrumentError,
    ScpiInstrument,
    SimulatedVna,
    commands_from_text,
    open_transport,
)
from .model import FrameConfig, load_profile
from .probes import SCOPE_QUANTITIES, InstrumentProbe, ScopeProbe, ScopeSpec
from .protocol import DeviceStatus
from .scope import KeysightScope, ScopeError, SocketTransport, VisaTransport, parse_si
from .sweep import (
    FAIL,
    Limit,
    Measurement,
    SweepRunner,
    counter_words,
    describe_words,
    format_duration,
    infer_width,
    parse_words,
    result_line,
    walking_words,
    write_results_csv,
)
from .transport import DemoDevice, SerialDevice, TransportError, check_firmware_accepts


def add_sweep_parser(commands: Any) -> None:
    sweep = commands.add_parser(
        "sweep", help="Mode mesure : envoyer une suite de mots, mesurer après chacun"
    )
    sweep.add_argument("--profile", type=Path, required=True, help="Profil de trame (Pilotage)")
    link = sweep.add_mutually_exclusive_group(required=True)
    link.add_argument("--port", help="COM7, /dev/ttyUSB1, etc.")
    link.add_argument("--demo", action="store_true", help="Carte simulée")
    words = sweep.add_mutually_exclusive_group(required=True)
    words.add_argument("--words", type=Path, metavar="FICHIER", help="Un mot par ligne")
    words.add_argument("--counter", nargs=2, metavar=("DEBUT", "FIN"), help="Compteur inclusif")
    words.add_argument("--walking-one", action="store_true", help="Un seul bit à 1, qui parcourt")
    words.add_argument("--walking-zero", action="store_true", help="Un seul bit à 0, qui parcourt")
    sweep.add_argument("--step", type=int, default=1, help="Pas du compteur (défaut : 1)")
    sweep.add_argument("--base", choices=("bin", "hex", "dec"), default="bin")
    sweep.add_argument("--width", type=int, help="Largeur des mots en bits (défaut : auto)")
    sweep.add_argument("--repeats", type=int, default=1, help="Trames par mot (défaut : 1)")
    sweep.add_argument("--settle-ms", type=float, default=20.0, help="Attente avant la mesure")
    sweep.add_argument(
        "--probe",
        choices=("none", "manual", "scpi", "scope"),
        default="none",
        help="Validation après chaque mot (défaut : aucune)",
    )
    sweep.add_argument("--scpi-lan", metavar="ADRESSE")
    sweep.add_argument("--scpi-visa", metavar="RESSOURCE")
    sweep.add_argument("--scpi-demo", action="store_true", help="VNA simulé")
    sweep.add_argument("--scpi-setup", action="append", default=[], metavar="CMD")
    sweep.add_argument("--scpi-trigger", action="append", default=[], metavar="CMD")
    sweep.add_argument("--scpi-read", action="append", default=[], metavar="REQUETE?")
    sweep.add_argument(
        "--scpi-labels", default="", help="Noms de colonnes séparés par des virgules"
    )
    sweep.add_argument("--scpi-timeout", type=float, default=10.0)
    sweep.add_argument("--scope-lan", metavar="ADRESSE")
    sweep.add_argument("--scope-visa", metavar="RESSOURCE")
    sweep.add_argument(
        "--scope-measure",
        action="append",
        default=[],
        metavar="VOIE:GRANDEUR",
        help="Ex. 2:frequency ; grandeurs : " + ", ".join(SCOPE_QUANTITIES),
    )
    sweep.add_argument("--min", dest="low", help="Valeur 1 minimale acceptée")
    sweep.add_argument("--max", dest="high", help="Valeur 1 maximale acceptée")
    sweep.add_argument("--stop-on-fail", action="store_true")
    sweep.add_argument("--output", type=Path, help="Fichier CSV des résultats")


def _words(args: argparse.Namespace, frame: FrameConfig) -> tuple[list[int], int]:
    text = args.words.read_text(encoding="utf-8") if args.words else ""
    if args.width is not None:
        width = args.width
    elif args.words:
        width = infer_width(text, args.base) or frame.bit_count
    else:
        width = frame.bit_count
    if args.words:
        return parse_words(text, width, args.base), width
    if args.counter:
        start = parse_words(args.counter[0], width, args.base)[0]
        stop = parse_words(args.counter[1], width, args.base)[0]
        return counter_words(start, stop, args.step, width), width
    return walking_words(width, ones=bool(args.walking_one)), width


def _limit(args: argparse.Namespace) -> Limit:
    return Limit(
        parse_si(args.low) if args.low is not None else None,
        parse_si(args.high) if args.high is not None else None,
    )


async def _make_probe(args: argparse.Namespace, cleanup: list[Any], runner: list[Any]) -> Any:
    if args.probe == "none":
        return None
    if args.probe == "manual":

        async def ask(config: FrameConfig, index: int) -> Measurement:
            prompt = (
                f"Mot {index + 1} {config.word:0{config.bit_count}b} envoyé. "
                "Entrée = valider · x = rejeter · s = sauter · r = renvoyer · q = arrêter · "
                "ou une valeur relevée : "
            )
            answer = (await asyncio.to_thread(input, prompt)).strip().lower()
            if answer == "q":
                runner[0].stop()
                return Measurement(action="skip")
            action = {"x": "fail", "n": "fail", "s": "skip", "r": "retry"}.get(answer, "ok")
            values: tuple[float, ...] = ()
            names: tuple[str, ...] = ()
            if action == "ok" and answer:
                try:
                    values, names = (parse_si(answer),), ("valeur relevée",)
                except ValueError:
                    pass
            return Measurement(values, names, action=action)

        return ask
    if args.probe == "scpi":
        if not (args.scpi_lan or args.scpi_visa or args.scpi_demo):
            raise ValueError("--probe scpi demande --scpi-lan, --scpi-visa ou --scpi-demo.")
        reads = commands_from_text("\n".join(args.scpi_read))
        if not reads:
            raise ValueError("--probe scpi demande au moins un --scpi-read.")
        simulated = SimulatedVna() if args.scpi_demo else None
        kind, address = (
            ("demo", "")
            if args.scpi_demo
            else ("lan", args.scpi_lan)
            if args.scpi_lan
            else ("visa", args.scpi_visa)
        )
        instrument = ScpiInstrument(
            open_transport(kind, address, timeout=args.scpi_timeout, simulated=simulated)
        )
        cleanup.append(instrument.close)
        print(f"Instrument : {instrument.identify()}")
        setup = commands_from_text("\n".join(args.scpi_setup))
        if setup:
            instrument.run(setup)
            instrument.check("Réglages")
        labels = [label.strip() for label in args.scpi_labels.split(",") if label.strip()]
        return InstrumentProbe(
            instrument,
            commands_from_text("\n".join(args.scpi_trigger)),
            reads,
            labels,
            on_word=simulated.set_word if simulated is not None else None,
        )
    if not (args.scope_lan or args.scope_visa):
        raise ValueError("--probe scope demande --scope-lan ou --scope-visa.")
    specs = []
    for item in args.scope_measure or ["1:frequency"]:
        channel, _, quantity = item.partition(":")
        specs.append(ScopeSpec(int(channel), quantity))
    transport = (
        SocketTransport(args.scope_lan) if args.scope_lan else VisaTransport(args.scope_visa)
    )
    scope = KeysightScope(transport)
    cleanup.append(lambda: scope.close(resume=False))
    print(f"Oscilloscope : {scope.identify()}")

    async def acquire() -> Any:
        return await asyncio.to_thread(scope.capture)

    return ScopeProbe(acquire, specs)


async def _run(args: argparse.Namespace) -> int:
    frame = load_profile(args.profile)
    words, width = _words(args, frame)
    if not 1 <= args.repeats <= 65535:
        raise ValueError("--repeats : entier de 1 à 65535.")
    base = replace(frame, bit_count=width, word=0, repeat_count=args.repeats)
    device: SerialDevice | DemoDevice = (
        DemoDevice(core_hz=frame.core_hz) if args.demo else SerialDevice(args.port)
    )
    cleanup: list[Any] = [device.close]
    runner_box: list[SweepRunner] = []
    try:
        device.connect()
        check_firmware_accepts(base, device.identify())

        async def send(config: FrameConfig) -> None:
            def work() -> None:
                status: DeviceStatus = device.send(config)
                deadline = time.monotonic() + config.frame_duration_ns / 1e9 * config.repeat_count
                deadline += 2.0
                while status.busy:
                    if time.monotonic() > deadline:
                        raise TransportError("La carte n'a pas terminé la trame dans le délai.")
                    time.sleep(0.002)
                    status = device.status()

            await asyncio.to_thread(work)

        probe = await _make_probe(args, cleanup, runner_box)
        runner = SweepRunner(
            base,
            words,
            send=send,
            measure=probe,
            settle=max(0.0, args.settle_ms) / 1000,
            limit=_limit(args),
            stop_on_fail=args.stop_on_fail,
            on_result=lambda result: print(result_line(result), flush=True),
        )
        runner_box.append(runner)
        print(f"{describe_words(words, width)} · CLK {base.frequency_hz / 1e6:g} MHz")
        try:
            summary = await runner.run()
        except KeyboardInterrupt:
            runner.stop()
            raise
        counts = summary.counts
        print(
            f"{summary.reason} · {counts.get('ok', 0)} OK · {counts.get(FAIL, 0)} échec(s) · "
            f"{counts.get('skipped', 0)} sauté(s) · {format_duration(summary.elapsed)}"
        )
        if summary.message:
            print(summary.message, file=sys.stderr)
        if args.output:
            print(f"Résultats : {write_results_csv(runner.results, args.output)}")
        if summary.reason == "error":
            return 1
        return 1 if counts.get(FAIL) else 0
    finally:
        for close in reversed(cleanup):
            try:
                close()
            except (OSError, ScopeError, InstrumentError, TransportError):
                pass


def run_sweep(args: argparse.Namespace) -> int:
    return asyncio.run(_run(args))
