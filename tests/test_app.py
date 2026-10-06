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
    assert [tab.text for tab in studio.tabs.tabs] == [
        "Pilotage",
        "Chronogramme",
        "Oscilloscope",
        "Mesure",
        "FPGA",
        "Journal",
    ]
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


def test_invalid_frequency_clears_every_previous_preview(tmp_path):
    studio = make_studio(tmp_path)
    assert studio.binary_preview.value != "—" and studio.timing_summary.value
    studio.frequency.value = "nan"
    studio._frequency_changed(None)
    assert studio.current_config is None and studio.waveform is None
    assert studio.binary_preview.value == "—"
    assert not studio.order_preview.value and not studio.timing_summary.value
    assert not studio.wave_canvas.visible and not studio.wave_canvas.shapes


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
        def validate_programming(self, bitstream):
            assert bitstream == tmp_path / "test.bit"

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


def test_pilotage_preparation_snapshots_frame_without_programming_or_sending(tmp_path, monkeypatch):
    from arty_frame_studio.model import load_profile, save_profile

    calls = []

    def prepare(root, frame, firmware, tools, *, log=None):
        calls.append((root, frame, firmware))
        profile = root / "profiles" / "pilotage-frame.json"
        profile.parent.mkdir(parents=True)
        save_profile(frame, profile)
        return SimpleNamespace(
            bitstream=root / "ready.bit",
            firmware=firmware,
            frame_profile=profile,
            reused=True,
        )

    async def exercise():
        studio = make_studio(tmp_path)
        studio.word.value = "00101"
        studio._word_changed()
        studio.core_clock.value = "150000000"
        studio._core_changed(None)
        studio.pilotage_pins["data_pin"].value = "JA1"
        studio._pilotage_hardware_changed()
        monkeypatch.setattr(app, "prepare_firmware", prepare)
        monkeypatch.setattr(app, "program_arty", lambda *_args, **_kw: pytest.fail("JTAG"))
        monkeypatch.setattr(
            app, "ensure_local_toolchain", lambda *_a, **_kw: pytest.fail("install")
        )
        await studio._prepare_from_pilotage()
        assert calls[0][0] == tmp_path
        assert calls[0][1].word == 5 and calls[0][1].bit_count == 5
        assert calls[0][2].data_pin == "JA1"
        assert calls[0][1].core_hz == calls[0][2].core_hz == 150000000
        assert load_profile(Path(studio.profile_path.value)) == calls[0][1]
        assert studio.windows_bitstream_path.value == str(tmp_path / "ready.bit")
        assert studio.last_sent is None and studio.device is None
        assert "réutilisé" in studio.preparation_note.value
        assert not studio.page.messages

    run_async(exercise())


def test_hardware_pin_edits_are_synchronized_and_duplicate_pins_block_preparation(tmp_path):
    studio = make_studio(tmp_path)
    studio.pilotage_pins["data_pin"].value = "JA1"
    studio._pilotage_hardware_changed()
    assert studio.fw_pins["data_pin"].value == "JA1"
    studio.fw_pins["clock_pin"].value = "JA1"
    studio._firmware_changed()
    assert studio.pilotage_pins["clock_pin"].value == "JA1"
    assert studio.prepare_local_button.disabled
    studio.fw_pins["clock_pin"].value = "JA2"
    studio.fw_core.value = "100000000"
    studio._firmware_changed()
    assert studio.core_hz == 100000000 and studio.core_clock.value == "100000000"
    assert not studio.prepare_local_button.disabled


def test_invalid_native_image_keeps_uart_open(tmp_path, monkeypatch):
    class ConnectedSerial:
        connected = True

        def close(self):
            pytest.fail("Invalid image must preserve the UART connection")

    async def exercise():
        studio = make_studio(tmp_path)
        monkeypatch.setattr(app, "SerialDevice", ConnectedSerial)
        device = ConnectedSerial()
        studio.device = device
        studio.mode.value = "uart"
        image = tmp_path / "invalid.bit"
        image.write_bytes(b"invalid FPGA file")
        studio.windows_bitstream_path.value = str(image)
        monkeypatch.setattr(app, "program_arty", lambda *_a, **_kw: pytest.fail("JTAG"))
        await studio._jtag_program(None)
        assert studio.device is device and device.connected
        assert not studio.programming_pending and not studio.tool_pending
        assert studio.page.messages

    run_async(exercise())


