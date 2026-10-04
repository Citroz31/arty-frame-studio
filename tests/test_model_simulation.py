from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from arty_frame_studio.model import (
    FrameConfig,
    binary_bit_count,
    divider_for_frequency,
    format_word,
    load_profile,
    parse_binary_frame,
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
        ({"repeat_count": -1}, "repeat_count"),
        ({"repeat_count": 65536}, "repeat_count"),
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


@pytest.mark.parametrize("separator", [" ", "\t", "\r\n", "\u00a0", "\u202f", "_"])
@pytest.mark.parametrize(
    "text,base,bits,value",
    [
        ("0B00{separator}101", "bin", 5, 5),
        ("0X0{separator}A5", "hex", 8, 165),
        ("1{separator}65", "dec", 8, 165),
    ],
)
def test_word_groups_accept_whitespace_and_underscores(
    separator: str, text: str, base: str, bits: int, value: int
) -> None:
    assert parse_word(text.format(separator=separator), base, bits) == value


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
    assert saved["schema_version"] == 3 and saved["frame"]["core_hz"] == 100_000_000
    assert load_profile(path).core_hz == 100_000_000
    del saved["frame"]["core_hz"]
    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="champs"):
        load_profile(path)


def test_continuous_emission_has_no_end_and_previews_a_window(tmp_path):
    import math

    from arty_frame_studio.model import CONTINUOUS, load_profile, save_profile
    from arty_frame_studio.simulation import waveform_svg

    config = FrameConfig(word=0b101, bit_count=3, divider=2, repeat_count=CONTINUOUS)
    assert config.continuous and not FrameConfig().continuous
    assert math.isinf(config.total_duration_ns)
    waveform = simulate(config, max_frames=3)
    assert waveform.frames_simulated == 3 and waveform.truncated
    # The window ends at the start of the next frame, never at an idle stop.
    assert waveform.transitions[-1].data == 1
    assert waveform.duration_ticks == 3 * config.frame_duration_ticks
    assert "émission continue" in waveform_svg(waveform)
    path = tmp_path / "continu.json"
    save_profile(config, path)
    assert load_profile(path) == config


def test_free_clock_frames_last_whole_clock_periods(tmp_path):
    from arty_frame_studio.model import free_clock_ticks, with_free_clock

    # LATCH 3 periods -> (2*3 - 1) * N, pause 2 periods -> 2*2*N; N = 4 here.
    assert free_clock_ticks(60, 40, 4) == (20, 16)
    # LATCH lasts at least one period; the pause may be zero.
    assert free_clock_ticks(0, 1, 4) == (4, 0)
    with pytest.raises(ValueError, match="LATCH limité"):
        free_clock_ticks(1e9, 0, 40_000)
    with pytest.raises(ValueError, match="pause limitée"):
        free_clock_ticks(5, 1e9, 40_000)
    config = FrameConfig(
        word=0b101, bit_count=3, divider=4, latch_ticks=20, gap_ticks=16, free_clock=True
    )
    assert (config.latch_periods, config.gap_periods, config.latch_active_ticks) == (3, 2, 24)
    assert config.frame_duration_ticks == 2 * 4 * (3 + 3 + 2)
    for bad in ({"latch_ticks": 8}, {"latch_ticks": 6}, {"gap_ticks": 4}):
        with pytest.raises(ValueError, match="CLK libre"):
            replace(config, **bad)
    with pytest.raises(ValueError, match="booléen"):
        replace(config, free_clock=1)
    converted = with_free_clock(FrameConfig(divider=20, latch_ticks=8, gap_ticks=40))
    assert converted.free_clock and (converted.latch_ticks, converted.gap_ticks) == (20, 40)
    assert with_free_clock(converted) is converted
    path = tmp_path / "libre.json"
    save_profile(config, path)
    assert load_profile(path) == config


