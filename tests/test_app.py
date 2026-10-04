"""Control-tree smoke tests without a browser, desktop or FPGA attached."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from arty_frame_studio import app
from arty_frame_studio.app import Studio, waveform_signal_points
from arty_frame_studio.model import FrameConfig
from arty_frame_studio.protocol import DeviceStatus, FirmwareInfo, Opcode, StatusCode
from arty_frame_studio.simulation import simulate
from arty_frame_studio.transport import CommandTimeout, DemoDevice, PortInfo


class PageStub:
    def __init__(self):
        self.updates = 0
        self.messages = []

    def update(self):
        self.updates += 1

    def open(self, control):
        self.messages.append(control)


def make_studio(tmp_path: Path) -> Studio:
    studio = Studio(PageStub(), tmp_path)
    studio.layout()
    studio._changed()
    return studio


def run_async(coroutine):
    async def supervise():
        # Some restricted sandboxes deny writes to asyncio's local socketpair.
        # A timer keeps thread completions observable without network access.
        async with asyncio.timeout(5):
            task = asyncio.create_task(coroutine)
            while not task.done():
                await asyncio.sleep(0.01)
            return await task

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(supervise())
    finally:
        # asyncio.run's executor shutdown uses the same blocked socketpair.
        # All tested worker operations have completed before closing this loop.
        loop.close()


def test_controls_construct_and_draw_chronogramme(tmp_path):
    studio = make_studio(tmp_path)
    assert studio.current_config == FrameConfig()
    assert len(studio.tabs.tabs) == 4
    curves = {shape.data for shape in studio.wave_canvas.shapes if shape.data}
    assert curves == {"signal:data", "signal:clk", "signal:latch"}
    labels = {shape.text for shape in studio.wave_canvas.shapes if hasattr(shape, "text")}
    assert {"DATA", "CLK", "LATCH"} <= labels
    assert "10 MHz" in studio.frequency_actual.value
    assert studio.send_button.disabled


def test_invalid_frequency_stays_invalid_when_other_fields_change(tmp_path):
    studio = make_studio(tmp_path)
    studio.frequency.value = "nan"
    studio._frequency_changed(None)
    studio.repeat.value = "2"
    studio._changed()
    assert studio.current_config is None
    assert studio.send_button.disabled
    assert not studio.wave_canvas.visible
    assert not studio.wave_canvas.shapes
    assert studio.validation.value


def test_profile_updates_all_editors(tmp_path):
    studio = make_studio(tmp_path)
    config = FrameConfig(5, 3, 8, 3, 0, 2, True, True)
    studio._set_profile(config)
    assert studio._config() == config
    assert studio.current_config == config
    assert studio.waveform.frames_simulated == 2
    assert "101" in studio.order_preview.value


def test_demo_connection_send_and_close(tmp_path):
    async def exercise():
        studio = make_studio(tmp_path)
        await studio._toggle_connection()
        assert studio.device.connected
        assert "aucun signal physique" in studio.connection_status.value
        assert not studio.send_button.disabled
        await studio._send(None)
        assert studio.last_sent == FrameConfig()
        await studio._stop(None)
        assert not studio.device_status.busy
        await studio._toggle_connection()
        assert studio.device is None
        assert studio.send_button.disabled
        assert not studio.page.messages

    run_async(exercise())


def test_profile_and_waveform_exports_are_wired(tmp_path):
    async def exercise():
        studio = make_studio(tmp_path)
        await studio._save_profile(None)
        assert (tmp_path / "profiles" / "default.json").is_file()
        studio.word.value = "0"
        await studio._load_profile(None)
        assert studio.current_config.word == FrameConfig().word
        for extension in (".svg", ".csv", ".vcd"):
            await studio._export(extension)
            assert (tmp_path / "exports" / f"chronogramme{extension}").is_file()
        assert not studio.page.messages

    run_async(exercise())


def test_program_none_return_is_success(tmp_path, monkeypatch):
    class ProgrammingTool:
        def program(self, bitstream, log=None):
            assert bitstream == tmp_path / "test.bit"
            return None

    async def exercise():
        studio = make_studio(tmp_path)
        bitstream = tmp_path / "test.bit"
        bitstream.write_bytes(b"placeholder")
        studio.bitstream_path.value = str(bitstream)
        monkeypatch.setattr(studio, "_toolchain", lambda: ProgrammingTool())
        await studio._program(None)
        assert "Programmation SRAM : opération terminée" in studio.tool_message.value
        assert any("Le FPGA est configuré" in line for line in studio.log_lines)
        assert not studio.page.messages

    run_async(exercise())


def test_windows_ping_timeout_stays_visible_and_never_enables_send(tmp_path, monkeypatch):
    calls = []

    class UnresponsiveDevice:
        connected = False

        def __init__(self, port, **kwargs):
            assert port == "COM7"

        def connect(self):
            calls.append("ping")
            raise CommandTimeout(Opcode.PING, 0)

        def close(self):
            calls.append("close")

    async def exercise():
        studio = make_studio(tmp_path)
        studio.mode.value = "uart"
        studio.port.value = "COM7"
        monkeypatch.setattr(app, "SerialDevice", UnresponsiveDevice)
        await studio._toggle_connection()
        assert studio.device is None
        assert "COM7 ouvert" in studio.connection_status.value
        assert "aucune réponse PING compatible" in studio.connection_status.value
        assert "bitstream" in studio.hardware_status.value
        assert studio.send_button.disabled
        assert calls == ["ping", "close"]

    run_async(exercise())


def test_native_jtag_result_never_claims_uart_firmware_loaded(tmp_path, monkeypatch):
    calls = []

    def probe(*, serial, dll_path):
        calls.append((serial, dll_path))
        return SimpleNamespace(serial="ARTY001A", idcode=0x03631093)

    async def exercise():
        studio = make_studio(tmp_path)
        studio.ftdi_serial.value = "ARTY001A"
        monkeypatch.setattr(app, "probe_arty", probe)
        await studio._jtag_probe(None)
        assert calls == [("ARTY001A", None)]
        assert "0x03631093" in studio.tool_message.value
        assert any("ne confirme pas" in line for line in studio.log_lines)
        assert studio.device is None
        assert studio.send_button.disabled

    run_async(exercise())


def test_native_program_does_not_need_build_tools_or_enable_uart_send(tmp_path, monkeypatch):
    calls = []

    def program(payload, *, serial, dll_path):
        calls.append((payload, serial, dll_path))
        return SimpleNamespace(serial="ARTY001A", status=0x4010)

    async def exercise():
        studio = make_studio(tmp_path)
        bitstream = tmp_path / "existing.bit"
        bitstream.write_bytes(b"checked by parser")
        studio.windows_bitstream_path.value = str(bitstream)
        studio.ftdi_serial.value = "ARTY001A"
        image = SimpleNamespace(
            path=bitstream, part="7a100tcsg324", sha256="checked-hash", payload=b"config"
        )
        monkeypatch.setattr(app, "read_bitstream", lambda path: image)
        monkeypatch.setattr(app, "program_arty", program)
        monkeypatch.setattr(studio, "_toolchain", lambda: pytest.fail("Unexpected build"))
        await studio._jtag_program(None)
        assert calls == [(b"config", "ARTY001A", None)]
        assert "SRAM chargée" in studio.tool_message.value
        assert any("vérifier PING" in line for line in studio.log_lines)
        assert studio.device is None
        assert studio.send_button.disabled

    run_async(exercise())


def test_programming_disables_uart_actions(tmp_path):
    async def exercise():
        studio = make_studio(tmp_path)
        await studio._toggle_connection()
        assert not studio.send_button.disabled
        studio.tool_pending = True
        studio.programming_pending = True
        studio._buttons()
        assert studio.send_button.disabled
        assert studio.stop_button.disabled
        assert studio.connect_button.disabled
        await studio._send(None)
        await studio._stop(None)
        await studio._toggle_connection()
        assert studio.device is not None and studio.device.connected
        assert studio.last_sent is None

    run_async(exercise())


def test_duration_validation_uses_model_limits_and_allows_zero_gap(tmp_path):
    studio = make_studio(tmp_path)
    studio.latch_ns.value = "1"
    studio._changed()
    assert studio.current_config is None
    assert "2.5" in studio.validation.value
    assert "latch_ticks" not in studio.validation.value
    studio.latch_ns.value = "3,75"
    studio.gap_ns.value = "0"
    studio._changed()
    assert studio.current_config.latch_ticks == 2
    assert studio.current_config.gap_ticks == 0


@pytest.mark.parametrize("lost_opcode", [Opcode.SEND, Opcode.STOP])
@pytest.mark.parametrize("lost_status", [False, True])
def test_lost_command_response_reads_status_without_repeating_command(
    tmp_path, lost_opcode, lost_status
):
    calls = []

    class LostResponseDevice(DemoDevice):
        def send(self, config):
            calls.append(Opcode.SEND)
            status = super().send(config)
            if lost_opcode == Opcode.SEND:
                raise CommandTimeout(Opcode.SEND, 1)
            return status

        def stop(self):
            calls.append(Opcode.STOP)
            status = super().stop()
            if lost_opcode == Opcode.STOP and calls.count(Opcode.STOP) == 1:
                raise CommandTimeout(Opcode.STOP, 2)
            return status

        def status(self):
            calls.append(Opcode.STATUS)
            if lost_status:
                raise CommandTimeout(Opcode.STATUS, 3)
            return super().status()

    async def exercise():
        studio = make_studio(tmp_path)
        studio.repeat.value = "65535"
        studio.divider.value = "2000"
        device = LostResponseDevice()
        studio.device = device
        device.connect()
        await studio._send(None)
        if lost_opcode == Opcode.STOP:
            await studio._stop(None)
        assert calls.count(lost_opcode) == 1
        assert calls[-1] == Opcode.STATUS
        assert studio.command_uncertain
        assert studio.send_button.disabled
        assert not studio.stop_button.disabled
        assert studio.last_sent is None
        assert any("sans répéter la commande" in line for line in studio.log_lines)
        if lost_status:
            assert "État inconnu" in studio.hardware_status.value
        else:
            assert "Commande non confirmée" in studio.hardware_status.value
        await studio._send(None)
        assert calls[-1] == Opcode.STATUS
        # A manually requested, acknowledged STOP clears the uncertainty.
        await studio._stop(None)
        assert not studio.command_uncertain
        assert not studio.send_button.disabled
        assert not studio.device_status.busy

    run_async(exercise())


def transition_ticks(points, duration_ticks, *, rising_only=False):
    left, right = points[0][0], points[-1][0]
    return [
        round((x2 - left) / (right - left) * duration_ticks)
        for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False)
        if x1 == x2 and y1 != y2 and (not rising_only or y2 < y1)
    ]


def test_canvas_geometry_matches_sampling_and_latch_edges():
    waveform = simulate(FrameConfig(5, 3, 2, 3, 2, 1, False, False))
    clock = waveform_signal_points(waveform, "clk", 1200)
    data = waveform_signal_points(waveform, "data", 1200)
    latch = waveform_signal_points(waveform, "latch", 1200)
    # 101 is sampled at ticks 2, 6 and 10. Latch follows the last
    # falling edge at 12, waits 2 ticks, then remains active for 3 ticks.
    assert transition_ticks(clock, 19, rising_only=True) == [2, 6, 10]
    assert transition_ticks(data, 19) == [4, 8, 12]
    assert transition_ticks(latch, 19) == [14, 17]
    for points in (clock, data, latch):
        assert all(x2 >= x1 for (x1, _), (x2, _) in zip(points, points[1:], strict=False))


def test_canvas_geometry_resizes_without_changing_time_or_levels():
    waveform = simulate(FrameConfig(5, 3, 2, 3, 2, 2, False, False))
    for signal in ("data", "clk", "latch"):
        wide = waveform_signal_points(waveform, signal, 1200)
        narrow = waveform_signal_points(waveform, signal, 600)
        assert [y for _, y in wide] == [y for _, y in narrow]
        assert transition_ticks(wide, waveform.duration_ticks) == transition_ticks(
            narrow, waveform.duration_ticks
        )
        for points in (wide, narrow):
            assert points[0][0] >= 0
            assert points[-1][0] <= (1200 if points is wide else 600)


def test_canvas_geometry_preserves_active_low_latch_polarity():
    high = simulate(FrameConfig(5, 3, 2, 3, 2, 1, False, False))
    low = simulate(FrameConfig(5, 3, 2, 3, 2, 1, False, True))
    normal = waveform_signal_points(high, "latch", 800)
    inverted = waveform_signal_points(low, "latch", 800)
    assert [x for x, _ in normal] == [x for x, _ in inverted]
    levels = {y for _, y in normal}
    assert len(levels) == 2
    for (_, normal_y), (_, inverted_y) in zip(normal, inverted, strict=True):
        assert normal_y + inverted_y == pytest.approx(sum(levels))


def test_led_test_walks_virtual_and_board_leds_in_demo(tmp_path):
    async def exercise():
        studio = make_studio(tmp_path)
        studio.led_step = 0
        assert studio.led_test_button.disabled
        await studio._toggle_connection()
        assert studio.firmware_info.led_test
        assert "révision 4" in studio.firmware_status.value
        assert not studio.led_test_button.disabled
        seen = []
        original = studio._show_leds

        def record(pattern):
            seen.append(pattern)
            original(pattern)

        studio._show_leds = record
        await studio._led_test(None)
        assert seen[:4] == [0b0001, 0b0010, 0b0100, 0b1000] and seen[-1] is None
        assert studio.device.led_pattern is None
        assert all(lamp.bgcolor == app.LED_OFF for lamp in studio.led_lamps)
        assert any("Test LED terminé" in line for line in studio.log_lines)
        assert not studio.page.messages

    run_async(exercise())


def test_legacy_firmware_keeps_led_test_disabled(tmp_path, monkeypatch):
    from arty_frame_studio.transport import LEGACY_FIRMWARE

    class LegacyDevice(DemoDevice):
        def identify(self):
            return LEGACY_FIRMWARE

    async def exercise():
        studio = make_studio(tmp_path)
        monkeypatch.setattr(app, "DemoDevice", lambda core_hz: LegacyDevice(core_hz=core_hz))
        await studio._toggle_connection()
        assert studio.led_test_button.disabled
        assert "sans test LED" in studio.firmware_status.value

    run_async(exercise())


def test_connected_firmware_clock_requantizes_the_frame(tmp_path):
    async def exercise():
        studio = make_studio(tmp_path)
        studio.frequency.value = "50"
        studio._frequency_changed(None)
        assert studio.current_config.divider == 4
        # The demo then simulates a firmware built for a 150 MHz core.
        studio.core_clock.value = "150000000"
        studio._core_changed(None)
        await studio._toggle_connection()
        assert studio.core_clock.disabled
        config = studio.current_config
        assert config.core_hz == 150_000_000 and config.divider == 3
        assert "150 MHz / 3" in studio.frequency_actual.value
        await studio._send(None)
        assert studio.last_sent.core_hz == 150_000_000
        await studio._toggle_connection()
        assert not studio.core_clock.disabled

    run_async(exercise())


def test_core_clock_choice_keeps_requested_frequency_and_durations(tmp_path):
    studio = make_studio(tmp_path)
    studio.core_clock.value = "100000000"
    studio._core_changed(None)
    config = studio.current_config
    assert config.core_hz == 100_000_000
    assert config.frequency_hz == 10e6 and config.divider == 10
    assert config.latch_ticks == 4  # 20 ns on a 5 ns grid
    assert "résolution 5 ns" in studio.wave_note.value


def test_firmware_card_summarizes_warns_and_rejects_invalid_pins(tmp_path):
    studio = make_studio(tmp_path)
    assert "Firmware de référence" in studio.fw_summary.value
    assert "L11" in studio.fw_warnings.value
    studio.fw_pins["data_pin"].value = "JC3"
    studio.fw_pins["clock_pin"].value = "JC1"
    studio.fw_pins["latch_pin"].value = "JC7"
    studio.fw_core.value = "150000000"
    studio._firmware_changed()
    assert "Firmware personnalisé" in studio.fw_summary.value
    assert "CLK maximale 150 MHz" in studio.fw_summary.value
    assert studio.fw_warnings.value == ""
    studio.fw_pins["latch_pin"].value = "JC3"
    studio._firmware_changed()
    assert "invalide" in studio.fw_summary.value


def test_firmware_configuration_save_and_load(tmp_path):
    async def exercise():
        studio = make_studio(tmp_path)
        studio.fw_drive.value = "12"
        await studio._save_firmware(None)
        studio.fw_drive.value = "4"
        await studio._load_firmware(None)
        assert studio._firmware_settings().drive_ma == 12
        assert (tmp_path / "profiles" / "firmware.json").is_file()
        assert not studio.page.messages

    run_async(exercise())


def test_local_and_remote_builds_receive_the_selected_firmware(tmp_path, monkeypatch):
    from arty_frame_studio.firmware_config import FirmwareBuildConfig

    builds = []

    class Chain:
        def build(self, log=None, firmware=None):
            builds.append(firmware)
            return tmp_path / "build" / "arty_frame.bit"

    class Client:
        def __init__(self, target, token):
            assert (target.repository, target.ref, token) == ("me/fork", "main", "tok")

        def build(self, firmware, directory, progress=None):
            builds.append(firmware)
            assert directory == tmp_path / "builds"
            return SimpleNamespace(
                bitstream=directory / "fw" / "arty_frame.bit", run_url="https://github.com/run"
            )

    async def exercise():
        studio = make_studio(tmp_path)
        studio.fw_core.value = "100000000"
        monkeypatch.setattr(studio, "_toolchain", lambda: Chain())
        await studio._build(None)
        assert studio.windows_bitstream_path.value == studio.bitstream_path.value
        assert studio.windows_bitstream_path.value == str(tmp_path / "build" / "arty_frame.bit")
        studio.gh_repository.value = "me/fork"
        studio.gh_token.value = "tok"
        monkeypatch.setattr(app, "GitHubBuildClient", Client)
        await studio._remote_build(None)
        assert studio.windows_bitstream_path.value == studio.bitstream_path.value
        assert builds == [FirmwareBuildConfig(core_hz=100_000_000)] * 2
        assert studio.windows_bitstream_path.value.endswith("arty_frame.bit")
        assert "téléchargé et vérifié" in studio.tool_message.value
        from arty_frame_studio.remote_build import GitHubBuildClient

        monkeypatch.setattr(app, "GitHubBuildClient", GitHubBuildClient)
        studio.gh_token.value = ""
        await studio._remote_build(None)
        assert "Jeton" in str(studio.page.messages[-1].content.value)

    run_async(exercise())


def test_connected_profile_for_another_core_is_converted(tmp_path):
    async def exercise():
        studio = make_studio(tmp_path)
        await studio._toggle_connection()
        studio._set_profile(FrameConfig(divider=3, latch_ticks=6, core_hz=150_000_000))
        config = studio.current_config
        assert config.core_hz == 200_000_000
        assert config.divider == 4  # 50 MHz, never above the profile frequency
        assert config.latch_ticks == 8  # 20 ns
        assert any("converti" in line for line in studio.log_lines)

    run_async(exercise())


def test_stop_remains_effective_while_a_build_is_running(tmp_path):
    async def exercise():
        studio = make_studio(tmp_path)
        await studio._toggle_connection()
        studio.divider.value = studio.repeat.value = "65535"
        studio._divider_changed(None)
        await studio._send(None)
        assert studio.device_status.busy
        assert studio.led_test_button.disabled
        finished = threading.Event()
        compilation = asyncio.create_task(
            studio._tool_action("Compilation GitHub", lambda: finished.wait(3))
        )
        try:
            while not studio.tool_pending:
                await asyncio.sleep(0.01)
            assert not studio.stop_button.disabled
            await studio._stop(None)
            assert not studio.device_status.busy
            assert studio.tool_pending
            assert not studio.page.messages
        finally:
            finished.set()
            await compilation

    run_async(exercise())


def test_identity_never_assigns_reference_pins_to_unknown_custom_firmware(tmp_path):
    studio = make_studio(tmp_path)
    studio.mode.value = "uart"
    studio._firmware_identified(FirmwareInfo(2, 150_000_000, 3, 0x12345678))
    assert "personnalisé" in studio.hardware_pinout.value
    assert "JB1" not in studio.hardware_pinout.value
    assert "3.33333 ns" in studio.quantization_note.value
    studio._firmware_identified(None)
    assert studio.hardware_pinout.value == ""


def test_demo_identity_and_led_result_are_explicitly_simulated(tmp_path):
    async def exercise():
        studio = make_studio(tmp_path)
        studio.led_step = 0
        await studio._toggle_connection()
        await studio._led_test(None)
        assert "simulé" in studio.firmware_status.value
        assert not studio.hardware_pinout.visible
        assert "Simuler" in studio.led_test_button.text
        assert any("10 commandes simulées" in line for line in studio.log_lines)

    run_async(exercise())


def test_info_timeout_does_not_claim_ping_failed(tmp_path, monkeypatch):
    class InfoTimeoutDevice(DemoDevice):
        def __init__(self, port, **kwargs):
            super().__init__()

        def identify(self):
            raise CommandTimeout(Opcode.INFO, 1)

    async def exercise():
        studio = make_studio(tmp_path)
        studio.mode.value = "uart"
        studio.port.value = "COM7"
        monkeypatch.setattr(app, "SerialDevice", InfoTimeoutDevice)
        await studio._toggle_connection()
        assert "PING reçu" in studio.connection_status.value
        assert "INFO incomplète" in studio.connection_status.value
        assert studio.device is None and studio.firmware_info is None
        assert studio.send_button.disabled

    run_async(exercise())


def test_continuous_switch_sends_until_stop_and_round_trips_profiles(tmp_path):
    async def exercise():
        studio = make_studio(tmp_path)
        studio.repeat.value = "12"
        studio.continuous.value = True
        studio._continuous_changed()
        assert studio.repeat.disabled
        assert studio.current_config.continuous
        assert "illimitée" in studio.timing_summary.value
        assert "émission continue" in studio.wave_note.value
        await studio._toggle_connection()
        await studio._send(None)
        assert studio.device_status.busy
        assert "Émission continue" in studio.hardware_status.value
        assert any("jusqu'à Arrêter" in line for line in studio.log_lines)
        await studio._stop(None)
        assert not studio.device_status.busy
        assert "arrêtée" in studio.hardware_status.value
        # Back to a finite count: the number typed earlier is still there.
        studio.continuous.value = False
        studio._continuous_changed()
        assert not studio.repeat.disabled and studio.current_config.repeat_count == 12
        studio._set_profile(FrameConfig(repeat_count=0))
        assert studio.continuous.value and studio.repeat.disabled
        assert studio._config().continuous
        studio._set_profile(FrameConfig(repeat_count=5))
        assert not studio.continuous.value and studio.repeat.value == "5"

    run_async(exercise())


def test_revision_two_firmware_is_flagged_without_continuous_emission(tmp_path):
    studio = make_studio(tmp_path)
    studio.mode.value = "uart"
    studio._firmware_identified(FirmwareInfo(2, 200_000_000, 3, 0))
    assert "sans émission continue" in studio.firmware_status.value
    studio._firmware_identified(FirmwareInfo(3, 200_000_000, 7, 0))
    assert "sans émission continue" not in studio.firmware_status.value


def test_free_clock_switch_rounds_to_clock_periods_and_round_trips(tmp_path):
    from arty_frame_studio.app import waveform_signal_points

    studio = make_studio(tmp_path)
    studio.divider.value = "4"
    studio._divider_changed(None)
    studio.latch_ns.value = "60"
    studio.gap_ns.value = "40"
    studio.free_clock.value = True
    studio._changed()
    config = studio.current_config
    assert config.free_clock and (config.latch_ticks, config.gap_ticks) == (20, 16)
    assert "LATCH 3 période(s) de CLK" in studio.timing_summary.value
    assert "CLK continue" in studio.timing_summary.value
    studio._set_profile(FrameConfig(divider=4, latch_ticks=20, gap_ticks=16, free_clock=True))
    assert studio.free_clock.value and studio.latch_ns.value == "60"
    assert studio._config() == FrameConfig(divider=4, latch_ticks=20, gap_ticks=16, free_clock=True)
    studio._set_profile(FrameConfig())
    assert not studio.free_clock.value
    dense = simulate(
        FrameConfig(
            word=1,
            bit_count=1,
            divider=1,
            latch_ticks=65535,
            gap_ticks=65534,
            repeat_count=0,
            free_clock=True,
        )
    )
    points = waveform_signal_points(dense, "clk", 1200)
    assert len(points) <= 3 * 1200
    assert {y for _, y in points} == {148.0, 178.0}


def test_revision_three_firmware_is_flagged_without_free_clock(tmp_path):
    studio = make_studio(tmp_path)
    studio.mode.value = "uart"
    studio._firmware_identified(FirmwareInfo(3, 200_000_000, 7, 0))
    assert "sans CLK libre" in studio.firmware_status.value
    studio._firmware_identified(FirmwareInfo(4, 200_000_000, 15, 0))
    assert "sans" not in studio.firmware_status.value


def test_zero_repeat_requires_explicit_continuous_switch(tmp_path):
    studio = make_studio(tmp_path)
    studio.repeat.value = "0"
    studio._changed()
    assert studio.current_config is None
    assert studio.send_button.disabled
    assert "Répéter jusqu'à Arrêter" in studio.validation.value
    studio.continuous.value = True
    studio._continuous_changed()
    assert studio.current_config.continuous
    assert studio.repeat.disabled


@pytest.mark.parametrize("capabilities, free_clock", [(3, False), (7, True)])
def test_unsupported_emission_modes_disable_send_but_keep_stop(tmp_path, capabilities, free_clock):
    async def exercise():
        studio = make_studio(tmp_path)
        await studio._toggle_connection()
        studio.continuous.value = True
        studio.free_clock.value = free_clock
        studio._changed()
        studio._firmware_identified(FirmwareInfo(3, 200_000_000, capabilities, 0))
        studio._buttons()
        assert studio.send_button.disabled
        assert not studio.stop_button.disabled
        assert studio.compatibility_note.visible
        assert "Firmware incompatible" in studio.compatibility_note.value
        studio._firmware_identified(FirmwareInfo(4, 200_000_000, 15, 0))
        studio._buttons()
        assert not studio.send_button.disabled
        assert not studio.compatibility_note.visible

    run_async(exercise())


def test_free_clock_displays_real_quantized_duration_and_clears_invalid_helpers(tmp_path):
    studio = make_studio(tmp_path)
    studio.free_clock.value = True
    studio.latch_ns.value = "20"
    studio.gap_ns.value = "140"
    studio._changed()
    assert "100 ns" in studio.quantization_note.value
    assert studio.latch_ns.helper_text == "Réalisé : 100 ns"
    assert studio.gap_ns.helper_text == "Réalisé : 100 ns"
    studio.latch_ns.value = "nan"
    studio._changed()
    assert studio.current_config is None
    assert not studio.latch_ns.helper_text and not studio.gap_ns.helper_text


def test_partial_first_frame_is_not_counted_as_a_complete_frame(tmp_path):
    studio = make_studio(tmp_path)
    studio._set_profile(
        FrameConfig(
            word=0,
            bit_count=1,
            divider=1,
            latch_ticks=65535,
            gap_ticks=65534,
            repeat_count=0,
            free_clock=True,
        )
    )
    assert studio.waveform.frames_simulated == 0
    assert studio.waveform.partial_last_frame
    assert "0 trame(s) complète(s) + 1 partielle" in studio.wave_note.value
    assert "budget de transitions" in studio.wave_note.value


def test_quick_profiles_prepare_without_changing_an_active_emission(tmp_path):
    from arty_frame_studio.model import load_profile

    async def exercise():
        studio = make_studio(Path(__file__).resolve().parents[1])
        await studio._toggle_connection()
        await studio._clock_example()
        assert studio.current_config == load_profile(
            Path(__file__).resolve().parents[1] / "examples" / "horloge_seule_10mhz_continue.json"
        )
        assert studio.last_sent is None
        assert studio.send_button.text == "Démarrer CLK continue"
        await studio._send(None)
        clock_config = studio.last_sent
        assert studio.device_status.busy
        assert any("SEND" in line and "CLK libre" in line for line in studio.log_lines)
        assert "Illimité" in studio.repeat.helper_text
        await studio._sipo_example()
        assert studio.current_config.word == 0xA5
        assert studio.current_config.bit_count == 8
        assert studio.current_config.frequency_hz == 10e6
        assert not studio.current_config.continuous
        assert studio.last_sent == clock_config
        assert studio.device_status.busy
        assert not studio.stop_button.disabled
        await studio._stop(None)
        assert not studio.device_status.busy
        assert not studio.page.messages

    run_async(exercise())


@pytest.mark.parametrize("busy,uncertain", [(True, False), (False, True)])
def test_disconnecting_uart_does_not_claim_the_physical_emission_stopped(tmp_path, busy, uncertain):
    async def exercise():
        studio = make_studio(tmp_path)
        studio.mode.value = "uart"
        studio.device = DemoDevice()
        studio.device.connect()
        studio.device_status = DeviceStatus(StatusCode.OK, busy, 0)
        studio.command_uncertain = uncertain
        await studio._toggle_connection()
        assert studio.device is None
        assert "non vérifié" in studio.hardware_status.value
        assert "Aucune émission" not in studio.hardware_status.value
        assert any("reconnecter" in line.lower() for line in studio.log_lines)

    run_async(exercise())


def test_port_discovery_prefers_usb_ftdi_and_preserves_manual_selection(tmp_path, monkeypatch):
    async def exercise():
        studio = make_studio(tmp_path)
        ports = [
            PortInfo("COM3", "Bluetooth", "BTHENUM"),
            PortInfo("COM7", "USB Serial Port", "USB VID:PID=0403:6010 SER=ARTY001B"),
        ]
        monkeypatch.setattr(app, "list_ports", lambda: ports)
        await studio._refresh_ports()
        assert studio.port.value == "COM7"
        assert studio.device is None
        studio.port.value = "COM3"
        await studio._refresh_ports()
        assert studio.port.value == "COM3"
        ports.clear()
        await studio._refresh_ports()
        assert not studio.port.value

    run_async(exercise())
