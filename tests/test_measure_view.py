"""Onglet Mesure sans navigateur : balayage de mots, validations et verrous."""

import asyncio
import csv
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from arty_frame_studio.app import Studio
from arty_frame_studio.measure_view import MeasurePanel, results_chart_shapes
from arty_frame_studio.model import FrameConfig
from arty_frame_studio.sweep import FAIL, OK, SKIPPED, Limit, StepResult, result_line


class PageStub:
    def __init__(self):
        self.updates = 0

    def update(self):
        self.updates += 1


def run_async(coroutine):
    async def supervise():
        async with asyncio.timeout(15):
            task = asyncio.create_task(coroutine)
            while not task.done():
                await asyncio.sleep(0.01)
            return await task

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(supervise())
    finally:
        loop.close()


def make_studio(tmp_path: Path) -> Studio:
    studio = Studio(PageStub(), tmp_path)
    studio.layout()
    studio._changed()
    return studio


def click(action):
    return SimpleNamespace(control=SimpleNamespace(data=action))


async def wait_until(condition, timeout=8.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


def configure(panel, *, words="00000000\n00000001\n00000010", probe="none"):
    panel.word_list.value = words
    panel.probe_kind.value = probe
    panel.settle.value = "0"
    panel._mode_changed()


async def connected_studio(tmp_path):
    studio = make_studio(tmp_path)
    await studio._toggle_connection()  # demo board
    assert studio.device_ready()
    return studio, studio.measure_panel


def test_tab_is_part_of_the_studio_and_start_needs_a_connected_board(tmp_path):
    studio = make_studio(tmp_path)
    panel = studio.measure_panel
    assert studio.tabs.tabs[3].text == "Mesure" and studio.tabs.tabs[4].text == "FPGA"
    assert panel.start_button.disabled and "Connecter la carte" in panel.status.value
    assert "4 mot(s) de 8 bits" in panel.words_note.value or "mot(s)" in panel.words_note.value


def test_words_are_previewed_with_the_inferred_width_and_errors(tmp_path):
    panel = make_studio(tmp_path).measure_panel
    # 24-digit words fix the frame width: the Pilotage keeps its own 26 bits.
    panel.word_list.value = "000000000000000000000000\n000000000000000000000001"
    panel._words_changed()
    assert "2 mot(s) de 24 bits" in panel.words_note.value
    assert "le Pilotage est sur 26 bits" in panel.words_note.value
    panel.word_list.value = "0102"
    panel._words_changed()
    assert panel.words_note.color != panel.stats.color and "invalide" in panel.words_note.value
    panel.word_mode.value = "counter"
    panel.counter_start.value, panel.counter_stop.value, panel.counter_step.value = "0", "1011", "1"
    panel.width_field.value = "4"
    panel._mode_changed()
    assert "12 mot(s) de 4 bits · 0000 → 1011" in panel.words_note.value
    assert panel.counter_row.visible and not panel.word_list.visible
    panel.word_mode.value = "walk1"
    panel._mode_changed()
    assert "4 mot(s) de 4 bits · 0001 → 1000" in panel.words_note.value
    panel.width_field.value = "99"
    panel._words_changed()
    assert "Largeur entre 1 et 26" in panel.words_note.value


def test_sweep_without_measurement_sends_every_word_to_the_board(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure(panel, words="00000000\n00000001\n00000010\n00000011")
        sent = []
        original = studio.device.send

        def spy(config):
            sent.append((config.word, config.bit_count, config.repeat_count))
            return original(config)

        studio.device.send = spy
        await panel._start()
        await wait_until(lambda: not panel.running and panel.summary is not None)
        return studio, panel, sent

    studio, panel, sent = run_async(scenario())
    assert sent == [(0, 8, 1), (1, 8, 1), (2, 8, 1), (3, 8, 1)]
    assert panel.summary.reason == "finished" and panel.summary.counts[OK] == 4
    assert "Terminé · 4 OK" in panel.status.value
    # The manual send button and the connection stay unlocked afterwards.
    assert not studio.sweep_active and not studio.connect_button.disabled
    assert len(panel.results_list.controls) == 4 and not panel.manual_card.visible
    assert (tmp_path / "profiles" / "mesure.json").is_file()


def test_manual_validation_waits_for_each_decision(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure(panel, words="0001\n0010\n0011\n0100", probe="manual")
        panel.width_field.value = "4"
        await panel._start()
        await wait_until(lambda: panel.manual_card.visible)
        assert "Mot 1/4 envoyé : 0001" in panel.manual_title.value
        assert studio.sweep_active and studio.send_button.disabled  # locked while waiting
        panel.manual_value.value = "-3,5"
        await panel._decide(click("ok"))
        await wait_until(lambda: "Mot 2/4" in panel.manual_title.value)
        before = panel.manual._future
        await panel._decide(click("retry"))
        # The same word is sent again and a new validation is awaited.
        await wait_until(lambda: panel.manual._future is not before and panel.manual.waiting)
        assert len(panel.results) == 1 and "Mot 2/4" in panel.manual_title.value
        panel.manual_note.value = "phase fausse"
        await panel._decide(click("fail"))
        await wait_until(lambda: "Mot 3/4" in panel.manual_title.value)
        await panel._decide(click("skip"))
        await wait_until(lambda: "Mot 4/4" in panel.manual_title.value)
        await panel._decide(click("ok"))
        await wait_until(lambda: not panel.running)
        return studio, panel

    studio, panel = run_async(scenario())
    assert [r.status for r in panel.results] == [OK, FAIL, SKIPPED, OK]
    assert panel.results[0].values == (-3.5,) and panel.results[1].note == "phase fausse"
    assert not studio.sweep_active
    assert "1 échec(s)" in panel.status.value


def test_invalid_manual_value_is_reported_and_keeps_waiting(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure(panel, words="1\n0", probe="manual")
        panel.width_field.value = "1"
        await panel._start()
        await wait_until(lambda: panel.manual_card.visible)
        panel.manual_value.value = "abc"
        await panel._decide(click("ok"))
        assert "Valeur relevée" in panel.status.value and panel.manual.waiting
        await panel._stop()
        await wait_until(lambda: not panel.running)
        return panel

    panel = run_async(scenario())
    assert panel.summary.reason == "stopped" and panel.continue_button.visible


def test_stop_then_resume_continues_at_the_next_word(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure(panel, words="001\n010\n011", probe="manual")
        panel.width_field.value = "3"
        await panel._start()
        await wait_until(lambda: panel.manual_card.visible)
        await panel._decide(click("ok"))
        await wait_until(lambda: "Mot 2/3" in panel.manual_title.value)
        await panel._stop()
        await wait_until(lambda: not panel.running)
        assert panel.summary.next_index == 1 and panel.continue_button.visible
        await panel._continue()
        await wait_until(
            lambda: panel.manual_card.visible and "Mot 2/3" in panel.manual_title.value
        )
        await panel._decide(click("ok"))
        await wait_until(lambda: "Mot 3/3" in panel.manual_title.value)
        await panel._decide(click("ok"))
        await wait_until(lambda: not panel.running)
        return panel

    panel = run_async(scenario())
    assert [r.word for r in panel.results] == [1, 2, 3] and panel.summary.reason == "finished"


def test_demo_instrument_measures_a_value_per_word_with_limits(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure(panel, words="000000\n000100\n001000", probe="instrument")
        panel.width_field.value = "6"
        await panel._toggle_instrument()
        assert "SIMULATED VNA" in panel.instrument_status.value and panel.instrument is not None
        panel.preset.value = "1"
        panel._preset_chosen(object())
        assert "CALCulate:MARKer1:Y?" in panel.instrument_reads.value
        panel.instrument_labels.value = "S21 (dB), phase (°)"
        panel.limit_low.value = "-3"
        await panel._start()
        await wait_until(lambda: not panel.running)
        # Connected instrument cannot be disconnected mid-run, and can be afterwards.
        await panel._toggle_instrument()
        return panel

    panel = run_async(scenario())
    assert [r.values[0] for r in panel.results] == [-1.5, -2.5, -3.5]
    assert [r.status for r in panel.results] == [OK, OK, FAIL]
    assert panel.results[0].names == ("S21 (dB)", "phase (°)")
    assert "min -3.5" in panel.stats.value and panel.instrument is None
    assert any(shape.data == "chart:line" for shape in panel.chart.shapes)


def test_instrument_probe_needs_a_connection_and_read_commands(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure(panel, probe="instrument")
        await panel._start()
        first = panel.status.value
        await panel._toggle_instrument()
        await panel._start()
        return first, panel.status.value

    first, second = run_async(scenario())
    assert "Instrument non connecté" in first
    assert "au moins une commande de lecture" in second


def test_scope_probe_uses_the_scope_tab_connection(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure(panel, words="00000001\n00000010", probe="scope")
        await panel._start()
        refused = panel.status.value
        await studio.scope_panel._toggle_connection()  # demo scope
        assert studio.scope_ready()
        panel.scope_quantity["A"].value = "frequency"
        panel.scope_channel["A"].value = "2"
        panel.scope_quantity["B"].value = "vpp"
        panel.scope_channel["B"].value = "2"
        await panel._start()
        locked = studio.scope_panel.run_button.disabled
        await wait_until(lambda: not panel.running)
        unlocked = studio.scope_panel.sweep_locked
        return refused, panel, locked, unlocked

    refused, panel, locked, unlocked = run_async(scenario())
    assert "Oscilloscope non connecté" in refused
    assert [r.status for r in panel.results] == [OK, OK]
    names = panel.results[0].names
    assert names == ("CH2 fréquence (Hz)", "CH2 Vpp (V)")
    assert abs(panel.results[0].values[0] - 10e6) < 1e5  # the demo CLK on CH2
    assert locked and not unlocked


def test_send_failure_stops_with_the_word_and_unlocks_the_application(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure(panel, words="1\n0")
        panel.width_field.value = "1"
        studio.device.close()  # the board disappears
        await panel._start()
        await wait_until(lambda: not panel.running)
        return studio, panel

    studio, panel = run_async(scenario())
    assert panel.summary.reason == "error" and "Envoi du mot 1" in panel.status.value
    assert not studio.sweep_active and not panel.manual_card.visible


def test_csv_export_and_clear(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure(panel, words="01\n10", probe="none")
        panel.width_field.value = "2"
        await panel._start()
        await wait_until(lambda: not panel.running)
        panel._export_csv()
        files = list((tmp_path / "exports").glob("mesure-*.csv"))
        panel._clear()
        return panel, files

    panel, files = run_async(scenario())
    rows = list(csv.reader(files[0].read_text(encoding="utf-8").splitlines()))
    assert rows[0][:5] == ["pas", "mot_bin", "mot_hex", "mot_dec", "statut"]
    assert [row[1] for row in rows[1:]] == ["01", "10"] and str(files[0]) in panel.export_note.value
    assert not panel.results and panel.status.value == "Prêt" and panel.export_button.disabled


def test_preferences_are_remembered_and_a_broken_file_is_ignored(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure(panel, words="0011\n0101", probe="manual")
        panel.stop_on_fail.value = True
        panel.limit_high.value = "2"
        await panel._start()
        await wait_until(lambda: panel.manual_card.visible)
        await panel._stop()
        await wait_until(lambda: not panel.running)

    run_async(scenario())
    again = make_studio(tmp_path).measure_panel
    assert again.word_list.value == "0011\n0101" and again.probe_kind.value == "manual"
    assert again.stop_on_fail.value is True and again.limit_high.value == "2"
    (tmp_path / "profiles" / "mesure.json").write_text("[", encoding="utf-8")
    assert make_studio(tmp_path).measure_panel.word_mode.value == "list"
    (tmp_path / "profiles" / "mesure.json").write_text(
        '{"probe": "bogus", "word_mode": "counter", "settle": 7}', encoding="utf-8"
    )
    other = make_studio(tmp_path).measure_panel
    assert other.probe_kind.value == "manual" and other.word_mode.value == "counter"


def test_continuous_pilotage_settings_do_not_hang_the_sweep(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        studio.continuous.value = True
        studio._changed()
        assert studio.current_config.continuous
        configure(panel, words="1\n0")
        panel.width_field.value = "1"
        panel.repeats.value = "3"
        await panel._start()
        await wait_until(lambda: not panel.running)
        return panel

    panel = run_async(scenario())
    assert panel.summary.reason == "finished" and panel.runner.base.repeat_count == 3


def test_chart_shows_values_limits_and_decimates_long_runs():
    empty = results_chart_shapes([], 800)
    assert any("apparaît" in getattr(shape, "text", "") for shape in empty)
    results = [
        StepResult(i, i, 8, FAIL if i == 5 else OK, (math.sin(i / 5),), ("niveau (V)",))
        for i in range(10)
    ]
    shapes = results_chart_shapes(results, 800, limit=Limit(-0.5, 0.5), label="niveau (V)")
    circles = [shape for shape in shapes if str(shape.data).startswith("chart:point:")]
    assert len(circles) == 10 and any(shape.data == "chart:line" for shape in shapes)
    texts = [shape.text for shape in shapes if getattr(shape, "text", None)]
    assert "niveau (V)" in texts and "pas 1" in texts and "pas 10" in texts
    many = [StepResult(i, i, 20, OK, (float(i % 7),)) for i in range(20_000)]
    long = results_chart_shapes(many, 800)
    line = next(shape for shape in long if shape.data == "chart:line")
    assert len(line.elements) <= 1600 and not [s for s in long if "point" in str(s.data)]
    flat = results_chart_shapes(
        [StepResult(0, 0, 8, OK, (1.0,)), StepResult(1, 1, 8, OK, (1.0,))], 800
    )
    assert any(shape.data == "chart:line" for shape in flat)  # constant values still draw


def test_result_lines_show_word_status_and_values():
    line = result_line(StepResult(2, 5, 8, FAIL, (-3.5, math.nan), ("S21", "phase"), "hors limite"))
    assert line.startswith("    3  00000101  Échec") and "S21 -3.5" in line
    assert "phase —" in line and line.endswith("hors limite")
    assert "OK" in result_line(StepResult(0, 0, 8, OK))
    assert isinstance(MeasurePanel, type) and FrameConfig is not None


# -- états, TR et VNA ------------------------------------------------------------------------
STATES_TEXT = "000000000100 ; TX ; premier\n000001000100 ; RX ; second\n000000000111"


def configure_states(panel, text=STATES_TEXT, *, probe="none"):
    panel.word_mode.value = "states"
    panel.state_list.value = text
    panel.probe_kind.value = probe
    panel.settle.value = "0"
    panel._mode_changed()


def test_states_file_is_loaded_and_previewed_with_its_tr_columns(tmp_path):
    panel = make_studio(tmp_path).measure_panel
    path = tmp_path / "etats.csv"
    path.write_text("mot;TR;nom\n0001 ; TX ; haut\n0010 ; RX\n0011\n", encoding="utf-8")
    panel.load_states_file(path)
    assert panel.word_mode.value == "states" and panel.states_box.visible
    assert not panel.word_list.visible and "0001" in panel.state_list.value
    assert (
        "3 état(s) de 4 bits · 0001 → 0011 · TR : 1 TX, 1 RX, 1 sans TR" in panel.words_note.value
    )
    assert panel.words_note.value.startswith("etats.csv")
    panel.tx_level.value = "0"  # TX devient le niveau bas
    panel._words_changed()
    assert [state.tr for state in panel._states] == [0, 1, None]
    panel.state_list.value = "0001 ; ALLUME"
    panel._words_changed()
    assert "Ligne 1 : TR « ALLUME »" in panel.words_note.value
    panel.load_states_file(tmp_path / "absent.csv")
    assert "illisible" in panel.words_note.value


def test_states_drive_the_tr_pin_before_each_word_and_keep_it_in_the_results(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure_states(panel)
        events = []
        send, tr = studio.device.send, studio.device.tr
        studio.device.send = lambda config: (events.append(("send", config.word)), send(config))[1]
        studio.device.tr = lambda level: (events.append(("tr", level)), tr(level))[1]
        await panel._start()
        await wait_until(lambda: not panel.running and panel.summary is not None)
        panel._export_csv()
        return studio, panel, events

    studio, panel, events = run_async(scenario())
    assert events == [("tr", 1), ("send", 4), ("tr", 0), ("send", 68), ("send", 7)]
    assert studio.device.tr_level == 0
    assert [(r.tr, r.name) for r in panel.results] == [(1, "premier"), (0, "second"), (None, "")]
    assert "TR1 « premier »" in panel.results_list.controls[0].value
    (csv_file,) = (tmp_path / "exports").glob("mesure-*.csv")
    header = csv_file.read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("pas,mot_bin,mot_hex,mot_dec,tr,nom,statut")


def test_tr_states_are_refused_with_a_firmware_that_has_no_tr_pin(tmp_path):
    from dataclasses import replace

    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        old = replace(studio.device.firmware, revision=4, capabilities=0x0F)
        studio.device.firmware = studio.firmware_info = old
        configure_states(panel)
        panel._words_changed()
        preview = panel.words_note.value
        await panel._start()
        refused = panel.status.value
        configure_states(panel, "000000000100\n000000000101")  # sans colonne TR : accepté
        await panel._start()
        await wait_until(lambda: not panel.running and panel.summary is not None)
        return preview, refused, panel

    preview, refused, panel = run_async(scenario())
    assert "n'a pas de broche TR" in preview and "révision 5" in refused
    assert panel.summary.reason == "finished" and panel.summary.counts[OK] == 2


def connect_vna_simulation(panel):
    async def connect():
        panel.probe_kind.value = "vna"
        panel.instrument_kind.value = "demo"
        panel._mode_changed()
        await panel._toggle_instrument()

    return connect()


def test_vna_campaign_writes_one_touchstone_file_per_state_and_restores_the_instrument(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure_states(panel, probe="vna")
        panel.vna_folder_input.value = str(tmp_path / "campagne")
        panel.vna_ports.value = "2"
        panel.vna_freq.value = "3.5G"
        await connect_vna_simulation(panel)
        assert "N5245B-SIM" in panel.instrument_status.value
        assert panel.vna_box.visible and panel.connection_box.visible
        simulated = panel.simulated
        await panel._start()
        await wait_until(lambda: not panel.running and panel.summary is not None)
        return studio, panel, simulated

    studio, panel, simulated = run_async(scenario())
    folder = tmp_path / "campagne"
    assert panel.summary.reason == "finished" and panel.summary.counts[OK] == 3
    assert sorted(path.name for path in folder.glob("*.s2p")) == [
        "0001_000000000100_tr1.s2p",
        "0002_000001000100_tr0.s2p",
        "0003_000000000111.s2p",
    ]
    assert (folder / "resultats.csv").is_file()
    assert panel.results[0].names == ("S21 (dB)", "S21 phase (°)")
    assert panel.results[0].values[0] == pytest.approx(-2.35)  # 4 × 0,5 dB + 0,35 dB à 3,5 GHz
    assert panel.results[0].file == "0001_000000000100_tr1.s2p"
    assert "Canal 1 · 2 port(s) · 51 point(s)" in panel.vna_note.value
    assert simulated.sweeps == [4, 68, 7] and simulated.mode == "CONT"  # état d'origine
    assert list(simulated.measurements) == ["CH1_S11_1"]
    assert studio.device.tr_history == [1, 0]
    assert "Campagne enregistrée" in panel.export_note.value
    assert not studio.sweep_active


def test_vna_resume_skips_the_states_already_in_the_folder(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure_states(panel, probe="vna")
        panel.vna_folder_input.value = str(tmp_path / "campagne")
        await connect_vna_simulation(panel)
        await panel._start()
        await wait_until(lambda: not panel.running and panel.summary is not None)
        first = len(panel.simulated.sweeps)
        (tmp_path / "campagne" / "0002_000001000100_tr0.s2p").unlink()
        panel.vna_skip.value = True
        await panel._start()
        await wait_until(lambda: not panel.running and panel.summary is not None)
        return panel, first

    panel, first = run_async(scenario())
    assert first == 3 and len(panel.simulated.sweeps) == 4  # un seul état remesuré
    kept = "Déjà mesuré (fichier conservé)"
    assert [r.note for r in panel.results] == [kept, "", kept]


def test_vna_settings_are_checked_before_anything_is_sent(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure_states(panel, probe="vna")
        panel.vna_folder_input.value = str(tmp_path / "campagne")
        await panel._start()
        not_connected = panel.status.value
        await connect_vna_simulation(panel)
        messages = []
        good = {"vna_channel": "1", "vna_track": "S21", "vna_freq": ""}
        for name, bad in (
            ("vna_channel", "abc"),
            ("vna_channel", "7"),
            ("vna_track", "S33"),
            ("vna_track", "gain"),
            ("vna_freq", "beaucoup"),
        ):
            field = getattr(panel, name)
            field.value = bad
            await panel._start()
            messages.append(panel.status.value)
            field.value = good[name]
        return panel, studio, not_connected, messages

    panel, studio, not_connected, messages = run_async(scenario())
    assert "VNA non connecté" in not_connected
    assert "Canal du VNA : entier" in messages[0] and "canal 7 n'existe pas" in messages[1]
    assert "S33" in messages[2] and "Paramètre suivi" in messages[3]
    assert "Fréquence suivie" in messages[4]
    assert not studio.sweep_active and panel.simulated.sweeps == []


def test_the_two_simulations_are_not_mixed_up(tmp_path):
    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure_states(panel, probe="instrument")
        panel.instrument_kind.value = "demo"
        await panel._toggle_instrument()  # simulation du mode SCPI
        panel.probe_kind.value = "vna"
        panel._mode_changed()
        await panel._start()
        return panel.status.value

    assert "simulation connectée est celle du mode SCPI" in run_async(scenario())


def test_detection_fills_the_connection_fields_and_explains_an_empty_result(tmp_path):
    from arty_frame_studio import measure_view
    from arty_frame_studio.pna import FoundInstrument

    vna = FoundInstrument("lan", "192.168.1.60", "Keysight Technologies,N5245B,MY1,A.1")
    usb = FoundInstrument(
        "visa", "USB0::0x2A8D::0x0001::MY2::0::INSTR", "Keysight Technologies,P9374A,MY2,A.2"
    )
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return [vna, usb] if len(calls) == 1 else []

    async def scenario(monkeypatch_target):
        panel = make_studio(tmp_path).measure_panel
        panel.probe_kind.value = "vna"
        panel._mode_changed()
        await panel._detect()
        found = (
            panel.instrument_kind.value,
            panel.instrument_address.value,
            panel.detected.visible,
            panel.instrument_status.value,
        )
        panel.detected.value = "1"
        panel._detected_chosen()
        second = (panel.instrument_kind.value, panel.instrument_address.value)
        await panel._scan()
        return found, second, panel.instrument_status.value, panel.detected.visible

    original = measure_view.discover_instruments
    measure_view.discover_instruments = fake
    try:
        found, second, empty, still_visible = run_async(scenario(None))
    finally:
        measure_view.discover_instruments = original
    assert found[:3] == ("lan", "192.168.1.60", True) and "2 instrument(s) trouvé(s)" in found[3]
    assert second == ("visa", "USB0::0x2A8D::0x0001::MY2::0::INSTR")
    assert "Aucun instrument trouvé" in empty and "adresse IP" in empty and not still_visible
    assert calls[0]["scan_network"] is False and calls[1]["scan_network"] is True
    assert (
        calls[1]["hosts"] == []
    )  # l'adresse remplie vient d'être choisie dans la liste : LAN→visa


def test_a_frame_the_board_does_not_confirm_stops_the_sweep(tmp_path):
    from arty_frame_studio.protocol import DeviceStatus, StatusCode

    async def scenario():
        studio, panel = await connected_studio(tmp_path)
        configure(panel, words="00000001\n00000010")
        studio.device.send = lambda config: DeviceStatus(StatusCode.OK, False, 0)
        await panel._start()
        await wait_until(lambda: not panel.running)
        return panel

    panel = run_async(scenario())
    assert panel.summary.reason == "error" and "Trame non confirmée" in panel.status.value
    assert "0 trame(s) terminée(s) sur 1" in panel.status.value
