"""Validate a firmware bundle independently of its download or programming path."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .bitstream import BitstreamImage, read_bitstream
from .firmware_config import FirmwareBuildConfig


class FirmwareArtifactError(RuntimeError):
    """The file, requested configuration and build evidence do not agree."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Rapport JSON trop volumineux.")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Objet JSON attendu.")
        return value
    except (OSError, ValueError) as exc:
        raise FirmwareArtifactError(f"{path.name} absent ou invalide : {exc}") from exc


def _number(value: object) -> float:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if math.isfinite(number):
                return number
    except OverflowError:
        pass
    raise FirmwareArtifactError("Le rapport de timing doit contenir des valeurs numériques finies.")


def validate_firmware_artifact(
    directory: Path,
    firmware: FirmwareBuildConfig,
    *,
    expected_commit: str | None = None,
    image: BitstreamImage | None = None,
) -> dict[str, Any]:
    """Check target, SHA, source identity, configuration, receipt and routed timing.

    This verifies the build evidence supplied with the file; it does not read
    the identity of a programmed board or certify the electrical output path.
    """
    image = image or read_bitstream(directory / "arty_frame.bit")
    manifest = _read_object(directory / "firmware-manifest.json")
    if manifest.get("sha256") != image.sha256:
        raise FirmwareArtifactError("Le .bit ne correspond pas au SHA256 de son manifeste.")
    if (
        manifest.get("part") != "xc7a100tcsg324-1"
        or manifest.get("idcode") != f"0x{image.idcode:08X}"
    ):
        raise FirmwareArtifactError("La cible ou l'IDCODE du manifeste ne correspond pas au .bit.")
    source = manifest.get("source_commit")
    if not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{40}", source):
        raise FirmwareArtifactError("Le manifeste doit identifier le commit source du firmware.")
    if expected_commit is not None and source != expected_commit:
        raise FirmwareArtifactError("Le commit du firmware ne correspond pas à l'exécution GitHub.")
    try:
        declared = FirmwareBuildConfig.from_dict(manifest["firmware_config"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FirmwareArtifactError("Configuration du manifeste invalide.") from exc
    if declared != firmware or manifest.get("build_id") != firmware.build_id:
        raise FirmwareArtifactError("Le firmware ne correspond pas à la configuration demandée.")

    required = firmware.core_hz / 1e6
    timing = _read_object(directory / "timing.json")
    try:
        core = timing["fmax"]["core_clock"]
        achieved = _number(core["achieved"])
        constraint = _number(core["constraint"])
        reported = _number(manifest["routed_core_fmax_mhz"])
        requirement = _number(manifest["timing_requirement_mhz"])
    except (KeyError, TypeError) as exc:
        raise FirmwareArtifactError("Rapport de timing du cœur absent ou invalide.") from exc
    if (
        achieved < required
        or constraint < required - 1e-7
        or achieved != reported
        or requirement != required
    ):
        raise FirmwareArtifactError("Le timing routé ne confirme pas l'horloge demandée.")

    receipt = _read_object(directory / "successful-build.json")
    try:
        receipt_config = FirmwareBuildConfig.from_dict(receipt["firmware_config"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FirmwareArtifactError("Configuration du reçu de compilation invalide.") from exc
    if (
        receipt_config != firmware
        or receipt.get("bitstream_sha256") != image.sha256
        or receipt.get("build_id") != firmware.build_id
        or receipt.get("part") != "xc7a100tcsg324-1"
        or receipt.get("timing_clock") != "core_clock"
        or receipt.get("timing_requirement_mhz") != required
    ):
        raise FirmwareArtifactError("Le reçu de compilation ne correspond pas au firmware.")
    return manifest
