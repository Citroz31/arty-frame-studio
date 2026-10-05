"""Oscilloscope Keysight : transports, pilote SCPI, simulateur et mesures."""

from __future__ import annotations

import math
import sys
import types
from dataclasses import replace

import pytest

from arty_frame_studio.model import FrameConfig
from arty_frame_studio.scope import (
    Acquisition,
    ChannelSettings,
    KeysightScope,
    Measurements,
    ScopeError,
    ScopeSettings,
    ScopeTimeout,
    SocketTransport,
    Trace,
    TriggerSettings,
    TriggerTimeout,
    VisaTransport,
    clock_period,
    format_si,
    frame_preset,
    list_visa_resources,
    measure_trace,
    measurement_warnings,
    nice_ceiling,
    open_resource_manager,
    parse_ieee_block,
    parse_si,
    period_cursors,
)
from arty_frame_studio.scope_sim import SimulatedKeysight, signal_source

SIPO = FrameConfig(word=0b10100101, bit_count=8, divider=20, latch_ticks=8, gap_ticks=40)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def simulated(config: FrameConfig = SIPO, mapping=None) -> tuple[KeysightScope, SimulatedKeysight]:
    source = signal_source(config)
    instrument = SimulatedKeysight(lambda: source, mapping)
    clock = Clock()
    return KeysightScope(instrument, sleep=clock.sleep, clock=clock), instrument


def square(period: float, duty: float = 0.5, cycles: int = 10, points: int = 2000) -> Trace:
    times = tuple(index * cycles * period / points for index in range(points))
    volts = tuple(3.3 if (t % period) < duty * period else 0.0 for t in times)
    return Trace(1, times, volts)


# -- blocs et transports ------------------------------------------------------
def test_ieee_blocks_are_parsed_and_checked():
    assert parse_ieee_block(b"#15abcde\n") == b"abcde"
    assert parse_ieee_block(b"#210" + bytes(range(10))) == bytes(range(10))
    for bad in (b"", b"abc", b"#x12", b"#2a", b"#0abc", b"#15abc"):
        with pytest.raises(ScopeError):
            parse_ieee_block(bad)


class FakeSocket:
    """``None`` in ``chunks`` stands for a pause longer than the timeout."""

    def __init__(self, chunks: list[bytes | None]) -> None:
        self.chunks = list(chunks)
        self.sent = bytearray()
        self.closed = False
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _: int) -> bytes:
        chunk = self.chunks.pop(0) if self.chunks else None
        if chunk is None:
            raise TimeoutError
        return chunk

    def close(self) -> None:
        self.closed = True


def lan(chunks: list[bytes | None]) -> tuple[SocketTransport, FakeSocket]:
    fake = FakeSocket(chunks)
    return SocketTransport("192.168.1.50", connect=lambda address, timeout: fake), fake


def test_lan_transport_splits_lines_and_blocks_across_packets():
    transport, fake = lan([b"KEYSIGHT,DSO", b"-X 1202A\n+1\n", b"#2", b"05ab", b"cde\n"])
    assert transport.query("*IDN?") == "KEYSIGHT,DSO-X 1202A"
    assert transport.query("*OPC?") == "+1"
    assert transport.query_block(":WAV:DATA?") == b"abcde"
    transport.write(":RUN")
    assert fake.sent == b"*IDN?\n*OPC?\n:WAV:DATA?\n:RUN\n"
    with pytest.raises(ScopeTimeout):
        transport.query("*IDN?")
    transport.close()
    assert fake.closed


def test_lan_transport_discards_a_reply_that_arrives_after_its_timeout():
    transport, fake = lan([])
    with pytest.raises(ScopeTimeout):
        transport.query(":MEASure:FREQuency? CHANnel1")
    # The late answer must not be read as the reply to the next query.
    fake.chunks = [b"+1.000E+07", b"\n", None, b"KEYSIGHT\n"]
    assert transport.query("*IDN?") == "KEYSIGHT"
    assert fake.timeouts[-2:] == [0.3, 5.0]


