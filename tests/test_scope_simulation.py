"""La démo observe une émission exacte, indépendamment du chronogramme borné."""

from __future__ import annotations

from dataclasses import replace

import pytest

from arty_frame_studio.model import FrameConfig
from arty_frame_studio.scope_sim import SimulatedKeysight, signal_source
from arty_frame_studio.simulation import simulate


@pytest.mark.parametrize("free_clock", [False, True])
@pytest.mark.parametrize("active_low", [False, True])
@pytest.mark.parametrize("repeat_count", [0, 1, 3])
def test_source_matches_ideal_sequence_and_its_finite_or_continuous_end(
    free_clock, active_low, repeat_count
):
    config = FrameConfig(
        word=0b101,
        bit_count=3,
        divider=20,
        latch_ticks=20,
        gap_ticks=40,
        repeat_count=repeat_count,
        latch_active_low=active_low,
        free_clock=free_clock,
    )
    waveform = simulate(config, max_frames=3)
    source = signal_source(config)
    assert source.period == pytest.approx(config.frame_duration_ns * 1e-9)
    assert source.duration == (
        None if config.continuous else pytest.approx(config.total_duration_ns * 1e-9)
    )
    for point in waveform.transitions:
        for signal in ("data", "clk", "latch"):
            # Sample inside a plateau to avoid binary floating-point boundary ambiguity.
            time = (point.time_ns + config.tick_ns / 10) * 1e-9
            assert source.level(signal, time) == getattr(point, signal)
    if not config.continuous:
        after = config.total_duration_ns * 1e-9 + source.period
        assert source.level("data", after) == source.level("clk", after) == 0
        assert source.level("latch", after) == int(active_low)
    assert source.level("data", -source.period) == source.level("clk", -source.period) == 0
    assert source.level("latch", -source.period) == int(active_low)


