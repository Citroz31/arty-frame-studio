"""Pinned, portable FPGA tools for a Windows user account.

Installation is an explicit operation. Importing this module, checking the tools
or compiling with an existing configuration never starts a download.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .toolchain import TARGET_PART, ToolchainConfig, ToolchainError

Log = Callable[[str], None]
TOOLS_VERSION = "openxc7-20260930-oss-20260324"


@dataclass(frozen=True)
class ToolArchive:
    filename: str
    url: str
    sha256: str
    size: int


# Digests are the publisher's GitHub release asset digests, also published in
# openXC7's SHA256SUMS. Do not discover/accept a new digest during installation.
OSS_CAD_SUITE = ToolArchive(
    "oss-cad-suite-windows-x64-20260324.exe",
    "https://github.com/YosysHQ/oss-cad-suite-build/releases/download/2026-03-24/"
    "oss-cad-suite-windows-x64-20260324.exe",
    "111238a7e52892c561f23bb1e19c197f750a09688aa12787ead7a407e0750476",
    330628408,
)
OPENXC7 = ToolArchive(
    "openxc7-toolchain-windows-amd64-20260930.tgz",
    "https://github.com/cavearr/toolchain-openxc7-releases/releases/download/2026-09-30/"
    "openxc7-toolchain-windows-amd64-20260930.tgz",
    "4592f26732360d78124f933fb7e524545cf00954f0c24da26ec36b84a170fac6",
    116026078,
)
ARCHIVES = (OSS_CAD_SUITE, OPENXC7)


def default_tools_dir(project_root: Path | str) -> Path:
    """Keep large portable tools outside a project's OneDrive directory."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local).expanduser().resolve() / "ArtyFrameStudio" / "fpga-tools"
    return Path(project_root).expanduser().resolve() / "build" / ".tools"


def _log(log: Log | None, message: str) -> None:
    if log is not None:
        log(message)


def _check_windows() -> None:
    if (
        platform.system() != "Windows"
        or platform.machine().lower() not in ("amd64", "x86_64")
        or struct.calcsize("P") != 8
        or sys.version_info < (3, 11)
    ):
        raise ToolchainError(
            "L'installation portable exige Windows x64 et Python 64 bits ≥ 3.11. "
            "Sous Linux, utiliser scripts/bootstrap-fpga-tools.sh."
        )


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _download_verified(archive: ToolArchive, cache: Path, log: Log | None) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / archive.filename
    if (
        target.is_file()
        and target.stat().st_size == archive.size
        and _sha256(target) == archive.sha256
    ):
        _log(log, f"Archive déjà vérifiée : {archive.filename}")
        return target
    temporary = cache / (archive.filename + ".download")
    try:
        request = urllib.request.Request(
            archive.url, headers={"User-Agent": "Arty-Frame-Studio/0.1"}
        )
        _log(log, f"Téléchargement : {archive.filename} ({archive.size / 1e6:.0f} Mo)")
        digest = hashlib.sha256()
        total = 0
        next_progress = 0
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=45) as response, temporary.open("wb") as out:
            if urllib.parse.urlparse(response.url).scheme != "https":
                raise ToolchainError("Le téléchargement doit rester en HTTPS.")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > archive.size or time.monotonic() - started > 900:
                    raise ToolchainError("Téléchargement trop volumineux ou délai dépassé.")
                digest.update(chunk)
                out.write(chunk)
                progress = total * 100 // archive.size
                if progress >= next_progress:
                    _log(log, f"{archive.filename} : {progress} %")
                    next_progress = progress + 10
        if total != archive.size or digest.hexdigest() != archive.sha256:
            raise ToolchainError(
                f"SHA256/taille incorrects pour {archive.filename}. "
                "L'archive a été rejetée ; aucun outil téléchargé n'a été exécuté."
            )
        os.replace(temporary, target)
        _log(log, f"SHA256 vérifié : {archive.filename}")
        return target
    except (OSError, urllib.error.URLError) as exc:
        raise ToolchainError(
            f"Téléchargement impossible : {archive.filename} : {exc}. "
            "Vérifier Internet puis relancer l'installation."
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _safe_member(name: str, destination: Path) -> Path:
    """Reject paths interpreted differently by tar/POSIX and Windows."""
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or "\\" in name
        or "\0" in name
        or any(
            part == ".."
            or ":" in part
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
            for part in posix.parts
        )
    ):
        raise ToolchainError(f"Chemin interdit dans l'archive : {name}")
    target = destination.joinpath(*posix.parts).resolve()
    if not target.is_relative_to(destination.resolve()):
        raise ToolchainError(f"Chemin hors du dossier des outils : {name}")
    return target