def test_lan_transport_reports_closed_links_and_bad_addresses():
    transport, _ = lan([b""])
    with pytest.raises(ScopeError, match="fermé"):
        transport.query("*IDN?")
    with pytest.raises(ValueError):
        SocketTransport("bad host!", connect=lambda *args: None)

    def refuse(address, timeout):
        raise OSError("connection refused")

    with pytest.raises(ScopeError, match="Utility"):
        SocketTransport("192.168.1.50", connect=refuse)


class FakeInstrument:
    def __init__(self) -> None:
        self.written: list[str] = []
        self.closed = False
        self.cleared = 0
        self.fail: Exception | None = None

    def clear(self) -> None:
        self.cleared += 1

    def write(self, command: str) -> None:
        self.written.append(command)

    def query(self, command: str) -> str:
        if self.fail:
            raise self.fail
        return "KEYSIGHT TECHNOLOGIES,DSO-X 1202A,CN12345678,02.12\n"

    def query_binary_values(self, command, datatype, is_big_endian, container):
        assert datatype == "B" and container is bytes and not is_big_endian
        return container([1, 2, 3])

    def close(self) -> None:
        self.closed = True


class FakeManager:
    def __init__(self) -> None:
        self.instrument = FakeInstrument()

    def open_resource(self, resource: str) -> FakeInstrument:
        assert resource.startswith("USB0::")
        return self.instrument

    def list_resources(self) -> tuple[str, ...]:
        return (
            "USB0::0x2A8D::0x0396::CN12345678::0::INSTR",
            "ASRL7::INSTR",  # the Arty UART: never offered as an oscilloscope
            "TCPIP0::192.168.1.50::inst0::INSTR",
            "TCPIP0::192.168.1.50::5025::SOCKET",
        )


def test_visa_transport_uses_pyvisa_binary_blocks_and_maps_timeouts():
    manager = FakeManager()
    transport = VisaTransport("USB0::0x2A8D::0x0396::CN12345678::0::INSTR", manager=manager)
    instrument = manager.instrument
    assert instrument.timeout == 5000 and instrument.read_termination == "\n"
    assert transport.query("*IDN?").endswith("02.12")
    assert transport.query_block(":WAV:DATA?") == b"\x01\x02\x03"
    instrument.fail = RuntimeError("VI_ERROR_TMO (-1073807339): Timeout expired")
    with pytest.raises(ScopeTimeout):
        transport.query("*IDN?")
    assert instrument.cleared == 1  # device clear: no late reply left in the buffers
    instrument.fail = RuntimeError("VI_ERROR_RSRC_NFOUND")
    with pytest.raises(ScopeError, match="VISA"):
        transport.query("*IDN?")
    transport.close()
    assert instrument.closed
    with pytest.raises(ValueError):
        VisaTransport("  ", manager=manager)


def test_resource_manager_falls_back_and_lists_instruments(monkeypatch):
    calls = []

    def resource_manager(*args):
        calls.append(args)
        if not args:
            raise OSError("Could not locate a VISA implementation")
        return FakeManager()

    fake = types.SimpleNamespace(ResourceManager=resource_manager)
    monkeypatch.setitem(sys.modules, "pyvisa", fake)
    assert isinstance(open_resource_manager(), FakeManager)
    assert calls == [(), ("@py",)]
    assert list_visa_resources() == [
        "TCPIP0::192.168.1.50::inst0::INSTR",
        "USB0::0x2A8D::0x0396::CN12345678::0::INSTR",
    ]