def test_long_free_clock_source_does_not_repeat_a_cropped_preview_as_a_frame():
    config = FrameConfig(
        word=1,
        bit_count=1,
        divider=1,
        latch_ticks=65_535,
        gap_ticks=65_534,
        repeat_count=0,
        free_clock=True,
    )
    preview = simulate(config, max_frames=1)
    assert preview.partial_last_frame
    source = signal_source(config)
    assert source.period == pytest.approx(config.frame_duration_ns * 1e-9)
    assert sum(len(times) for times, _ in source.edges.values()) < 100
    # DATA remains low after the one-bit payload, while LATCH and CLK continue
    # through the part of the frame absent from the capped preview.
    time = preview.duration_ns * 1e-9 + 100.5 * config.tick_ns * 1e-9
    assert source.level("data", time) == 0
    assert source.level("latch", time) == 1
    assert source.level("clk", time) == int(time // (config.divider * config.tick_ns * 1e-9)) % 2


def test_single_waits_even_in_auto_and_force_has_no_signal_trigger():
    instrument = SimulatedKeysight(lambda: None, noise=0)
    instrument.write(":TRIGger:SWEep AUTO")
    instrument.write(":SINGle")
    assert instrument.query(":OPERegister:CONDition?") == "+8"
    assert not instrument.records
    instrument.write(":TRIGger:FORCe")
    assert instrument.query(":OPERegister:CONDition?") == "+0"
    assert instrument.query(":TER?") == "+0"
    assert instrument.records


def test_single_waiting_after_old_capture_does_not_replace_last_trace():
    current = signal_source(FrameConfig(word=0b101, bit_count=3))
    instrument = SimulatedKeysight(lambda: current, noise=0)
    instrument.write(":SINGle")
    old_record = instrument.records[1]
    current = None
    instrument.write(":SINGle")
    assert instrument.query(":OPERegister:CONDition?") == "+8"
    assert instrument.records[1] is old_record


def test_transfer_point_count_and_settings_do_not_reacquire_or_retime_snapshot():
    calls = []

    def source():
        calls.append(1)
        return signal_source(FrameConfig())

    instrument = SimulatedKeysight(source, noise=0)
    instrument.write(":SINGle")
    before = instrument.query(":WAVeform:PREamble?").split(",")
    instrument.write(":TIMebase:SCALe 0.000001")
    instrument.write(":TIMebase:POSition 0.001")
    instrument.write(":CHANnel1:SCALe 10")
    instrument.write(":WAVeform:POINts 500")
    after = instrument.query(":WAVeform:PREamble?").split(",")
    assert len(calls) == 1
    assert int(after[2]) == 500
    assert float(after[4]) == pytest.approx(2 * float(before[4]))
    assert after[5:] == before[5:]
    assert len(instrument.query_block(":WAVeform:DATA?")) == 500
    assert len(calls) == 1


def test_finite_emission_does_not_invent_pretrigger_cycles():
    config = FrameConfig(word=0b101, bit_count=3)
    source = signal_source(config)
    instrument = SimulatedKeysight(lambda: source, noise=0)
    instrument.write(":TRIGger:EDGE:SOURce CHANnel2")
    instrument.write(":SINGle")
    trace = instrument.analysis[2]
    assert all(
        value == 0 for time, value in zip(trace.times, trace.volts, strict=True) if time < -60e-9
    )


def test_free_clock_first_falling_trigger_is_not_a_fictitious_edge_at_start():
    config = FrameConfig(word=1, bit_count=1, latch_ticks=20, free_clock=True)
    source = signal_source(config)
    instrument = SimulatedKeysight(lambda: source, noise=0)
    instrument.write(":TRIGger:EDGE:SOURce CHANnel2")
    instrument.write(":TRIGger:EDGE:SLOPe NEGative")
    trigger = instrument._trigger_time(source, {1: "data", 2: "clk"})
    assert trigger is not None and trigger > 1 / config.frequency_hz


@pytest.mark.parametrize("slope", ["POSitive", "NEGative"])
def test_trigger_instant_matches_the_simulated_voltage_crossing(slope):
    source = signal_source(FrameConfig())
    instrument = SimulatedKeysight(lambda: source, noise=0)
    instrument.write(":TRIGger:EDGE:SOURce CHANnel2")
    instrument.write(f":TRIGger:EDGE:SLOPe {slope}")
    trigger = instrument._trigger_time(source, {1: "data", 2: "clk"})
    assert trigger is not None
    assert instrument._voltage(source, "clk", trigger) == pytest.approx(1.65, abs=1e-8)


def test_trigger_threshold_unreached_by_narrow_analog_pulse_does_not_trigger():
    source = signal_source(FrameConfig(latch_ticks=1))
    instrument = SimulatedKeysight(lambda: source, lambda: {1: "latch", 2: "clk"}, rise_time=10e-9)
    instrument.write(":TRIGger:EDGE:LEVel 3.2")
    instrument.write(":SINGle")
    assert instrument.query(":OPERegister:CONDition?") == "+8"
    assert not instrument.records


def test_measurement_threshold_uses_captured_vertical_scale():
    source = signal_source(FrameConfig())
    instrument = SimulatedKeysight(lambda: source, noise=0)
    instrument.write(":SINGle")
    frequency = instrument.query(":MEASure:FREQuency? CHANnel2")
    instrument.write(":CHANnel2:SCALe 100")
    assert instrument.query(":MEASure:FREQuency? CHANnel2") == frequency


@pytest.mark.parametrize("active_low", [False, True])
def test_latch_pulse_at_frame_boundary_keeps_its_end_edge(active_low):
    config = FrameConfig(gap_ticks=0, repeat_count=0, latch_active_low=active_low)
    source = signal_source(config)
    instrument = SimulatedKeysight(lambda: source, lambda: {1: "latch", 2: "clk"}, noise=0)
    instrument.write(f":TRIGger:EDGE:SLOPe {'POSitive' if active_low else 'NEGative'}")
    trigger = instrument._trigger_time(source, {1: "latch", 2: "clk"})
    assert trigger is not None and trigger > source.period
    assert instrument._voltage(source, "latch", trigger) == pytest.approx(1.65, abs=1e-8)
    before = instrument._voltage(source, "latch", source.period - 1e-15)
    after = instrument._voltage(source, "latch", source.period + 1e-15)
    assert abs(before - after) < 1e-4


@pytest.mark.parametrize("reference,divisions", [("LEFT", 1), ("CENTer", 5), ("RIGHT", 9)])
def test_time_reference_uses_manual_reference_positions(reference, divisions):
    source = signal_source(replace(FrameConfig(), repeat_count=0))
    instrument = SimulatedKeysight(lambda: source, noise=0)
    instrument.write(f":TIMebase:REFerence {reference}")
    instrument.write(":SINGle")
    preamble = instrument.query(":WAVeform:PREamble?").split(",")
    assert float(preamble[5]) == pytest.approx(-divisions * instrument.time_scale)
    assert instrument.query(":TRIGger:MODE?") == "EDGE"
