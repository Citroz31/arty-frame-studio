"""Onglet Oscilloscope sans navigateur : démo, Run/Stop, réglages, curseurs, export."""

import asyncio
import math
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from arty_frame_studio.app import Studio
from arty_frame_studio.model import FrameConfig
from arty_frame_studio.scope import (
    Acquisition,
    ChannelSettings,
    KeysightScope,
    Measurements,
    ScopeConnectionError,
    ScopeError,
    ScopeSettings,
    Trace,
    TriggerSettings,
)
from arty_frame_studio.scope_sim import SimulatedKeysight, signal_source
from arty_frame_studio.scope_view import (
    MARGIN_BOTTOM,
    MARGIN_TOP,
    MARGIN_X,
    SCREEN_HEIGHT,
    Cursors,
    ScopePanel,
    cursor_readout,
    scope_canvas_shapes,
)


class PageStub:
    def __init__(self):
        self.updates = 0

    def update(self):
        self.updates += 1


def run_async(coroutine):
    # Same supervision as tests/test_app.py: no reliance on asyncio's socketpair.
    async def supervise():
        async with asyncio.timeout(10):
            task = asyncio.create_task(coroutine)
            while not task.done():
                await asyncio.sleep(0.01)
            return await task

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(supervise())
    finally:
        loop.close()


def make_panel(tmp_path: Path, config: FrameConfig | None = None):
    logs: list[tuple[str, str]] = []
    frame = config or FrameConfig()
    panel = ScopePanel(
        PageStub(),
        project_root=tmp_path,
        log=lambda message, color: logs.append((message, color)),
        frame_source=lambda: frame,
    )
    panel.build()
    return panel, logs


def click(target, direction):
    return SimpleNamespace(control=SimpleNamespace(data=(target, direction)))


def test_demo_scope_measures_data_and_clock_then_disconnects(tmp_path):
    panel, logs = make_panel(tmp_path)
    assert panel.run_button.disabled and panel.single_button.disabled
    run_async(panel._toggle_connection())
    simulator = panel.scope._transport
    assert "DSO-X 1202A" in panel.identity.value
    assert panel.frequency_text[2].value == "10.00 MHz"
    assert panel.period_text[2].value == "T = 100.0 ns"
    assert "Rapport cyclique 50.0 %" in panel.detail_text[2].value
    # Alternating bits at 10 MHz: DATA reads 5 MHz, as on the user's DSOX1202A.
    assert panel.frequency_text[1].value == "5.000 MHz"
    assert panel.channel_title[2].value == "CH2 · CLK"
    traces = {shape.data for shape in panel.canvas.shapes if shape.data}
    assert {"trace:1", "trace:2"} <= traces
    assert panel.status.value.startswith("Acquisition 1 · déclenchée")
    assert not panel.run_button.disabled and not panel.export_button.disabled
    assert panel.screenshot_button.disabled  # no screen to copy in the demo
    assert panel.warning.visible and "fréquence DATA" in panel.warning.value
    assert any("Oscilloscope connecté" in message for message, _ in logs)

    run_async(panel.disconnect())
    assert panel.scope is None and simulator.closed
    assert ":RUN" not in simulator.commands  # Closing the app never starts the instrument.
    assert panel.identity.value == "Non connecté"
    assert panel.run_button.disabled


def hardware_panel(tmp_path, monkeypatch):
    from arty_frame_studio import scope_view

    present = [signal_source(FrameConfig())]
    instrument = SimulatedKeysight(lambda: present[0], noise=0)
    scope = KeysightScope(instrument)
    scope.apply_settings(
        ScopeSettings(time_scale=500e-9).with_channel(1, ChannelSettings(scale=10.0))
    )
    scope.capture()
    # A single frame is over; only the stopped record remains on the instrument.
    present[0] = None
    instrument.commands.clear()
    monkeypatch.setattr(scope_view, "VisaTransport", lambda resource: instrument)
    panel, logs = make_panel(tmp_path)
    panel.source.value = "visa"
    panel._source_changed()
    panel.address.value = "USB0::0x2A8D::0x0396::MY12345678::INSTR"
    return panel, instrument, logs


