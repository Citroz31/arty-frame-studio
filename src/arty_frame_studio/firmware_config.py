"""Firmware personnalisé : horloge du cœur et broches DATA/CLK/LATCH.

La configuration par défaut reproduit exactement le firmware de référence et le
fichier ``firmware/constraints/arty_a7_100t.xdc``. Une configuration modifiée
produit un XDC et des paramètres Yosys pour ``arty_top``, ainsi qu'un
identifiant de build lu par la commande INFO. Les broches proviennent du Master
XDC Digilent Arty A7-100 (révisions D/E) ; aucune autre broche n'est acceptée.
"""

from __future__ import annotations

import json
import zlib
from dataclasses import asdict, dataclass, fields
from functools import cache
from pathlib import Path
from typing import Any

BOARD_OSCILLATOR_HZ = 100_000_000
REFERENCE_CORE_HZ = 200_000_000
MIN_CORE_HZ = 50_000_000
# Le build de référence passe à 218 MHz ; au-delà, aucune marge n'est démontrée.
MAX_CORE_HZ = 200_000_000
VCO_MIN_HZ = 800_000_000
VCO_MAX_HZ = 1_600_000_000
DRIVES_MA = (4, 8, 12, 16)
SLEWS = ("SLOW", "FAST")
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PllSetting:
    """VCO = 100 MHz × multiplier ; cœur = VCO / output_divider (DIVCLK = 1)."""

    core_hz: int
    multiplier: int
    output_divider: int

    @property
    def vco_hz(self) -> int:
        return BOARD_OSCILLATOR_HZ * self.multiplier

    @property
    def tick_ns(self) -> float:
        return 1e9 / (2 * self.core_hz)


@cache
def pll_settings() -> tuple[PllSetting, ...]:
    """Fréquences de cœur entières réalisables, de la plus haute à la plus basse.

    Pour chaque fréquence, le VCO le plus proche de 1 GHz est retenu ; 200 MHz
    donne donc ×10 / 5, la configuration du firmware de référence.
    """
    best: dict[int, PllSetting] = {}
    for multiplier in range(8, 17):
        vco = BOARD_OSCILLATOR_HZ * multiplier
        if not VCO_MIN_HZ <= vco <= VCO_MAX_HZ:
            continue
        for divider in range(1, 129):
            if vco % divider:
                continue
            core = vco // divider
            if not MIN_CORE_HZ <= core <= MAX_CORE_HZ:
                continue
            setting = PllSetting(core, multiplier, divider)
            current = best.get(core)
            if current is None or (abs(vco - 1_000_000_000), multiplier) < (
                abs(current.vco_hz - 1_000_000_000),
                current.multiplier,
            ):
                best[core] = setting
    return tuple(sorted(best.values(), key=lambda setting: -setting.core_hz))


def pll_for(core_hz: int) -> PllSetting:
    for setting in pll_settings():
        if setting.core_hz == core_hz:
            return setting
    raise ValueError(
        f"Horloge de cœur {core_hz} Hz non réalisable : choisir une valeur entière "
        f"de 100 MHz × M / O entre {MIN_CORE_HZ // 10**6} et {MAX_CORE_HZ // 10**6} MHz."
    )


@dataclass(frozen=True)
class PmodPin:
    name: str
    package_pin: str
    bank: int
    # Paire différentielle de la banque (``L11``) ou chaîne vide.
    pair: str

    @property
    def connector(self) -> str:
        return self.name[:2]

    @property
    def high_speed(self) -> bool:
        """JB et JC sont routés en paires appariées, sans résistance série."""
        return self.connector in ("JB", "JC")


def _pins(connector: str, bank: int, entries: str) -> dict[str, PmodPin]:
    result = {}
    for position, item in zip((1, 2, 3, 4, 7, 8, 9, 10), entries.split(), strict=True):
        package_pin, _, pair = item.partition(":")
        name = f"{connector}{position}"
        result[name] = PmodPin(name, package_pin, bank, pair)
    return result


# Digilent Arty-A7-100-Master.xdc : ja[0..7], jb[0..7], jc[0..7], jd[0..7].
PMOD_PINS: dict[str, PmodPin] = {
    **_pins("JA", 15, "G13 B11:L4 A11:L4 D12:L6 D13:L6 B18:L10 A18:L10 K16"),
    **_pins("JB", 15, "E15:L11 E16:L11 D15:L12 C15:L12 J17:L23 J18:L23 K15:L24 J15:L24"),
    **_pins("JC", 14, "U12:L20 V12:L20 V10:L21 V11:L21 U14:L22 V14:L22 T13:L23 U13:L23"),
    **_pins("JD", 35, "D4:L11 D3:L12 F4:L13 F3:L13 E2:L14 D2:L14 H2:L15 G2:L15"),
}


