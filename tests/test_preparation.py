"""Prepare local firmware without implicitly programming or emitting frames."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from arty_frame_studio.bitstream import BitstreamError, read_bitstream
from arty_frame_studio.firmware_config import FirmwareBuildConfig
from arty_frame_studio.model import PROFILE_SCHEMA, FrameConfig, load_profile
from arty_frame_studio.preparation import prepare_firmware
from arty_frame_studio.toolchain import Toolchain, ToolchainConfig, ToolchainError

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_BITSTREAM = ROOT / "firmware/prebuilt/arty_frame.bit"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project with spaces"
    # Reuse the real published container and provenance rather than accepting
    # a fake .bit in the path used for the supplied firmware.
    for folder in ("rtl", "constraints", "sim", "prebuilt"):
        shutil.copytree(ROOT / "firmware" / folder, root / "firmware" / folder)
    return root


def no_toolchain() -> Toolchain:
    raise AssertionError("Preparing the supplied firmware must not install or invoke tools.")


class RecordingToolchain(Toolchain):
    """Fake synthesis, with real container and receipt validation on cache reuse.

    The copied container is only a parser fixture for these workflow tests;
    this fixture does not establish that its RTL has the requested custom PLL.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(ToolchainConfig(build_dir=root.parent / "local build cache"), root)
        self.builds: list[FirmwareBuildConfig] = []
        self.failure: ToolchainError | None = None

    def publish_fixture(self, firmware: FirmwareBuildConfig) -> None:
        self.build_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REFERENCE_BITSTREAM, self.bitstream)
        image = read_bitstream(self.bitstream)
        self.receipt.write_text(
            json.dumps(
                {
                    "version": 1,
                    "part": self.config.part,
                    "input_sha256": self._input_digest(firmware),
                    "bitstream_sha256": image.sha256,
                    "timing_clock": "core_clock",
                    "timing_requirement_mhz": firmware.core_hz / 1e6,
                    "build_id": firmware.build_id,
                    "firmware_config": firmware.to_dict(),
                }
            ),
            encoding="utf-8",
        )

    def build(
        self,
        log: Callable[[str], None] | None = None,
        firmware: FirmwareBuildConfig | None = None,
    ) -> Path:
        assert firmware is not None
        self.builds.append(firmware)
        if self.failure is not None:
            raise self.failure
        self.publish_fixture(firmware)
        return self.bitstream

    def program(self, bitstream: Path, log: Callable[[str], None] | None = None) -> None:
        raise AssertionError("Preparation must never program hardware.")


@pytest.mark.parametrize(
    "frame",
    [
        FrameConfig(word=0b10110110, bit_count=8, divider=40),
        FrameConfig(word=0, bit_count=1, divider=1, latch_ticks=200, gap_ticks=0),
        FrameConfig(
            word=0b111000,
            bit_count=6,
            divider=10,
            latch_ticks=10,
            gap_ticks=20,
            repeat_count=0,
            free_clock=True,
            lsb_first=True,
            latch_active_low=True,
        ),
    ],
)
def test_runtime_frame_changes_reuse_supplied_firmware_without_tools(
    project: Path, frame: FrameConfig
) -> None:
    supplied = project / "firmware/prebuilt/arty_frame.bit"
    original = supplied.read_bytes()
    messages: list[str] = []

    result = prepare_firmware(
        project, frame, FirmwareBuildConfig(), no_toolchain, log=messages.append
    )

    assert result.reused is True
    assert result.bitstream == supplied
    assert result.frame == frame
    assert result.firmware == FirmwareBuildConfig()
    assert supplied.read_bytes() == original
    assert not (project / "build").exists()
    assert any("aucune recompilation" in message for message in messages)
    assert "aucune émission ni programmation" in messages[-1]


def test_preparation_saves_a_reloadable_profile_and_separate_hardware_snapshot(
    project: Path,
) -> None:
    frame = FrameConfig(word=0b10100100, bit_count=8, divider=20, repeat_count=0)
    result = prepare_firmware(project, frame, FirmwareBuildConfig(), no_toolchain)

    assert result.frame_profile == project / "profiles/pilotage-frame.json"
    assert load_profile(result.frame_profile) == frame
    profile = json.loads(result.frame_profile.read_text(encoding="utf-8"))
    assert profile == {"schema_version": PROFILE_SCHEMA, "frame": asdict(frame)}
    snapshot = json.loads(result.snapshot.read_text(encoding="utf-8"))
    assert snapshot["frame"] == asdict(frame)
    assert snapshot["firmware"] == FirmwareBuildConfig().to_dict()
    assert snapshot["automatic_send"] is False
    assert snapshot["bitstream"] == str(result.bitstream)
    assert snapshot["bitstream_sha256"] == read_bitstream(result.bitstream).sha256
    assert snapshot["reused"] is True
    assert not list(result.snapshot.parent.glob(".pilotage-*"))


def test_preparing_custom_hardware_builds_once_then_reuses_for_new_runtime_frame(
    project: Path,
) -> None:
    firmware = FirmwareBuildConfig(core_hz=150_000_000, data_pin="JC1", clock_pin="JC3")
    chain = RecordingToolchain(project)
    frame = FrameConfig(core_hz=firmware.core_hz, divider=15)

    first = prepare_firmware(project, frame, firmware, lambda: chain)
    second_frame = replace(frame, word=0b101010101, bit_count=9, divider=30, repeat_count=0)
    second = prepare_firmware(project, second_frame, firmware, lambda: chain)

    assert first.reused is False
    assert second.reused is True
    assert chain.builds == [firmware]
    assert first.bitstream == second.bitstream == chain.bitstream
    assert load_profile(second.frame_profile) == second_frame
    assert json.loads(second.snapshot.read_text())["frame"] == asdict(second_frame)