def test_native_local_output_without_successful_receipt_keeps_uart_open(tmp_path, monkeypatch):
    import json
    import shutil

    class ConnectedSerial:
        connected = True

        def close(self):
            pytest.fail("A revoked local build must preserve the UART connection")

    async def exercise():
        studio = make_studio(tmp_path)
        monkeypatch.setattr(app, "SerialDevice", ConnectedSerial)
        device = ConnectedSerial()
        studio.device = device
        studio.mode.value = "uart"
        build = tmp_path / "build"
        build.mkdir()
        image = build / "arty_frame.bit"
        source = Path(__file__).resolve().parents[1] / "firmware/prebuilt/arty_frame.bit"
        shutil.copy2(source, image)
        (tmp_path / "toolchain.json").write_text(json.dumps({"build_dir": str(build)}))
        studio.windows_bitstream_path.value = str(image)
        monkeypatch.setattr(app, "program_arty", lambda *_a, **_kw: pytest.fail("JTAG"))
        await studio._jtag_program(None)
        assert studio.device is device and device.connected
        assert "recompilez avec succès" in studio.tool_message.value
        assert not studio.programming_pending and not studio.tool_pending

    run_async(exercise())


def test_valid_native_image_is_checked_before_uart_closes(tmp_path, monkeypatch):
    calls = []

    class ConnectedSerial:
        connected = True

        def close(self):
            calls.append("close")
            self.connected = False

    async def exercise():
        studio = make_studio(tmp_path)
        monkeypatch.setattr(app, "SerialDevice", ConnectedSerial)
        studio.device = ConnectedSerial()
        studio.mode.value = "uart"
        studio.device_status = DeviceStatus(StatusCode.OK, False, 0)
        bit = tmp_path / "valid.bit"
        bit.write_bytes(b"validated by test double")
        studio.windows_bitstream_path.value = str(bit)

        def validate(path):
            assert studio.device.connected and studio.programming_pending
            assert studio.send_button.disabled and studio.connect_button.disabled
            calls.append("validate")
            return SimpleNamespace(
                payload=b"frozen image", part="7a100tcsg324", sha256="hash"
            ), None

        def program(payload, **_kwargs):
            assert studio.device is None
            assert payload == b"frozen image"
            calls.append("program")
            return SimpleNamespace(serial="ARTY001", status=0x4010, seconds=6.2, tck_hz=6_000_000)

        monkeypatch.setattr(studio, "_checked_windows_image", validate)
        monkeypatch.setattr(app, "program_arty", program)
        await studio._jtag_program(None)
        assert calls == ["validate", "close", "program"]
        assert studio.device is None and not studio.page.messages

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


def test_late_uart_replies_are_explained_in_the_journal(tmp_path, monkeypatch):
    class SlowBoard:
        connected = False

        def __init__(self, port, **kwargs):
            pass

        def connect(self):
            raise CommandTimeout(Opcode.PING, 1, "Réponse(s) tardive(s) à une requête précédente")

        def link_notes(self):
            return ["Réponses de la carte en retard : jusqu'à 1020 ms pour 750 ms prévus."]

        def close(self):
            pass

    async def exercise():
        studio = make_studio(tmp_path)
        studio.mode.value = "uart"
        studio.port.value = "COM7"
        monkeypatch.setattr(app, "SerialDevice", SlowBoard)
        await studio._toggle_connection()
        lines = [line for line in studio.log_lines if "Liaison UART" in line]
        assert lines and "1020 ms" in lines[0]
        assert studio.device is None

    run_async(exercise())


