"""Custom firmware settings: PLL choices, Pmod pins and generated constraints."""

from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from arty_frame_studio.firmware_config import (
    BOARD_OSCILLATOR_HZ,
    MAX_CORE_HZ,
    MAX_FRAME_CLOCK_HZ,
    MIN_CORE_HZ,
    MIN_FRAME_CLOCK_HZ,
    PFD_MIN_HZ,
    PMOD_PINS,
    REFERENCE_CORE_HZ,
    VCO_MAX_HZ,
    VCO_MIN_HZ,
    FirmwareBuildConfig,
    frame_clock_neighbors,
    plan_frame_clock,
    pll_for,
    pll_settings,
)

ROOT = Path(__file__).resolve().parents[1]


def test_default_configuration_is_the_committed_reference_xdc():
    config = FirmwareBuildConfig()
    assert config.is_reference and config.build_id == 0
    committed = (ROOT / "firmware/constraints/arty_a7_100t.xdc").read_text(encoding="utf-8")
    assert config.xdc() == committed
    assert config.yosys_parameters() == {
        "CORE_HZ": 200_000_000,
        "PLL_MULT": 10,
        "PLL_IN_DIV": 1,
        "PLL_OUT_DIV": 5,
        "BUILD_ID": 0,
    }


def test_every_pll_setting_is_exact_and_inside_the_pll_limits():
    settings = pll_settings()
    # Every integer PLL setting of speed grade -1, once per exact frequency.
    assert len(settings) > 6000
    assert settings[0].core_hz == MAX_CORE_HZ and settings[-1].core_hz == MIN_CORE_HZ
    assert len({setting.core_hz for setting in settings}) == len(settings)
    assert len({setting.exact_hz for setting in settings}) == len(settings)
    for setting in settings:
        assert VCO_MIN_HZ <= setting.vco_hz <= VCO_MAX_HZ
        assert setting.pfd_hz >= PFD_MIN_HZ and 1 <= setting.input_divider <= 5
        assert 2 <= setting.multiplier <= 64 and 1 <= setting.output_divider <= 128
        assert setting.exact_hz == Fraction(
            BOARD_OSCILLATOR_HZ * setting.multiplier,
            setting.input_divider * setting.output_divider,
        )
        assert abs(setting.exact_hz - setting.core_hz) <= Fraction(1, 2)
        assert setting.period_ns == 1_000_000_000 / setting.exact_hz
    # The reference keeps x10 / 1 / 5 and earlier D=1 choices are unchanged.
    assert pll_for(REFERENCE_CORE_HZ).describe().startswith("100 MHz × 10 / (1 × 5)")
    assert (pll_for(150_000_000).multiplier, pll_for(150_000_000).input_divider) == (9, 1)
    assert pll_for(150_000_000).tick_ns == pytest.approx(1e9 / 300e6)
    # 100 MHz x 53 / (5 x 7): only a phase-detector divider reaches it.
    assert pll_for(151_428_571).input_divider == 5


@pytest.mark.parametrize("core_hz", [MAX_CORE_HZ + 1, 133_333_334, 6_000_000, 200e6])
def test_unreachable_or_untimed_core_clock_is_rejected(core_hz):
    with pytest.raises(ValueError, match="cœur"):
        FirmwareBuildConfig(core_hz=core_hz)


@pytest.mark.parametrize(
    ("requested", "achieved", "core_hz", "divider"),
    [
        ("150e6", Fraction(150_000_000), 150_000_000, 1),
        ("151e6", Fraction(1_060_000_000, 7), 151_428_571, 1),
        ("120e6", Fraction(120_000_000), 240_000_000, 2),
        ("122e6", Fraction(122_000_000), 244_000_000, 2),
        ("10e6", Fraction(10_000_000), REFERENCE_CORE_HZ, 20),
        ("3e3", Fraction(3_000), 195_000_000, 65_000),
    ],
)
def test_frame_clock_plan_finds_the_nearest_frequency(requested, achieved, core_hz, divider):
    plan = plan_frame_clock(requested)
    assert (plan.achieved_hz, plan.core_hz, plan.divider) == (achieved, core_hz, divider)
    assert plan.setting == pll_for(core_hz)