def test_missing_pyvisa_is_explained(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyvisa", None)
    with pytest.raises(ScopeError, match="PyVISA"):
        open_resource_manager()
    assert list_visa_resources() == []


# -- pilote et simulateur -----------------------------------------------------
def test_identity_settings_round_trip_and_scpi_errors():
    scope, instrument = simulated()
    assert "DSO-X 1202A" in scope.identify()
    settings = ScopeSettings(
        (ChannelSettings(scale=0.5, offset=1.0, probe=10.0), ChannelSettings(enabled=False)),
        time_scale=20e-9,
        time_position=100e-9,
        trigger=TriggerSettings(source=2, slope="NEG", level=1.2, sweep="NORM"),
    )
    scope.apply_settings(settings)
    assert scope.read_settings() == settings
    with pytest.raises(ScopeError, match="refusé"):
        scope.apply_settings(settings.with_channel(1, ChannelSettings(scale=1000.0)))
    instrument.write(":NOT:A:COMMAND 1")
    assert scope.errors() and scope.errors() == []


def test_autoscale_then_capture_measures_a_10_mhz_clock():
    scope, _ = simulated()
    settings = scope.autoscale()
    assert settings.time_scale == 50e-9 and settings.trigger.source == 2
    acquisition = scope.capture()
    assert acquisition.triggered
    clock = acquisition.measurements[2]
    assert clock.source == "oscilloscope"
    assert clock.frequency == pytest.approx(10e6, rel=5e-3)
    assert clock.period == pytest.approx(100e-9, rel=5e-3)
    assert clock.duty == pytest.approx(0.5, abs=0.03)
    assert 3.2 < clock.vpp < 3.8
    trace = acquisition.trace(2)
    assert len(trace.times) == 1000
    assert trace.times[0] == pytest.approx(-250e-9) and trace.times[-1] < 250e-9
    assert max(trace.volts) == pytest.approx(3.3, abs=0.3)
    assert min(trace.volts) == pytest.approx(0.0, abs=0.3)
    # The local estimate agrees with the instrument's measurement.
    assert acquisition.local[2].frequency == pytest.approx(10e6, rel=5e-3)


def test_burst_and_free_running_clocks_both_read_10_mhz():
    for config in (FrameConfig(divider=20), replace(SIPO, free_clock=True, latch_ticks=20)):
        scope, _ = simulated(config, lambda: {1: "data", 2: "clk"})
        scope.apply_settings(ScopeSettings(time_scale=500e-9))
        acquisition = scope.capture()
        assert acquisition.measurements[2].frequency == pytest.approx(10e6, rel=5e-3)


def test_normal_trigger_without_edge_times_out_and_stops():
    scope, instrument = simulated()
    scope.apply_settings(ScopeSettings(trigger=TriggerSettings(level=10.0, sweep="NORM")))
    with pytest.raises(TriggerTimeout, match="déclenchement"):
        scope.capture(timeout=0.5)
    assert ":STOP" in instrument.commands
    # In Auto, the scope acquires anyway but reports no trigger.
    scope.apply_settings(ScopeSettings(trigger=TriggerSettings(level=10.0, sweep="AUTO")))
    assert not scope.capture().triggered


def test_unconnected_probe_shows_noise_without_frequency():
    scope, _ = simulated(mapping=lambda: {1: None, 2: "clk"})
    acquisition = scope.capture()
    assert acquisition.measurements[1].frequency is None
    assert acquisition.measurements[1].vpp < 0.2


def test_close_resumes_the_scope_and_screenshot_is_real_only():
    scope, instrument = simulated()
    with pytest.raises(ScopeError, match="simulation"):
        scope.screenshot()
    scope.close()
    assert instrument.commands[-1] == ":RUN" and instrument.closed


def test_signal_source_matches_the_frame():
    source = signal_source(SIPO)
    assert source.period == pytest.approx(SIPO.frame_duration_ns * 1e-9)
    assert len(source.rising_edges("clk")) == 8
    assert source.level("latch", 860e-9) == 1 and source.level("latch", 900e-9) == 0


# -- mesures, curseurs, formats ------------------------------------------------
def test_local_measurements_of_square_and_flat_signals():
    measured = measure_trace(square(100e-9, duty=0.25))
    assert measured.frequency == pytest.approx(10e6, rel=1e-2)
    assert measured.duty == pytest.approx(0.25, abs=0.01)
    assert measured.vpp == pytest.approx(3.3)
    flat = measure_trace(Trace(1, (0.0, 1.0, 2.0, 3.0), (1.0, 1.0, 1.0, 1.0)))
    assert flat.frequency is None and flat.vpp == 0
    assert measure_trace(Trace(1, (0.0,), (1.0,))).vpp is None


def test_clock_period_ignores_gaps_between_bursts():
    burst = [index * 100e-9 for index in range(8)]
    rising = burst + [970e-9 + value for value in burst]
    assert clock_period(rising) == pytest.approx(100e-9)
    assert clock_period([1.0]) is None


def test_period_cursors_snap_to_the_nearest_rising_edges():
    trace = square(100e-9)
    first, second = period_cursors(trace, 320e-9)
    assert first == pytest.approx(300e-9, abs=2e-9)
    assert second - first == pytest.approx(100e-9, abs=2e-9)
    assert period_cursors(Trace(1, (0.0, 1.0, 2.0), (0.0, 0.0, 0.0)), 0.0) is None


def test_si_formatting_and_parsing():
    assert format_si(10e6, "Hz") == "10.00 MHz"
    assert format_si(100e-9, "s") == "100.0 ns"
    assert format_si(3.3, "V") == "3.300 V"
    assert format_si(None, "V") == "—" and format_si(math.inf, "V") == "—"
    # Rounding up to the next decade keeps four significant digits.
    assert format_si(9.99995e6, "Hz") == "10.00 MHz"
    assert format_si(999.96e3, "Hz") == "1.000 MHz"
    assert format_si(-99.996e-9, "s") == "-100.0 ns"
    assert parse_si("1,65", "V") == pytest.approx(1.65)
    assert parse_si("100n", "s") == pytest.approx(100e-9)
    assert parse_si("100 ns", "s") == pytest.approx(100e-9)
    assert parse_si("-2.5u") == pytest.approx(-2.5e-6)
    assert parse_si("2e-6", "s") == pytest.approx(2e-6)
    for bad in ("", "abc", "nan"):
        with pytest.raises(ValueError):
            parse_si(bad)
    assert nice_ceiling(0.55, (0.5, 1.0, 2.0)) == 1.0
    assert nice_ceiling(50.0, (0.5, 1.0, 2.0)) == 2.0


def test_frame_preset_triggers_on_the_clock_channel_and_keeps_probes():
    current = ScopeSettings(
        (ChannelSettings(probe=10.0, scale=5.0, coupling="AC"), ChannelSettings(enabled=False))
    )
    preset = frame_preset(SIPO, {1: "clk", 2: "data"}, current)
    assert preset.trigger == TriggerSettings(source=1, slope="POS", level=1.65, sweep="AUTO")
    assert preset.channel(1) == ChannelSettings(probe=10.0, scale=1.0, offset=-0.5)
    assert preset.channel(2).enabled and preset.channel(2).offset == 3.8
    assert preset.time_scale == 50e-9  # five CLK periods of 100 ns on screen
    assert frame_preset(None, {}, current).trigger.source == 2


def test_measurement_warnings_point_to_probe_factor_and_ground_lead():
    def acquisition(**values):
        measures = Measurements(**values)
        return Acquisition(ScopeSettings(), (), {1: measures}, True, local={1: measures})

    mapping = {1: "data", 2: "clk"}
    # The user's capture: about 33 V levels and 66 Vpp for a 3.3 V output.
    found = measurement_warnings(acquisition(vmax=45.0, high=33.0, low=0.0), mapping)
    assert "facteur de sonde" in found[0] and "ressort de masse" in found[1]
    found = measurement_warnings(acquisition(vmax=0.35, high=0.33, low=0.0), mapping)
    assert found and "10:1 réglée en 1:1" in found[0]
    assert measurement_warnings(acquisition(vmax=3.5, high=3.3, low=0.0), mapping) == []
    # Nothing is said about a channel that is not wired to the Arty.
    assert measurement_warnings(acquisition(vmax=45.0, high=33.0, low=0.0), {}) == []
