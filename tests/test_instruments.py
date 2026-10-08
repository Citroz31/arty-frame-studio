"""Instrument SCPI générique, VNA simulé et sondes du mode mesure."""

import asyncio
import math

import pytest

from arty_frame_studio.instruments import (
    KEYSIGHT_PNA,
    PRESETS,
    InstrumentError,
    ScpiInstrument,
    SimulatedVna,
    commands_from_text,
    is_query,
    open_transport,
    parse_numbers,
)
from arty_frame_studio.model import FrameConfig
from arty_frame_studio.probes import (
    InstrumentProbe,
    ManualProbe,
    ScopeProbe,
    ScopeSpec,
    scope_value,
)
from arty_frame_studio.scope import (
    Acquisition,
    Measurements,
    ScopeError,
    ScopeSettings,
    Trace,
)

CONFIG = FrameConfig(word=5, bit_count=8)


def run(coroutine):
    async def supervise():
        async with asyncio.timeout(10):
            return await coroutine

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(supervise())
    finally:
        loop.close()


def test_command_lists_ignore_comments_and_refuse_control_text():
    text = "# réglages\n*CLS\n\nINIT:IMM;*OPC?  # balayage\n  CALC:MARK1:Y?\n"
    assert commands_from_text(text) == ["*CLS", "INIT:IMM;*OPC?", "CALC:MARK1:Y?"]
    with pytest.raises(ValueError, match="Ligne 2"):
        commands_from_text("*CLS\nCAFÉ?")
    with pytest.raises(ValueError, match="Ligne 1"):
        commands_from_text("X" * 300)
    assert is_query("CALC:MARK1:Y? ") and not is_query("INIT:IMM")


def test_replies_are_parsed_into_numbers():
    assert parse_numbers("-1.500000E+00,+0.000000E+00") == [-1.5, 0.0]
    assert parse_numbers("+1.0E+01") == [10.0]
    assert parse_numbers("0.5 -3") == [0.5, -3.0]
    with pytest.raises(InstrumentError, match="non numérique"):
        parse_numbers("NaN-like text")


def test_simulated_vna_answers_a_word_dependent_marker_value():
    transport = SimulatedVna()
    instrument = ScpiInstrument(transport)
    assert "SIMULATED VNA" in instrument.identify()
    transport.set_word(0)
    first = instrument.measure(["INITiate:IMMediate;*OPC?"], ["CALCulate:MARKer1:Y?"])
    transport.set_word(4)
    second = instrument.measure(["INITiate:IMMediate;*OPC?"], ["CALCulate:MARKer1:Y?"])
    assert first == [-1.5, 0.0] and second == [-2.5, 0.0]
    assert instrument.errors() == []
    assert transport.commands.count("INITiate:IMMediate;*OPC?") == 2


def test_wrong_commands_surface_the_instrument_error_message():
    instrument = ScpiInstrument(SimulatedVna())
    with pytest.raises(InstrumentError, match="Undefined header"):
        instrument.measure(["FOO:BAR"], ["CALC:MARK1:Y?"], check_errors=True)
    with pytest.raises(InstrumentError, match="sans « \\? »"):
        instrument.measure([], ["CALC:MARK1:Y"])
    with pytest.raises(InstrumentError, match="au moins une requête"):
        instrument.measure([], [])


def test_presets_are_editable_starting_points_with_a_warning():
    assert PRESETS[0].reads == "" and KEYSIGHT_PNA in PRESETS
    assert "vérifier" in KEYSIGHT_PNA.note.lower() and "?" in KEYSIGHT_PNA.reads


def test_transport_kinds_and_bad_choice():
    assert isinstance(open_transport("demo", ""), SimulatedVna)
    with pytest.raises(ValueError, match="lan, visa ou demo"):
        open_transport("usb", "x")
    with pytest.raises(ValueError):
        open_transport("lan", "bad host!")
    transport = SimulatedVna()
    ScpiInstrument(transport).close()
    transport.close()
    with pytest.raises(ScopeError, match="fermé"):
        transport.write("*CLS")