def _extract_tar(archive: Path, destination: Path, log: Log | None = None) -> None:
    """Extract only files/directories; never create links or special files."""
    _log(log, "Extraction openXC7 (base Artix-7 et outils natifs)…")
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as package:
            members = package.getmembers()
            # Validate the entire archive before writing its first member.
            for member in members:
                _safe_member(member.name, destination)
                if not member.isfile() and not member.isdir():
                    raise ToolchainError(f"Lien/fichier spécial interdit : {member.name}")
            if sum(member.size for member in members) > 4 * 1024**3:
                raise ToolchainError("L'archive décompressée dépasse 4 Go.")
            for member in members:
                target = _safe_member(member.name, destination)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = package.extractfile(member)
                    if source is None:
                        raise ToolchainError(f"Fichier absent de l'archive : {member.name}")
                    with source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
    except (OSError, tarfile.TarError) as exc:
        raise ToolchainError(f"Extraction openXC7 impossible : {exc}") from exc


def _extract_suite(archive: Path, destination: Path, log: Log | None) -> None:
    # The SHA256-checked publisher asset is a portable 7-Zip self-extractor,
    # not a system installer. Its destination is a private staging directory.
    _log(log, "Extraction OSS CAD Suite (sans installation système)…")
    try:
        result = subprocess.run(
            [str(archive), "-y", f"-o{destination}"],
            cwd=destination,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolchainError(f"Extraction OSS CAD Suite impossible : {exc}") from exc
    if result.returncode:
        raise ToolchainError(
            f"Extraction OSS CAD Suite échouée ({result.returncode}) : {result.stdout[-2000:]}"
        )


def _settings(installation: Path, project: Path) -> dict[str, Any]:
    suite = installation / "oss-cad-suite"
    xc7 = installation / "openxc7"
    project_id = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:12]
    build = installation.parent.parent / "builds" / project_id
    return {
        "yosys": str(suite / "bin/yosys.exe"),
        "yosys_mapping": "abc9",
        "nextpnr_xilinx": str(xc7 / "bin/nextpnr-xilinx.exe"),
        "nextpnr_backend": "himbaechel",
        "fasm2frames": [str(suite / "lib/python3.exe"), str(xc7 / "libexec/fasm2frames")],
        "xc7frames2bit": str(xc7 / "bin/xc7frames2bit.exe"),
        "openfpgaloader": str(suite / "bin/openFPGALoader.exe"),
        "tool_dirs": [str(suite / "bin"), str(suite / "lib"), str(xc7 / "bin")],
        "python_path": [str(xc7 / "lib/python3.12/site-packages")],
        "chipdb": str(xc7 / "chipdb/chipdb-xc7a100t.bin"),
        "prjxray_db": str(xc7 / "share/nextpnr/external/prjxray-db/artix7"),
        "part": TARGET_PART,
        "build_dir": str(build),
        "nextpnr_seeds": [8],
        "timing_margin": 0.03,
    }


def _required_files(installation: Path) -> list[Path]:
    suite = installation / "oss-cad-suite"
    xc7 = installation / "openxc7"
    return [
        suite / "bin/yosys.exe",
        suite / "lib/python3.exe",
        suite / "lib/libpython3.11.dll",
        suite / "bin/openFPGALoader.exe",
        xc7 / "bin/nextpnr-xilinx.exe",
        xc7 / "bin/xc7frames2bit.exe",
        xc7 / "libexec/fasm2frames",
        xc7 / "lib/python3.12/site-packages/fasm/__init__.py",
        xc7 / "lib/python3.12/site-packages/prjxray/__init__.py",
        xc7 / "lib/python3.12/site-packages/yaml/__init__.py",
        xc7 / "lib/python3.12/site-packages/textx/__init__.py",
        xc7 / "lib/python3.12/site-packages/arpeggio/__init__.py",
        xc7 / "chipdb/chipdb-xc7a100t.bin",
        xc7 / f"share/nextpnr/external/prjxray-db/artix7/{TARGET_PART}/part.yaml",
    ]


