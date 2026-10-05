"""Onglet Oscilloscope sans navigateur : démo, Run/Stop, réglages, curseurs, export."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from arty_frame_studio.app import Studio
from arty_frame_studio.model import FrameConfig
from arty_frame_studio.scope import ScopeSettings, TriggerSettings
from arty_frame_studio.scope_view import (
    MARGIN_X,
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
    assert not panel.warning.visible
    assert any("Oscilloscope connecté" in message for message, _ in logs)

    run_async(panel.disconnect())
    assert panel.scope is None and simulator.closed
    assert simulator.commands[-1] == ":RUN"  # the instrument is handed back running
    assert panel.identity.value == "Non connecté"
    assert panel.run_button.disabled


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
