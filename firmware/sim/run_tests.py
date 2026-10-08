#!/usr/bin/env python3
"""Run meaningful RTL simulations; requires Icarus Verilog in PATH."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

VARIANTS = {"tb_protocol": [[], [("CORE_HALF_NS", "40.0")]]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=Path, help="Retain generated simulation files here")
    args = parser.parse_args()
    if not shutil.which("iverilog") or not shutil.which("vvp"):
        parser.error("Installer Icarus Verilog (iverilog et vvp) avant de lancer ces tests.")
    root = Path(__file__).resolve().parents[2]
    rtl = sorted((root / "firmware" / "rtl").glob("*.v"))
    models = root / "firmware" / "sim" / "xilinx_primitives_sim.v"
    tests = sorted((root / "tests" / "rtl").glob("tb_*.sv"))
    with tempfile.TemporaryDirectory(prefix="arty-rtl-") as temporary:
        output = args.keep.resolve() if args.keep else Path(temporary)
        output.mkdir(parents=True, exist_ok=True)
        # Also elaborate the complete board wrapper, using simulation-only
        # primitive models, to catch integration and port errors.
        subprocess.run(
            [
                "iverilog",
                "-g2012",
                "-Wall",
                "-s",
                "arty_top",
                "-o",
                str(output / "arty_top.vvp"),
                *map(str, rtl),
                str(models),
            ],
            check=True,
        )
        for test in tests:
            # Extra parameter sets: the protocol bench also runs a core clock
            # slower than the control clock, the other side of engine_link.
            for index, overrides in enumerate(VARIANTS.get(test.stem, [[]])):
                compiled = output / f"{test.stem}-{index}.vvp"
                subprocess.run(
                    [
                        "iverilog",
                        "-g2012",
                        "-Wall",
                        "-s",
                        test.stem,
                        *(f"-P{test.stem}.{name}={value}" for name, value in overrides),
                        "-o",
                        str(compiled),
                        *map(str, rtl),
                        str(models),
                        str(test),
                    ],
                    check=True,
                )
                # tb_top replays about 42 ms of the real 115200-baud UART at 5 ns
                # steps: minutes on a slow runner. The limit only catches a hang.
                subprocess.run(["vvp", str(compiled)], check=True, timeout=900)
    print("RTL: toutes les simulations ont réussi (aucune mesure matérielle).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