def test_reset_and_connect_pulses_the_board_reset_once(tmp_path, monkeypatch):
    calls = []

    class Board:
        connected = False

        def __init__(self, port, **kwargs):
            pass

        def connect(self, **kwargs):
            calls.append(kwargs)
            raise CommandTimeout(Opcode.PING, 1)

        def close(self):
            pass

    async def exercise():
        studio = make_studio(tmp_path)
        studio.mode.value = "uart"
        studio.port.value = "COM7"
        studio._buttons()
        assert studio.reset_connect_button.visible
        monkeypatch.setattr(app, "SerialDevice", Board)
        await studio._reset_and_connect()
        await studio._toggle_connection()
        assert calls == [{"reset_board": True}, {}]
        assert any("DTR" in line for line in studio.log_lines)

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

    def program(payload, *, serial, dll_path, tck_hz):
        calls.append((payload, serial, dll_path))
        return SimpleNamespace(serial="ARTY001A", status=0x4010, seconds=6.2, tck_hz=6_000_000)

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


def test_tr_switch_sets_the_pin_through_the_board_and_reverts_on_failure(tmp_path):
    from dataclasses import replace

    async def exercise():
        studio = make_studio(tmp_path)
        assert studio.tr_switch.disabled
        await studio._toggle_connection()
        assert not studio.tr_switch.disabled and "0 V après le chargement" in studio.tr_note.value
        studio.tr_switch.value = True
        await studio._tr_toggled()
        assert studio.device.tr_level == 1 and studio.tr_switch.value is True
        assert any("TR : 3,3 V" in line for line in studio.log_lines)
        studio.tr_switch.value = False
        await studio._tr_toggled()
        assert studio.device.tr_level == 0 and studio.device.tr_history == [1, 0]
        # Pendant un balayage la broche appartient au balayage : l'interrupteur revient.
        studio.sweep_active = True
        studio._buttons()
        assert studio.tr_switch.disabled
        studio.tr_switch.value = True
        await studio._tr_toggled()
        assert studio.tr_switch.value is False and studio.device.tr_history == [1, 0]
        studio.sweep_active = False
        # Une commande refusée laisse l'interrupteur sur le niveau réel de la broche.
        studio.device.tr = lambda level: (_ for _ in ()).throw(RuntimeError("liaison coupée"))
        studio.tr_switch.value = True
        await studio._tr_toggled()
        assert studio.tr_switch.value is False
        # Un firmware sans TR désactive l'interrupteur et dit pourquoi.
        old = replace(studio.device.firmware, revision=4, capabilities=0x0F)
        studio.firmware_info = old
        studio._buttons()
        assert studio.tr_switch.disabled and "Firmware sans broche TR" in studio.tr_note.value

    run_async(exercise())


def test_led_test_walks_virtual_and_board_leds_in_demo(tmp_path):
    async def exercise():
        studio = make_studio(tmp_path)
        studio.led_step = 0
        assert studio.led_test_button.disabled
        await studio._toggle_connection()
        assert studio.firmware_info.led_test
        assert "révision 5" in studio.firmware_status.value
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
    assert "sans broche TR" in studio.firmware_status.value  # révision 4 : pas de TR
    studio._firmware_identified(FirmwareInfo(5, 200_000_000, 31, 0))
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


def test_binary_is_the_default_notation_with_an_automatic_bit_count(tmp_path):
    studio = make_studio(tmp_path)
    default = FrameConfig()
    assert studio.base.value == "bin"
    assert studio.word.value == f"{default.word:026b}"
    assert studio.bit_count.value == "26" and studio.bit_count.disabled
    assert studio.current_config == default
    # Typing and removing digits changes the frame length at once.
    for text, bits in (("1", 1), ("101", 3), ("1010 0101", 8), ("0001", 4), ("10", 2)):
        studio.word.value = text
        studio._word_changed()
        assert studio.bit_count.value == str(bits)
        assert studio.current_config.bit_count == bits
    assert studio.current_config.word == 0b10
    studio.word.value = "1010 0101"
    studio._word_changed()
    assert studio.current_config.word == 0xA5 and studio.binary_preview.value == "10100101"