def test_hardware_connection_reads_stopped_screen_with_its_real_axes(tmp_path, monkeypatch):
    panel, instrument, _ = hardware_panel(tmp_path, monkeypatch)
    record = instrument.records[1]
    assert panel.read_button.disabled
    run_async(panel._toggle_connection())
    assert panel.acquisition.from_display and panel.acquisition_kind == "visa"
    assert panel.settings.time_scale == 500e-9
    assert panel.settings.channel(1).scale == 10.0
    assert panel.time_scale.value == repr(500e-9) and panel.vscale[1].value == repr(10.0)
    texts = [getattr(shape, "text", "") for shape in panel.canvas.shapes]
    assert any("10 V/div" in text for text in texts if text)
    assert any("500 ns/div" in text for text in texts if text)
    assert not any("Aucune acquisition" in text for text in texts if text)
    assert {"trace:1", "trace:2"} <= {shape.data for shape in panel.canvas.shapes}
    assert "écran existant lu" in panel.status.value
    assert "sans réarmer" in panel.acquisition_note.value
    assert not panel.read_button.disabled
    assert instrument.records[1] is record and not instrument.running
    assert not {":SINGle", ":TRIGger:FORCe", ":TER?", ":RUN"}.intersection(instrument.commands)


def test_hardware_run_and_read_screen_preserve_the_stopped_single(tmp_path, monkeypatch):
    panel, instrument, _ = hardware_panel(tmp_path, monkeypatch)
    record = instrument.records[1]
    panel.refresh.value = "0.2"

    async def scenario():
        await panel._toggle_connection()
        await panel._toggle_run()
        while panel.count < 3:
            await asyncio.sleep(0.01)
        await panel._toggle_run()
        await panel._read_screen()

    run_async(scenario())
    assert panel.count == 4 and not panel.running
    assert panel.acquisition.from_display
    assert instrument.records[1] is record and not instrument.running
    assert not {":STOP", ":SINGle", ":TRIGger:FORCe", ":TER?", ":RUN"}.intersection(
        instrument.commands
    )


def test_hardware_single_explicitly_requests_a_new_capture(tmp_path, monkeypatch):
    panel, instrument, _ = hardware_panel(tmp_path, monkeypatch)
    run_async(panel._toggle_connection())
    record = instrument.records[1]
    instrument.commands.clear()
    run_async(panel._single())
    assert not panel.acquisition.from_display
    assert ":SINGle" in instrument.commands
    assert ":TRIGger:FORCe" in instrument.commands  # Auto, after the one-shot frame ended.
    assert instrument.records[1] is not record
    assert "forcée sans front" in panel.status.value


@pytest.mark.parametrize("was_running", [False, True])
@pytest.mark.parametrize("action", ["disconnect", "shutdown"])
def test_hardware_close_keeps_the_instrument_run_state(tmp_path, monkeypatch, was_running, action):
    panel, instrument, _ = hardware_panel(tmp_path, monkeypatch)
    if was_running:
        instrument.write(":RUN")
    run_async(panel._toggle_connection())
    assert instrument.running is was_running
    instrument.commands.clear()
    run_async(getattr(panel, action)())
    assert panel.scope is None and instrument.closed
    assert instrument.running is was_running
    assert not {":RUN", ":STOP", ":SINGle", ":TRIGger:FORCe"}.intersection(instrument.commands)


def test_rejected_hardware_connection_closes_without_starting_the_scope(tmp_path, monkeypatch):
    panel, instrument, _ = hardware_panel(tmp_path, monkeypatch)
    query = instrument.query
    monkeypatch.setattr(
        instrument, "query", lambda text: "PULS" if text == ":TRIGger:MODE?" else query(text)
    )
    run_async(panel._toggle_connection())
    assert panel.scope is None and instrument.closed and not instrument.running
    assert ":RUN" not in instrument.commands


