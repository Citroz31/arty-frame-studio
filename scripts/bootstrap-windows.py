"""Prepare native portable FPGA tools using an ordinary Windows user account."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# This script also works from an unpacked source tree before editable install.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from arty_frame_studio.local_tools import ensure_local_toolchain  # noqa: E402
from arty_frame_studio.toolchain import ToolchainError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Installer les outils FPGA portables sous Windows x64, sans administrateur."
    )
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--tools-dir", type=Path, help="Dossier des outils (hors OneDrive conseillé)."
    )
    parser.add_argument("--config", type=Path, help="Configuration JSON à créer (toolchain.json).")
    arguments = parser.parse_args()
    try:
        ensure_local_toolchain(
            arguments.project_root,
            lambda message: print(message, flush=True),
            tools_dir=arguments.tools_dir,
            config_path=arguments.config,
        )
    except (ToolchainError, OSError, ValueError) as exc:
        print(f"Préparation impossible : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