# -- sondes -------------------------------------------------------------------------
def acquisition(volts, *, frequency=10e6, duty=0.5, channels=(1,)):
    times = tuple(index * 1e-9 for index in range(len(volts)))
    traces = tuple(Trace(channel, times, tuple(volts)) for channel in channels)
    measures = {
        channel: Measurements(
            frequency=frequency, period=None, vpp=3.3, vmax=3.4, vmin=-0.1, duty=duty
        )
        for channel in channels
    }
    return Acquisition(ScopeSettings(), traces, measures, True)


def test_scope_probe_reads_chosen_quantities_and_marks_missing_ones():
    async def acquire():
        return acquisition([0.0, 3.3, 3.3, 0.0])

    probe = ScopeProbe(
        acquire,
        [
            ScopeSpec(1, "frequency"),
            ScopeSpec(1, "vavg"),
            ScopeSpec(1, "duty"),
            ScopeSpec(1, "period"),
        ],
    )
    result = run(probe(CONFIG, 0))
    assert result.names[:3] == ("CH1 fréquence (Hz)", "CH1 moyenne (V)", "CH1 rapport cyclique (%)")
    assert result.values[0] == 10e6 and result.values[1] == pytest.approx(1.65)
    assert result.values[2] == 50.0 and math.isnan(result.values[3])
    assert result.note == "Non mesurable : CH1 période (s)"


def test_scope_probe_needs_a_displayed_channel_and_a_valid_choice():
    async def acquire():
        return acquisition([0.0, 1.0], channels=(1,))

    with pytest.raises(InstrumentError, match="CH2 n'est pas affichée"):
        run(ScopeProbe(acquire, [ScopeSpec(2, "vpp")])(CONFIG, 0))
    with pytest.raises(ValueError):
        ScopeProbe(acquire, [])
    for bad in ((3, "vpp"), (1, "rms")):
        with pytest.raises(ValueError):
            ScopeSpec(*bad)
    assert scope_value(acquisition([1.0, 3.0]), ScopeSpec(1, "vavg")) == 2.0


def test_instrument_probe_tells_the_simulator_the_word_and_names_the_columns():
    transport = SimulatedVna()
    probe = InstrumentProbe(
        ScpiInstrument(transport),
        ["INITiate:IMMediate;*OPC?"],
        ["CALCulate:MARKer1:Y?"],
        ["S21 (dB)", "phase (°)"],
        on_word=transport.set_word,
    )
    first = run(probe(FrameConfig(word=0, bit_count=8), 0))
    second = run(probe(FrameConfig(word=8, bit_count=8), 1))
    assert first.names == ("S21 (dB)", "phase (°)") and first.values == (-1.5, 0.0)
    assert second.values == (-3.5, 0.0)
    # The error queue is read once, after the first measurement only.
    assert transport.commands.count(":SYSTem:ERRor?") == 1
    odd = InstrumentProbe(ScpiInstrument(SimulatedVna()), [], ["CALC:MARK1:Y?"], ["un seul"])
    assert run(odd(CONFIG, 0)).names == ("un seul", "valeur2")
    with pytest.raises(ValueError):
        InstrumentProbe(ScpiInstrument(SimulatedVna()), [], [])


def test_manual_probe_waits_then_returns_the_operator_decision():
    events = []

    async def scenario():
        probe = ManualProbe(
            lambda config, index: events.append(("wait", config.word, index)),
            lambda: events.append("done"),
        )
        assert not probe.decide("ok")  # nothing is waiting yet
        task = asyncio.create_task(probe(CONFIG, 3))
        await asyncio.sleep(0.01)
        assert probe.waiting
        assert probe.decide("fail", "phase fausse", -4.5)
        result = await task
        assert not probe.waiting and not probe.decide("ok")
        return result

    result = run(scenario())
    assert result.action == "fail" and result.note == "phase fausse"
    assert result.values == (-4.5,) and result.names == ("valeur relevée",)
    assert events == [("wait", 5, 3), "done"]


def test_manual_probe_ignores_non_finite_values_and_survives_cancellation():
    async def scenario():
        probe = ManualProbe()
        task = asyncio.create_task(probe(CONFIG, 0))
        await asyncio.sleep(0.01)
        probe.decide("ok", value=float("nan"))
        assert (await task).values == ()
        task = asyncio.create_task(probe(CONFIG, 1))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return probe.waiting

    assert run(scenario()) is False