def test_front_panel_axes_are_drawn_even_when_screen_transfer_fails(tmp_path, monkeypatch):
    panel, _, logs = hardware_panel(tmp_path, monkeypatch)

    def unavailable(scope, **kwargs):
        raise ScopeError("Pas de trace disponible")

    monkeypatch.setattr(KeysightScope, "read_display", unavailable)
    run_async(panel._toggle_connection())
    assert panel.scope is not None and panel.acquisition is None
    texts = [getattr(shape, "text", "") for shape in panel.canvas.shapes]
    assert any("10 V/div" in text for text in texts if text)
    assert any("500 ns/div" in text for text in texts if text)
    assert any("Aucune acquisition" in text for text in texts if text)
    assert "Pas de trace disponible" in panel.status.value
    assert any("Pas de trace disponible" in message for message, _ in logs)
    assert not panel.read_button.disabled


def test_connection_choices_are_remembered(tmp_path):
    panel, _ = make_panel(tmp_path)
    panel.mapping[1].value = "latch"
    panel.refresh.value = "2.0"
    run_async(panel._toggle_connection())
    run_async(panel.disconnect())
    again, _ = make_panel(tmp_path)
    assert again.mapping[1].value == "latch" and again.refresh.value == "2.0"
    assert again.source.value == "demo" and again.address.disabled
    # A damaged file is ignored rather than blocking the tab.
    (tmp_path / "profiles" / "oscilloscope.json").write_text("{", encoding="utf-8")
    assert make_panel(tmp_path)[0].mapping[1].value == "data"


def test_lan_preferences_enable_the_address_field(tmp_path):
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "oscilloscope.json").write_text(
        '{"source": "lan", "address": "192.168.1.50", "mapping": {"2": "bogus"}}',
        encoding="utf-8",
    )
    panel, logs = make_panel(tmp_path)
    assert panel.source.value == "lan" and panel.address.value == "192.168.1.50"
    assert not panel.address.disabled and panel.mapping[2].value == "clk"
    assert "5025" in panel.source_hint.value


def test_unreachable_lan_scope_is_reported(tmp_path, monkeypatch):
    from arty_frame_studio import scope_view

    def unreachable(host):
        raise scope_view.ScopeError(f"Oscilloscope injoignable sur {host}:5025")

    monkeypatch.setattr(scope_view, "SocketTransport", unreachable)
    panel, logs = make_panel(tmp_path)
    panel.source.value = "lan"
    panel._source_changed()
    panel.address.value = "192.168.1.50"
    run_async(panel._toggle_connection())
    assert panel.scope is None and not panel.pending
    assert "injoignable" in panel.status.value
    assert any("injoignable" in message for message, _ in logs)
    assert not (tmp_path / "profiles" / "oscilloscope.json").exists()


def test_run_refreshes_until_stop(tmp_path):
    panel, _ = make_panel(tmp_path)
    panel.refresh.value = "0.2"

    async def scenario():
        await panel._toggle_connection()
        await panel._toggle_run()
        assert panel.running and panel.run_button.text == "Stop"
        while panel.count < 3:
            await asyncio.sleep(0.02)
        await panel._toggle_run()
        await panel.run_task

    run_async(scenario())
    assert not panel.running and panel.run_button.text == "Run"
    assert panel.count >= 3


def test_steppers_autoscale_and_frame_preset_drive_the_instrument(tmp_path):
    panel, logs = make_panel(tmp_path)

    async def scenario():
        await panel._toggle_connection()
        await panel._step(click("time", 1))
        await panel._step(click(1, -1))

    run_async(scenario())
    simulator = panel.scope._transport
    assert panel.settings.time_scale == 100e-9 and simulator.time_scale == 100e-9
    assert panel.settings.channel(1).scale == 0.5 and simulator.channels[1].scale == 0.5
    assert panel.time_scale.value == repr(100e-9)

    run_async(panel._autoscale())
    assert any("Auto scale appliqué" in message for message, _ in logs)
    assert panel.settings.trigger.source == 2  # the fastest signal: CLK
    assert panel.frequency_text[2].value == "10.00 MHz"

    panel.settings = ScopeSettings(time_scale=1e-6)
    run_async(panel._preset())
    assert panel.settings.time_scale == 50e-9 and simulator.time_scale == 50e-9
    assert panel.settings.trigger == TriggerSettings(source=2, level=1.65)


