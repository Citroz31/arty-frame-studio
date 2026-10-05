from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from dataclasses import replace
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
    cells = {}
    for index, cell_name in enumerate(("data_ddr", "clock_ddr", "latch_ddr")):
        cells[cell_name] = {
            "type": "ODDR",
            "parameters": {"INIT": "0", "SRTYPE": "ASYNC", "DDR_CLK_EDGE": "SAME_EDGE"},
            "attributes": {"src": "firmware/rtl/arty_top.v"},
            "port_directions": {"C": "input", "D1": "input", "D2": "input", "CE": "input",
                                "R": "input", "S": "input", "Q": "output"},
            "connections": {"C": [10], "D1": [11], "D2": [12], "CE": ["1"],
                            "R": [42], "S": ["0"], "Q": [100 + index]},
        }
    cells["reset_flop"] = {"type": "FDCE", "connections": {"CLR": [43]}}
    netlist = {"creator": "fake-yosys", "modules": {"arty_top": {
        "cells": cells, "netnames": {"reset": {"bits": [42]}}}}}
    Path(path).write_text(os.environ.get("FAKE_NETLIST", json.dumps(netlist)))
elif name == "nextpnr-xilinx":
    fasm = (args[args.index("--fasm") + 1] if "--fasm" in args
            else next(arg[5:] for arg in args if arg.startswith("fasm=")))
    seed = args[args.index("--seed") + 1] if "--seed" in args else "none"
    skipped = (os.environ.get("FAKE_SKIP_ROUTE_KIND")
               if os.environ.get("FAKE_SKIP_ROUTE_SEED") == seed else None)
    if skipped != "fasm":
        Path(fasm).write_text(f"CLBLL_L_X1Y1.SLICEL_X0.A5LUT.INIT[0] = 1\n# seed {seed}\n")
    if "--report" in args and skipped != "timing":
        Path(args[args.index("--report") + 1]).write_text(json.dumps({"fmax": {}, "seed": seed}))
    if "--write" in args and skipped != "routed":
        source = json.loads(Path(args[args.index("--json") + 1]).read_text())
        cells = {}
        for cell_name, cell in source["modules"]["arty_top"]["cells"].items():
            if cell["type"] != "ODDR":
                continue
            cells[cell_name] = {
                "type": "OLOGICE3_OUTFF", "parameters": cell["parameters"],
                "attributes": {"X_ORIG_TYPE": "ODDR", "X_ORIG_PORT_SR": "R"},
                "connections": {"SR": [901]},
            }
        routed = {"seed": seed, "modules": {"top": {"cells": cells,
                                     "netnames": {"reset": {"bits": [901]}}}}}
        fault = os.environ.get("FAKE_ROUTED_FAULT")
        if fault == "wrong_reset":
            cells["data_ddr"]["connections"]["SR"] = ["0"]
        elif fault == "set_port":
            cells["clock_ddr"]["attributes"]["X_ORIG_PORT_SR"] = "S"
        elif fault == "missing_oddr":
            del cells["latch_ddr"]
        elif fault == "missing_reset":
            del routed["modules"]["top"]["netnames"]["reset"]
        elif fault == "wrong_mode":
            cells["latch_ddr"]["parameters"]["SRTYPE"] = "SYNC"
        Path(args[args.index("--write") + 1]).write_text(json.dumps(routed))
    default_timing = "Info: Max frequency for clock 'core_clock': "
    default_timing += "210.52 MHz (PASS at 200.00 MHz)"
    timing = os.environ.get("FAKE_TIMING", default_timing)
    timing = os.environ.get(f"FAKE_TIMING_SEED_{seed}", timing)
    print(timing)
    if os.environ.get("FAKE_TIMING_EXIT") and "FAIL" in timing:
        sys.exit(1)
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
    if os.name == "nt":
        pytest.skip("Ces exécutables de test utilisent des shebangs Unix ; voir les tests natifs.")
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
    assert calls[0][-1].startswith("scratchpad -set abc.exe yosys-abc;")
    assert "synth_xilinx -family xc7 -flatten -nodram -abc9 -top arty_top" in calls[0][-1]
    assert calls[1][calls[1].index("--freq") + 1] == "200"
    assert calls[2][calls[2].index("--part") + 1] == "xc7a100tcsg324-1"
    assert calls[3][calls[3].index("--part_name") + 1] == "xc7a100tcsg324-1"
    assert calls[-1] == ["openFPGALoader", "-b", "arty_a7_100t", str(bitstream)]