def test_changed_hardware_requires_a_new_build_even_when_old_cache_is_valid(project: Path) -> None:
    old = FirmwareBuildConfig(core_hz=150_000_000)
    new = replace(old, data_pin="JC1", latch_pin="JC3")
    chain = RecordingToolchain(project)
    chain.publish_fixture(old)
    # The cache is genuine according to its own recorded hardware request,
    # but it cannot serve the newly selected output pins.
    assert chain.validate_programming(chain.bitstream)

    result = prepare_firmware(project, FrameConfig(core_hz=new.core_hz), new, lambda: chain)

    assert result.reused is False
    assert chain.builds == [new]
    receipt = json.loads(chain.receipt.read_text())
    assert FirmwareBuildConfig.from_dict(receipt["firmware_config"]) == new


@pytest.mark.parametrize("damage", ["missing", "invalid_json", "wrong_sha", "changed_rtl"])
def test_missing_or_stale_local_receipt_requires_rebuilding(project: Path, damage: str) -> None:
    firmware = FirmwareBuildConfig(core_hz=150_000_000)
    chain = RecordingToolchain(project)
    chain.publish_fixture(firmware)
    if damage == "missing":
        chain.receipt.unlink()
    elif damage == "invalid_json":
        chain.receipt.write_text("{incomplete", encoding="utf-8")
    elif damage == "wrong_sha":
        values = json.loads(chain.receipt.read_text())
        values["bitstream_sha256"] = "0" * 64
        chain.receipt.write_text(json.dumps(values), encoding="utf-8")
    else:
        top = project / "firmware/rtl/arty_top.v"
        top.write_bytes(top.read_bytes() + b"\n// Edited since synthesis\n")

    result = prepare_firmware(
        project, FrameConfig(core_hz=firmware.core_hz), firmware, lambda: chain
    )

    assert result.reused is False
    assert chain.builds == [firmware]
    assert chain.validate_programming(result.bitstream)


def test_missing_supplied_bitstream_can_reuse_a_valid_local_reference_build(project: Path) -> None:
    firmware = FirmwareBuildConfig()
    chain = RecordingToolchain(project)
    chain.publish_fixture(firmware)
    (project / "firmware/prebuilt/arty_frame.bit").unlink()

    result = prepare_firmware(project, FrameConfig(), firmware, lambda: chain)

    assert result.reused is True
    assert result.bitstream == chain.bitstream
    assert chain.builds == []


def test_legacy_local_reference_receipt_remains_reusable(project: Path) -> None:
    chain = RecordingToolchain(project)
    chain.publish_fixture(FirmwareBuildConfig())
    receipt = json.loads(chain.receipt.read_text())
    del receipt["firmware_config"]
    del receipt["build_id"]
    chain.receipt.write_text(json.dumps(receipt), encoding="utf-8")
    (project / "firmware/prebuilt/arty_frame.bit").unlink()

    result = prepare_firmware(project, FrameConfig(), FirmwareBuildConfig(), lambda: chain)

    assert result.reused is True
    assert chain.builds == []


def test_clock_mismatch_is_rejected_before_tools_or_profile_changes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="même horloge"):
        prepare_firmware(
            tmp_path / "absent project",
            FrameConfig(core_hz=200_000_000),
            FirmwareBuildConfig(core_hz=150_000_000),
            no_toolchain,
        )
    assert list(tmp_path.iterdir()) == []


def test_requested_settings_survive_failed_build_without_claiming_a_prepared_bitstream(
    project: Path,
) -> None:
    firmware = FirmwareBuildConfig(core_hz=150_000_000, data_pin="JC1")
    frame = FrameConfig(core_hz=firmware.core_hz, word=0b1101, bit_count=4)
    chain = RecordingToolchain(project)
    chain.failure = ToolchainError("routing failed")

    with pytest.raises(ToolchainError, match="routing failed"):
        prepare_firmware(project, frame, firmware, lambda: chain)

    assert chain.builds == [firmware]
    assert load_profile(project / "profiles/pilotage-frame.json") == frame
    snapshot = json.loads((project / "profiles/pilotage-preparation.json").read_text())
    assert snapshot["frame"] == asdict(frame)
    assert snapshot["firmware"] == firmware.to_dict()
    assert snapshot["automatic_send"] is False
    assert snapshot["bitstream"] is None
    assert "reused" not in snapshot
    assert "bitstream_sha256" not in snapshot


@pytest.mark.parametrize("damage", ["invalid_container", "modified_bitstream", "changed_rtl"])
def test_invalid_supplied_firmware_is_refused_without_silent_tool_install(
    project: Path, damage: str
) -> None:
    bitstream = project / "firmware/prebuilt/arty_frame.bit"
    if damage == "invalid_container":
        bitstream.write_bytes(b"not a configuration container")
    elif damage == "modified_bitstream":
        raw = bytearray(bitstream.read_bytes())
        raw[8192] ^= 1
        bitstream.write_bytes(raw)
    else:
        top = project / "firmware/rtl/arty_top.v"
        top.write_bytes(top.read_bytes() + b"\n// Changed since published\n")

    with pytest.raises(BitstreamError):
        prepare_firmware(project, FrameConfig(), FirmwareBuildConfig(), no_toolchain)

    snapshot = json.loads((project / "profiles/pilotage-preparation.json").read_text())
    assert snapshot["bitstream"] is None
    assert load_profile(project / "profiles/pilotage-frame.json") == FrameConfig()