def test_normal_trigger_waits_then_the_50_percent_level_recovers(tmp_path):
    panel, logs = make_panel(tmp_path)
    panel.trigger_wait = 0.2

    async def scenario():
        await panel._toggle_connection()
        panel.trigger_sweep.value = "NORM"
        panel.trigger_level.value = "10"
        await panel._settings_changed()

    run_async(scenario())
    assert panel.status.value == "En attente de déclenchement (mode Normal)"
    assert any("Aucun déclenchement" in message for message, _ in logs)
    assert panel.count == 1  # the previous trace stays on screen

    run_async(panel._trigger_middle())
    assert abs(panel.settings.trigger.level - 1.65) < 0.1
    assert panel.count == 2 and "déclenchée" in panel.status.value


def test_invalid_entries_are_reported_without_reaching_the_scope(tmp_path):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    sent = len(panel.scope._transport.commands)
    panel.offset[1].value = "abc"
    run_async(panel._settings_changed())
    assert panel.status.value.startswith("Décalage CH1 : Valeur numérique attendue")
    assert len(panel.scope._transport.commands) == sent


def test_cursors_snap_to_a_clock_period_and_follow_the_pointer(tmp_path):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    panel.cursor_mode.value = "time"
    panel._cursors_changed()
    assert not panel.sliders["x1"].disabled and panel.sliders["y1"].disabled
    panel._snap_period()
    delta = (panel.cursors.x2 - panel.cursors.x1) * panel.settings.time_scale
    assert abs(delta - 100e-9) < 1e-9
    assert "1/ΔX 10.0" in panel.cursor_text.value or "1/ΔX 9.99" in panel.cursor_text.value
    cursors = {shape.data for shape in panel.canvas.shapes if shape.data}
    assert {"cursor:X1", "cursor:X2"} <= cursors

    plot_width = panel.width - 2 * MARGIN_X
    traces = [shape for shape in panel.canvas.shapes if shape.data == "trace:2"]
    panel._pointer(SimpleNamespace(local_x=MARGIN_X + 0.9 * plot_width, local_y=100))
    assert panel.cursors.x2 == 4.0 and panel.sliders["x2"].value == 4.0
    # Moving a cursor keeps the trace objects: only the cursors are sent again.
    assert [shape for shape in panel.canvas.shapes if shape.data == "trace:2"][0] is traces[0]

    panel.cursor_mode.value = "volt"
    panel.cursor_channel.value = "2"
    panel._cursors_changed()
    assert panel.cursor_text.value.startswith("CH2 · Y1")


@pytest.mark.parametrize(
    "mode,first,second,start,target",
    [
        ("time", "x1", "x2", -1, 2),
        ("volt", "y1", "y2", -1, 2),
    ],
)
def test_cursor_drag_keeps_the_selected_cursor_when_crossing_another(
    tmp_path, mode, first, second, start, target
):
    panel, _ = make_panel(tmp_path)
    panel.cursor_mode.value = mode
    panel._cursors_changed()
    panel._set_cursors(**{first: start, second: 1})

    def point(position):
        width = panel.width - 2 * MARGIN_X
        height = SCREEN_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
        return SimpleNamespace(
            local_x=MARGIN_X + (position + 5) / 10 * width,
            local_y=MARGIN_TOP + (4 - position) / 8 * height,
        )

    panel.screen.on_pan_start(point(start))
    # A fast pointer update beyond the other line used to switch selection.
    panel.screen.on_pan_update(point(target))
    assert getattr(panel.cursors, first) == target
    assert getattr(panel.cursors, second) == 1
    # Leaving the screen clamps the selected cursor, without moving the other.
    panel.screen.on_pan_update(point(20))
    assert getattr(panel.cursors, first) == (5 if mode == "time" else 4)
    assert getattr(panel.cursors, second) == 1
    panel.screen.on_pan_end(SimpleNamespace())
    panel.screen.on_tap_down(point(0.5))
    assert getattr(panel.cursors, second) == 0.5


