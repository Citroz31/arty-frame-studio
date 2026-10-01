"""Local Yosys/nextpnr-xilinx/Project X-Ray builds, with SRAM programming.

No command is interpreted by a shell. Only a successful build of the current
sources/configuration creates a programming receipt; failed rebuilds revoke it.
"""

from __future__ import annotations

import hashlib
import json
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
from typing import IO, Any

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
    nextpnr_xilinx: Command = "nextpnr-xilinx"
    fasm2frames: Command = "fasm2frames"
    xc7frames2bit: Command = "xc7frames2bit"
    openfpgaloader: Command = "openFPGALoader"
    chipdb: Path | None = None
    prjxray_db: Path | None = None
    part: str = TARGET_PART
    build_dir: Path = Path("build")

    def __post_init__(self) -> None:
        if self.part != TARGET_PART:
            raise ValueError(f"Cette application exige le composant {TARGET_PART}.")
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
        for name in ("chipdb", "prjxray_db", "build_dir"):
            value = getattr(self, name)
            if name == "build_dir" and value is None:
                raise ValueError("build_dir doit être un chemin non vide.")
            if not isinstance(value, Path) and (value is not None or name == "build_dir"):
                raise ValueError(f"{name} doit être un pathlib.Path.")

    @classmethod
    def from_json(cls, path: Path | str) -> ToolchainConfig:
        """Resolve data paths relative to the configuration file's directory."""
        path = Path(path).expanduser().resolve()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
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
                data[name] = tuple(value)
        for name in ("chipdb", "prjxray_db", "build_dir"):
            if name in data and data[name] is not None:
                if not isinstance(data[name], str) or not data[name].strip():
                    raise ValueError(f"{name} doit être un chemin non vide.")
                candidate = Path(data[name]).expanduser()
                data[name] = (
                    candidate if candidate.is_absolute() else path.parent / candidate
                ).resolve()
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

    def _sources(self) -> list[Path]:
        return sorted((self.project_root / "firmware/rtl").glob("*.v"))

    def _part_file(self) -> Path | None:
        if self.config.prjxray_db is None:
            return None
        return self.config.prjxray_db / self.config.part / "part.yaml"

    def doctor(self) -> list[DoctorResult]:
        """Check local files/executables. Does not claim FPGA or timing validation."""
        results = []
        for name in _TOOLS:
            executable = self._args(name)[0]
            found = shutil.which(executable)
            results.append(DoctorResult(name, found is not None, found or f"Absent : {executable}"))
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

    def _input_digest(self) -> str:
        digest = hashlib.sha256()
        settings = asdict(self.config)
        digest.update(json.dumps(settings, sort_keys=True, default=str).encode())
        for path in [*self._sources(), self.constraints]:
            digest.update(str(path.relative_to(self.project_root)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _run(
        self,
        args: list[str],
        log: Log | None,
        journal: IO[str],
        *,
        stdout_path: Path | None = None,
    ) -> str:
        command_line = "$ " + shlex.join(args)
        journal.write(command_line + "\n")
        journal.flush()
        if log:
            log(command_line)
        try:
            if stdout_path is None:
                process = subprocess.Popen(
                    args,
                    cwd=self.project_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
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
        if code != 0:
            raise ToolchainError(f"{args[0]} a échoué (code {code}). Voir {journal.name}.")
        return "".join(captured)

    @staticmethod
    def _require_output(path: Path) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise ToolchainError(f"L'outil n'a pas produit de sortie non vide : {path}")

    @staticmethod
    def _check_core_timing(report: str) -> None:
        # nextpnr reports routed Fmax per clock. Require the synthesized BUFG
        # net named core_clock and a 200 MHz (or tighter) requirement explicitly.
        pattern = (
            r"Max frequency for clock ['\"]([^'\"]+)['\"]:\s*([0-9.]+)\s*MHz"
            r"\s*\((PASS|FAIL) at\s*([0-9.]+)\s*MHz\)"
        )
        matches = re.findall(pattern, report)
        core_results = [
            (actual, verdict, target)
            for clock, actual, verdict, target in matches
            if clock == "core_clock"
        ]
        if (
            not core_results
            or any(verdict == "FAIL" for _, _, verdict, _ in matches)
            or any(
                float(actual) < 200 or float(target) < 200 or verdict != "PASS"
                for actual, verdict, target in core_results
            )
        ):
            raise ToolchainError(
                "Timing 200 MHz non confirmé pour core_clock dans le rapport nextpnr. "
                "Cette version doit propager/contraindre l'horloge PLL et afficher "
                "« Max frequency for clock 'core_clock': ... (PASS at 200.00 MHz) ». "
                "Aucun bitstream n'est autorisé."
            )

    def build(self, log: Log | None = None) -> Path:
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
            source_digest = self._input_digest()
            netlist = run_dir / "arty_frame.json"
            fasm = run_dir / "arty_frame.fasm"
            frames = run_dir / "arty_frame.frames"
            bitstream = run_dir / "arty_frame.bit"
            with journal_path.open("w", encoding="utf-8") as journal:
                # Yosys has its own command interpreter: escape every path inside
                # its double-quoted token syntax, independently of shell escaping.
                def quote(path: Path) -> str:
                    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'

                script = (
                    "read_verilog "
                    + " ".join(quote(path) for path in self._sources())
                    + "; synth_xilinx -family xc7 -flatten -nodram -top arty_top; write_json "
                    + quote(netlist)
                )
                self._run([*self._args("yosys"), "-p", script], log, journal)
                self._require_output(netlist)
                report = self._run(
                    [
                        *self._args("nextpnr_xilinx"),
                        "--chipdb",
                        str(self.config.chipdb),
                        "--xdc",
                        str(self.constraints),
                        "--json",
                        str(netlist),
                        "--fasm",
                        str(fasm),
                        "--freq",
                        "200",
                    ],
                    log,
                    journal,
                )
                self._require_output(fasm)
                self._check_core_timing(report)
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
                if self._input_digest() != source_digest:
                    raise ToolchainError(
                        "Les sources/configurations ont changé pendant la compilation."
                    )
                # Publish only a fully checked run, never an output left by a failed run.
                os.replace(bitstream, self.bitstream)
                receipt = {
                    "version": 1,
                    "part": self.config.part,
                    "input_sha256": source_digest,
                    "bitstream_sha256": self._file_digest(self.bitstream),
                    "timing_clock": "core_clock",
                    "timing_requirement_mhz": 200,
                    "timing_scope": "nextpnr register paths; excludes physical GPIO/DDR validation",
                    "run_dir": str(run_dir),
                }
                temporary_receipt = run_dir / "successful-build.json"
                temporary_receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
                os.replace(temporary_receipt, self.receipt)
            if log:
                log(f"Bitstream validé : {self.bitstream}")
            return self.bitstream

    def program(self, bitstream: Path, log: Log | None = None) -> None:
        with self._exclusive():
            path = Path(bitstream).expanduser().resolve()
            if path != self.bitstream:
                raise ToolchainError(
                    f"Programmez le bitstream validé de ce projet : {self.bitstream}"
                )
            try:
                receipt: Any = json.loads(self.receipt.read_text(encoding="utf-8"))
                valid = (
                    isinstance(receipt, dict)
                    and receipt.get("version") == 1
                    and receipt.get("part") == self.config.part
                    and receipt.get("input_sha256") == self._input_digest()
                    and receipt.get("bitstream_sha256") == self._file_digest(path)
                    and receipt.get("timing_requirement_mhz") == 200
                )
            except (OSError, ValueError):
                valid = False
            if not valid:
                raise ToolchainError(
                    "Bitstream absent, modifié ou périmé : "
                    "recompilez avec succès avant de programmer."
                )
            with (self.build_dir / "program.log").open("w", encoding="utf-8") as journal:
                self._run(
                    [
                        *self._args("openfpgaloader"),
                        "-b",
                        TARGET_BOARD,
                        str(path),
                    ],
                    log,
                    journal,
                )
            if log:
                log("Programmation SRAM terminée. Le FPGA perd cette configuration hors tension.")
