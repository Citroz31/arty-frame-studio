from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from arty_frame_studio.toolchain import Toolchain, ToolchainConfig, ToolchainError

FAKE_TOOL = r"""#!/usr/bin/env python3
import json
import os
import shlex
import sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["FAKE_HISTORY"], "a") as stream:
    stream.write(json.dumps([name, *args]) + "\n")
if os.environ.get("FAKE_FAIL") == name:
    print("intentional failure", file=sys.stderr)
    sys.exit(23)
if os.environ.get("FAKE_SKIP_OUTPUT") == name:
    sys.exit(0)
if name == "yosys":
    path = shlex.split(args[-1].split("write_json ", 1)[1])[0]
    Path(path).write_text('{"modules": {"arty_top": {}}}')
elif name == "nextpnr-xilinx":
    Path(args[args.index("--fasm") + 1]).write_text("CLBLL_L_X1Y1.SLICEL_X0.A5LUT.INIT[0] = 1\n")
    default_timing = "Info: Max frequency for clock 'core_clock': "
    default_timing += "210.52 MHz (PASS at 200.00 MHz)"
    print(os.environ.get("FAKE_TIMING", default_timing))
elif name == "fasm2frames":
    print("0x00000000 0x00000001")
    print("diagnostic only", file=sys.stderr)
    if os.environ.get("FAKE_MUTATE_SOURCE"):
        Path(os.environ["FAKE_MUTATE_SOURCE"]).write_text("changed during build")
elif name == "xc7frames2bit":
    Path(args[args.index("--output_file") + 1]).write_bytes(b"fresh bitstream bytes")
else:
    print("SRAM programming completed")
"""


