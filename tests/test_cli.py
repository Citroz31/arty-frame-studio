from dataclasses import dataclass
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


def test_native_jtag_probe_does_not_open_a_com_port(monkeypatch, capsys):
    @dataclass
    class Result:
        serial: str = "ARTY001A"
        idcode: int = 0x03631093

    calls = []

    def probe(*, serial, dll_path):
        calls.append((serial, dll_path))
        return Result()

    monkeypatch.setattr(cli, "probe_arty", probe)
    monkeypatch.setattr(cli, "SerialDevice", lambda *a, **k: pytest.fail("Unexpected UART"))
    assert main(["jtag-diagnose", "--serial", "ARTY001A", "--ftdi-dll", "ftd2xx.dll"]) == 0
    assert calls == [("ARTY001A", Path("ftd2xx.dll"))]
    output = capsys.readouterr().out
    assert "Artix-7 100T détecté par JTAG" in output
    assert "firmware UART reste à vérifier" in output


def test_native_jtag_missing_driver_is_reported_without_programming(monkeypatch, capsys):
    def unavailable(**kwargs):
        raise RuntimeError("DLL FTDI D2XX indisponible")

    monkeypatch.setattr(cli, "probe_arty", unavailable)
    assert main(["jtag-diagnose"]) == 1
    assert "DLL FTDI D2XX indisponible" in capsys.readouterr().err


def test_native_program_validates_file_before_opening_usb(tmp_path, monkeypatch, capsys):
    bitstream = tmp_path / "wrong.bit"
    bitstream.write_bytes(b"not a bitstream")
    monkeypatch.setattr(cli, "program_arty", lambda *a, **k: pytest.fail("Unexpected USB"))
    assert main(["jtag-program", "--bitstream", str(bitstream)]) == 1
    assert "Erreur" in capsys.readouterr().err


def test_native_program_uses_existing_bitstream_without_build_or_com(monkeypatch, capsys):
    @dataclass
    class Result:
        serial: str = "ARTY001A"
        idcode: int = 0x03631093
        status: int = 0x00004010

    calls = []

    def program(payload, *, serial, dll_path):
        calls.append((payload, serial, dll_path))
        return Result()

    @dataclass
    class Image:
        path: Path = Path("firmware.bit")
        part: str = "7a100tcsg324"
        sha256: str = "checked-file-hash"
        payload: bytes = b"validated configuration data"

    monkeypatch.setattr(cli, "read_bitstream", lambda path: Image())
    monkeypatch.setattr(cli, "program_arty", program)
    monkeypatch.setattr(cli, "Toolchain", lambda *a, **k: pytest.fail("Unexpected build"))
    monkeypatch.setattr(cli, "SerialDevice", lambda *a, **k: pytest.fail("Unexpected UART"))
    assert main(["jtag-program", "--bitstream", "firmware.bit", "--serial", "ARTY001A"]) == 0
    assert calls == [(Image().payload, "ARTY001A", None)]
    output = capsys.readouterr().out
    assert "Configuration SRAM terminée" in output
    assert "Vérifiez le firmware UART" in output


def test_firmware_check_works_without_hardware(monkeypatch, capsys):
    monkeypatch.setattr(cli, "SerialDevice", lambda *a, **k: pytest.fail("Unexpected UART"))
    monkeypatch.setattr(cli, "program_arty", lambda *a, **k: pytest.fail("Unexpected JTAG"))
    assert main(["firmware-check", "--project-root", str(Path(__file__).resolve().parents[1])]) == 0
    assert "sources RTL/XDC" in capsys.readouterr().out


def test_firmware_config_command_writes_settings_and_constraints(tmp_path, capsys):
    from arty_frame_studio.firmware_config import FirmwareBuildConfig

    output, xdc = tmp_path / "fw.json", tmp_path / "fw.xdc"
    args = ["firmware-config", "--core-mhz", "150", "--data", "jc3", "--clock", "JC1"]
    assert main([*args, "--latch", "JC7", "--output", str(output), "--xdc", str(xdc)]) == 0
    firmware = FirmwareBuildConfig.load(output)
    assert (firmware.core_hz, firmware.data_pin) == (150_000_000, "JC3")
    assert xdc.read_text() == firmware.xdc()
    assert "cœur 150 MHz" in capsys.readouterr().out
    assert main(["firmware-config", "--input", str(output), "--drive", "16"]) == 0
    assert main(["firmware-config", "--data", "JB2"]) == 1
    assert "distinctes" in capsys.readouterr().err


def test_remote_build_requires_a_token(monkeypatch, capsys):
    monkeypatch.delenv("ARTY_GITHUB_TOKEN", raising=False)
    assert main(["remote-build"]) == 1
    assert "ARTY_GITHUB_TOKEN" in capsys.readouterr().err


def test_led_test_command_identifies_then_walks_leds(monkeypatch, capsys):
    from arty_frame_studio import transport
    from arty_frame_studio.transport import DemoDevice

    demo = DemoDevice()
    monkeypatch.setattr(cli, "SerialDevice", lambda port: demo)
    monkeypatch.setattr(transport.time, "sleep", lambda _: None)
    assert main(["led-test", "--port", "COM7"]) == 0
    out = capsys.readouterr().out
    assert '"revision": 2' in out and "commandes confirmées" in out
    assert not demo.connected
