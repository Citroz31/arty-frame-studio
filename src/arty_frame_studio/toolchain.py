"""Local Yosys/nextpnr-xilinx/Project X-Ray builds, with SRAM programming.

No command is interpreted by a shell. Only a successful build of the current
sources/configuration creates a programming receipt; failed rebuilds revoke it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import IO, Any, Literal

from .bitstream import BitstreamError, BitstreamImage, read_bitstream
from .firmware_config import FirmwareBuildConfig

TARGET_PART = "xc7a100tcsg324-1"
TARGET_BOARD = "arty_a7_100t"
Command = str | tuple[str, ...]
Log = Callable[[str], None]


class ToolchainError(RuntimeError):
    """A missing dependency, failed build, or invalid programming receipt."""


@dataclass(frozen=True)
class DoctorResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ToolchainConfig:
    yosys: Command = "yosys"
    yosys_mapping: Literal["abc", "abc9"] = "abc9"
    nextpnr_xilinx: Command = "nextpnr-xilinx"
    nextpnr_backend: Literal["classic", "himbaechel"] = "classic"
    fasm2frames: Command = "fasm2frames"
    xc7frames2bit: Command = "xc7frames2bit"
    openfpgaloader: Command = "openFPGALoader"
    chipdb: Path | None = None
    prjxray_db: Path | None = None
    part: str = TARGET_PART
    build_dir: Path = Path("build")
    # Portable bundles keep DLLs, ABC and Python modules outside the host
    # installation. Extend only the child process environment, never the PC.
    tool_dirs: tuple[Path, ...] = ()
    python_path: tuple[Path, ...] = ()
    # Placement seeds tried in order. The core paths sit close to 5 ns and
    # placement alone moves the routed Fmax by ~15 %: the sweep stops at the
    # first seed with timing_margin above the requirement, otherwise keeps
    # the passing seed with the highest Fmax.
    nextpnr_seeds: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8)
    timing_margin: float = 0.03

    def __post_init__(self) -> None:
        if self.part != TARGET_PART:
            raise ValueError(f"Cette application exige le composant {TARGET_PART}.")
        if self.nextpnr_backend not in ("classic", "himbaechel"):
            raise ValueError("nextpnr_backend doit être classic ou himbaechel.")
        if self.yosys_mapping not in ("abc", "abc9"):
            raise ValueError("yosys_mapping doit être abc ou abc9.")
        for name in _TOOLS:
            command = getattr(self, name)
            args = (command,) if isinstance(command, str) else command
            if not isinstance(args, tuple) or not args:
                raise ValueError(f"{name} : fournir un exécutable ou une liste d'arguments.")
            if any(not isinstance(arg, str) or not arg or "\0" in arg for arg in args):
                raise ValueError(f"{name} : arguments vides ou invalides.")
            if name == "nextpnr_xilinx" and any("timing-allow-fail" in arg for arg in args):
                raise ValueError("--timing-allow-fail est interdit : le timing doit réussir.")
            if name == "openfpgaloader" and len(args) != 1:
                raise ValueError("openfpgaloader accepte uniquement le chemin de l'exécutable.")
        if (
            not isinstance(self.nextpnr_seeds, tuple)
            or not 1 <= len(self.nextpnr_seeds) <= 32
            or any(type(seed) is not int or not 0 <= seed < 2**31 for seed in self.nextpnr_seeds)
            or len(set(self.nextpnr_seeds)) != len(self.nextpnr_seeds)
        ):
            raise ValueError("nextpnr_seeds : 1 à 32 entiers distincts, positifs ou nuls.")
        if (
            type(self.timing_margin) not in (int, float)
            or not math.isfinite(self.timing_margin)
            or not 0 <= self.timing_margin <= 0.5
        ):
            raise ValueError("timing_margin : marge relative entre 0 et 0.5 (0.03 = 3 %).")
        for name in ("chipdb", "prjxray_db", "build_dir"):
            value = getattr(self, name)
            if name == "build_dir" and value is None:
                raise ValueError("build_dir doit être un chemin non vide.")
            if not isinstance(value, Path) and (value is not None or name == "build_dir"):
                raise ValueError(f"{name} doit être un pathlib.Path.")
        for name in ("tool_dirs", "python_path"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or any(not isinstance(path, Path) for path in value):
                raise ValueError(f"{name} doit être un tuple de pathlib.Path.")

    @classmethod
    def from_json(cls, path: Path | str) -> ToolchainConfig:
        """Resolve data paths relative to the configuration file's directory."""
        path = Path(path).expanduser().resolve()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            # The usual Windows case: the open-source toolchain is not installed.
            raise ToolchainError(
                f"Chaîne FPGA locale non configurée : {path.name} est absent ({path.parent}). "
                "Sous Windows, utilisez « Installer les outils Windows locaux » pour créer "
                "la configuration sans Linux, WSL ni droits administrateur. "
                "Le firmware existant reste disponible via « Charger le .bit sous Windows » ; "
                "« Compiler sur GitHub » est une autre possibilité."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolchainError(f"Configuration illisible : {path} : {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("La configuration doit être un objet JSON.")
        unknown = set(data) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f"Clés de configuration inconnues : {', '.join(sorted(unknown))}.")
        for name in _TOOLS:
            value = data.get(name)
            if isinstance(value, list):
                args = list(value)
            elif isinstance(value, str):
                args = [value]
            else:
                continue
            if not args or any(not isinstance(argument, str) for argument in args):
                raise ValueError(f"{name} : fournir un exécutable ou une liste d'arguments texte.")
            interpreter = Path(args[0]).name.lower().removesuffix(".exe")
            for index, argument in enumerate(args):
                # Portable tool folders need absolute executable/script paths:
                # Windows does not use Popen(cwd=...) to resolve the executable.
                is_script = index != 0 and (
                    argument.lower().endswith(".py")
                    or (index == 1 and interpreter.startswith("python"))
                )
                if (
                    (index == 0 or is_script)
                    and not argument.startswith("-")
                    and (is_script or "/" in argument or "\\" in argument)
                ):
                    candidate = Path(argument).expanduser()
                    if not candidate.is_absolute():
                        args[index] = str((path.parent / candidate).resolve())
            data[name] = args[0] if isinstance(value, str) else tuple(args)
        if isinstance(data.get("nextpnr_seeds"), list):
            data["nextpnr_seeds"] = tuple(data["nextpnr_seeds"])
        for name in ("chipdb", "prjxray_db", "build_dir"):
            if name in data and data[name] is not None:
                if not isinstance(data[name], str) or not data[name].strip():
                    raise ValueError(f"{name} doit être un chemin non vide.")
                candidate = Path(data[name]).expanduser()
                data[name] = (
                    candidate if candidate.is_absolute() else path.parent / candidate
                ).resolve()
        for name in ("tool_dirs", "python_path"):
            if name not in data:
                continue
            value = data[name]
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise ValueError(f"{name} doit être une liste de chemins non vides.")
            directories = []
            for item in value:
                candidate = Path(item).expanduser()
                directories.append(
                    (candidate if candidate.is_absolute() else path.parent / candidate).resolve()
                )
            data[name] = tuple(directories)
        return cls(**data)


_TOOLS = ("yosys", "nextpnr_xilinx", "fasm2frames", "xc7frames2bit", "openfpgaloader")


class Toolchain:
    def __init__(self, config: ToolchainConfig, project_root: Path | str) -> None:
        self.config = config
        self.project_root = Path(project_root).expanduser().resolve()
        self.build_dir = (
            config.build_dir
            if config.build_dir.is_absolute()
            else self.project_root / config.build_dir
        ).resolve()
        if self.build_dir == self.project_root or self.build_dir == Path("/"):
            raise ValueError("Utilisez un sous-dossier dédié pour build_dir.")
        self.bitstream = self.build_dir / "arty_frame.bit"
        self.receipt = self.build_dir / "successful-build.json"
        self.constraints = self.project_root / "firmware/constraints/arty_a7_100t.xdc"

    def _args(self, name: str) -> list[str]:
        value: Command = getattr(self.config, name)
        return [value] if isinstance(value, str) else list(value)

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if self.config.python_path:
            # A host virtualenv must not redirect the bundle's Python standard
            # library or load packages from the user's unrelated installation.
            environment.pop("PYTHONHOME", None)
            environment["PYTHONNOUSERSITE"] = "1"
        for variable, paths in (
            ("PATH", self.config.tool_dirs),
            ("PYTHONPATH", self.config.python_path),
        ):
            if paths:
                prefix = os.pathsep.join(
                    str(path if path.is_absolute() else self.project_root / path) for path in paths
                )
                existing = environment.get(variable, "")
                environment[variable] = prefix + (os.pathsep + existing if existing else "")
        return environment

    def _sources(self) -> list[Path]:
        return sorted((self.project_root / "firmware/rtl").glob("*.v"))

    def _part_file(self) -> Path | None:
        if self.config.prjxray_db is None:
            return None
        return self.config.prjxray_db / self.config.part / "part.yaml"

    def doctor(self) -> list[DoctorResult]:
        """Check local files/executables. Does not claim FPGA or timing validation."""
        results = []
        environment = self._environment()
        for name in _TOOLS:
            arguments = self._args(name)
            executable = arguments[0]
            found = shutil.which(executable, path=environment.get("PATH"))
            results.append(DoctorResult(name, found is not None, found or f"Absent : {executable}"))
            # A Python interpreter may exist even when its converter script
            # is missing. Check the script before running synthesis/routing.
            for index, argument in enumerate(arguments[1:], start=1):
                interpreter = Path(executable).name.lower().removesuffix(".exe")
                if argument.lower().endswith(".py") or (
                    index == 1 and interpreter.startswith("python") and not argument.startswith("-")
                ):
                    script = Path(argument)
                    if not script.is_absolute():
                        script = self.project_root / script
                    results.append(DoctorResult(f"{name} script", script.is_file(), str(script)))
        for name in ("tool_dirs", "python_path"):
            for directory in getattr(self.config, name):
                path = directory if directory.is_absolute() else self.project_root / directory
                results.append(DoctorResult(name, path.is_dir(), str(path)))
        chipdb = self.config.chipdb
        results.append(
            DoctorResult(
                "chipdb",
                bool(chipdb and chipdb.is_file()),
                str(chipdb) if chipdb else "Configurer une chipdb csg324/100T externe.",
            )
        )
        part_file = self._part_file()
        results.append(
            DoctorResult(
                "Project X-Ray",
                bool(part_file and part_file.is_file()),
                str(part_file) if part_file else "Configurer prjxray_db (artix7).",
            )
        )
        results.append(
            DoctorResult("RTL", bool(self._sources()), f"{len(self._sources())} fichiers Verilog")
        )
        results.append(DoctorResult("XDC", self.constraints.is_file(), str(self.constraints)))
        return results

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        self.build_dir.mkdir(parents=True, exist_ok=True)
        lock = self.build_dir / ".toolchain.lock"
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ToolchainError(
                f"Compilation/programmation déjà active ({lock}). Après un arrêt brutal, "
                "supprimez ce verrou uniquement si aucun outil ne tourne."
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(str(os.getpid()))
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _input_digest(self, firmware: FirmwareBuildConfig | None = None) -> str:
        digest = hashlib.sha256()
        settings = asdict(self.config)
        for name in ("tool_dirs", "python_path"):
            if not settings[name]:
                # Empty environment extensions preserve historical receipts.
                del settings[name]
        digest.update(json.dumps(settings, sort_keys=True, default=str).encode())
        if firmware is not None and not firmware.is_reference:
            # The reference build keeps its historical digest; a custom build
            # also covers the configuration that generated its XDC/parameters.
            digest.update(firmware.canonical_json().encode())
        for path in [*self._sources(), self.constraints]:
            digest.update(str(path.relative_to(self.project_root)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _run(
        self,
        args: list[str],
        log: Log | None,
        journal: IO[str],
        *,
        stdout_path: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        code, output = self._execute(args, log, journal, stdout_path=stdout_path, env=env)
        if code != 0:
            raise ToolchainError(f"{args[0]} a échoué (code {code}). Voir {journal.name}.")
        return output

    def _execute(
        self,
        args: list[str],
        log: Log | None,
        journal: IO[str],
        *,
        stdout_path: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        command_line = "$ " + (
            subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
        )
        journal.write(command_line + "\n")
        journal.flush()
        if log:
            log(command_line)
        try:
            env = self._environment() if env is None else env
            if stdout_path is None:
                process = subprocess.Popen(
                    args,
                    cwd=self.project_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )
            else:
                # fasm2frames writes frame data to stdout: preserve it byte-for-byte,
                # and put its diagnostics (stderr) in the build journal.
                with stdout_path.open("wb") as output:
                    process = subprocess.Popen(
                        args,
                        cwd=self.project_root,
                        stdout=output,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env=env,
                    )
        except OSError as exc:
            raise ToolchainError(f"Impossible de lancer {args[0]} : {exc}") from exc
        assert process.stdout is not None or process.stderr is not None
        stream = process.stdout if stdout_path is None else process.stderr
        assert stream is not None
        captured = []
        try:
            for line in stream:
                captured.append(line)
                journal.write(line)
                journal.flush()
                if log:
                    log(line.rstrip("\r\n"))
        finally:
            stream.close()
            code = process.wait()
        journal.write(f"[exit {code}]\n")
        journal.flush()
        return code, "".join(captured)

    @staticmethod
    def _require_output(path: Path) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise ToolchainError(f"L'outil n'a pas produit de sortie non vide : {path}")

    @staticmethod
    def _normalize_himbaechel_oddr(source: Path, destination: Path) -> int:
        """Preserve Yosys output and remove only inactive ODDR set connections.

        openXC7/nextpnr c68c1358 maps both ODDR R and S onto the physical SR
        port. A constant-zero S can overwrite R and leave a dangling GND user.
        Its packer treats an absent S as inactive. Limit that workaround to
        the project's reset-only, initially-low, asynchronous SAME_EDGE mode.
        """
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolchainError(f"Netlist Yosys JSON illisible : {source} : {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("modules"), dict):
            raise ToolchainError("Netlist Yosys invalide : objet modules manquant.")
        normalized = 0
        for module_name, module in data["modules"].items():
            if not isinstance(module, dict) or not isinstance(module.get("cells", {}), dict):
                raise ToolchainError(f"Netlist Yosys invalide : cellules de {module_name}.")
            for cell_name, cell in module.get("cells", {}).items():
                if not isinstance(cell, dict):
                    raise ToolchainError(f"Netlist Yosys invalide : {module_name}.{cell_name}.")
                if cell.get("type") != "ODDR":
                    continue
                parameters = cell.get("parameters", {})
                connections = cell.get("connections", {})
                directions = cell.get("port_directions", {})
                if not all(
                    isinstance(item, dict) for item in (parameters, connections, directions)
                ):
                    raise ToolchainError(f"ODDR {module_name}.{cell_name} : champs JSON invalides.")
                init = parameters.get("INIT")
                init_is_zero = (type(init) is int and init == 0) or (
                    isinstance(init, str) and bool(init) and set(init) == {"0"}
                )
                reset = connections.get("R")
                reset_is_connected = (
                    isinstance(reset, list)
                    and len(reset) == 1
                    and ((type(reset[0]) is int and reset[0] >= 0) or reset[0] in ("0", "1"))
                )
                if (
                    connections.get("S") != ["0"]
                    or not reset_is_connected
                    or directions.get("S") != "input"
                    or directions.get("R") != "input"
                    or not init_is_zero
                    or parameters.get("SRTYPE") != "ASYNC"
                    or parameters.get("DDR_CLK_EDGE") != "SAME_EDGE"
                ):
                    raise ToolchainError(
                        f"Normalisation ODDR himbaechel refusée pour {module_name}.{cell_name} : "
                        "exiger S constant à 0, R connecté, INIT=0, SRTYPE=ASYNC "
                        "et DDR_CLK_EDGE=SAME_EDGE."
                    )
                del connections["S"]
                del directions["S"]
                normalized += 1
        try:
            with destination.open("w", encoding="utf-8") as stream:
                json.dump(data, stream, separators=(",", ":"))
                stream.write("\n")
        except OSError as exc:
            raise ToolchainError(
                f"Impossible d'écrire la copie du netlist : {destination}"
            ) from exc
        return normalized

    @staticmethod
    def _verify_himbaechel_oddr_reset(source: Path, routed: Path) -> None:
        """Confirm that all three packed output SR pins still use reset.

        nextpnr renumbers JSON bit identifiers. Match the named reset network
        independently in each netlist instead of comparing bit IDs across tools.
        """
        try:
            before = json.loads(source.read_text(encoding="utf-8"))["modules"]["arty_top"]
            after = json.loads(routed.read_text(encoding="utf-8"))["modules"]["top"]
            before_reset = before["netnames"]["reset"]["bits"]
            after_reset = after["netnames"]["reset"]["bits"]
            expected = {"data_ddr", "clock_ddr", "latch_ddr"}
            original_cells = {
                name: cell for name, cell in before["cells"].items() if cell["type"] == "ODDR"
            }
            routed_cells = {
                name: cell
                for name, cell in after["cells"].items()
                if cell.get("attributes", {}).get("X_ORIG_TYPE") == "ODDR"
            }
            reset_bits_valid = all(
                isinstance(bits, list) and len(bits) == 1 and type(bits[0]) is int and bits[0] >= 0
                for bits in (before_reset, after_reset)
            )
            valid = (
                reset_bits_valid
                and set(original_cells) == expected
                and set(routed_cells) == expected
                and all(
                    original_cells[name]["connections"]["R"] == before_reset
                    and routed_cells[name]["type"] == "OLOGICE3_OUTFF"
                    and routed_cells[name]["attributes"].get("X_ORIG_PORT_SR") == "R"
                    and routed_cells[name]["connections"]["SR"] == after_reset
                    and all(
                        routed_cells[name]["parameters"][parameter]
                        == original_cells[name]["parameters"][parameter]
                        for parameter in ("INIT", "SRTYPE", "DDR_CLK_EDGE")
                    )
                    for name in expected
                )
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ToolchainError(
                f"Vérification du reset ODDR impossible dans le netlist routé : {routed}"
            ) from exc
        if not valid:
            raise ToolchainError(
                "Reset ODDR non confirmé : DATA/CLK/LATCH doivent conserver leur "
                "reset R sur le port physique SR relié au réseau reset. "
                "Aucun bitstream n'est autorisé."
            )

    @staticmethod
    def _check_core_timing(report: str, required_mhz: float = 200.0) -> None:
        # nextpnr reports routed Fmax per clock. Require the synthesized BUFG
        # net named core_clock and the configured (or tighter) requirement.
        pattern = (
            r"Max frequency for clock ['\"]([^'\"]+)['\"]:\s*([0-9.]+)\s*MHz"
            r"\s*\((PASS|FAIL) at\s*([0-9.]+)\s*MHz\)"
        )
        matches = re.findall(pattern, report)
        # Placement estimates can fail before the final routed report passes.
        # Keep the last result for each clock; _run still requires exit code 0.
        final_results = {
            clock: (actual, verdict, target) for clock, actual, verdict, target in matches
        }
        core_results = [final_results["core_clock"]] if "core_clock" in final_results else []
        if (
            not core_results
            or any(verdict == "FAIL" for _, verdict, _ in final_results.values())
            or any(
                float(actual) < required_mhz
                or float(target) < required_mhz - 0.005
                or verdict != "PASS"
                for actual, verdict, target in core_results
            )
        ):
            raise ToolchainError(
                f"Timing {required_mhz:g} MHz non confirmé pour core_clock dans le rapport "
                "nextpnr. Cette version doit propager/contraindre l'horloge PLL et afficher "
                f"« Max frequency for clock 'core_clock': ... (PASS at {required_mhz:.2f} MHz) ». "
                "Aucun bitstream n'est autorisé."
            )

    @staticmethod
    def _core_fmax(report: str) -> float:
        """Final routed Fmax of core_clock (the last report line for it)."""
        values = re.findall(r"Max frequency for clock ['\"]core_clock['\"]:\s*([0-9.]+)", report)
        return float(values[-1])

    @staticmethod
    def _note(message: str, log: Log | None, journal: IO[str]) -> None:
        journal.write(message + "\n")
        journal.flush()
        if log:
            log(message)

    def _place_and_route(
        self,
        command: list[str],
        outputs: list[Path],
        firmware: FirmwareBuildConfig,
        log: Log | None,
        journal: IO[str],
    ) -> int:
        """Sweep the seeds; keep the first with the margin, else the best passing.

        nextpnr exits with an error when the routed timing fails; that case, or
        a report that does not pass, moves to the next seed. Any other failure
        stops the build. A passing route below ``timing_margin`` is saved and
        the sweep goes on; the best saved route is restored if no seed reaches
        the margin. The last timing error is reported when all seeds fail.
        ``outputs[0]`` is the FASM file; all outputs belong to the same route.
        """
        required = firmware.core_hz / 1e6
        target = required * (1 + self.config.timing_margin)
        seeds = self.config.nextpnr_seeds
        best: tuple[float, int] | None = None
        failure: ToolchainError | None = None

        def saved(path: Path) -> Path:
            return path.with_name(path.name + ".best")

        for attempt, seed in enumerate(seeds, start=1):
            # These paths are shared by the attempts. Revoke their previous
            # contents before invoking nextpnr, keeping only the separately
            # saved best route. A partial/failed tool invocation must never
            # borrow another seed's FASM, timing report or reset netlist.
            for path in outputs:
                path.unlink(missing_ok=True)
            code, report = self._execute([*command, "--seed", str(seed)], log, journal)
            timing_reported = "Max frequency for clock" in report
            if code != 0 and not timing_reported:
                raise ToolchainError(f"{command[0]} a échoué (code {code}). Voir {journal.name}.")
            if code == 0:
                # A clean exit missing any expected output is a tool failure,
                # not a timing miss and not a reason to authorize old files.
                for path in outputs:
                    self._require_output(path)
            try:
                self._check_core_timing(report, required)
            except ToolchainError as exc:
                failure = exc
                self._note(
                    f"[nextpnr] graine {seed} : timing {required:g} MHz non atteint "
                    f"({attempt}/{len(seeds)}).",
                    log,
                    journal,
                )
                continue
            if code != 0:
                raise ToolchainError(f"{command[0]} a échoué (code {code}). Voir {journal.name}.")
            fmax = self._core_fmax(report)
            if fmax >= target:
                for path in outputs:
                    saved(path).unlink(missing_ok=True)
                self._note(f"[nextpnr] graine {seed} retenue : {fmax:.2f} MHz.", log, journal)
                return seed
            if best is None or fmax > best[0]:
                best = (fmax, seed)
                for path in outputs:
                    shutil.copy2(path, saved(path))
            self._note(
                f"[nextpnr] graine {seed} : {fmax:.2f} MHz, marge inférieure à "
                f"{self.config.timing_margin:.0%} ({target:.2f} MHz) ({attempt}/{len(seeds)}).",
                log,
                journal,
            )
        if best is None:
            if failure is None:
                raise AssertionError("La liste des graines nextpnr ne peut pas être vide.")
            raise failure
        fmax, seed = best
        for path in outputs:
            os.replace(saved(path), path)
        self._note(
            f"[nextpnr] aucune graine n'atteint {target:.2f} MHz ; graine {seed} retenue, "
            f"la meilleure : {fmax:.2f} MHz (PASS at {required:.2f} MHz).",
            log,
            journal,
        )
        return seed

    def build(self, log: Log | None = None, firmware: FirmwareBuildConfig | None = None) -> Path:
        """Compile the reference firmware, or a validated custom configuration.

        A custom configuration writes its XDC into the run directory and sets
        arty_top parameters with Yosys ``chparam``; the committed sources stay
        untouched. The receipt records the configuration used.
        """
        firmware = firmware or FirmwareBuildConfig()
        with self._exclusive():
            # Revoke any former receipt before checking dependencies or invoking tools.
            self.receipt.unlink(missing_ok=True)
            unavailable = [
                result
                for result in self.doctor()
                if not result.ok and result.name != "openfpgaloader"
            ]
            if unavailable:
                raise ToolchainError(
                    "; ".join(f"{item.name} : {item.detail}" for item in unavailable)
                )
            run_dir = self.build_dir / "runs" / uuid.uuid4().hex
            run_dir.mkdir(parents=True)
            journal_path = self.build_dir / "build.log"
            source_digest = self._input_digest(firmware)
            constraints = self.constraints
            if not firmware.is_reference:
                constraints = run_dir / "arty_frame.xdc"
                constraints.write_text(firmware.xdc(), encoding="utf-8")
            netlist = run_dir / "arty_frame.json"
            fasm = run_dir / "arty_frame.fasm"
            frames = run_dir / "arty_frame.frames"
            bitstream = run_dir / "arty_frame.bit"
            with journal_path.open("w", encoding="utf-8") as journal:
                # Yosys has its own command interpreter: escape every path inside
                # its double-quoted token syntax, independently of shell escaping.
                def quote(path: Path) -> str:
                    return '"' + path.as_posix().replace('"', '\\"') + '"'

                parameters = (
                    ""
                    if firmware.is_reference
                    else "".join(
                        f"chparam -set {name} {value} arty_top; "
                        for name, value in firmware.yosys_parameters().items()
                    )
                )
                script = (
                    # Yosys 66306a8ca's interactive ABC pool waits for an echo
                    # that Readline truncates with long temporary paths. A
                    # basename selects the batch ABC invocation instead.
                    "scratchpad -set abc.exe yosys-abc; read_verilog "
                    + " ".join(quote(path) for path in self._sources())
                    + "; "
                    + parameters
                    + "synth_xilinx -family xc7 -flatten -nodram "
                    + ("-abc9 " if self.config.yosys_mapping == "abc9" else "")
                    + "-top arty_top; write_json "
                    + quote(netlist)
                )
                yosys_env = self._environment()
                yosys_executable = shutil.which(self._args("yosys")[0], path=yosys_env.get("PATH"))
                if yosys_executable:
                    # Also find ABC beside a portable absolute Yosys path.
                    yosys_env["PATH"] = (
                        str(Path(yosys_executable).parent) + os.pathsep + yosys_env.get("PATH", "")
                    )
                self._run([*self._args("yosys"), "-p", script], log, journal, env=yosys_env)
                self._require_output(netlist)
                place_route_netlist = netlist
                if self.config.nextpnr_backend == "himbaechel":
                    place_route_netlist = run_dir / "arty_frame.himbaechel.json"
                    count = self._normalize_himbaechel_oddr(netlist, place_route_netlist)
                    normalization = (
                        f"[ODDR himbaechel] {count} connexion(s) S inactive(s) retirée(s), "
                        f"R conservé ; copie {place_route_netlist}, original Yosys {netlist}."
                    )
                    journal.write(normalization + "\n")
                    journal.flush()
                    if log:
                        log(normalization)
                place_route = [
                    *self._args("nextpnr_xilinx"),
                    "--chipdb",
                    str(self.config.chipdb),
                    "--json",
                    str(place_route_netlist),
                    "--freq",
                    f"{firmware.core_hz / 1e6:g}",
                ]
                if self.config.nextpnr_backend == "himbaechel":
                    place_route.extend(
                        [
                            "--device",
                            self.config.part,
                            "-o",
                            f"xdc={constraints}",
                            "-o",
                            f"fasm={fasm}",
                            "--report",
                            str(run_dir / "timing.json"),
                            "--write",
                            str(run_dir / "routed.json"),
                        ]
                    )
                else:
                    place_route.extend(["--xdc", str(constraints), "--fasm", str(fasm)])
                outputs = [fasm]
                if self.config.nextpnr_backend == "himbaechel":
                    outputs += [run_dir / "timing.json", run_dir / "routed.json"]
                seed = self._place_and_route(place_route, outputs, firmware, log, journal)
                self._require_output(fasm)
                if self.config.nextpnr_backend == "himbaechel":
                    self._require_output(run_dir / "routed.json")
                    self._verify_himbaechel_oddr_reset(netlist, run_dir / "routed.json")
                    verification = "[ODDR himbaechel] Reset R→SR vérifié sur DATA/CLK/LATCH."
                    journal.write(verification + "\n")
                    journal.flush()
                    if log:
                        log(verification)
                self._run(
                    [
                        *self._args("fasm2frames"),
                        "--db-root",
                        str(self.config.prjxray_db),
                        "--part",
                        self.config.part,
                        str(fasm),
                    ],
                    log,
                    journal,
                    stdout_path=frames,
                )
                self._require_output(frames)
                self._run(
                    [
                        *self._args("xc7frames2bit"),
                        "--part_file",
                        str(self._part_file()),
                        "--part_name",
                        self.config.part,
                        "--frm_file",
                        str(frames),
                        "--output_file",
                        str(bitstream),
                    ],
                    log,
                    journal,
                )
                self._require_output(bitstream)
                try:
                    image = read_bitstream(bitstream)
                except BitstreamError as exc:
                    raise ToolchainError(f"Bitstream produit invalide : {exc}") from exc
                if self._input_digest(firmware) != source_digest:
                    raise ToolchainError(
                        "Les sources/configurations ont changé pendant la compilation."
                    )
                # Publish only a fully checked run, never an output left by a failed run.
                os.replace(bitstream, self.bitstream)
                receipt = {
                    "version": 1,
                    "part": self.config.part,
                    "input_sha256": source_digest,
                    "bitstream_sha256": image.sha256,
                    "timing_clock": "core_clock",
                    "timing_requirement_mhz": firmware.core_hz / 1e6,
                    "timing_scope": "nextpnr register paths; excludes physical GPIO/DDR validation",
                    "run_dir": str(run_dir),
                    "build_id": firmware.build_id,
                    "firmware_config": firmware.to_dict(),
                    "nextpnr_seed": seed,
                }
                temporary_receipt = run_dir / "successful-build.json"
                temporary_receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
                os.replace(temporary_receipt, self.receipt)
            if log:
                log(f"Bitstream validé : {self.bitstream}")
            return self.bitstream

    def validate_programming(self, bitstream: Path) -> BitstreamImage:
        """Read and validate the current build before touching hardware.

        The UI can call this before closing an active UART connection. The
        programmer repeats this check under the build lock, so a changed file
        or receipt cannot gain authorization from an earlier preflight.
        """
        path = Path(bitstream).expanduser().resolve()
        if path != self.bitstream:
            raise ToolchainError(f"Programmez le bitstream validé de ce projet : {self.bitstream}")
        try:
            receipt: Any = json.loads(self.receipt.read_text(encoding="utf-8"))
            firmware = (
                FirmwareBuildConfig.from_dict(receipt["firmware_config"])
                if isinstance(receipt, dict) and "firmware_config" in receipt
                else FirmwareBuildConfig()
            )
            image = read_bitstream(path)
            valid = (
                isinstance(receipt, dict)
                and receipt.get("version") == 1
                and receipt.get("part") == self.config.part
                and receipt.get("input_sha256") == self._input_digest(firmware)
                and receipt.get("bitstream_sha256") == image.sha256
                and receipt.get("timing_clock") == "core_clock"
                and receipt.get("timing_requirement_mhz") == firmware.core_hz / 1e6
                and (
                    receipt.get("build_id") == firmware.build_id
                    if "firmware_config" in receipt
                    else receipt.get("build_id", firmware.build_id) == firmware.build_id
                )
            )
        except (OSError, ValueError, TypeError, BitstreamError):
            valid = False
        if not valid:
            raise ToolchainError(
                "Bitstream absent, invalide, modifié ou périmé : "
                "recompilez avec succès avant de programmer."
            )
        return image

    def program(self, bitstream: Path, log: Log | None = None) -> None:
        with self._exclusive():
            image = self.validate_programming(bitstream)
            with (self.build_dir / "program.log").open("w", encoding="utf-8") as journal:
                self._run(
                    [
                        *self._args("openfpgaloader"),
                        "-b",
                        TARGET_BOARD,
                        str(image.path),
                    ],
                    log,
                    journal,
                )
            if log:
                log("Programmation SRAM terminée. Le FPGA perd cette configuration hors tension.")