@pytest.fixture
def toolchain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toolchain:
    root = tmp_path / "project with spaces"
    (root / "firmware/rtl").mkdir(parents=True)
    (root / "firmware/constraints").mkdir(parents=True)
    (root / "firmware/rtl/arty_top.v").write_text("module arty_top; endmodule\n")
    (root / "firmware/constraints/arty_a7_100t.xdc").write_text(
        "create_clock -period 5 [get_nets core_clock]\n"
    )
    tools = tmp_path / "fake tools"
    tools.mkdir()
    for name in ("yosys", "nextpnr-xilinx", "fasm2frames", "xc7frames2bit", "openFPGALoader"):
        path = tools / name
        path.write_text(FAKE_TOOL.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        path.chmod(0o755)
    db = tmp_path / "database/artix7"
    (db / "xc7a100tcsg324-1").mkdir(parents=True)
    (db / "xc7a100tcsg324-1/part.yaml").write_text("idcode: 0x03631093\n")
    chipdb = tmp_path / "xc7a100tcsg324-1.bin"
    chipdb.write_bytes(b"external chip database fixture")
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("FAKE_HISTORY", str(tmp_path / "history.jsonl"))
    return Toolchain(ToolchainConfig(chipdb=chipdb, prjxray_db=db), root)


def history() -> list[list[str]]:
    return [json.loads(line) for line in Path(os.environ["FAKE_HISTORY"]).read_text().splitlines()]


def test_complete_flow_preserves_frames_and_programs_sram(toolchain: Toolchain) -> None:
    logs: list[str] = []
    assert all(result.ok for result in toolchain.doctor())
    bitstream = toolchain.build(log=logs.append)
    assert bitstream == toolchain.project_root / "build/arty_frame.bit"
    assert bitstream.read_bytes() == b"fresh bitstream bytes"
    assert toolchain.receipt.exists()
    frames = next((toolchain.build_dir / "runs").glob("*/arty_frame.frames"))
    assert frames.read_text() == "0x00000000 0x00000001\n"
    assert "diagnostic only" in (toolchain.build_dir / "build.log").read_text()
    assert logs[0].startswith("$ yosys ")
    toolchain.program(bitstream)
    calls = history()
    assert [call[0] for call in calls] == [
        "yosys",
        "nextpnr-xilinx",
        "fasm2frames",
        "xc7frames2bit",
        "openFPGALoader",
    ]
    assert "--timing-allow-fail" not in calls[1]
    assert "synth_xilinx -family xc7 -flatten -nodram -top arty_top" in calls[0][-1]
    assert calls[1][calls[1].index("--freq") + 1] == "200"
    assert calls[2][calls[2].index("--part") + 1] == "xc7a100tcsg324-1"
    assert calls[3][calls[3].index("--part_name") + 1] == "xc7a100tcsg324-1"
    assert calls[-1] == ["openFPGALoader", "-b", "arty_a7_100t", str(bitstream)]


@pytest.mark.parametrize("stage", ["yosys", "nextpnr-xilinx", "fasm2frames", "xc7frames2bit"])
def test_stage_failure_revokes_previous_build(
    toolchain: Toolchain,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    old = toolchain.build()
    monkeypatch.setenv("FAKE_FAIL", stage)
    with pytest.raises(ToolchainError, match="code 23"):
        toolchain.build()
    assert old.exists()  # Bytes stay available for inspection; receipt is revoked.
    assert not toolchain.receipt.exists()
    with pytest.raises(ToolchainError, match="périmé"):
        toolchain.program(old)
    assert history()[-1][0] == stage


@pytest.mark.parametrize(
    "timing",
    [
        "No timing report available",
        "Max frequency for clock 'core_clock': 199.00 MHz (FAIL at 200.00 MHz)",
        "Max frequency for clock 'core_clock': 250.00 MHz (PASS at 100.00 MHz)",
        "Max frequency for clock 'different_clock': 250.00 MHz (PASS at 200.00 MHz)",
        "Max frequency for clock 'core_clock': 250.00 MHz (PASS at 200.00 MHz)\n"
        "Max frequency for clock 'core_clock': 180.00 MHz (FAIL at 200.00 MHz)",
        "Max frequency for clock 'core_clock': 250.00 MHz (PASS at 200.00 MHz)\n"
        "Max frequency for clock 'other': 80.00 MHz (FAIL at 100.00 MHz)",
    ],
)
def test_missing_or_failed_core_timing_blocks_bit_generation(
    toolchain: Toolchain,
    monkeypatch: pytest.MonkeyPatch,
    timing: str,
) -> None:
    monkeypatch.setenv("FAKE_TIMING", timing)
    with pytest.raises(ToolchainError, match="Timing 200 MHz"):
        toolchain.build()
    assert not toolchain.receipt.exists()
    assert [call[0] for call in history()] == ["yosys", "nextpnr-xilinx"]


@pytest.mark.parametrize("stage", ["yosys", "nextpnr-xilinx", "fasm2frames", "xc7frames2bit"])
def test_empty_outputs_cannot_reuse_stale_artifact(
    toolchain: Toolchain,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    toolchain.build()
    monkeypatch.setenv("FAKE_SKIP_OUTPUT", stage)
    with pytest.raises(ToolchainError, match="sortie non vide"):
        toolchain.build()
    assert not toolchain.receipt.exists()


@pytest.mark.parametrize("modification", ["source", "constraints", "bitstream"])
def test_modified_inputs_or_artifact_block_programming(
    toolchain: Toolchain,
    modification: str,
) -> None:
    bitstream = toolchain.build()
    path = {
        "source": toolchain._sources()[0],
        "constraints": toolchain.constraints,
        "bitstream": bitstream,
    }[modification]
    path.write_bytes(path.read_bytes() + b"modified")
    with pytest.raises(ToolchainError, match="périmé"):
        toolchain.program(bitstream)
    assert history()[-1][0] == "xc7frames2bit"


def test_program_cannot_load_external_unvalidated_file(
    toolchain: Toolchain, tmp_path: Path
) -> None:
    external = tmp_path / "external.bit"
    external.write_bytes(b"unknown")
    with pytest.raises(ToolchainError, match="validé de ce projet"):
        toolchain.program(external)


def test_missing_dependency_also_revokes_receipt(toolchain: Toolchain) -> None:
    bitstream = toolchain.build()
    assert toolchain.config.chipdb is not None
    toolchain.config.chipdb.unlink()
    with pytest.raises(ToolchainError, match="chipdb"):
        toolchain.build()
    with pytest.raises(ToolchainError, match="périmé"):
        toolchain.program(bitstream)


def test_exclusive_build_program_lock(toolchain: Toolchain) -> None:
    toolchain.build_dir.mkdir()
    (toolchain.build_dir / ".toolchain.lock").write_text("active")
    with pytest.raises(ToolchainError, match="déjà active"):
        toolchain.build()


def test_sources_changed_during_compilation_revoke_build(
    toolchain: Toolchain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_MUTATE_SOURCE", str(toolchain._sources()[0]))
    with pytest.raises(ToolchainError, match="changé pendant"):
        toolchain.build()
    assert not toolchain.receipt.exists()
    assert not toolchain.bitstream.exists()


def test_program_error_is_visible_and_logged(
    toolchain: Toolchain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bitstream = toolchain.build()
    monkeypatch.setenv("FAKE_FAIL", "openFPGALoader")
    with pytest.raises(ToolchainError, match="code 23"):
        toolchain.program(bitstream)
    assert "intentional failure" in (toolchain.build_dir / "program.log").read_text()


def test_json_paths_and_interpreter_arguments(tmp_path: Path) -> None:
    path = tmp_path / "toolchain.json"
    path.write_text(
        json.dumps(
            {
                "chipdb": "db/chip.bin",
                "prjxray_db": "db/artix7",
                "fasm2frames": ["python3", "/opt/xray/fasm2frames.py"],
                "build_dir": "build",
            }
        )
    )
    config = ToolchainConfig.from_json(path)
    assert config.chipdb == tmp_path / "db/chip.bin"
    assert config.prjxray_db == tmp_path / "db/artix7"
    assert config.build_dir == tmp_path / "build"
    assert config.fasm2frames == ("python3", "/opt/xray/fasm2frames.py")


@pytest.mark.parametrize(
    "data",
    [
        {"part": "xc7a35tcsg324-1"},
        {"extra_flag": "--timing-allow-fail"},
        {"nextpnr_xilinx": ["nextpnr-xilinx", "--timing-allow-fail"]},
        {"openfpgaloader": ["openFPGALoader", "--write-flash"]},
        {"yosys": []},
        {"yosys": [123]},
        {"chipdb": ""},
        {"build_dir": None},
        [],
    ],
)
def test_invalid_or_unsafe_configuration_rejected(tmp_path: Path, data: object) -> None:
    path = tmp_path / "toolchain.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        ToolchainConfig.from_json(path)


def test_argument_with_shell_metacharacters_is_literal(
    toolchain: Toolchain, tmp_path: Path
) -> None:
    executable = tmp_path / "literal; touch injected"
    executable.write_text("#!" + sys.executable + "\nprint('literal command')\n")
    executable.chmod(0o755)
    changed = ToolchainConfig(
        chipdb=toolchain.config.chipdb,
        prjxray_db=toolchain.config.prjxray_db,
        openfpgaloader=str(executable),
    )
    candidate = Toolchain(changed, toolchain.project_root)
    candidate.program(candidate.build())
    assert not (toolchain.project_root / "injected").exists()
