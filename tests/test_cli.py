from dataclasses import dataclass
from pathlib import Path

import pytest

from arty_frame_studio import cli
from arty_frame_studio.cli import main
from arty_frame_studio.model import FrameConfig
from arty_frame_studio.protocol import DeviceStatus, FirmwareInfo, Opcode, StatusCode
from arty_frame_studio.scope import KeysightScope
from arty_frame_studio.scope_sim import SimulatedKeysight, signal_source
from arty_frame_studio.toolchain import ToolchainConfig, ToolchainError
from arty_frame_studio.transport import CommandTimeout, DeviceError, PortInfo, TransportError


def test_profile_simulation_and_demo_are_usable_from_cli(tmp_path: Path, capsys) -> None:
    profile = tmp_path / "frame.json"
    output = tmp_path / "exports" / "wave"
    assert main(["profile", str(profile)]) == 0
    assert main(["simulate", "--profile", str(profile), "--output", str(output)]) == 0
    assert all(output.with_suffix(ext).is_file() for ext in (".svg", ".csv", ".vcd"))
    assert main(["send", "--profile", str(profile), "--demo", "--wait"]) == 0
    assert "Terminé : 1 trame(s)" in capsys.readouterr().out


def test_scope_demo_measures_the_profile_frame(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        KeysightScope, "read_display", lambda *a, **k: pytest.fail("Demo must acquire its frame")
    )
    profile = tmp_path / "frame.json"
    points = tmp_path / "scope.csv"
    assert main(["profile", str(profile)]) == 0
    assert main(["scope", "--demo", "--profile", str(profile), "--csv", str(points)]) == 0
    out = capsys.readouterr().out
    assert "DSO-X 1202A" in out
    assert "CH2 : fréquence 10.00 MHz · période 100.0 ns" in out
    assert "CH1 : fréquence 5.000 MHz" in out
    assert points.read_text(encoding="utf-8").startswith("temps_CH1_s,CH1_V,temps_CH2_s,CH2_V")
    # The simulator has no screen to copy: reported as an error, not a crash.
    assert main(["scope", "--demo", "--png", str(tmp_path / "screen.png")]) == 1
    assert "Copie d'écran indisponible" in capsys.readouterr().err


@pytest.mark.parametrize("connection", ["lan", "visa"])
def test_real_scope_reads_the_existing_stopped_screen_without_rearming(
    connection, tmp_path, monkeypatch, capsys
):
    present = signal_source(FrameConfig())
    instrument = SimulatedKeysight(lambda: present, noise=0)
    KeysightScope(instrument).capture()
    record = instrument.records[1]
    instrument.write(":CHANnel2:DISPlay 0")
    present = None  # A new acquisition would lose the completed frame.
    instrument.commands.clear()
    address = "192.0.2.1" if connection == "lan" else "USB0::KEYSIGHT::INSTR"
    calls = []

    def connect(selected):
        calls.append(selected)
        return instrument

    monkeypatch.setattr(cli, "SocketTransport" if connection == "lan" else "VisaTransport", connect)
    points = tmp_path / "stopped-screen.csv"
    assert main(["scope", f"--{connection}", address, "--csv", str(points)]) == 0
    output = capsys.readouterr().out
    assert calls == [address]
    assert "écran existant (origine du déclenchement inconnue)" in output
    assert "sans front" not in output
    assert "CH1 :" in output and "CH2 :" not in output
    assert instrument.records[1] is record
    assert instrument.closed and not instrument.running
    assert not {":STOP", ":SINGle", ":TRIGger:FORCe", ":TER?", ":RUN"}.intersection(
        instrument.commands
    )
    assert points.read_text(encoding="utf-8").startswith("temps_CH1_s,CH1_V\n")


def test_real_scope_single_explicitly_acquires_with_the_requested_timeout(monkeypatch, capsys):
    source = signal_source(FrameConfig())
    instrument = SimulatedKeysight(lambda: source, noise=0)
    monkeypatch.setattr(cli, "SocketTransport", lambda address: instrument)
    monkeypatch.setattr(
        KeysightScope, "read_display", lambda *a, **k: pytest.fail("Single must acquire")
    )
    capture = KeysightScope.capture
    calls = []

    def acquire(scope, **options):
        calls.append(options)
        return capture(scope, **options)

    monkeypatch.setattr(KeysightScope, "capture", acquire)
    assert main(["scope", "--lan", "192.0.2.1", "--single", "--timeout", "0.25"]) == 0
    assert calls == [{"timeout": 0.25, "mapping": {1: "data", 2: "clk"}}]
    assert ":SINGle" in instrument.commands
    assert instrument.closed
    output = capsys.readouterr().out
    assert "déclenchée" in output and "écran existant" not in output


