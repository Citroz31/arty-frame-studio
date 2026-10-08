"""Firmware personnalisé : horloge du cœur et broches DATA/CLK/LATCH.

La configuration par défaut reproduit exactement le firmware de référence et le
fichier ``firmware/constraints/arty_a7_100t.xdc``. Une configuration modifiée
produit un XDC et des paramètres Yosys pour ``arty_top``, ainsi qu'un
identifiant de build lu par la commande INFO. Les broches proviennent du Master
XDC Digilent Arty A7-100 (révisions D/E) ; aucune autre broche n'est acceptée.
"""

from __future__ import annotations

import json
import math
import zlib
from dataclasses import asdict, dataclass, fields
from decimal import ROUND_FLOOR, Decimal
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Any

BOARD_OSCILLATOR_HZ = 100_000_000
REFERENCE_CORE_HZ = 200_000_000
# Limites du PLLE2_ADV du xc7a100tcsg324-1, vitesse -1 (DS181) : comparateur
# de phase (PFD = 100 MHz / D) de 19 à 450 MHz, VCO de 800 à 1600 MHz, sortie
# de 6,25 à 800 MHz. Le réseau d'horloge BUFG est limité à 464 MHz.
VCO_MIN_HZ = 800_000_000
VCO_MAX_HZ = 1_600_000_000
PFD_MIN_HZ = 19_000_000
PFD_MAX_HZ = 450_000_000
PLL_OUTPUT_MAX_HZ = 800_000_000
BUFG_MAX_HZ = 464_000_000
MIN_CORE_HZ = VCO_MIN_HZ // 128
# Limite absolue de ce firmware : l'horloge de cœur la plus élevée dont le
# timing routé du moteur de trame est démontré par nextpnr-xilinx (environ un
# placement sur trois passe à 300 MHz, le balayage essaie jusqu'à 16 graines ;
# voir docs/hardware.md). Elle reste sous les limites du silicium ci-dessus.
# CLK vaut le cœur / N : c'est aussi la fréquence CLK maximale (N = 1).
MAX_CORE_HZ = 300_000_000
MAX_DIVIDER = 65_535
DRIVES_MA = (4, 8, 12, 16)
SLEWS = ("SLOW", "FAST")
SCHEMA_VERSION = 1
DEFAULT_TR_PIN = "JB4"


@dataclass(frozen=True)
class PllSetting:
    """Cœur = 100 MHz × multiplier / (input_divider × output_divider).

    ``core_hz`` est la fréquence exacte arrondie au hertz : c'est la valeur
    annoncée par INFO et paramètre ``CORE_HZ`` du firmware.
    """

    core_hz: int
    multiplier: int
    output_divider: int
    input_divider: int = 1

    @property
    def exact_hz(self) -> Fraction:
        return Fraction(
            BOARD_OSCILLATOR_HZ * self.multiplier, self.input_divider * self.output_divider
        )

    @property
    def vco_hz(self) -> Fraction:
        return Fraction(BOARD_OSCILLATOR_HZ * self.multiplier, self.input_divider)

    @property
    def pfd_hz(self) -> Fraction:
        return Fraction(BOARD_OSCILLATOR_HZ, self.input_divider)

    @property
    def period_ns(self) -> Fraction:
        """Période exacte du cœur : 10 ns × D × O / M."""
        return Fraction(10 * self.input_divider * self.output_divider, self.multiplier)

    @property
    def tick_ns(self) -> float:
        return float(self.period_ns / 2)

    def describe(self) -> str:
        return (
            f"100 MHz × {self.multiplier} / ({self.input_divider} × {self.output_divider}) = "
            f"{format_hz(self.exact_hz)}"
        )


def format_hz(value: float | Fraction, digits: int = 9) -> str:
    """Fréquence lisible : ``151.428571 MHz``, ``3.05180 kHz``."""
    number = float(value)
    for scale, unit in ((1e6, "MHz"), (1e3, "kHz"), (1.0, "Hz")):
        if abs(number) >= scale or unit == "Hz":
            return f"{number / scale:.{digits}g} {unit}"
    raise AssertionError("unreachable")


def _valid_vco(multiplier: int, input_divider: int) -> bool:
    pfd = Fraction(BOARD_OSCILLATOR_HZ, input_divider)
    return (
        2 <= multiplier <= 64
        and 1 <= input_divider <= 56
        and PFD_MIN_HZ <= pfd <= PFD_MAX_HZ
        and VCO_MIN_HZ <= pfd * multiplier <= VCO_MAX_HZ
    )


