"""Control-tree smoke tests without a browser, desktop or FPGA attached."""

import asyncio
from pathlib import Path

import pytest

from arty_frame_studio import app
from arty_frame_studio.app import Studio, waveform_signal_points
from arty_frame_studio.model import FrameConfig
from arty_frame_studio.protocol import Opcode
from arty_frame_studio.simulation import simulate
from arty_frame_studio.transport import CommandTimeout


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