def test_frame_clock_plan_rounding_preferences_and_limits():
    below = plan_frame_clock("151e6", never_above=True)
    assert below.achieved_hz == 150_000_000 and below.error_hz == -1e6
    lower, upper = frame_clock_neighbors("151e6")
    assert lower is not None and upper is not None
    assert lower.achieved_hz == 150_000_000 and upper.achieved_hz == Fraction(1_060_000_000, 7)
    # Equal frequency: the loaded firmware, then the reference, needs no build.
    assert plan_frame_clock("50e6").core_hz == REFERENCE_CORE_HZ
    assert plan_frame_clock("50e6", current_core_hz=150_000_000).core_hz == 150_000_000
    assert plan_frame_clock(MAX_FRAME_CLOCK_HZ).divider == 1
    with pytest.raises(ValueError, match="limite absolue"):
        plan_frame_clock(MAX_FRAME_CLOCK_HZ + 1)
    with pytest.raises(ValueError, match="trop lente"):
        plan_frame_clock(MIN_FRAME_CLOCK_HZ / 2)
    for bad in ("abc", "nan", "-5", 0, float("inf")):
        with pytest.raises(ValueError):
            plan_frame_clock(bad)


def test_pin_table_matches_digilent_master_xdc_and_board_resources():
    assert len(PMOD_PINS) == 32
    assert len({pin.package_pin for pin in PMOD_PINS.values()}) == 32
    fixed = {"E3", "C2", "A9", "D10", "H5", "J5", "T9", "T10"}
    assert not fixed & {pin.package_pin for pin in PMOD_PINS.values()}
    assert [PMOD_PINS[f"JB{n}"].package_pin for n in (1, 2, 3, 4, 7, 8, 9, 10)] == [
        "E15",
        "E16",
        "D15",
        "C15",
        "J17",
        "J18",
        "K15",
        "J15",
    ]
    assert PMOD_PINS["JB1"].pair == PMOD_PINS["JB2"].pair == "L11"
    assert PMOD_PINS["JC1"].package_pin == "U12" and PMOD_PINS["JD10"].package_pin == "G2"
    assert PMOD_PINS["JA1"].package_pin == "G13" and PMOD_PINS["JA10"].package_pin == "K16"


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"data_pin": "JB5"}, "DATA"),
        ({"clock_pin": "E15"}, "CLK"),
        ({"latch_pin": "JB1"}, "distinctes"),
        ({"drive_ma": 6}, "mA"),
        ({"drive_ma": True}, "mA"),
        ({"slew": "fast"}, "SLOW ou FAST"),
    ],
)
def test_invalid_pins_and_io_settings_are_rejected(changes, message):
    with pytest.raises(ValueError, match=message):
        replace(FirmwareBuildConfig(), **changes)


def test_custom_configuration_generates_its_own_xdc_and_identity():
    config = FirmwareBuildConfig(
        core_hz=150_000_000, data_pin="JC3", clock_pin="JC1", latch_pin="JC7", drive_ma=12
    )
    xdc = config.xdc()
    assert "create_clock -period 6.666666666 -name core_clock [get_nets core_clock]" in xdc
    assert "PACKAGE_PIN V10 IOSTANDARD LVCMOS33 SLEW FAST DRIVE 12} [get_ports data_out]" in xdc
    assert "PACKAGE_PIN U12 IOSTANDARD LVCMOS33 SLEW FAST DRIVE 12} [get_ports frame_clk]" in xdc
    assert "PACKAGE_PIN U14 IOSTANDARD LVCMOS33 SLEW FAST DRIVE 12} [get_ports latch_enable]" in xdc
    assert "uart_rx]" in xdc and "PACKAGE_PIN A9" in xdc
    assert config.build_id not in (0, FirmwareBuildConfig().build_id)
    assert replace(config, drive_ma=8).build_id != config.build_id
    assert config.yosys_parameters()["PLL_MULT"] * 100_000_000 == 150_000_000 * 6
    assert config.warnings() == []


def test_electrical_warnings_explain_risky_choices():
    assert "paire différentielle L11" in FirmwareBuildConfig().warnings()[0]
    risky = FirmwareBuildConfig(data_pin="JA1", clock_pin="JB1", latch_pin="JC1", slew="SLOW")
    notes = " ".join(risky.warnings())
    assert "200 Ω" in notes and "plusieurs connecteurs" in notes and "SLEW SLOW" in notes


