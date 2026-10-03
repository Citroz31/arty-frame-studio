from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from arty_frame_studio.model import (
    FrameConfig,
    divider_for_frequency,
    load_profile,
    parse_word,
    save_profile,
    ticks_for_ns,
)
from arty_frame_studio.simulation import export_csv, export_vcd, simulate, waveform_svg


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"bit_count": 0}, "bit_count"),
        ({"bit_count": 27}, "bit_count"),
        ({"bit_count": True}, "bit_count"),
        ({"divider": 0}, "divider"),
        ({"divider": 65536}, "divider"),
        ({"latch_ticks": 0}, "latch_ticks"),
        ({"gap_ticks": -1}, "gap_ticks"),
        ({"repeat_count": 0}, "repeat_count"),
        ({"word": -1}, "valeur"),
        ({"word": 1 << 26}, "valeur"),
        ({"word": True}, "valeur"),
        ({"lsb_first": 1}, "booléens"),
        ({"latch_active_low": "false"}, "booléens"),
    ],
)
def test_invalid_configuration_is_rejected(updates: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(FrameConfig(), **updates)


def test_word_order_and_frequency_are_exact() -> None:
    config = FrameConfig(word=0b110, bit_count=3, divider=1)
    assert config.bits == (1, 1, 0)
    assert replace(config, lsb_first=True).bits == (0, 1, 1)
    assert config.frequency_hz == 200_000_000
    assert config.frame_duration_ns == ((2 * 3 + 1) + 8 + 40) * 2.5
    assert replace(config, repeat_count=7).total_duration_ns == config.frame_duration_ns * 7


@pytest.mark.parametrize(
    "frequency,divider",
    [
        (200e6, 1),
        (199.9e6, 2),
        (199999999.0, 2),
        (150e6, 2),
        (100e6, 2),
        (99999999.0, 3),
        (90e6, 3),
        (80e6, 3),
        (66.6666667e6, 3),
        (66666666.6, 4),
        (10e6, 20),
    ],
)
def test_frequency_never_exceeds_the_request(frequency: float, divider: int) -> None:
    assert divider_for_frequency(frequency) == divider
    assert divider_for_frequency(200e6 / 65535) == 65535


def test_selected_frequency_is_the_highest_not_above_the_request() -> None:
    for step in range(1, 4000):
        frequency = 200e6 / (1 + step * 0.37)
        divider = divider_for_frequency(frequency)
        assert 200e6 / divider <= frequency
        assert divider == 1 or 200e6 / (divider - 1) > frequency


def test_exact_dividers_and_immediately_lower_frequencies() -> None:
    import math

    for divider in range(1, 65536):
        exact = 200e6 / divider
        assert divider_for_frequency(exact) == divider
        if divider < 65535:
            below = math.nextafter(exact, -math.inf)
            selected = divider_for_frequency(below)
            assert selected == divider + 1
            assert 200e6 / selected <= below


@pytest.mark.parametrize("frequency", [0, -1, 1, 201e6, float("nan"), float("inf")])
def test_unachievable_frequency_is_rejected(frequency: float) -> None:
    with pytest.raises(ValueError):
        divider_for_frequency(frequency)


@pytest.mark.parametrize(
    "text,base,bits,value",
    [("0b10 10_01", "bin", 6, 41), ("0X2a", "hex", 6, 42), (" 42 ", "dec", 6, 42)],
)
def test_parse_valid_words(text: str, base: str, bits: int, value: int) -> None:
    assert parse_word(text, base, bits) == value


@pytest.mark.parametrize(
    "text,base,bits",
    [
        ("", "bin", 26),
        ("102", "bin", 26),
        ("-1", "dec", 26),
        ("0x40", "hex", 6),
        ("1", "other", 26),
        ("1", "bin", 27),
        ("nan", "dec", 26),
    ],
)
def test_invalid_words_are_rejected(text: str, base: str, bits: int) -> None:
    with pytest.raises(ValueError):
        parse_word(text, base, bits)


def test_durations_quantize_on_half_tick_grid() -> None:
    assert ticks_for_ns(3.6) == 1
    assert ticks_for_ns(3.75) == 2
    assert ticks_for_ns(0, allow_zero=True) == 0
    assert ticks_for_ns(65535 * 2.5) == 65535
    for value in (0, -1, float("inf"), 65536 * 2.5):
        with pytest.raises(ValueError):
            ticks_for_ns(value)


def test_profile_roundtrip_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    config = FrameConfig(word=13, bit_count=4, lsb_first=True, latch_active_low=True)
    save_profile(config, path)
    assert load_profile(path) == config
    for bad in (
        {},
        {"schema_version": True, "frame": {}},
        {"schema_version": 1, "frame": {"word": 1}},
    ):
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError):
            load_profile(path)
    path.write_text(" " * 65537, encoding="utf-8")
    with pytest.raises(ValueError, match="volumineux"):
        load_profile(path)