def test_himbaechel_build_uses_device_vopts_and_report(toolchain: Toolchain) -> None:
    chain = Toolchain(
        replace(toolchain.config, nextpnr_backend="himbaechel"), toolchain.project_root
    )
    assert chain.build().is_file()
    place_route = history()[1]
    assert "--xdc" not in place_route and "--fasm" not in place_route
    assert place_route[place_route.index("--device") + 1] == "xc7a100tcsg324-1"
    assert f"xdc={chain.constraints}" in place_route
    assert any(arg.startswith("fasm=") for arg in place_route)
    assert "--report" in place_route
    assert "--write" in place_route
    assert "--timing-allow-fail" not in place_route
    original = next((chain.build_dir / "runs").glob("*/arty_frame.json"))
    normalized = original.with_name("arty_frame.himbaechel.json")
    before = json.loads(original.read_text())
    expected = deepcopy(before)
    for cell in expected["modules"]["arty_top"]["cells"].values():
        if cell["type"] == "ODDR":
            del cell["connections"]["S"]
            del cell["port_directions"]["S"]
    assert json.loads(normalized.read_text()) == expected
    assert place_route[place_route.index("--json") + 1] == str(normalized)
    journal = (chain.build_dir / "build.log").read_text()
    assert "3 connexion(s) S inactive(s) retirée(s), R conservé" in journal
    assert "Reset R→SR vérifié sur DATA/CLK/LATCH" in journal


def test_classic_build_preserves_oddr_set_and_reset_ports(toolchain: Toolchain) -> None:
    toolchain.build()
    original = next((toolchain.build_dir / "runs").glob("*/arty_frame.json"))
    for cell in json.loads(original.read_text())["modules"]["arty_top"]["cells"].values():
        if cell["type"] == "ODDR":
            assert cell["connections"]["S"] == ["0"]
            assert cell["connections"]["R"] == [42]
            assert cell["port_directions"]["S"] == "input"
    assert not original.with_name("arty_frame.himbaechel.json").exists()
    place_route = history()[1]
    assert place_route[place_route.index("--json") + 1] == str(original)
    assert "--write" not in place_route


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("connections", "S", ["1"]),
        ("connections", "S", ["x"]),
        ("connections", "S", [101]),
        ("connections", "R", []),
        ("connections", "R", ["x"]),
        ("parameters", "INIT", "1"),
        ("parameters", "INIT", "x"),
        ("parameters", "SRTYPE", "SYNC"),
        ("parameters", "DDR_CLK_EDGE", "OPPOSITE_EDGE"),
    ],
)
def test_himbaechel_normalization_rejects_unsupported_oddr_modes(
    toolchain: Toolchain,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    value: object,
) -> None:
    # Obtain the fixture netlist, then make exactly one unsupported ODDR change.
    toolchain.build()
    original = next((toolchain.build_dir / "runs").glob("*/arty_frame.json"))
    changed = json.loads(original.read_text())
    changed["modules"]["arty_top"]["cells"]["data_ddr"][section][field] = value
    monkeypatch.setenv("FAKE_NETLIST", json.dumps(changed))
    chain = Toolchain(
        replace(toolchain.config, nextpnr_backend="himbaechel"), toolchain.project_root
    )
    with pytest.raises(ToolchainError, match="Normalisation ODDR himbaechel refusée"):
        chain.build()
    assert not chain.receipt.exists()
    assert history()[-1][0] == "yosys"
    assert not list((chain.build_dir / "runs").glob("*/arty_frame.himbaechel.json"))


