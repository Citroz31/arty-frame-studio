"""The downloadable firmware must match all compiled sources and build reports."""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from arty_frame_studio.bitstream import BitstreamError, read_bitstream
from arty_frame_studio.prebuilt import (
    OBSOLETE_UART_SHA256,
    validate_programming_image,
    verify_prebuilt_firmware,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bundled_project(tmp_path):
    for folder in ("rtl", "constraints", "sim", "prebuilt"):
        shutil.copytree(ROOT / "firmware" / folder, tmp_path / "firmware" / folder)
    return tmp_path


def test_published_firmware_matches_sources_and_routed_timing():
    manifest = verify_prebuilt_firmware(ROOT)
    assert manifest["routed_core_fmax_mhz"] >= 200
    assert manifest["hardware_validated"] is False


def test_changed_rtl_is_rejected_before_programming(bundled_project):
    top = bundled_project / "firmware/rtl/arty_top.v"
    top.write_bytes(top.read_bytes() + b"\n// Changed since synthesis\n")
    with pytest.raises(BitstreamError, match="arty_top.v"):
        verify_prebuilt_firmware(bundled_project)


def test_changed_bitstream_is_rejected(bundled_project):
    bit = bundled_project / "firmware/prebuilt/arty_frame.bit"
    data = bytearray(bit.read_bytes())
    # Flip a frame-data byte, retaining the container/IDCODE for a valid parse.
    data[8192] ^= 1
    bit.write_bytes(data)
    with pytest.raises(BitstreamError, match="SHA256"):
        verify_prebuilt_firmware(bundled_project)


def test_missing_compiled_source_in_manifest_is_rejected(bundled_project):
    path = bundled_project / "firmware/prebuilt/firmware-manifest.json"
    manifest = json.loads(path.read_text())
    del manifest["source_sha256"]["firmware/constraints/arty_a7_100t.xdc"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(BitstreamError, match="toutes les sources"):
        verify_prebuilt_firmware(bundled_project)


@pytest.mark.parametrize("field,value", [("achieved", 199), ("constraint", 100), ("achieved", 219)])
def test_timing_failure_or_mismatched_report_is_rejected(bundled_project, field, value):
    path = bundled_project / "firmware/prebuilt/timing.json"
    timing = json.loads(path.read_text())
    timing["fmax"]["core_clock"][field] = value
    path.write_text(json.dumps(timing))
    with pytest.raises(BitstreamError, match="timing"):
        verify_prebuilt_firmware(bundled_project)


def test_known_swapped_uart_image_is_rejected_even_when_renamed(tmp_path):
    image = replace(
        read_bitstream(ROOT / "firmware/prebuilt/arty_frame.bit"),
        path=tmp_path / "renamed.bit",
        sha256=OBSOLETE_UART_SHA256,
    )
    with pytest.raises(BitstreamError, match="RX et TX"):
        validate_programming_image(image, tmp_path)


def test_custom_bitstream_does_not_claim_bundled_source_provenance(tmp_path):
    image = replace(
        read_bitstream(ROOT / "firmware/prebuilt/arty_frame.bit"),
        path=tmp_path / "custom.bit",
    )
    assert validate_programming_image(image, ROOT) is None
