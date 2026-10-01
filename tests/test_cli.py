from pathlib import Path

import pytest

from arty_frame_studio import cli
from arty_frame_studio.cli import main
from arty_frame_studio.protocol import DeviceStatus, Opcode, StatusCode
from arty_frame_studio.transport import CommandTimeout, PortInfo, TransportError


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


@pytest.mark.parametrize("outcome", ["valid", "timeout", "open_error"])
def test_windows_diagnostic_opens_only_selected_port_and_always_closes(
    outcome, monkeypatch, capsys
):
    calls = []

    class Probe:
        def __init__(self, port, *, timeout):
            self.port = port
            calls.append(("open", port, timeout))

        def connect(self):
            calls.append("ping")
            if outcome == "timeout":
                raise CommandTimeout(Opcode.PING, 0, "Aucun octet reçu pendant le délai.")
            if outcome == "open_error":
                raise TransportError("Impossible d'ouvrir le port COM7 : accès refusé.")
            return DeviceStatus(StatusCode.OK, False, 0)

        def close(self):
            calls.append("close")

    monkeypatch.setattr(cli, "SerialDevice", Probe)
    monkeypatch.setattr(
        cli,
        "list_ports",
        lambda: [
            PortInfo("COM7", "USB Serial Port", "USB VID:PID=0403:6010"),
            PortInfo("COM8", "Other"),
        ],
    )
    result = main(["diagnose", "--port", "COM7", "--timeout", "2"])
    output = capsys.readouterr()
    assert calls == [("open", "COM7", 2), "ping", "close"]
    assert "0403:6010" in output.out
    if outcome == "valid":
        assert result == 0
        assert "réponse PING valide" in output.out
    else:
        assert result == 1
        assert "réponse PING valide" not in output.out
        if outcome == "timeout":
            assert "Le port USB/UART a été ouvert" in output.err
        else:
            assert "accès refusé" in output.err
            assert "Le port USB/UART a été ouvert" not in output.err


def test_port_listing_shows_hardware_identity_without_probing(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "list_ports", lambda: [PortInfo("COM7", "USB Serial Port", "USB VID:PID=0403:6010")]
    )
    monkeypatch.setattr(
        cli, "SerialDevice", lambda *args, **kwargs: pytest.fail("Unexpected probe")
    )
    assert main(["ports"]) == 0
    assert "COM7\tUSB Serial Port\tUSB VID:PID=0403:6010" in capsys.readouterr().out