@pytest.mark.parametrize(
    "fault", ["wrong_reset", "set_port", "missing_oddr", "missing_reset", "wrong_mode"]
)
def test_himbaechel_packer_must_preserve_all_oddr_resets(
    toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    monkeypatch.setenv("FAKE_ROUTED_FAULT", fault)
    chain = Toolchain(
        replace(toolchain.config, nextpnr_backend="himbaechel"), toolchain.project_root
    )
    with pytest.raises(ToolchainError, match="ODDR"):
        chain.build()
    assert [call[0] for call in history()] == ["yosys", "nextpnr-xilinx"]
    assert not chain.receipt.exists()
    assert not chain.bitstream.exists()


def test_unsupported_nextpnr_backend_is_rejected() -> None:
    with pytest.raises(ValueError, match="nextpnr_backend"):
        ToolchainConfig(nextpnr_backend="unsupported")  # type: ignore[arg-type]


def test_routed_timing_supersedes_placement_estimate(
    toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FAKE_TIMING",
        "Info: Max frequency for clock 'core_clock': 180.00 MHz (FAIL at 200.00 MHz)\n"
        "Info: Max frequency for clock 'core_clock': 210.52 MHz (PASS at 200.00 MHz)",
    )
    assert toolchain.build().is_file()


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
    # Every configured placement seed is tried before the build is refused.
    seeds = toolchain.config.nextpnr_seeds
    calls = history()
    assert [call[0] for call in calls] == ["yosys", *["nextpnr-xilinx"] * len(seeds)]
    assert [call[call.index("--seed") + 1] for call in calls[1:]] == [str(s) for s in seeds]


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
    script = tmp_path / "xray" / "fasm2frames.py"
    path.write_text(
        json.dumps(
            {
                "chipdb": "db/chip.bin",
                "prjxray_db": "db/artix7",
                "fasm2frames": ["python3", str(script)],
                "build_dir": "build",
            }
        )
    )
    config = ToolchainConfig.from_json(path)
    assert config.chipdb == tmp_path / "db/chip.bin"
    assert config.prjxray_db == tmp_path / "db/artix7"
    assert config.build_dir == tmp_path / "build"
    assert config.fasm2frames == ("python3", str(script))


def test_portable_tool_paths_resolve_from_json_directory(tmp_path: Path) -> None:
    folder = tmp_path / "native tools with spaces"
    folder.mkdir()
    path = folder / "toolchain.json"
    path.write_text(
        json.dumps(
            {
                "yosys": "tools/bin/yosys.exe",
                "nextpnr_xilinx": "tools/bin/nextpnr-xilinx.exe",
                "fasm2frames": [".venv/Scripts/python.exe", "tools/prjxray/utils/fasm2frames.py"],
                "xc7frames2bit": "tools/bin/xc7frames2bit.exe",
                "openfpgaloader": "tools/bin/openFPGALoader.exe",
            }
        )
    )
    config = ToolchainConfig.from_json(path)
    assert config.yosys == str(folder / "tools/bin/yosys.exe")
    assert config.fasm2frames == (
        str(folder / ".venv/Scripts/python.exe"),
        str(folder / "tools/prjxray/utils/fasm2frames.py"),
    )
    assert config.nextpnr_xilinx == str(folder / "tools/bin/nextpnr-xilinx.exe")
    assert config.openfpgaloader == str(folder / "tools/bin/openFPGALoader.exe")


def test_native_process_keeps_paths_and_metacharacters_literal(tmp_path: Path) -> None:
    candidate = Toolchain(ToolchainConfig(), tmp_path)
    arguments = ["with spaces", "literal;value", r"C:\Arty tools\frame.bit"]
    with (tmp_path / "native-process.log").open("w", encoding="utf-8") as journal:
        output = candidate._run(
            [sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *arguments],
            None,
            journal,
        )
    assert json.loads(output) == arguments


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


def test_custom_firmware_build_uses_generated_constraints_and_parameters(
    toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arty_frame_studio.firmware_config import FirmwareBuildConfig

    firmware = FirmwareBuildConfig(
        core_hz=150_000_000, data_pin="JC3", clock_pin="JC1", latch_pin="JC7"
    )
    monkeypatch.setenv(
        "FAKE_TIMING", "Info: Max frequency for clock 'core_clock': 171.20 MHz (PASS at 150.00 MHz)"
    )
    committed = toolchain.constraints.read_text()
    bitstream = toolchain.build(firmware=firmware)
    yosys, nextpnr = history()[:2]
    for name, value in firmware.yosys_parameters().items():
        assert f"chparam -set {name} {value} arty_top;" in yosys[-1]
    assert nextpnr[nextpnr.index("--freq") + 1] == "150"
    xdc = Path(nextpnr[nextpnr.index("--xdc") + 1])
    assert xdc.parent.parent.name == "runs" and xdc.read_text() == firmware.xdc()
    assert toolchain.constraints.read_text() == committed
    receipt = json.loads(toolchain.receipt.read_text())
    assert receipt["timing_requirement_mhz"] == 150
    assert receipt["build_id"] == firmware.build_id
    assert FirmwareBuildConfig.from_dict(receipt["firmware_config"]) == firmware
    toolchain.program(bitstream)


def test_custom_firmware_requires_timing_at_its_own_core_clock(
    toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arty_frame_studio.firmware_config import FirmwareBuildConfig

    monkeypatch.setenv(
        "FAKE_TIMING", "Info: Max frequency for clock 'core_clock': 175.00 MHz (PASS at 160.00 MHz)"
    )
    with pytest.raises(ToolchainError, match="Timing 180 MHz"):
        toolchain.build(firmware=FirmwareBuildConfig(core_hz=180_000_000))
    assert not toolchain.receipt.exists()


def test_reference_build_keeps_committed_constraints_without_parameters(
    toolchain: Toolchain,
) -> None:
    toolchain.build()
    yosys, nextpnr = history()[:2]
    assert "chparam" not in yosys[-1]
    assert nextpnr[nextpnr.index("--xdc") + 1] == str(toolchain.constraints)
    assert json.loads(toolchain.receipt.read_text())["build_id"] == 0


def test_placement_seed_sweep_keeps_the_first_seed_that_meets_timing(
    toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = "ERROR: Max frequency for clock 'core_clock': 192.09 MHz (FAIL at 200.00 MHz)"
    monkeypatch.setenv("FAKE_TIMING", failing)
    monkeypatch.setenv("FAKE_TIMING_EXIT", "1")  # real nextpnr exits 1 on a timing miss
    monkeypatch.setenv(
        "FAKE_TIMING_SEED_3",
        "Info: Max frequency for clock 'core_clock': 214.00 MHz (PASS at 200.00 MHz)",
    )
    logs: list[str] = []
    assert toolchain.build(log=logs.append).is_file()
    calls = history()
    assert [call[call.index("--seed") + 1] for call in calls if call[0] == "nextpnr-xilinx"] == [
        "1",
        "2",
        "3",
    ]
    assert json.loads(toolchain.receipt.read_text())["nextpnr_seed"] == 3
    assert sum("non atteint" in line for line in logs) == 2
    assert any("graine 3 retenue : 214.00 MHz" in line for line in logs)
    assert (
        "graine 2 : timing 200 MHz non atteint" in (toolchain.build_dir / "build.log").read_text()
    )


def test_seed_sweep_continues_until_the_timing_margin(
    toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 201 MHz passes 200 MHz but not the 3 % margin (206 MHz): keep sweeping.
    monkeypatch.setenv(
        "FAKE_TIMING", "Info: Max frequency for clock 'core_clock': 201.00 MHz (PASS at 200.00 MHz)"
    )
    monkeypatch.setenv(
        "FAKE_TIMING_SEED_3",
        "Info: Max frequency for clock 'core_clock': 207.10 MHz (PASS at 200.00 MHz)",
    )
    logs: list[str] = []
    toolchain.build(log=logs.append)
    seeds = [call[call.index("--seed") + 1] for call in history() if call[0] == "nextpnr-xilinx"]
    assert seeds == ["1", "2", "3"]
    assert json.loads(toolchain.receipt.read_text())["nextpnr_seed"] == 3
    assert any("graine 3 retenue : 207.10 MHz" in line for line in logs)
    assert not list(toolchain.build_dir.rglob("*.best"))


def test_best_passing_seed_is_restored_when_none_reaches_the_margin(
    toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = "ERROR: Max frequency for clock 'core_clock': 192.09 MHz (FAIL at 200.00 MHz)"
    monkeypatch.setenv("FAKE_TIMING", failing)
    monkeypatch.setenv("FAKE_TIMING_EXIT", "1")
    for seed, mhz in ((2, "200.04"), (4, "204.50"), (6, "202.00")):
        monkeypatch.setenv(
            f"FAKE_TIMING_SEED_{seed}",
            f"Info: Max frequency for clock 'core_clock': {mhz} MHz (PASS at 200.00 MHz)",
        )
    logs: list[str] = []
    toolchain.build(log=logs.append)
    seeds = [call[call.index("--seed") + 1] for call in history() if call[0] == "nextpnr-xilinx"]
    assert seeds == [str(seed) for seed in range(1, 9)]
    assert json.loads(toolchain.receipt.read_text())["nextpnr_seed"] == 4
    # The FASM used for the bitstream is the one routed with seed 4.
    fasm = next(toolchain.build_dir.rglob("*.fasm"))
    assert "# seed 4" in fasm.read_text()
    assert any("graine 4 retenue, la meilleure : 204.50 MHz" in line for line in logs)
    assert not list(toolchain.build_dir.rglob("*.best"))


def test_himbaechel_fallback_restores_one_coherent_route(
    toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
) -> None:
    toolchain.config = replace(
        toolchain.config, nextpnr_backend="himbaechel", nextpnr_seeds=(1, 2, 3)
    )
    monkeypatch.setenv(
        "FAKE_TIMING",
        "ERROR: Max frequency for clock 'core_clock': 192.09 MHz (FAIL at 200.00 MHz)",
    )
    monkeypatch.setenv("FAKE_TIMING_EXIT", "1")
    for seed, mhz in ((1, "201.00"), (2, "204.50")):
        monkeypatch.setenv(
            f"FAKE_TIMING_SEED_{seed}",
            f"Info: Max frequency for clock 'core_clock': {mhz} MHz (PASS at 200.00 MHz)",
        )
    toolchain.build()
    receipt = json.loads(toolchain.receipt.read_text())
    run = Path(receipt["run_dir"])
    assert receipt["nextpnr_seed"] == 2
    assert "# seed 2" in (run / "arty_frame.fasm").read_text()
    assert json.loads((run / "timing.json").read_text())["seed"] == "2"
    assert json.loads((run / "routed.json").read_text())["seed"] == "2"
    assert not list(run.glob("*.best"))
    attempted_seeds = [
        call[call.index("--seed") + 1] for call in history() if call[0] == "nextpnr-xilinx"
    ]
    assert attempted_seeds == [
        "1",
        "2",
        "3",
    ]


@pytest.mark.parametrize("omitted", ["fasm", "timing", "routed"])
def test_seed_cannot_reuse_an_earlier_himbaechel_output(
    toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch, omitted: str
) -> None:
    # Seed 1 saves a passing route. Seed 2 reports a better PASS but omits
    # one file: it must not acquire seed 1's still-existing artifact.
    toolchain.config = replace(toolchain.config, nextpnr_backend="himbaechel", nextpnr_seeds=(1, 2))
    monkeypatch.setenv(
        "FAKE_TIMING", "Info: Max frequency for clock 'core_clock': 201.00 MHz (PASS at 200.00 MHz)"
    )
    monkeypatch.setenv(
        "FAKE_TIMING_SEED_2",
        "Info: Max frequency for clock 'core_clock': 207.10 MHz (PASS at 200.00 MHz)",
    )
    monkeypatch.setenv("FAKE_SKIP_ROUTE_SEED", "2")
    monkeypatch.setenv("FAKE_SKIP_ROUTE_KIND", omitted)
    expected_name = "arty_frame.fasm" if omitted == "fasm" else f"{omitted}.json"
    with pytest.raises(ToolchainError, match=expected_name):
        toolchain.build()
    assert not toolchain.receipt.exists()
    assert [call[0] for call in history()] == ["yosys", "nextpnr-xilinx", "nextpnr-xilinx"]


def test_zero_margin_keeps_the_first_passing_seed(
    toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    toolchain.config = replace(toolchain.config, timing_margin=0)
    monkeypatch.setenv(
        "FAKE_TIMING", "Info: Max frequency for clock 'core_clock': 200.04 MHz (PASS at 200.00 MHz)"
    )
    toolchain.build()
    seeds = [call[call.index("--seed") + 1] for call in history() if call[0] == "nextpnr-xilinx"]
    assert seeds == ["1"]


@pytest.mark.parametrize("margin", [-0.01, 0.6, float("nan"), True, "0.03"])
def test_invalid_timing_margin_is_rejected(margin: object) -> None:
    with pytest.raises(ValueError, match="timing_margin"):
        ToolchainConfig(timing_margin=margin)  # type: ignore[arg-type]


def test_nextpnr_crash_is_not_retried_with_another_seed(
    toolchain: Toolchain, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FAIL", "nextpnr-xilinx")
    with pytest.raises(ToolchainError, match="code 23"):
        toolchain.build()
    assert [call[0] for call in history()] == ["yosys", "nextpnr-xilinx"]


@pytest.mark.parametrize("seeds", [[], [1, 1], [-1], ["1"], list(range(33))])
def test_invalid_seed_lists_are_rejected(seeds: list[object]) -> None:
    with pytest.raises(ValueError, match="nextpnr_seeds"):
        ToolchainConfig(nextpnr_seeds=tuple(seeds))  # type: ignore[arg-type]


def test_missing_toolchain_file_explains_the_windows_route(tmp_path: Path) -> None:
    with pytest.raises(ToolchainError) as error:
        ToolchainConfig.from_json(tmp_path / "toolchain.json")
    message = str(error.value)
    assert "non configurée" in message and "toolchain.json" in message
    assert "Compiler sur GitHub" in message and "Charger le .bit sous Windows" in message
