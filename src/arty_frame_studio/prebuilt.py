"""Check the bundled firmware's exact source and build provenance before JTAG."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .bitstream import BitstreamError, BitstreamImage, read_bitstream

OBSOLETE_UART_SHA256 = "5849a6ffaf0cf05d3823e250ac7f6091d8c219c15194b61eabd411823e2e6b4e"


def _object(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 65_536:
            raise ValueError("Fichier de provenance trop volumineux.")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Objet JSON attendu.")
        return value
    except (OSError, ValueError) as exc:
        raise BitstreamError(f"Provenance du firmware invalide : {path.name} ({exc}).") from exc


def validate_programming_image(image: BitstreamImage, project_root: Path) -> dict[str, Any] | None:
    """Refuse the known bad UART image and check the bundled image against sources.

    Custom .bit files still receive the normal target/container checks. This
    receipt identifies the bundled file, not a design currently running on a
    board, and does not establish physical timing or hardware validation.
    """
    if image.sha256 == OBSOLETE_UART_SHA256:
        raise BitstreamError(
            "Firmware obsolète : RX et TX étaient inversés. Télécharger la version "
            "corrigée de firmware/prebuilt/arty_frame.bit avant de programmer la carte."
        )
    root = project_root.resolve()
    directory = root / "firmware" / "prebuilt"
    if image.path.resolve() != (directory / "arty_frame.bit").resolve():
        return None
    manifest = _object(directory / "firmware-manifest.json")
    if manifest.get("sha256") != image.sha256:
        raise BitstreamError("Le .bit fourni ne correspond pas au SHA256 de son manifeste.")
    if manifest.get("part") != "xc7a100tcsg324-1":
        raise BitstreamError("Le manifeste ne cible pas l'Arty A7-100T attendue.")
    if manifest.get("idcode") != f"0x{image.idcode:08X}":
        raise BitstreamError("L'IDCODE du manifeste ne correspond pas au .bit fourni.")
    source_commit = manifest.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(char not in "0123456789abcdef" for char in source_commit)
    ):
        raise BitstreamError("Le manifeste doit identifier le commit ayant produit le firmware.")
    paths = sorted(
        path
        for path in (root / "firmware").rglob("*")
        if path.is_file() and path.suffix in (".v", ".xdc")
    )
    expected_paths = {path.relative_to(root).as_posix() for path in paths}
    sources = manifest.get("source_sha256")
    if not expected_paths or not isinstance(sources, dict) or set(sources) != expected_paths:
        raise BitstreamError("Le manifeste doit couvrir toutes les sources RTL et contraintes XDC.")
    for path in paths:
        name = path.relative_to(root).as_posix()
        if hashlib.sha256(path.read_bytes()).hexdigest() != sources[name]:
            raise BitstreamError(
                f"Firmware fourni périmé : {name} ne correspond plus au manifeste. "
                "Recompiler puis remplacer le .bit et ses rapports ensemble."
            )
    timing = _object(directory / "timing.json")
    try:
        core = timing["fmax"]["core_clock"]
        achieved = float(core["achieved"])
        constraint = float(core["constraint"])
        declared = float(manifest["routed_core_fmax_mhz"])
        valid = (
            all(math.isfinite(value) for value in (achieved, constraint, declared))
            and achieved >= 200
            and constraint >= 200
            and achieved == declared
            and manifest["timing_requirement_mhz"] == 200
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BitstreamError("Rapport de timing du firmware fourni invalide.") from exc
    if not valid:
        raise BitstreamError(
            "Le rapport et le manifeste doivent confirmer le timing de cœur à 200 MHz."
        )
    return manifest


def verify_prebuilt_firmware(project_root: Path) -> dict[str, Any]:
    image = read_bitstream(project_root / "firmware" / "prebuilt" / "arty_frame.bit")
    manifest = validate_programming_image(image, project_root)
    assert manifest is not None
    return manifest