@dataclass(frozen=True)
class FirmwareBuildConfig:
    core_hz: int = REFERENCE_CORE_HZ
    data_pin: str = "JB1"
    clock_pin: str = "JB2"
    latch_pin: str = "JB3"
    drive_ma: int = 8
    slew: str = "FAST"

    def __post_init__(self) -> None:
        if type(self.core_hz) is not int:
            raise ValueError("L'horloge de cœur doit être un entier en Hz.")
        pll_for(self.core_hz)
        pins = (self.data_pin, self.clock_pin, self.latch_pin)
        for label, pin in zip(("DATA", "CLK", "LATCH"), pins, strict=True):
            if not isinstance(pin, str) or pin not in PMOD_PINS:
                raise ValueError(
                    f"Broche {label} inconnue : {pin!r}. Choisir JA1-JA10, JB1-JB10, "
                    "JC1-JC10 ou JD1-JD10 (5, 6, 11 et 12 sont masse et 3,3 V)."
                )
        if len(set(pins)) != 3:
            raise ValueError("DATA, CLK et LATCH doivent utiliser trois broches distinctes.")
        if type(self.drive_ma) is not int or self.drive_ma not in DRIVES_MA:
            raise ValueError("Courant de sortie LVCMOS33 : 4, 8, 12 ou 16 mA.")
        if self.slew not in SLEWS:
            raise ValueError("Vitesse de front : SLOW ou FAST.")

    @property
    def pll(self) -> PllSetting:
        return pll_for(self.core_hz)

    @property
    def is_reference(self) -> bool:
        return self == FirmwareBuildConfig()

    @property
    def build_id(self) -> int:
        """0 pour la référence ; sinon CRC32 non nul de la configuration, sur 31 bits.

        31 bits gardent un entier décimal positif pour ``chparam`` de Yosys.
        """
        if self.is_reference:
            return 0
        return zlib.crc32(self.canonical_json().encode("ascii")) & 0x7FFFFFFF or 1

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def summary(self) -> str:
        pins = ", ".join(
            f"{label} {name} ({PMOD_PINS[name].package_pin})"
            for label, name in (
                ("DATA", self.data_pin),
                ("CLK", self.clock_pin),
                ("LATCH", self.latch_pin),
            )
        )
        return (
            f"cœur {self.core_hz / 1e6:g} MHz, {pins}, "
            f"LVCMOS33 {self.drive_ma} mA {self.slew}, build 0x{self.build_id:08X}"
        )

    def warnings(self) -> list[str]:
        """Points de vigilance électriques ; ils n'empêchent pas la compilation."""
        data, clock, latch = (
            PMOD_PINS[name] for name in (self.data_pin, self.clock_pin, self.latch_pin)
        )
        notes = []
        slow = [pin.name for pin in (data, clock, latch) if not pin.high_speed]
        if slow:
            notes.append(
                f"{', '.join(slow)} : Pmod standard avec résistance série de 200 Ω, "
                "fronts lents ; préférer JB ou JC au-delà de quelques MHz."
            )
        if data.high_speed and data.connector == clock.connector and data.pair == clock.pair:
            notes.append(
                f"DATA et CLK partagent la paire différentielle {data.pair} de "
                f"{data.connector} : couplage fort. Utiliser deux paires distinctes."
            )
        if len({data.connector, clock.connector, latch.connector}) > 1:
            notes.append(
                "Sorties réparties sur plusieurs connecteurs : longueurs, masses et "
                "décalages différents ; mesurer le skew au récepteur."
            )
        if self.slew == "SLOW" and self.core_hz > 50_000_000:
            notes.append("SLEW SLOW limite les fronts : déconseillé si CLK dépasse 50 MHz.")
        if self.drive_ma <= 4:
            notes.append("4 mA : fronts lents sur une liaison chargée ; vérifier à l'oscilloscope.")
        return notes

    def yosys_parameters(self) -> dict[str, int]:
        return {
            "CORE_HZ": self.core_hz,
            "PLL_MULT": self.pll.multiplier,
            "PLL_OUT_DIV": self.pll.output_divider,
            "BUILD_ID": self.build_id,
        }

    def xdc(self) -> str:
        """Contraintes complètes ; la configuration par défaut donne le XDC du dépôt."""
        mhz = f"{self.core_hz / 1e6:g}"

        def output(port: str, name: str) -> str:
            pin = PMOD_PINS[name].package_pin
            return (
                f"set_property -dict {{PACKAGE_PIN {pin} IOSTANDARD LVCMOS33 "
                f"SLEW {self.slew} DRIVE {self.drive_ma}}} [get_ports {port}]"
            )

        outputs = ", ".join(
            f"{label} {name} ({PMOD_PINS[name].package_pin})"
            for label, name in (
                ("DATA", self.data_pin),
                ("CLK", self.clock_pin),
                ("LATCH", self.latch_pin),
            )
        )
        lines = [
            "# Arty A7-100T xc7a100tcsg324-1, board revisions D/E.",
            "# Digilent: https://github.com/Digilent/digilent-xdc/blob/master/Arty-A7-100-Master.xdc",
            "# Generated by arty_frame_studio.firmware_config; edit the configuration,",
            "# not this file. Only nextpnr-xilinx-supported commands are used.",
            f"# The second create_clock constrains the synthesized {mhz} MHz BUFG net.",
            "# create_generated_clock is NOT implemented by this nextpnr XDC parser.",
            "set_property -dict {PACKAGE_PIN E3 IOSTANDARD LVCMOS33} [get_ports clk100]",
            "create_clock -period 10.000 -name clk100 [get_ports clk100]",
            f"create_clock -period {1e9 / self.core_hz:.3f} -name core_clock [get_nets core_clock]",
            "",
            "# ck_rst: red RESET button, active low (not the user push buttons).",
            "set_property -dict {PACKAGE_PIN C2 IOSTANDARD LVCMOS33} [get_ports reset_n]",
            "",
            "# FT2232 USB-UART. Digilent names these from the PC (DTE) point of view:",
            "# uart_txd_in (A9) is the PC transmit line, an FPGA INPUT; uart_rxd_out (D10)",
            "# is the PC receive line, an FPGA OUTPUT. LiteX digilent_arty: tx=D10, rx=A9.",
            "set_property -dict {PACKAGE_PIN A9 IOSTANDARD LVCMOS33} [get_ports uart_rx]",
            "set_property -dict {PACKAGE_PIN D10 IOSTANDARD LVCMOS33} [get_ports uart_tx]",
            "",
            f"# Outputs: {outputs}.",
            "# Pmod pins 5/11 are ground, 6/12 are 3.3 V supply.",
            output("data_out", self.data_pin),
            output("frame_clk", self.clock_pin),
            output("latch_enable", self.latch_pin),
            "",
            "# Four monochrome LEDs LD4-LD7: locked, busy, reserved, completed (LSB first).",
            "# A host LED command temporarily replaces them with its test pattern.",
            "set_property -dict {PACKAGE_PIN H5 IOSTANDARD LVCMOS33} [get_ports {led[0]}]",
            "set_property -dict {PACKAGE_PIN J5 IOSTANDARD LVCMOS33} [get_ports {led[1]}]",
            "set_property -dict {PACKAGE_PIN T9 IOSTANDARD LVCMOS33} [get_ports {led[2]}]",
            "set_property -dict {PACKAGE_PIN T10 IOSTANDARD LVCMOS33} [get_ports {led[3]}]",
        ]
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "firmware": asdict(self)}

    @classmethod
    def from_dict(cls, data: object) -> FirmwareBuildConfig:
        if (
            not isinstance(data, dict)
            or set(data) != {"schema_version", "firmware"}
            or data["schema_version"] != SCHEMA_VERSION
            or not isinstance(data["firmware"], dict)
        ):
            raise ValueError(
                "Configuration firmware invalide : schema_version=1 et firmware requis."
            )
        values = data["firmware"]
        names = {field.name for field in fields(cls)}
        unknown = set(values) - names
        if unknown:
            raise ValueError(f"Champs firmware inconnus : {', '.join(sorted(unknown))}.")
        return cls(**values)

    @classmethod
    def from_json(cls, text: str) -> FirmwareBuildConfig:
        if len(text) > 4096:
            raise ValueError("Configuration firmware trop volumineuse.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Configuration firmware illisible : {exc}") from exc
        return cls.from_dict(data)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path) -> FirmwareBuildConfig:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))