def test_free_clock_simulation_never_pauses_clk():
    config = FrameConfig(
        word=0b101,
        bit_count=3,
        divider=2,
        latch_ticks=6,
        gap_ticks=8,
        repeat_count=2,
        free_clock=True,
    )
    waveform = simulate(config)
    clk_edges = [t.time_ticks for t in waveform.transitions]
    # One CLK edge every N ticks from 0 to the end of the second frame.
    assert clk_edges == list(range(0, 2 * config.frame_duration_ticks + 1, 2))
    levels = {t.time_ticks: (t.data, t.clk, t.latch) for t in waveform.transitions}
    # LATCH from the last falling edge (tick 12) for 2 periods, then the pause.
    assert [levels[tick][2] for tick in (10, 12, 16, 18, 20, 26)] == [0, 1, 1, 1, 0, 0]
    assert levels[28] == (1, 0, 0) and not waveform.truncated
    assert waveform.duration_ticks == 2 * config.frame_duration_ticks


@pytest.mark.parametrize("repeat_count", [0, 1])
def test_free_clock_preview_is_bounded_for_huge_frames(repeat_count):
    from arty_frame_studio.simulation import MAX_FREE_CLOCK_TRANSITIONS, waveform_svg

    config = FrameConfig(
        word=1,
        bit_count=1,
        divider=1,
        latch_ticks=65535,
        gap_ticks=65534,
        repeat_count=repeat_count,
        free_clock=True,
    )
    waveform = simulate(config)
    assert waveform.frames_simulated == 0 and waveform.truncated
    assert waveform.partial_last_frame
    assert len(waveform.transitions) == MAX_FREE_CLOCK_TRANSITIONS
    assert waveform.duration_ticks == waveform.transitions[-1].time_ticks
    # The window ends on a real rising edge in LATCH, without an artificial STOP.
    assert waveform.transitions[-1].clk == 1 and waveform.transitions[-1].latch == 1
    assert waveform.duration_ticks < config.frame_duration_ticks
    assert "0 trame(s) complète(s) + 1 partielle" in waveform_svg(waveform)
    svg = waveform_svg(simulate(replace(config, latch_ticks=1, gap_ticks=4, repeat_count=0)))
    # Bit labels only within the bit window, not on LATCH/pause CLK edges.
    assert svg.count('fill="#2563eb">') == 4


def test_custom_clock_csv_preserves_time_and_vcd_describes_cropped_window(tmp_path):
    config = FrameConfig(
        word=1,
        bit_count=1,
        divider=1,
        latch_ticks=65535,
        gap_ticks=65534,
        repeat_count=0,
        free_clock=True,
        core_hz=150_000_000,
    )
    waveform = simulate(config)
    csv_path = tmp_path / "custom.csv"
    export_csv(waveform, csv_path)
    with csv_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == ["time_ns", "DATA", "CLK", "LATCH"]
        times = [float(row["time_ns"]) for row in reader]
    assert times == [transition.time_ns for transition in waveform.transitions]
    vcd_path = tmp_path / "custom.vcd"
    export_vcd(waveform, vcd_path)
    vcd = vcd_path.read_text(encoding="utf-8")
    assert "core_hz=150000000" in vcd and "continuous=1 free_clock=1" in vcd
    assert "complete_frames=0 partial_last_frame=1 truncated=1" in vcd
    assert "A cropped window does not imply STOP" in vcd
    assert f"#{round(waveform.transitions[-1].time_ns * 1000)}\n" in vcd


def test_free_clock_preview_reduces_complete_frames_without_exceeding_budget():
    from arty_frame_studio.simulation import MAX_FREE_CLOCK_TRANSITIONS

    config = FrameConfig(
        word=1,
        bit_count=1,
        divider=1,
        latch_ticks=10001,
        gap_ticks=10000,
        repeat_count=20,
        free_clock=True,
    )
    waveform = simulate(config, max_frames=20)
    assert waveform.frames_simulated == 2 and not waveform.partial_last_frame
    assert waveform.truncated and len(waveform.transitions) <= MAX_FREE_CLOCK_TRANSITIONS
    assert waveform.duration_ticks == 2 * config.frame_duration_ticks


