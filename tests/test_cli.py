from pathlib import Path

from arty_frame_studio.cli import main


def test_profile_simulation_and_demo_are_usable_from_cli(tmp_path: Path, capsys) -> None:
    profile = tmp_path / "frame.json"
    output = tmp_path / "exports" / "wave"
    assert main(["profile", str(profile)]) == 0
    assert main(["simulate", "--profile", str(profile), "--output", str(output)]) == 0
    assert all(output.with_suffix(ext).is_file() for ext in (".svg", ".csv", ".vcd"))
    assert main(["send", "--profile", str(profile), "--demo", "--wait"]) == 0
    assert "Terminé : 1 trame(s)" in capsys.readouterr().out


def test_invalid_profile_and_missing_toolchain_return_errors(tmp_path: Path, capsys) -> None:
    assert main(["simulate", "--profile", str(tmp_path / "missing.json")]) == 1
    assert main(["doctor", "--toolchain", str(tmp_path / "absent.json")]) == 1
    assert "Erreur" in capsys.readouterr().err