def test_cursor_drag_selection_is_reset_when_cursor_mode_changes(tmp_path):
    panel, _ = make_panel(tmp_path)
    panel.cursor_mode.value = "time"
    panel._cursors_changed()
    panel._pan_start(SimpleNamespace(local_x=panel.width / 2, local_y=100))
    assert panel._drag_cursor in ("x1", "x2")
    panel.cursor_mode.value = "off"
    panel._cursors_changed()
    assert panel._drag_cursor is None


def test_csv_export_writes_both_channels(tmp_path):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    panel._export_csv()
    files = list((tmp_path / "exports").glob("oscilloscope-*.csv"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert lines[0] == "temps_CH1_s,CH1_V,temps_CH2_s,CH2_V"
    assert len(lines) == 1 + panel.settings.points
    assert str(files[0]) in panel.export_note.value


def test_canvas_without_acquisition_shows_markers_and_help(tmp_path):
    settings = ScopeSettings()
    shapes = scope_canvas_shapes(None, settings, Cursors(), 900, labels={1: "DATA"})
    texts = [shape.text for shape in shapes if getattr(shape, "text", None)]
    assert any("Aucune acquisition" in text for text in texts)
    assert {"1▶", "2▶", "◀T", "▼"} <= set(texts)
    assert any(text.startswith("CH1 DATA · 1 V/div") for text in texts)
    single = ScopeSettings((settings.channel(1), settings.channel(2).__class__(enabled=False)))
    shapes = scope_canvas_shapes(None, single, Cursors(), 900)
    texts = [getattr(shape, "text", None) for shape in shapes]
    assert "2▶" not in texts
    assert cursor_readout(Cursors(), settings) == "Curseurs désactivés."
    readout = cursor_readout(Cursors(mode="both", x1=0, x2=2, y1=1, y2=-1), settings)
    assert "ΔX 100.0 ns" in readout and "1/ΔX 10.00 MHz" in readout
    assert "ΔY -2.000 V" in readout


def test_scope_tab_is_part_of_the_studio_and_closes_with_it(tmp_path):
    studio = Studio(PageStub(), tmp_path)
    studio.layout()
    studio._changed()
    panel = studio.scope_panel
    run_async(panel._toggle_connection())
    simulator = panel.scope._transport
    # The demo follows the frame of the Pilotage tab.
    studio.frequency.value = "5"
    studio._frequency_changed(None)
    run_async(panel._single())
    assert panel.frequency_text[2].value == "5.000 MHz"
    run_async(studio.shutdown())
    assert simulator.closed and panel.scope is None


async def wait_for_thread(event: threading.Event) -> None:
    while not event.is_set():
        await asyncio.sleep(0.005)


def pause_capture(panel, monkeypatch):
    started, release = threading.Event(), threading.Event()
    capture = panel.scope.capture

    def blocked_capture(**kwargs):
        acquisition = capture(**kwargs)
        started.set()
        assert release.wait(3), "Test did not release the capture thread"
        return acquisition

    monkeypatch.setattr(panel.scope, "capture", blocked_capture)
    return started, release


def test_stop_waits_for_capture_and_a_restart_owns_one_loop(tmp_path, monkeypatch):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    started, release = pause_capture(panel, monkeypatch)

    async def scenario():
        await panel._toggle_run()
        await wait_for_thread(started)
        previous = panel.run_task
        stop = asyncio.create_task(panel._toggle_run())
        await asyncio.sleep(0.01)
        assert panel.stopping and not stop.done()
        assert panel.run_button.disabled
        await panel._toggle_run()  # An event queued during Stop cannot restart early.
        assert panel.run_task is previous
        release.set()
        await stop
        assert previous.done() and not panel.running
        panel.refresh.value = "5.0"
        await panel._toggle_run()
        assert panel.run_task is not previous
        while panel.count < 3:
            await asyncio.sleep(0.005)
        await panel._toggle_run()  # Wakes the five-second refresh pause immediately.
        assert panel.run_task.done()

    run_async(scenario())
    assert not panel.running


def test_shutdown_waits_for_a_capture_thread_without_publishing_it(tmp_path, monkeypatch):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    simulator = panel.scope._transport
    started, release = pause_capture(panel, monkeypatch)

    async def scenario():
        single = asyncio.create_task(panel._single())
        await wait_for_thread(started)
        assert panel.capturing and panel.single_button.disabled
        assert panel.autoscale_button.disabled and panel.run_button.disabled
        count = panel.count
        close = asyncio.create_task(panel.shutdown())
        await asyncio.sleep(0.01)
        assert not close.done() and not simulator.closed
        release.set()
        await asyncio.gather(single, close)
        assert panel.count == count  # No UI acquisition after shutdown began.

    run_async(scenario())
    assert simulator.closed and panel.scope is None
    assert ":RUN" not in simulator.commands


def test_shutdown_during_connection_closes_the_new_scope(tmp_path, monkeypatch):
    panel, _ = make_panel(tmp_path)
    started, release = threading.Event(), threading.Event()
    opened = []
    open_scope = panel._open

    def blocked_open():
        started.set()
        assert release.wait(3)
        result = open_scope()
        opened.append(result[0])
        return result

    monkeypatch.setattr(panel, "_open", blocked_open)

    async def scenario():
        connect = asyncio.create_task(panel._toggle_connection())
        await wait_for_thread(started)
        close = asyncio.create_task(panel.shutdown())
        await asyncio.sleep(0.01)
        assert not close.done()
        release.set()
        await asyncio.gather(connect, close)

    run_async(scenario())
    assert panel.scope is None and opened[0]._transport.closed
    assert ":RUN" not in opened[0]._transport.commands
    assert panel.count == 0


def test_shutdown_joins_a_disconnect_already_waiting_for_a_capture(tmp_path, monkeypatch):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    started, release = pause_capture(panel, monkeypatch)

    async def scenario():
        single = asyncio.create_task(panel._single())
        await wait_for_thread(started)
        disconnect = asyncio.create_task(panel.disconnect())
        await asyncio.sleep(0.01)
        close = asyncio.create_task(panel.shutdown())
        await asyncio.sleep(0.01)
        assert not close.done() and not disconnect.done()
        release.set()
        await asyncio.gather(single, disconnect, close)

    run_async(scenario())
    assert panel.scope is None and not panel.disconnecting


def test_pending_settings_survive_an_older_capture(tmp_path, monkeypatch):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    started, release = pause_capture(panel, monkeypatch)

    async def scenario():
        capture = asyncio.create_task(panel._single())
        await wait_for_thread(started)
        panel.time_scale.value = repr(100e-9)
        apply = asyncio.create_task(panel._settings_changed())
        await asyncio.sleep(0.01)
        assert panel.applying == 1 and panel.settings.time_scale == 100e-9
        assert panel._display_settings().time_scale == 50e-9
        assert "Dernière trace conservée" in panel.acquisition_note.value
        release.set()
        await asyncio.gather(capture, apply)

    run_async(scenario())
    assert panel.settings.time_scale == panel.acquisition.settings.time_scale == 100e-9
    assert panel.time_scale.value == repr(100e-9) and panel.applying == 0


def test_disconnect_discards_settings_waiting_behind_a_capture(tmp_path, monkeypatch):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    simulator = panel.scope._transport
    started, release = pause_capture(panel, monkeypatch)

    async def scenario():
        capture = asyncio.create_task(panel._single())
        await wait_for_thread(started)
        panel.time_scale.value = repr(100e-9)
        apply = asyncio.create_task(panel._settings_changed())
        await asyncio.sleep(0.01)
        assert panel.applying == 1
        close = asyncio.create_task(panel.disconnect())
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(capture, apply, close)

    run_async(scenario())
    assert simulator.closed and simulator.time_scale == 50e-9
    assert panel.scope is None and panel.applying == 0
    assert "déconnecté" in panel.acquisition_note.value


def test_transport_failure_disconnects_but_preserves_the_last_trace(tmp_path, monkeypatch):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    previous = panel.acquisition
    simulator = panel.scope._transport

    def interrupted(**kwargs):
        raise ScopeConnectionError("Liaison interrompue.")

    monkeypatch.setattr(panel.scope, "capture", interrupted)
    run_async(panel._single())
    assert panel.scope is None and simulator.closed and not panel.capturing
    assert panel.acquisition is previous
    assert "Reconnecter" in panel.status.value
    assert panel.run_button.disabled and not panel.export_button.disabled


def test_rejected_settings_restore_actual_instrument_values(tmp_path, monkeypatch):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    previous = panel.settings

    def rejected(settings):
        raise ScopeError("Réglage refusé")

    monkeypatch.setattr(panel.scope, "apply_settings", rejected)
    panel.time_scale.value = repr(100e-9)
    run_async(panel._settings_changed())
    assert panel.settings == previous and panel.time_scale.value == repr(previous.time_scale)
    assert panel.status.value == "Réglage refusé" and panel.applying == 0
    assert "nouveaux réglages" not in panel.acquisition_note.value


def test_last_trace_axes_and_cursors_stay_on_the_acquisition_settings(tmp_path):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    previous = panel.acquisition
    panel.trigger_wait = 0.05
    panel.cursor_mode.value = "time"
    panel._cursors_changed()
    panel.trigger_sweep.value = "NORM"
    panel.trigger_level.value = "10"
    panel.time_scale.value = repr(100e-9)
    run_async(panel._settings_changed())
    assert panel.acquisition is previous and panel.settings.time_scale == 100e-9
    assert panel.cursor_text.value == cursor_readout(panel.cursors, previous.settings)
    texts = [getattr(shape, "text", "") for shape in panel.canvas.shapes]
    assert any("50 ns/div" in text for text in texts if text)
    assert "Dernière trace conservée" in panel.acquisition_note.value


def test_nonstandard_readback_values_are_preserved_when_another_control_changes(tmp_path):
    panel, _ = make_panel(tmp_path)
    settings = ScopeSettings(time_scale=75e-9).with_channel(
        1, replace(ScopeSettings().channel(1), scale=0.75, probe=20.0)
    )
    panel.settings = settings
    panel._show_settings(settings)
    panel.trigger_level.value = "2"
    result = panel._settings_from_controls()
    assert result.time_scale == 75e-9
    assert result.channel(1).scale == 0.75 and result.channel(1).probe == 20.0


def test_demo_measurements_and_exports_are_identified_and_exports_do_not_overwrite(tmp_path):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    assert "Simulation" in panel.detail_text[1].value
    assert "aucun signal réel" in panel.acquisition_note.value
    panel._export_csv()
    panel._export_csv()
    files = list((tmp_path / "exports").glob("oscilloscope-*-simulation.csv"))
    assert len(files) == 2 and "simulés" in panel.export_note.value


def test_empty_numeric_entry_is_rejected_and_bad_refresh_falls_back(tmp_path):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    sent = len(panel.scope._transport.commands)
    panel.trigger_level.value = ""
    run_async(panel._settings_changed())
    assert panel.status.value.startswith("Niveau : Valeur numérique attendue")
    assert len(panel.scope._transport.commands) == sent
    for value in ("nan", "inf", "0", "-1", "oops"):
        panel.refresh.value = value
        assert panel._interval() == 0.5


def test_demo_resolution_warning_uses_the_known_clock_not_an_aliased_local_value(tmp_path):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    panel.acquisition = replace(
        panel.acquisition,
        traces=(Trace(2, (0.0, 100e-9), (0.0, 3.3)),),
        measurements={2: Measurements(frequency=1e6, source="local")},
    )
    panel._show_measurements()
    assert panel.warning.visible and "au plus 1 points par période" in panel.warning.value


def test_real_resolution_warning_uses_an_instrument_measurement(tmp_path):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    panel.acquisition_kind = "lan"
    panel.acquisition_expected_clock_hz = None
    panel.acquisition = replace(
        panel.acquisition,
        traces=(Trace(2, (0.0, 100e-9), (0.0, 3.3)),),
        measurements={2: Measurements(frequency=10e6, source="oscilloscope")},
    )
    panel._show_measurements()
    assert "des fronts peuvent manquer" in panel.warning.value


def test_screenshot_file_error_is_reported_and_releases_busy_state(tmp_path, monkeypatch):
    panel, _ = make_panel(tmp_path)
    run_async(panel._toggle_connection())
    panel.source.value = "lan"
    monkeypatch.setattr(panel.scope, "screenshot", lambda: b"PNG")
    (tmp_path / "exports").write_text("A file blocks the export directory", encoding="utf-8")
    run_async(panel._screenshot())
    assert not panel.pending and panel.status.value.startswith("Copie d'écran :")
    assert not panel.screenshot_button.disabled


def test_search_failure_is_reported_and_releases_busy_state(tmp_path, monkeypatch):
    from arty_frame_studio import scope_view

    panel, _ = make_panel(tmp_path)
    panel.source.value = "visa"
    panel._source_changed()

    def failed_search():
        raise ScopeError("VISA indisponible")

    monkeypatch.setattr(scope_view, "list_visa_resources", failed_search)
    run_async(panel._search())
    assert panel.status.value == "Recherche VISA : VISA indisponible"
    assert not panel.pending and not panel.search_button.disabled


def test_connection_details_fold_after_connect_and_can_be_reopened(tmp_path):
    panel, _ = make_panel(tmp_path)
    assert panel.connection_details.visible and not panel.connection_toggle.visible
    run_async(panel._toggle_connection())
    assert not panel.connection_details.visible and panel.connection_toggle.visible
    panel._toggle_connection_details()
    assert panel.connection_details.visible
    panel._toggle_connection_details()
    assert not panel.connection_details.visible
    run_async(panel.disconnect())
    assert panel.connection_details.visible and not panel.connection_toggle.visible


def test_changing_source_updates_address_controls_in_the_page(tmp_path):
    panel, _ = make_panel(tmp_path)
    updates = panel.page.updates
    panel.source.value = "lan"
    panel._source_changed(SimpleNamespace(control=panel.source))
    assert not panel.address.disabled and panel.page.updates == updates + 1


def test_each_drawn_acquisition_is_stamped_on_the_screen(tmp_path):
    # Retour d'essai : « Acquisition 11 » dans l'état, mais un écran resté vide.
    # Le numéro écrit dans le canevas montre si le dessin suit l'acquisition.
    panel, _ = make_panel(tmp_path)

    async def scenario():
        await panel._toggle_connection()
        await panel._single()

    run_async(scenario())
    stamps = [shape.text for shape in panel.canvas.shapes if shape.data == "stamp"]
    assert len(stamps) == 1 and stamps[0].startswith(f"Acq. {panel.count} · ")
    run_async(panel.disconnect())


def test_non_finite_points_never_reach_the_canvas():
    settings = ScopeSettings()
    trace = Trace(1, (0.0, 1e-8, float("nan"), 3e-8), (0.0, 3.3, 1.0, float("inf")))
    acquisition = Acquisition(settings, (trace,), {}, True)
    shapes = scope_canvas_shapes(acquisition, settings, Cursors(), 900)
    path = next(shape for shape in shapes if shape.data == "trace:1")
    coordinates = [value for element in path.elements for value in (element.x, element.y)]
    assert len(path.elements) == 2 and all(math.isfinite(value) for value in coordinates)
    # A trace with no finite point draws nothing rather than failing.
    empty = Trace(1, (float("nan"),), (0.0,))
    shapes = scope_canvas_shapes(
        Acquisition(settings, (empty,), {}, True), settings, Cursors(), 900
    )
    assert not [shape for shape in shapes if shape.data == "trace:1"]