@pytest.mark.parametrize(
    "text,value,bits",
    [
        ("0", 0, 1),
        ("1", 1, 1),
        ("0101", 5, 4),
        ("1010 0101", 0xA5, 8),
        ("0b10_10", 10, 4),
        (" 0B00\t10\r\n_01 ", 9, 6),
        ("0b00\u00a010\u202f01", 9, 6),
        ("1" * 26, (1 << 26) - 1, 26),
        ("0" * 26, 0, 26),
    ],
)
def test_binary_frames_take_their_length_from_the_digits(text: str, value: int, bits: int) -> None:
    assert parse_binary_frame(text) == (value, bits)
    assert binary_bit_count(text) == bits


@pytest.mark.parametrize(
    "text,message",
    [
        ("", "au moins un bit"),
        ("0b", "au moins un bit"),
        ("\t _ 0B_\r\n", "au moins un bit"),
        ("1021", "seulement 0 et 1"),
        ("0x10", "seulement 0 et 1"),
        ("0b0b1", "seulement 0 et 1"),
        ("-01", "seulement 0 et 1"),
        ("\uff10\uff11", "seulement 0 et 1"),
        ("1" * 27, "27 bits saisis : 26 bits maximum"),
        ("0b" + "0" * 27, "27 bits saisis : 26 bits maximum"),
    ],
)
def test_invalid_binary_frames_are_explained(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_binary_frame(text)


def test_words_are_formatted_for_each_notation() -> None:
    assert format_word(0xA5, 8, "bin") == "10100101"
    assert format_word(0xA5, 12, "bin") == "000010100101"
    assert format_word(0xA5, 8, "hex") == "A5"
    assert format_word(0xA5, 8, "dec") == "165"
    with pytest.raises(ValueError):
        format_word(1, 1, "oct")


@pytest.mark.parametrize("base", ["bin", "hex", "dec"])
@pytest.mark.parametrize(
    "word,bits",
    [(0, 1), (0, 26), (1, 26), (5, 8), ((1 << 26) - 1, 26)],
)
def test_formatted_words_preserve_value_and_binary_frame_length(
    word: int, bits: int, base: str
) -> None:
    formatted = format_word(word, bits, base)
    assert parse_word(formatted, base, bits) == word
    if base == "bin":
        assert parse_binary_frame(formatted) == (word, bits)


@pytest.mark.parametrize("base", ["bin", "hex", "dec"])
@pytest.mark.parametrize(
    "word,bits",
    [(-1, 3), (8, 3), (1 << 26, 26), (True, 3), (1.0, 3), (0, 0), (0, 27), (0, True), (0, 3.0)],
)
def test_format_word_rejects_invalid_frame_values(word: int, bits: int, base: str) -> None:
    with pytest.raises(ValueError):
        format_word(word, bits, base)


def test_binary_length_survives_profile_roundtrip_and_simulation(tmp_path: Path) -> None:
    word, bits = parse_binary_frame("0b0001_0010")
    config = FrameConfig(word=word, bit_count=bits)
    path = tmp_path / "binary.json"
    save_profile(config, path)
    restored = load_profile(path)
    assert format_word(restored.word, restored.bit_count, "bin") == "00010010"
    assert tuple(point.data for point in simulate(restored).transitions if point.clk) == (
        0,
        0,
        0,
        1,
        0,
        0,
        1,
        0,
    )


@pytest.mark.parametrize("text", [None, 1, True, b"01"])
def test_parsers_require_text(text: str) -> None:
    for parse in (binary_bit_count, parse_binary_frame):
        with pytest.raises(ValueError, match="texte"):
            parse(text)
    with pytest.raises(ValueError, match="texte"):
        parse_word(text, "bin", 2)