def test_binary_entry_beyond_26_bits_is_refused_without_sending(tmp_path):
    studio = make_studio(tmp_path)
    studio.word.value = "1" * 27
    studio._word_changed()
    assert studio.bit_count.value == "27"
    assert studio.current_config is None and studio.send_button.disabled
    assert "26 bits maximum" in studio.validation.value
    studio.word.value = "1" * 26
    studio._word_changed()
    assert studio.current_config.bit_count == 26 and not studio.validation.visible
    for text, message in (("", "au moins un bit"), ("1021", "seulement 0 et 1")):
        studio.word.value = text
        studio._word_changed()
        assert studio.current_config is None and message in studio.validation.value


def test_notation_changes_keep_the_value_and_its_length(tmp_path):
    studio = make_studio(tmp_path)
    studio.word.value = "00010100101"  # 11 bits, leading zeros included
    studio._word_changed()
    studio.base.value = "hex"
    studio._base_changed(None)
    assert studio.word.value == "A5" and studio.bit_count.value == "11"
    assert not studio.bit_count.disabled
    assert studio.current_config.bit_count == 11 and studio.current_config.word == 0xA5
    # In hexadecimal the bit count stays a manual setting.
    studio.bit_count.value = "12"
    studio._changed()
    studio.base.value = "dec"
    studio._base_changed(None)
    assert studio.word.value == "165" and studio.bit_count.value == "12"
    studio.base.value = "bin"
    studio._base_changed(None)
    assert studio.word.value == "000010100101" and studio.bit_count.disabled
    assert studio.current_config.bit_count == 12


def test_profiles_are_shown_in_the_selected_notation(tmp_path):
    studio = make_studio(tmp_path)
    profile = FrameConfig(word=0xA5, bit_count=8, divider=20)
    studio._set_profile(profile)
    assert studio.word.value == "10100101" and studio.bit_count.value == "8"
    assert studio._config() == profile
    studio.base.value = "hex"
    studio._base_changed(None)
    studio._set_profile(FrameConfig(word=0x5, bit_count=12))
    assert studio.word.value == "5" and studio.bit_count.value == "12"
    assert studio._config().bit_count == 12


@pytest.mark.parametrize(
    "base,text,bits,destination",
    [
        ("bin", "0002", 4, "hex"),
        ("bin", "0x", 2, "hex"),
        ("bin", "1" * 27, 27, "dec"),
        ("hex", "10", 0, "bin"),
        ("hex", "10", 27, "bin"),
        ("hex", "0010", 1, "bin"),
        ("dec", "0b10", 26, "bin"),
        ("dec", "0b", 26, "hex"),
        ("dec", "000A", 26, "hex"),
    ],
)
def test_invalid_frame_cannot_be_reinterpreted_by_changing_notation(
    tmp_path, base, text, bits, destination
):
    studio = make_studio(tmp_path)
    studio.base.value = base
    studio._base_changed(None)
    studio.word.value = text
    studio.bit_count.value = str(bits)
    studio._word_changed()
    assert studio.current_config is None
    studio.base.value = destination
    studio._base_changed(None)
    assert studio.base.value == studio.previous_base == base
    assert studio.word.value == text and studio.bit_count.value == str(bits)
    assert studio.current_config is None and studio.send_button.disabled
    assert "avant de changer de notation" in studio.validation.value


