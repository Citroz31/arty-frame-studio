"""Custom firmware settings: PLL choices, Pmod pins and generated constraints."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from arty_frame_studio.firmware_config import (
    BOARD_OSCILLATOR_HZ,
    PMOD_PINS,
    VCO_MAX_HZ,
    VCO_MIN_HZ,
    FirmwareBuildConfig,
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
        "PLL_OUT_DIV": 5,
        "BUILD_ID": 0,
    }


def test_every_pll_setting_is_exact_and_inside_the_vco_range():
    settings = pll_settings()
    assert settings[0].core_hz == 200_000_000
    assert (settings[0].multiplier, settings[0].output_divider) == (10, 5)
    assert len({setting.core_hz for setting in settings}) == len(settings)
    for setting in settings:
        assert VCO_MIN_HZ <= setting.vco_hz <= VCO_MAX_HZ
        assert setting.core_hz * setting.output_divider == BOARD_OSCILLATOR_HZ * setting.multiplier
        assert 50_000_000 <= setting.core_hz <= 200_000_000
    assert pll_for(150_000_000).tick_ns == pytest.approx(1e9 / 300e6)


@pytest.mark.parametrize("core_hz", [210_000_000, 133_333_333, 40_000_000, 200e6])
def test_unreachable_or_untimed_core_clock_is_rejected(core_hz):
    with pytest.raises(ValueError, match="cœur"):
        FirmwareBuildConfig(core_hz=core_hz)


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


@pytest.mark.parametrize("setting", pll_settings(), ids=lambda setting: str(setting.core_hz))
def test_period_rounding_never_relaxes_the_requested_clock(setting):
    xdc = FirmwareBuildConfig(core_hz=setting.core_hz).xdc()
    line = next(line for line in xdc.splitlines() if "-name core_clock " in line)
    period = Decimal(line.split("-period ")[1].split()[0])
    assert period * setting.core_hz <= Decimal(1_000_000_000)
    assert Decimal(1_000_000_000) / period - setting.core_hz < Decimal("0.1")