def _valid_pll(multiplier: int, input_divider: int, output_divider: int) -> bool:
    return (
        _valid_vco(multiplier, input_divider)
        and 1 <= output_divider <= 128
        and Fraction(BOARD_OSCILLATOR_HZ * multiplier, input_divider * output_divider)
        <= PLL_OUTPUT_MAX_HZ
    )


@cache
def pll_settings() -> tuple[PllSetting, ...]:
    """Toutes les horloges de cœur réalisables, de la plus haute à la plus basse.

    Chaque fréquence exacte 100 MHz × M / (D × O) entre ``MIN_CORE_HZ`` et
    ``MAX_CORE_HZ`` apparaît une fois. Parmi les réglages qui la donnent, le
    plus petit D (comparateur de phase le plus rapide), puis le VCO le plus
    proche de 1 GHz : 200 MHz donne ×10 / 1 / 5, la configuration du firmware
    de référence, et les réglages D = 1 des versions précédentes sont conservés.
    Deux fréquences exactes distinctes diffèrent d'au moins 100 MHz / 640²,
    soit 244 Hz : l'arrondi au hertz les identifie sans ambiguïté.
    """
    best: dict[Fraction, tuple[tuple[object, ...], PllSetting]] = {}
    # PFD >= 19 MHz : D <= 5. Les bornes du VCO fixent M pour chaque D.
    for input_divider in range(1, BOARD_OSCILLATOR_HZ // PFD_MIN_HZ + 1):
        for multiplier in range(2, 65):
            if not _valid_vco(multiplier, input_divider):
                continue
            for output_divider in range(1, 129):
                exact = Fraction(BOARD_OSCILLATOR_HZ * multiplier, input_divider * output_divider)
                if not MIN_CORE_HZ <= exact <= min(MAX_CORE_HZ, PLL_OUTPUT_MAX_HZ):
                    continue
                setting = PllSetting(round(exact), multiplier, output_divider, input_divider)
                rank = (input_divider, abs(setting.vco_hz - 1_000_000_000), multiplier)
                current = best.get(exact)
                if current is None or rank < current[0]:
                    best[exact] = (rank, setting)
    return tuple(sorted((item[1] for item in best.values()), key=lambda item: -item.exact_hz))


@cache
def _pll_index() -> dict[int, PllSetting]:
    return {setting.core_hz: setting for setting in pll_settings()}


def pll_for(core_hz: int) -> PllSetting:
    setting = _pll_index().get(core_hz) if type(core_hz) is int else None
    if setting is None:
        raise ValueError(
            f"Horloge de cœur {core_hz} Hz non réalisable : choisir une fréquence "
            f"100 MHz × M / (D × O) arrondie au hertz, entre {format_hz(MIN_CORE_HZ)} "
            f"et {format_hz(MAX_CORE_HZ)} (planificateur : plan_frame_clock)."
        )
    return setting


MAX_FRAME_CLOCK_HZ = MAX_CORE_HZ
MIN_FRAME_CLOCK_HZ = Fraction(MIN_CORE_HZ, MAX_DIVIDER)


@dataclass(frozen=True)
class FrameClockPlan:
    """CLK réalisable : horloge de cœur (réglage PLL) divisée par N."""

    requested_hz: Fraction
    setting: PllSetting
    divider: int

    @property
    def core_hz(self) -> int:
        return self.setting.core_hz

    @property
    def achieved_hz(self) -> Fraction:
        return self.setting.exact_hz / self.divider

    @property
    def error_hz(self) -> float:
        return float(self.achieved_hz - self.requested_hz)

    @property
    def error_ppm(self) -> float:
        return float((self.achieved_hz - self.requested_hz) / self.requested_hz * 1_000_000)

    @property
    def exact(self) -> bool:
        return self.achieved_hz == self.requested_hz

    def summary(self) -> str:
        if self.exact:
            gap = "exacte"
        else:
            sign = "+" if self.error_hz > 0 else "-"
            gap = f"écart {sign}{format_hz(abs(self.error_hz), 6)}, {self.error_ppm:+.4g} ppm"
        return (
            f"CLK {format_hz(self.achieved_hz)} ({gap}) = cœur "
            f"{format_hz(self.setting.exact_hz)} / N={self.divider} ; "
            f"PLL ×{self.setting.multiplier} /{self.setting.input_divider} "
            f"/{self.setting.output_divider}, pas des durées "
            f"{self.setting.tick_ns:.6g} ns"
        )


def requested_frequency(value: float | int | str | Fraction | Decimal) -> Fraction:
    """Fréquence saisie (Hz) en valeur exacte ; ``"150e6"`` ou ``150e6``."""
    try:
        if isinstance(value, Fraction):
            exact = value
        elif isinstance(value, str):
            exact = Fraction(Decimal(value.strip().replace(",", ".")))
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError
            exact = Fraction(Decimal(repr(value)))
        else:
            exact = Fraction(value)
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise ValueError("Fréquence : nombre fini attendu, en hertz.") from exc
    if exact <= 0:
        raise ValueError("La fréquence doit être strictement positive.")
    return exact


def plan_frame_clock(
    requested: float | int | str | Fraction | Decimal,
    *,
    never_above: bool = False,
    current_core_hz: int | None = None,
) -> FrameClockPlan:
    """Fréquence CLK réalisable la plus proche de la demande.

    Toutes les horloges de cœur ``pll_settings()`` et tous les diviseurs N de
    1 à 65535 sont considérés. ``never_above`` exclut les fréquences
    supérieures à la demande (récepteur jamais surcadencé). À écart égal :
    la fréquence inférieure, puis l'horloge du firmware chargé
    (``current_core_hz``, aucune recompilation), puis celle du firmware de
    référence, puis le cœur le plus proche de 200 MHz. Au-delà de
    ``MAX_FRAME_CLOCK_HZ``, la limite absolue, la demande est refusée.
    """
    target = requested_frequency(requested)
    if target > MAX_FRAME_CLOCK_HZ:
        raise ValueError(
            f"{format_hz(target)} dépasse la limite absolue de ce FPGA : CLK "
            f"{format_hz(MAX_FRAME_CLOCK_HZ)} au plus (cœur maximal validé, N = 1)."
        )
    if target < MIN_FRAME_CLOCK_HZ:
        raise ValueError(
            f"{format_hz(target)} est trop lente : CLK {format_hz(MIN_FRAME_CLOCK_HZ)} "
            f"au moins (cœur {format_hz(MIN_CORE_HZ)}, N = {MAX_DIVIDER})."
        )
    # Écart en flottant pour tous les candidats, puis classement exact des
    # quelques candidats à égalité numérique.
    candidates: list[tuple[float, PllSetting, int]] = []
    goal = float(target)
    for setting in pll_settings():
        core = float(setting.exact_hz)
        lower = max(1, min(MAX_DIVIDER, int(core // goal)))
        for divider in {lower, min(MAX_DIVIDER, lower + 1)}:
            achieved = core / divider
            if never_above and achieved > goal * (1 + 1e-12):
                continue
            candidates.append((abs(achieved - goal), setting, divider))
    if not candidates:
        raise ValueError(f"Aucune fréquence CLK réalisable sous {format_hz(target)}.")
    closest = min(error for error, _, _ in candidates)
    best: tuple[tuple[object, ...], FrameClockPlan] | None = None
    for error, setting, divider in candidates:
        if error > closest + goal * 1e-9:
            continue
        exact_core = setting.exact_hz
        exact = exact_core / divider
        if never_above and exact > target:
            continue
        preference = (
            0
            if setting.core_hz == current_core_hz
            else 1
            if setting.core_hz == REFERENCE_CORE_HZ
            else 2
        )
        rank = (
            abs(exact - target),
            exact > target,
            preference,
            abs(exact_core - REFERENCE_CORE_HZ),
            -exact_core,
        )
        if best is None or rank < best[0]:
            best = (rank, FrameClockPlan(target, setting, divider))
    if best is None:
        raise ValueError(f"Aucune fréquence CLK réalisable sous {format_hz(target)}.")
    return best[1]


def frame_clock_neighbors(
    requested: float | int | str | Fraction | Decimal,
) -> tuple[FrameClockPlan | None, FrameClockPlan | None]:
    """Fréquences CLK réalisables encadrant la demande : (inférieure ou égale, supérieure)."""
    target = requested_frequency(requested)
    below = above = None
    if target >= MIN_FRAME_CLOCK_HZ:
        below = plan_frame_clock(min(target, Fraction(MAX_FRAME_CLOCK_HZ)), never_above=True)
    if target < MAX_FRAME_CLOCK_HZ:
        best: FrameClockPlan | None = None
        for setting in pll_settings():
            core = setting.exact_hz
            divider = min(MAX_DIVIDER, max(1, math.ceil(core / target) - 1))
            while divider > 1 and core / divider <= target:
                divider -= 1
            achieved = core / divider
            if achieved <= target:
                continue
            if best is None or achieved < best.achieved_hz:
                best = FrameClockPlan(target, setting, divider)
        above = best
    return below, above


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
    # Static transmit/receive level (firmware revision 5): 3.3 V or 0 V, no clock.
    tr_pin: str = DEFAULT_TR_PIN
    drive_ma: int = 8
    slew: str = "FAST"

    def __post_init__(self) -> None:
        if type(self.core_hz) is not int:
            raise ValueError("L'horloge de cœur doit être un entier en Hz.")
        pll_for(self.core_hz)
        pins = (self.data_pin, self.clock_pin, self.latch_pin, self.tr_pin)
        for label, pin in zip(("DATA", "CLK", "LATCH", "TR"), pins, strict=True):
            if not isinstance(pin, str) or pin not in PMOD_PINS:
                raise ValueError(
                    f"Broche {label} inconnue : {pin!r}. Choisir JA1-JA10, JB1-JB10, "
                    "JC1-JC10 ou JD1-JD10 (5, 6, 11 et 12 sont masse et 3,3 V)."
                )
        if len(set(pins)) != 4:
            raise ValueError("DATA, CLK, LATCH et TR doivent utiliser quatre broches distinctes.")
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
        data = asdict(self)
        # TR est apparu avec la révision 5 : la broche par défaut ne change pas
        # l'identifiant des configurations déjà compilées.
        if self.tr_pin == DEFAULT_TR_PIN:
            del data["tr_pin"]
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    def summary(self) -> str:
        pins = ", ".join(
            f"{label} {name} ({PMOD_PINS[name].package_pin})"
            for label, name in (
                ("DATA", self.data_pin),
                ("CLK", self.clock_pin),
                ("LATCH", self.latch_pin),
                ("TR", self.tr_pin),
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
            "PLL_IN_DIV": self.pll.input_divider,
            "PLL_OUT_DIV": self.pll.output_divider,
            "BUILD_ID": self.build_id,
        }

    def xdc(self) -> str:
        """Contraintes complètes ; la configuration par défaut donne le XDC du dépôt."""
        mhz = f"{self.core_hz / 1e6:g}"
        # A 3-decimal period weakens 150 MHz to 149.9925 MHz (6.667 ns).
        # Truncate the exact PLL period (10 ns x D x O / M) with greater
        # precision so the custom constraint is never weaker than the PLL
        # clock. Keep the committed reference XDC.
        exact = self.pll.period_ns
        period = (Decimal(exact.numerator) / Decimal(exact.denominator)).quantize(
            Decimal("0.000000001"), rounding=ROUND_FLOOR
        )
        period_text = "5.000" if self.core_hz == REFERENCE_CORE_HZ else str(period)

        def output(port: str, name: str, slew: str | None = None) -> str:
            pin = PMOD_PINS[name].package_pin
            return (
                f"set_property -dict {{PACKAGE_PIN {pin} IOSTANDARD LVCMOS33 "
                f"SLEW {slew or self.slew} DRIVE {self.drive_ma}}} [get_ports {port}]"
            )

        outputs = ", ".join(
            f"{label} {name} ({PMOD_PINS[name].package_pin})"
            for label, name in (
                ("DATA", self.data_pin),
                ("CLK", self.clock_pin),
                ("LATCH", self.latch_pin),
                ("TR", self.tr_pin),
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
            f"create_clock -period {period_text} -name core_clock [get_nets core_clock]",
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
            f"# TR: static 3.3 V / 0 V level (transmit/receive switch), no clock: {self.tr_pin}"
            f" ({PMOD_PINS[self.tr_pin].package_pin}).",
            output("tr_out", self.tr_pin, "SLOW"),
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
        values = dict(data["firmware"])
        if "tr_pin" not in values:
            # Configuration enregistrée avant TR : si JB4 y sert déjà, prendre une broche libre.
            used = {values.get(key) for key in ("data_pin", "clock_pin", "latch_pin")}
            if DEFAULT_TR_PIN in used:
                values["tr_pin"] = next(
                    name for name in ("JB7", "JB8", "JB9", "JB10", *PMOD_PINS) if name not in used
                )
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