@pytest.mark.parametrize(
    "source,destination,text",
    [
        ("bin", "hex", ""),
        ("bin", "dec", " _ "),
        ("bin", "hex", "0b"),
        ("bin", "hex", "0\tb"),
        ("bin", "hex", "\u00a0_\u202f"),
        ("hex", "bin", "0x"),
        ("hex", "dec", " _ "),
    ],
)
def test_empty_frame_can_change_notation_without_inventing_bits(
    tmp_path, source, destination, text
):
    studio = make_studio(tmp_path)
    studio._set_profile(FrameConfig(word=1, bit_count=8))
    studio.base.value = source
    studio._base_changed(None)
    studio.word.value = text
    studio._word_changed()
    studio.base.value = destination
    studio._base_changed(None)
    assert studio.base.value == studio.previous_base == destination
    assert studio.word.value == "" and studio.current_config is None
    assert studio.bit_count.value == ("0" if destination == "bin" else "8")
    assert studio.bit_count.disabled == (destination == "bin")


def test_empty_frame_restores_a_valid_manual_width_after_invalid_edits(tmp_path):
    studio = make_studio(tmp_path)
    studio.word.value = "00101"
    studio._word_changed()
    studio.base.value = "hex"
    studio._base_changed(None)
    studio.bit_count.value = "0"
    studio.word.value = ""
    studio._changed()
    studio.base.value = "bin"
    studio._base_changed(None)
    assert studio.bit_count.value == "0" and studio.word.value == ""
    studio.base.value = "dec"
    studio._base_changed(None)
    assert studio.bit_count.value == "5" and not studio.bit_count.disabled
    assert studio.current_config is None


def test_hexadecimal_0b_is_a_value_rather_than_an_empty_binary_prefix(tmp_path):
    studio = make_studio(tmp_path)
    studio._set_profile(FrameConfig(word=1, bit_count=8))
    studio.base.value = "hex"
    studio._base_changed(None)
    studio.word.value = "0B"
    studio._word_changed()
    assert studio.current_config.word == 11
    studio.base.value = "bin"
    studio._base_changed(None)
    assert studio.word.value == "00001011" and studio.current_config.word == 11
    assert studio.current_config.bit_count == 8
    studio.base.value = "hex"
    studio._base_changed(None)
    assert studio.word.value == "B" and studio.current_config.word == 11


def test_binary_width_is_synchronized_by_every_refresh(tmp_path):
    studio = make_studio(tmp_path)
    for text, bits in (("00101", 5), ("", 0), ("1" * 27, 27)):
        studio.word.value = text
        studio._changed()
        assert studio.bit_count.value == str(bits) and studio.bit_count.disabled


def test_pasted_binary_groups_ignore_all_whitespace(tmp_path):
    studio = make_studio(tmp_path)
    studio.word.value = "0b00\t101\n0\u00a01\u202f1"
    studio._word_changed()
    assert studio.bit_count.value == "8" and studio.binary_preview.value == "00101011"
    assert studio.current_config == FrameConfig(word=0b00101011, bit_count=8)
    studio.base.value = "hex"
    studio._base_changed(None)
    assert studio.word.value == "2B" and studio.current_config.bit_count == 8


@pytest.mark.parametrize("core_hz", [100_000_000, 150_000_000, 200_000_000])
def test_minimum_frequency_profiles_and_dividers_stay_valid(tmp_path, core_hz):
    studio = make_studio(tmp_path)
    profile = FrameConfig(core_hz=core_hz, divider=65535)
    studio._set_profile(profile)
    assert studio.current_config == profile
    studio.frequency.value = "nan"
    studio._frequency_changed(None)
    studio._divider_changed(None)
    assert studio.current_config == profile


@pytest.mark.parametrize("base", ["bin", "hex", "dec"])
def test_profile_round_trip_and_send_keep_selected_notation_and_leading_bits(tmp_path, base):
    async def exercise():
        studio = make_studio(tmp_path)
        studio.base.value = base
        studio._base_changed(None)
        profile = FrameConfig(word=5, bit_count=12, lsb_first=True)
        studio._set_profile(profile)
        await studio._save_profile(None)
        studio.word.value = ""
        studio._word_changed()
        await studio._load_profile(None)
        assert studio.base.value == base and studio.bit_count.value == "12"
        assert studio.current_config == profile
        await studio._toggle_connection()
        await studio._send(None)
        assert studio.last_sent == profile
        assert not studio.page.messages

    run_async(exercise())