def test_json_roundtrip_is_strict(tmp_path):
    config = FirmwareBuildConfig(
        core_hz=100_000_000, data_pin="JB3", clock_pin="JB1", latch_pin="JB7"
    )
    path = tmp_path / "firmware.json"
    config.save(path)
    assert FirmwareBuildConfig.load(path) == config
    assert FirmwareBuildConfig.from_dict(
        {"schema_version": 1, "firmware": {"core_hz": 100_000_000}}
    ) == FirmwareBuildConfig(core_hz=100_000_000)
    for bad in (
        "[]",
        '{"schema_version": 2, "firmware": {}}',
        '{"schema_version": 1, "firmware": {"pins": "JB1"}}',
        "{",
        " " * 5000,
    ):
        with pytest.raises(ValueError):
            FirmwareBuildConfig.from_json(bad)


def test_build_identity_is_a_positive_31_bit_integer():
    for pin in ("JC1", "JC2", "JD1", "JA7", "JB9"):
        build_id = FirmwareBuildConfig(clock_pin=pin).build_id
        assert 0 < build_id < 2**31
        assert FirmwareBuildConfig(clock_pin=pin).yosys_parameters()["BUILD_ID"] == build_id


def test_period_rounding_never_relaxes_the_requested_clock():
    for setting in pll_settings():
        xdc = FirmwareBuildConfig(core_hz=setting.core_hz).xdc()
        line = next(line for line in xdc.splitlines() if "-name core_clock " in line)
        period = Fraction(Decimal(line.split("-period ")[1].split()[0]))
        # Never weaker than the exact PLL frequency, at most 1 ppm stricter.
        assert period * setting.exact_hz <= 1_000_000_000
        assert 1_000_000_000 / period - setting.exact_hz < setting.exact_hz / 1_000_000


def test_tr_pin_defaults_to_jb4_and_is_a_slow_static_output():
    config = FirmwareBuildConfig()
    xdc = config.xdc()
    assert config.tr_pin == "JB4" and "TR JB4 (C15)" in config.summary()
    assert "PACKAGE_PIN C15 IOSTANDARD LVCMOS33 SLEW SLOW DRIVE 8} [get_ports tr_out]" in xdc
    custom = replace(config, tr_pin="JC4", drive_ma=12)
    assert (
        "PACKAGE_PIN V11 IOSTANDARD LVCMOS33 SLEW SLOW DRIVE 12} [get_ports tr_out]" in custom.xdc()
    )
    assert custom.build_id not in (0, config.build_id) and not custom.is_reference
    with pytest.raises(ValueError, match="TR"):
        replace(config, tr_pin="JB5")
    with pytest.raises(ValueError, match="distinctes"):
        replace(config, tr_pin="JB1")
    with pytest.raises(ValueError, match="distinctes"):
        replace(config, data_pin="JB4")


def test_the_default_tr_pin_keeps_the_identifier_of_configurations_built_before_tr():
    import json
    import zlib

    config = FirmwareBuildConfig(
        core_hz=150_000_000, data_pin="JC3", clock_pin="JC1", latch_pin="JC7", drive_ma=12
    )
    before_tr = {
        "clock_pin": "JC1",
        "core_hz": 150_000_000,
        "data_pin": "JC3",
        "drive_ma": 12,
        "latch_pin": "JC7",
        "slew": "FAST",
    }
    expected = zlib.crc32(json.dumps(before_tr, sort_keys=True, separators=(",", ":")).encode())
    assert config.build_id == expected & 0x7FFFFFFF
    assert '"tr_pin"' not in config.canonical_json()
    assert '"tr_pin":"JC4"' in replace(config, tr_pin="JC4").canonical_json()


def test_a_configuration_saved_before_tr_never_collides_with_it():
    old = {"core_hz": 200_000_000, "data_pin": "JB4", "clock_pin": "JB2", "latch_pin": "JB3"}
    loaded = FirmwareBuildConfig.from_dict({"schema_version": 1, "firmware": old})
    assert loaded.data_pin == "JB4" and loaded.tr_pin == "JB7"
    plain = FirmwareBuildConfig.from_dict({"schema_version": 1, "firmware": {}})
    assert plain.tr_pin == "JB4"