@pytest.mark.parametrize("divider", [1, 2, 7, 65535])
@pytest.mark.parametrize("lsb_first", [False, True])
@pytest.mark.parametrize("active_low", [False, True])
def test_clock_samples_all_bits_and_latch_follows_last_clock(
    divider: int, lsb_first: bool, active_low: bool
) -> None:
    config = FrameConfig(
        word=0b10110,
        bit_count=5,
        divider=divider,
        latch_ticks=3,
        gap_ticks=7,
        repeat_count=2,
        lsb_first=lsb_first,
        latch_active_low=active_low,
    )
    waveform = simulate(config)
    rising = [point for point in waveform.transitions if point.clk]
    assert tuple(point.data for point in rising) == config.bits * 2
    assert len(rising) == 10
    for frame in range(2):
        start = frame * config.frame_duration_ticks
        assert [point.time_ticks - start for point in rising[frame * 5 : (frame + 1) * 5]] == [
            (2 * index + 1) * divider for index in range(5)
        ]
    active = [point for point in waveform.transitions if point.latch != int(active_low)]
    assert [point.time_ticks for point in active] == [
        (2 * config.bit_count + 1) * divider,
        config.frame_duration_ticks + (2 * config.bit_count + 1) * divider,
    ]
    assert all(point.clk == 0 for point in active)
    assert waveform.duration_ns == config.total_duration_ns
    assert waveform.transitions[-1].data == waveform.transitions[-1].clk == 0
    assert waveform.transitions[-1].latch == int(active_low)


def test_zero_gap_coalesces_events_and_trace_caps_repetitions() -> None:
    config = FrameConfig(
        word=1, bit_count=1, divider=1, latch_ticks=1, gap_ticks=0, repeat_count=65535
    )
    waveform = simulate(config, max_frames=4)
    assert waveform.truncated and waveform.frames_simulated == 4
    assert waveform.duration_ticks == config.frame_duration_ticks * 4
    assert waveform.transitions[-1].data == config.bits[0]
    assert len({point.time_ticks for point in waveform.transitions}) == len(waveform.transitions)
    for count in (0, 257, True):
        with pytest.raises(ValueError):
            simulate(config, count)


def test_exports_describe_the_same_trace_and_svg_is_valid(tmp_path: Path) -> None:
    waveform = simulate(FrameConfig(word=0b101, bit_count=3, divider=1))
    csv_path, vcd_path = tmp_path / "trace.csv", tmp_path / "trace.vcd"
    export_csv(waveform, csv_path)
    export_vcd(waveform, vcd_path)
    with csv_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [float(row["time_ns"]) for row in rows] == [
        point.time_ns for point in waveform.transitions
    ]
    vcd = vcd_path.read_text(encoding="utf-8")
    assert "$timescale 1ps $end" in vcd
    assert f"#{waveform.duration_ticks * 2500}\n" in vcd
    svg = waveform_svg(waveform)
    tree = ET.fromstring(svg)
    assert tree.attrib["width"] == "1200"
    assert "Chronogramme numérique idéal" in svg
    assert "DATA" in svg and "LATCH" in svg
    with pytest.raises(ValueError):
        waveform_svg(waveform, width=100)


def test_core_clock_sets_frequency_ticks_and_waveform_time() -> None:
    from arty_frame_studio.simulation import simulate

    config = FrameConfig(
        word=1, bit_count=1, divider=1, latch_ticks=3, gap_ticks=0, core_hz=150_000_000
    )
    assert config.frequency_hz == 150e6
    assert config.tick_ns == pytest.approx(1e9 / 300e6)
    assert config.frame_duration_ns == pytest.approx(6 * 1e9 / 300e6)
    waveform = simulate(config)
    assert waveform.duration_ns == pytest.approx(config.frame_duration_ns)
    assert waveform.transitions[1].time_ns == pytest.approx(1e9 / 300e6)
    assert divider_for_frequency(80e6, core_hz=150_000_000) == 2
    assert divider_for_frequency(150e6, core_hz=150_000_000) == 1
    assert ticks_for_ns(10, core_hz=150_000_000) == 3
    with pytest.raises(ValueError, match="150 MHz"):
        divider_for_frequency(160e6, core_hz=150_000_000)
    with pytest.raises(ValueError, match="cœur"):
        FrameConfig(core_hz=250_000_000)


def test_version_one_profiles_load_as_reference_firmware(tmp_path: Path) -> None:
    profile = Path(__file__).resolve().parents[1] / "examples/frame_26bits.json"
    assert load_profile(profile).core_hz == 200_000_000
    path = tmp_path / "custom.json"
    save_profile(FrameConfig(core_hz=100_000_000), path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2 and saved["frame"]["core_hz"] == 100_000_000
    assert load_profile(path).core_hz == 100_000_000
    del saved["frame"]["core_hz"]
    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="champs"):
        load_profile(path)