def test_scope_listing_and_bad_addresses(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "list_visa_resources", lambda: [])
    assert main(["scope-list"]) == 1
    assert "Keysight IO Libraries" in capsys.readouterr().err
    resource = "USB0::0x2A8D::0x0396::CN12345678::0::INSTR"
    monkeypatch.setattr(cli, "list_visa_resources", lambda: [resource])
    assert main(["scope-list"]) == 0
    assert capsys.readouterr().out.strip() == resource
    assert main(["scope", "--lan", "bad host!"]) == 1
    assert "Adresse IP" in capsys.readouterr().err


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "-1", "61"])
def test_scope_rejects_invalid_timeout_before_contacting_instrument(timeout, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SocketTransport", lambda *a: pytest.fail("Unexpected instrument I/O"))
    assert main(["scope", "--lan", "192.0.2.1", "--timeout", timeout]) == 1
    assert "délai de déclenchement" in capsys.readouterr().err


def test_demo_screen_export_is_rejected_before_acquisition(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "SimulatedKeysight", lambda *a: pytest.fail("Unexpected acquisition"))
    assert main(["scope", "--demo", "--png", str(tmp_path / "screen.png")]) == 1
    assert "export CSV" in capsys.readouterr().err
    assert not (tmp_path / "screen.png").exists()


def test_scope_channel_assignment_controls_measurement_interpretation(capsys):
    assert main(["scope", "--demo", "--ch1", "clk", "--ch2", "data"]) == 0
    output = capsys.readouterr()
    assert "CH1 : fréquence 10.00 MHz · période 100.0 ns" in output.out
    assert "CH2 : fréquence 5.000 MHz" in output.out
    assert "DATA" in output.err and "CLK" in output.err


def test_invalid_profile_and_missing_toolchain_return_errors(tmp_path: Path, capsys) -> None:
    assert main(["simulate", "--profile", str(tmp_path / "missing.json")]) == 1
    assert main(["doctor", "--toolchain", str(tmp_path / "absent.json")]) == 1
    assert "Erreur" in capsys.readouterr().err


@pytest.mark.parametrize("options", ["defaults", "project", "custom"])
def test_install_fpga_tools_forwards_paths_and_reports_local_configuration(
    options, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    project = tmp_path if options == "defaults" else tmp_path / "project with spaces"
    tools = Path("portable tools") if options == "custom" else None
    configuration = Path("configuration/native.json") if options == "custom" else None
    calls = []

    def install(project_root, log=None, *, tools_dir=None, config_path=None):
        calls.append((project_root, log, tools_dir, config_path))
        log("Archives SHA256 vérifiées.")
        return ToolchainConfig()

    monkeypatch.setattr(cli, "ensure_local_toolchain", install)
    arguments = ["install-fpga-tools"]
    if options != "defaults":
        arguments.extend(["--project-root", str(project)])
    if options == "custom":
        arguments.extend(["--tools-dir", str(tools), "--config", str(configuration)])
    assert main(arguments) == 0
    assert calls == [(project, print, tools, configuration)]
    output = capsys.readouterr().out
    expected = configuration or project / "toolchain.json"
    assert "Archives SHA256 vérifiées." in output
    assert f"Configuration locale prête : {expected.resolve()}" in output


def test_install_fpga_tools_reports_failure_without_claiming_configuration_is_ready(
    monkeypatch, capsys
):
    def unavailable(*args, **kwargs):
        raise ToolchainError("L'installation portable exige Windows x64.")

    monkeypatch.setattr(cli, "ensure_local_toolchain", unavailable)
    assert main(["install-fpga-tools"]) == 1
    output = capsys.readouterr()
    assert "Windows x64" in output.err
    assert "Configuration locale prête" not in output.out


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
    assert '"revision": 4' in out and "commandes confirmées" in out
    assert not demo.connected


def test_cli_continuous_send_stops_after_the_requested_duration(tmp_path: Path, capsys) -> None:
    profile = tmp_path / "frame.json"
    assert main(["profile", str(profile)]) == 0
    assert (
        main(["send", "--profile", str(profile), "--demo", "--continuous", "--duration", "0.05"])
        == 0
    )
    out = capsys.readouterr().out
    assert '"busy": true' in out and "STOP après 0.05 s" in out and "modulo 65536" in out
    assert main(["send", "--profile", str(profile), "--demo", "--continuous"]) == 0
    assert "simulation sera fermée" in capsys.readouterr().err
    assert main(["send", "--profile", str(profile), "--demo", "--duration", "-1"]) == 1
    assert "--duration" in capsys.readouterr().err


def test_cli_free_clock_rounds_profile_timings(tmp_path: Path, capsys) -> None:
    profile = tmp_path / "frame.json"
    output = tmp_path / "libre"
    assert main(["profile", str(profile)]) == 0
    assert (
        main(["simulate", "--profile", str(profile), "--output", str(output), "--free-clock"]) == 0
    )
    captured = capsys.readouterr()
    assert "CLK libre : LATCH 1 période(s), pause 1 période(s)" in captured.err
    assert (
        main(
            [
                "send",
                "--profile",
                str(profile),
                "--demo",
                "--free-clock",
                "--continuous",
                "--duration",
                "0.05",
            ]
        )
        == 0
    )
    assert "STOP après 0.05 s" in capsys.readouterr().out


def test_cli_demo_uses_the_profile_core_clock(tmp_path: Path, capsys) -> None:
    from arty_frame_studio.model import FrameConfig, save_profile

    profile = tmp_path / "custom-core.json"
    save_profile(FrameConfig(core_hz=150_000_000), profile)
    assert main(["send", "--profile", str(profile), "--demo", "--wait"]) == 0
    assert "Terminé : 1 trame(s)." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("failure", "expected_result", "expected_stops"),
    [
        ("status_error", 1, 1),
        ("interrupt", 130, 1),
        ("interrupt_stop_timeout", 130, 1),
        ("send_timeout", 1, 1),
        ("cleanup_stop_timeout", 1, 1),
        ("explicit_refusal", 1, 0),
    ],
)
def test_cli_wait_cleans_up_once_after_failure_or_interrupt(
    tmp_path: Path, monkeypatch, capsys, failure, expected_result, expected_stops
) -> None:
    from arty_frame_studio.model import FrameConfig, save_profile

    calls = []

    class Board:
        def __init__(self, port):
            assert port == "COM7"

        def connect(self):
            calls.append("connect")

        def identify(self):
            return FirmwareInfo(4, 200_000_000, 15, 0)

        def send(self, config):
            calls.append("send")
            if failure == "send_timeout":
                raise CommandTimeout(Opcode.SEND, 0)
            if failure == "explicit_refusal":
                raise DeviceError(DeviceStatus(StatusCode.BUSY, True, 0))
            return DeviceStatus(StatusCode.OK, True, 0)

        def status(self):
            calls.append("status")
            if failure in ("interrupt", "interrupt_stop_timeout"):
                raise KeyboardInterrupt
            raise TransportError("STATUS perdu")

        def stop(self):
            calls.append("stop")
            if failure in ("cleanup_stop_timeout", "interrupt_stop_timeout"):
                raise CommandTimeout(Opcode.STOP, 1)
            return DeviceStatus(StatusCode.OK, False, 1)

        def close(self):
            calls.append("close")

    monkeypatch.setattr(cli, "SerialDevice", Board)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    profile = tmp_path / "continuous.json"
    save_profile(FrameConfig(repeat_count=0), profile)
    assert main(["send", "--profile", str(profile), "--port", "COM7", "--wait"]) == expected_result
    assert calls.count("send") == 1 and calls.count("stop") == expected_stops
    assert calls[-1] == "close"
    stderr = capsys.readouterr().err
    if failure == "cleanup_stop_timeout":
        assert "L'émission peut encore être active" in stderr
    if failure == "interrupt":
        assert "Émission interrompue par STOP" in stderr
    if failure == "interrupt_stop_timeout":
        assert "Confirmation STOP non reçue" in stderr
        assert "Émission interrompue par STOP" not in stderr


@pytest.mark.parametrize("stop_timeout", [False, True])
def test_cli_duration_sends_stop_at_deadline_without_a_final_status(
    tmp_path: Path, monkeypatch, capsys, stop_timeout
) -> None:
    from arty_frame_studio.model import FrameConfig, save_profile

    calls = []
    now = [0.0]

    class Board:
        def __init__(self, port):
            assert port == "COM7"

        def connect(self):
            pass

        def identify(self):
            return FirmwareInfo(4, 200_000_000, 15, 0)

        def send(self, config):
            calls.append("send")
            return DeviceStatus(StatusCode.OK, True, 0)

        def status(self):
            pytest.fail("STATUS doit être évité à l'échéance d'arrêt")

        def stop(self):
            calls.append(("stop", now[0]))
            if stop_timeout:
                raise CommandTimeout(Opcode.STOP, 1)
            return DeviceStatus(StatusCode.OK, False, 0)

        def close(self):
            calls.append("close")

    monkeypatch.setattr(cli, "SerialDevice", Board)
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(cli.time, "sleep", lambda duration: now.__setitem__(0, now[0] + duration))
    profile = tmp_path / "continuous.json"
    save_profile(FrameConfig(repeat_count=0), profile)
    assert main(["send", "--profile", str(profile), "--port", "COM7", "--duration", "0.05"]) == (
        1 if stop_timeout else 0
    )
    assert calls == ["send", ("stop", 0.05), "close"]
    if stop_timeout:
        assert "STOP" in capsys.readouterr().err


def test_cli_unmonitored_hardware_continues_after_the_port_is_closed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from arty_frame_studio.model import FrameConfig, save_profile

    calls = []

    class Board:
        def __init__(self, port):
            assert port == "COM7"

        def connect(self):
            pass

        def identify(self):
            return FirmwareInfo(4, 200_000_000, 15, 0)

        def send(self, config):
            calls.append("send")
            return DeviceStatus(StatusCode.OK, True, 0)

        def stop(self):
            pytest.fail("Sans --wait/--duration, l'émission doit continuer sur le FPGA")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(cli, "SerialDevice", Board)
    profile = tmp_path / "continuous.json"
    save_profile(FrameConfig(repeat_count=0), profile)
    assert main(["send", "--profile", str(profile), "--port", "COM7"]) == 0
    assert calls == ["send", "close"]
    assert "commande stop" in capsys.readouterr().err
