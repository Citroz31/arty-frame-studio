"""Mesures physiques : périodicité, résolution et impulsions étroites."""

from __future__ import annotations

import math

import pytest

from arty_frame_studio.scope import Trace, clock_period, edge_times, measure_trace


def sampled_square(
    period: float = 100e-9,
    *,
    step: float = 0.1e-9,
    duty: float = 0.5,
    cycles: int = 10,
    phase: float = 0.0,
) -> Trace:
    count = round(cycles * period / step)
    times = tuple(index * step for index in range(count))
    volts = tuple(3.3 if (time + phase) % period < duty * period else 0.0 for time in times)
    return Trace(1, times, volts)


def test_short_glitch_does_not_replace_clock_period():
    assert clock_period([time * 1e-9 for time in (0, 100, 105, 200, 300, 400)]) == pytest.approx(
        100e-9
    )


def test_clock_gaps_are_not_accepted_as_a_periodic_data_waveform():
    trace = sampled_square(step=1e-9, cycles=30)
    # Two ten-cycle bursts separated by a ten-cycle idle interval.
    volts = tuple(
        0.0 if 1e-6 <= time < 2e-6 else value
        for time, value in zip(trace.times, trace.volts, strict=True)
    )
    burst = Trace(1, trace.times, volts)
    assert measure_trace(burst, clock=True).frequency == pytest.approx(10e6, rel=0.005)
    assert measure_trace(burst).frequency is None


def test_nonperiodic_data_does_not_gain_a_frequency_from_its_shortest_interval():
    times = tuple(index * 1e-9 for index in range(1400))
    starts = (100e-9, 300e-9, 600e-9, 1000e-9, 1300e-9)
    volts = tuple(
        3.3 if any(start <= time < start + 50e-9 for start in starts) else 0.0 for time in times
    )
    measured = measure_trace(Trace(1, times, volts))
    assert measured.frequency is None and measured.period is None
    assert measured.vpp == pytest.approx(3.3)


@pytest.mark.parametrize("duty", [0.01, 0.99])
def test_well_sampled_narrow_pulses_retain_levels_frequency_and_duty(duty):
    measured = measure_trace(sampled_square(duty=duty))
    assert measured.low == pytest.approx(0.0)
    assert measured.high == pytest.approx(3.3)
    assert measured.frequency == pytest.approx(10e6, rel=0.01)
    assert measured.duty == pytest.approx(duty, abs=0.002)


@pytest.mark.parametrize("duty,spike", [(0.01, 20.0), (0.5, 200.0)])
def test_isolated_overshoot_is_not_the_logic_high_or_clock_period(duty, spike):
    trace = sampled_square(duty=duty)
    volts = list(trace.volts)
    volts[551] = spike
    measured = measure_trace(Trace(1, trace.times, tuple(volts)), clock=True)
    assert measured.high == pytest.approx(3.3)
    assert measured.vmax == spike
    assert measured.frequency == pytest.approx(10e6, rel=0.01)


def test_sample_quantization_does_not_choose_the_shortest_clock_interval():
    # At 40 ns/sample, interpolated rising intervals alternate 80 and 120 ns.
    measured = measure_trace(sampled_square(step=40e-9, cycles=40, phase=7e-9), clock=True)
    assert measured.frequency == pytest.approx(10e6, rel=0.02)


def test_hysteresis_uses_the_crossing_before_threshold_confirmation():
    trace = Trace(1, (0.0, 1.0, 2.0, 3.0, 4.0, 5.0), (0.0, 0.0, 1.6, 1.7, 3.3, 3.3))
    assert edge_times(trace) == pytest.approx([2.5])


@pytest.mark.parametrize(
    "trace",
    [
        Trace(1, (0.0, 1.0), (0.0, 3.3, 0.0)),
        Trace(1, (0.0, 1.0, 1.0), (0.0, 3.3, 0.0)),
        Trace(1, (0.0, 2.0, 1.0), (0.0, 3.3, 0.0)),
        Trace(1, (0.0, 1.0, 2.0), (0.0, math.nan, 0.0)),
    ],
)
def test_invalid_trace_has_no_physically_valid_edges_or_measurements(trace):
    assert edge_times(trace) == []
    assert measure_trace(trace).frequency is None


def test_incompatible_intervals_and_invalid_time_axis_have_no_clock_period():
    assert clock_period([0.0, 1.0, 4.0]) is None
    assert clock_period([0.0, math.inf]) is None
    assert clock_period([0.0, 2.0, 1.0]) is None
