"""Prepare a compatible firmware and snapshot UART parameters without sending them."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .bitstream import read_bitstream
from .firmware_config import FirmwareBuildConfig
from .model import PROFILE_SCHEMA, FrameConfig
from .prebuilt import validate_programming_image
from .toolchain import Toolchain, ToolchainError


@dataclass(frozen=True)
class PreparationResult:
    bitstream: Path
    firmware: FirmwareBuildConfig
    frame: FrameConfig
    snapshot: Path
    reused: bool
    frame_profile: Path


def _save_snapshot(path: Path, values: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".pilotage-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(values, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_firmware(
    project_root: Path,
    frame: FrameConfig,
    firmware: FirmwareBuildConfig,
    get_toolchain: Callable[[], Toolchain],
    *,
    log: Callable[[str], None] | None = None,
) -> PreparationResult:
    """Reuse a verified compatible image, otherwise build locally.

    Frame words, the divider, latch durations and repetitions stay UART
    parameters. They do not become an autonomous power-on transmission.
    """
    if frame.core_hz != firmware.core_hz:
        raise ValueError("La trame et le firmware doivent utiliser la même horloge de cœur.")
    root = Path(project_root).resolve()
    snapshot = root / "profiles" / "pilotage-preparation.json"
    frame_profile = root / "profiles" / "pilotage-frame.json"
    values: dict[str, object] = {
        "version": 1,
        "frame": asdict(frame),
        "firmware": firmware.to_dict(),
        "automatic_send": False,
        "bitstream": None,
    }
    # Keep the requested settings even if synthesis subsequently fails.
    _save_snapshot(snapshot, values)
    _save_snapshot(frame_profile, {"schema_version": PROFILE_SCHEMA, "frame": asdict(frame)})
    bitstream = root / "firmware" / "prebuilt" / "arty_frame.bit"
    reused = False
    if firmware.is_reference and bitstream.is_file():
        image = read_bitstream(bitstream)
        validate_programming_image(image, root)
        reused = True
        if log:
            log("Firmware fourni compatible et vérifié : aucune recompilation nécessaire.")
    else:
        chain = get_toolchain()
        try:
            chain.validate_programming(chain.bitstream)
            receipt = json.loads(chain.receipt.read_text(encoding="utf-8"))
            previous = FirmwareBuildConfig.from_dict(
                receipt.get("firmware_config", FirmwareBuildConfig().to_dict())
            )
            reused = previous == firmware
        except (OSError, ValueError, TypeError, ToolchainError):
            reused = False
        if reused:
            bitstream = chain.bitstream
            if log:
                log("Compilation locale compatible déjà validée : réutilisation du .bit.")
        else:
            if log:
                log(f"Compilation locale demandée : {firmware.summary()}.")
            bitstream = chain.build(log=log, firmware=firmware)
    image = read_bitstream(bitstream)
    values.update(bitstream=str(bitstream), bitstream_sha256=image.sha256, reused=reused)
    _save_snapshot(snapshot, values)
    if log:
        log(
            "Les paramètres de trame sont conservés pour Envoyer dans Pilotage ; "
            "aucune émission ni programmation n'a été lancée."
        )
    return PreparationResult(bitstream, firmware, frame, snapshot, reused, frame_profile)