def _validate_files(installation: Path) -> None:
    missing = [str(path) for path in _required_files(installation) if not path.is_file()]
    if missing:
        raise ToolchainError("Installation incomplète : " + ", ".join(missing))
    info_file = installation / "openxc7/BUILD-INFO.json"
    try:
        info = json.loads(info_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolchainError(f"Métadonnées openXC7 illisibles : {info_file}") from exc
    if (
        not isinstance(info, dict)
        or info.get("release-tag") != "2026-09-30"
        or info.get("yosys-release-tag") != "2026-03-24"
        or info.get("target-platform") != "windows-amd64"
    ):
        raise ToolchainError("Versions/plateforme openXC7 incompatibles avec cet installateur.")


def _validate_runtime(installation: Path, log: Log | None) -> None:
    suite = installation / "oss-cad-suite"
    xc7 = installation / "openxc7"
    env = os.environ.copy()
    # Match environment.bat from the pinned OSS bundle: the Windows build
    # stores Python and its shared libraries in lib rather than bin.
    env["PATH"] = os.pathsep.join(
        [str(suite / "bin"), str(suite / "lib"), str(xc7 / "bin"), env.get("PATH", "")]
    )
    env["PYTHONPATH"] = str(xc7 / "lib/python3.12/site-packages")
    # A user's Python settings must not point the suite's interpreter at the
    # application's virtual environment or a separately installed Python.
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    for args in (
        [str(suite / "bin/yosys.exe"), "-V"],
        [str(xc7 / "bin/nextpnr-xilinx.exe"), "--version"],
        [
            str(suite / "lib/python3.exe"),
            "-c",
            "import fasm, prjxray, yaml; "
            "assert list(fasm.parse_fasm_string('CLBLL_L_X1Y1.SLICEL_X0.ALUT.INIT[0] = 1')); "
            "print('Python/FASM/Project X-Ray disponibles')",
        ],
        [str(suite / "lib/python3.exe"), str(xc7 / "libexec/fasm2frames"), "--help"],
    ):
        try:
            result = subprocess.run(
                args,
                cwd=installation,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToolchainError(f"Outil Windows impossible à démarrer : {exc}") from exc
        if result.returncode:
            raise ToolchainError(f"Vérification des outils échouée : {result.stdout[-2000:]}")
        _log(log, result.stdout.strip())


@contextmanager
def _installation_lock(tools: Path) -> Iterator[None]:
    lock = tools / ".install.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ToolchainError(
            f"Une installation est déjà active ({lock}). Après un arrêt brutal, "
            "supprimer ce verrou uniquement si aucune installation ne tourne."
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def ensure_local_toolchain(
    project_root: Path | str,
    log: Log | None = None,
    *,
    tools_dir: Path | str | None = None,
    config_path: Path | str | None = None,
) -> ToolchainConfig:
    """Install/reuse pinned native tools and atomically publish toolchain.json.

    No administrator rights, PATH registry edits, drivers, WSL or GitHub token
    are used. Network is needed only for archives absent from the verified cache.
    """
    _check_windows()
    project = Path(project_root).expanduser().resolve()
    tools = Path(tools_dir).expanduser().resolve() if tools_dir else default_tools_dir(project)
    config_file = (
        Path(config_path).expanduser().resolve() if config_path else project / "toolchain.json"
    )
    tools.mkdir(parents=True, exist_ok=True)
    installation = tools / TOOLS_VERSION
    with _installation_lock(tools):
        if (installation / "arty-tools.json").is_file():
            _validate_files(installation)
            _validate_runtime(installation, log)
            _log(log, f"Outils portables existants : {installation}")
        else:
            if installation.exists():
                raise ToolchainError(
                    f"Installation incomplète : {installation}. "
                    "Déplacer ce dossier puis relancer l'installation."
                )
            if shutil.disk_usage(tools).free < 4 * 1024**3:
                raise ToolchainError("Prévoir au moins 4 Go libres pour installer les outils FPGA.")
            downloads = [
                _download_verified(archive, tools / "downloads", log) for archive in ARCHIVES
            ]
            with tempfile.TemporaryDirectory(prefix=".install-", dir=tools) as staging:
                stage = Path(staging)
                _extract_suite(downloads[0], stage, log)
                _extract_tar(downloads[1], stage / "openxc7", log)
                _validate_files(stage)
                _validate_runtime(stage, log)
                manifest = {
                    "version": TOOLS_VERSION,
                    "archives": [
                        {"url": archive.url, "sha256": archive.sha256} for archive in ARCHIVES
                    ],
                }
                (stage / "arty-tools.json").write_text(
                    json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
                )
                # Rename on the same volume for atomic publication; keep a
                # separate name available for cleanup if publication fails.
                published_stage = tools / (stage.name + "-ready")
                try:
                    stage.rename(published_stage)
                    published_stage.rename(installation)
                finally:
                    if published_stage.exists():
                        shutil.rmtree(published_stage)
        settings = _settings(installation, project)
        config_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".toolchain-", dir=config_file.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(settings, indent=2) + "\n")
            # Validate before replacing an existing user configuration.
            config = ToolchainConfig.from_json(temporary)
            os.replace(temporary, config_file)
        finally:
            temporary.unlink(missing_ok=True)
        _log(log, f"Chaîne Windows locale prête : {config_file}")
        return config