@pytest.mark.parametrize("invalid_word", ["", "1" * 27, "0002"])
def test_invalid_binary_draft_keeps_stop_available_for_an_active_emission(tmp_path, invalid_word):
    async def exercise():
        studio = make_studio(tmp_path)
        studio.continuous.value = True
        studio._continuous_changed()
        await studio._toggle_connection()
        await studio._send(None)
        active = studio.last_sent
        assert studio.device_status.busy
        studio.word.value = invalid_word
        studio._word_changed()
        assert studio.current_config is None and studio.send_button.disabled
        assert not studio.stop_button.disabled
        assert studio.last_sent == active and studio.device_status.busy
        await studio._stop(None)
        assert not studio.device_status.busy and not studio.page.messages

    run_async(exercise())


def test_local_toolchain_buttons_wait_for_a_toolchain_file(tmp_path):
    studio = make_studio(tmp_path)
    # No toolchain.json: local tools cannot run until portable installation,
    # with an explanation instead of failing after a click.
    assert studio.build_button.disabled and studio.program_button.disabled
    assert studio.doctor_button.disabled
    assert studio.toolchain_note.visible
    assert "Charger le .bit sous Windows" in studio.toolchain_note.value
    (tmp_path / "toolchain.json").write_text("{}", encoding="utf-8")
    studio._toolchain_path_changed()
    assert not studio.toolchain_note.visible
    assert not studio.doctor_button.disabled and not studio.program_button.disabled


def test_programming_without_toolchain_keeps_the_uart_session(tmp_path, monkeypatch):
    from arty_frame_studio.transport import SerialDevice

    async def exercise():
        studio = make_studio(tmp_path)
        bitstream = tmp_path / "arty_frame.bit"
        bitstream.write_bytes(b"bit")
        studio.bitstream_path.value = str(bitstream)
        monkeypatch.setattr(SerialDevice, "connected", property(lambda self: True))
        studio.device = SerialDevice("COM7")
        closed = []

        async def toggle(_: object = None) -> None:
            closed.append(True)

        monkeypatch.setattr(studio, "_toggle_connection", toggle)
        await studio._program(None)
        assert not closed
        assert any("non configurée" in line for line in studio.log_lines)
        await studio._build(None)
        assert not studio.tool_pending
        assert sum("Chaîne FPGA locale non configurée" in line for line in studio.log_lines) == 2

    run_async(exercise())


def test_tool_messages_can_be_dismissed(tmp_path):
    studio = make_studio(tmp_path)
    studio.tool_panel.visible = True
    studio._dismiss_tool_panel()
    assert not studio.tool_panel.visible


def test_shutdown_releases_uart_even_if_scope_cleanup_fails(tmp_path, monkeypatch):
    async def exercise():
        studio = make_studio(tmp_path)
        closed = []
        studio.device = SimpleNamespace(close=lambda: closed.append("uart"))
        studio.poll_task = asyncio.create_task(asyncio.sleep(60))

        async def fail_scope():
            raise OSError("Scope disconnected during cleanup")

        monkeypatch.setattr(studio.scope_panel, "shutdown", fail_scope)
        with pytest.raises(OSError, match="Scope disconnected"):
            await studio.shutdown()
        assert studio.closing and studio.poll_task.cancelled()
        assert studio.device is None and closed == ["uart"]

    run_async(exercise())


def test_workspace_warnings_flag_onedrive_and_long_paths():
    from arty_frame_studio.app import workspace_warnings

    assert workspace_warnings(Path("C:/Arty")) == []
    synced = Path(
        "C:/Users/utilisateur/OneDrive - Entreprise/Documents/Projets/"
        "Programmation/Python/arty-frame-studio-main_v2/arty-frame-studio-main"
    )
    warnings = workspace_warnings(synced)
    assert len(warnings) == 2
    assert "OneDrive" in warnings[0] and "260" in warnings[1]
