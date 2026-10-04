"""Compilation du firmware sur GitHub Actions, pour un PC sans chaîne FPGA.

L'application déclenche le workflow ``firmware.yml`` du dépôt avec une
configuration validée, suit l'exécution puis télécharge l'artefact. Le jeton
GitHub (fine-grained : Actions en lecture/écriture sur ce seul dépôt) n'est
jamais écrit sur disque ni envoyé ailleurs qu'à ``api.github.com``.
L'artefact n'est accepté que si son ``.bit`` et son manifeste correspondent
à la configuration demandée.
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bitstream import BitstreamError
from .firmware_artifact import FirmwareArtifactError, validate_firmware_artifact
from .firmware_config import FirmwareBuildConfig

API_ROOT = "https://api.github.com"
WORKFLOW = "firmware.yml"
ARTIFACT_PREFIX = "arty-a7-100t-firmware-"
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
# Fichiers de l'artefact conservés ; tout autre nom est ignoré.
ARTIFACT_FILES = (
    "arty_frame.bit",
    "firmware-manifest.json",
    "successful-build.json",
    "timing.json",
    "build.log",
    "rtl-tests.log",
    "tool-versions.json",
    "SHA256SUMS.txt",
    "CHARGEMENT-WINDOWS.txt",
)
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
Progress = Callable[[str], None]


class RemoteBuildError(RuntimeError):
    """GitHub a refusé la demande, ou l'artefact ne correspond pas à la demande."""


@dataclass(frozen=True)
class RemoteBuildTarget:
    repository: str = "Citroz31/arty-frame-studio"
    ref: str = "main"

    def __post_init__(self) -> None:
        if not isinstance(self.repository, str) or not _REPOSITORY.fullmatch(self.repository):
            raise ValueError("Dépôt GitHub attendu sous la forme propriétaire/nom.")
        if (
            not isinstance(self.ref, str)
            or not _REF.fullmatch(self.ref)
            or ".." in self.ref
            or self.ref.endswith(("/", ".lock"))
        ):
            raise ValueError("Branche GitHub invalide.")


@dataclass(frozen=True)
class RemoteBuildResult:
    bitstream: Path
    manifest: dict[str, Any]
    run_url: str


class GitHubBuildClient:
    def __init__(
        self,
        target: RemoteBuildTarget,
        token: str,
        *,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            not isinstance(token, str)
            or not token.strip()
            or any(char.isspace() for char in token.strip())
        ):
            raise ValueError("Jeton GitHub requis (fine-grained, Actions : lecture et écriture).")
        self.target = target
        self._token = token.strip()
        self._open = opener or urllib.request.urlopen
        self._clock = clock
        self._sleep = sleep

    def _request(self, method: str, path: str, body: object = None, *, raw: bool = False) -> Any:
        url = path if path.startswith("https://") else API_ROOT + path
        if not url.startswith(API_ROOT + "/"):
            # The token is only ever sent to the GitHub API host.
            raise RemoteBuildError(f"URL GitHub inattendue : {url}")
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method)
        # Unredirected: the artifact redirect points to storage, not to GitHub.
        request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "arty-frame-studio")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._open(request, timeout=60) as response:
                content = response.read(MAX_ARTIFACT_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Close the error body now: an unclosed one triggers a
            # ResourceWarning at garbage collection on recent Pythons.
            exc.close()
            hint = {
                401: " Jeton refusé ou expiré.",
                403: " Droits insuffisants : Actions en écriture sur ce dépôt.",
                404: " Dépôt, branche ou workflow introuvable pour ce jeton.",
                422: " Le workflow refuse ces paramètres (branche ou entrées).",
            }.get(exc.code, "")
            raise RemoteBuildError(f"GitHub {method} {path} : HTTP {exc.code}.{hint}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RemoteBuildError(f"GitHub inaccessible : {exc}") from exc
        if len(content) > MAX_ARTIFACT_BYTES:
            raise RemoteBuildError("Réponse GitHub trop volumineuse.")
        if raw:
            return content
        if not content:
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise RemoteBuildError("Réponse GitHub illisible.") from exc

    def _repo_path(self, suffix: str) -> str:
        return f"/repos/{self.target.repository}{suffix}"

    def dispatch(self, firmware: FirmwareBuildConfig) -> str:
        """Déclenche le workflow ; renvoie l'identifiant qui nomme l'exécution."""
        request_id = uuid.uuid4().hex[:16]
        self._request(
            "POST",
            self._repo_path(f"/actions/workflows/{WORKFLOW}/dispatches"),
            {
                "ref": self.target.ref,
                "inputs": {
                    "firmware_config": (
                        ""
                        if firmware.is_reference
                        else json.dumps(firmware.to_dict(), sort_keys=True, separators=(",", ":"))
                    ),
                    "request_id": request_id,
                },
            },
        )
        return request_id

    def find_run(self, request_id: str, *, timeout: float = 180, poll: float = 5) -> dict[str, Any]:
        deadline = self._clock() + timeout
        path = self._repo_path(
            f"/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=30"
        )
        while True:
            runs = (self._request("GET", path) or {}).get("workflow_runs", [])
            for run in runs:
                if isinstance(run, dict) and request_id in str(run.get("display_title", "")):
                    return run
            if self._clock() >= deadline:
                raise RemoteBuildError(
                    "L'exécution GitHub Actions n'apparaît pas. Vérifier que firmware.yml "
                    "de la branche choisie accepte les entrées de l'application."
                )
            self._sleep(poll)

    def wait(
        self,
        run_id: int,
        *,
        timeout: float = 7200,
        poll: float = 15,
        progress: Progress | None = None,
    ) -> dict[str, Any]:
        deadline = self._clock() + timeout
        last = ""
        while True:
            run = self._request("GET", self._repo_path(f"/actions/runs/{int(run_id)}")) or {}
            status = str(run.get("status", ""))
            if status != last and progress:
                progress(f"GitHub Actions : {status or 'inconnu'}")
            last = status
            if status == "completed":
                if run.get("conclusion") != "success":
                    raise RemoteBuildError(
                        f"Compilation GitHub en échec ({run.get('conclusion')}). "
                        f"Journal : {run.get('html_url', '')}"
                    )
                return run
            if self._clock() >= deadline:
                raise RemoteBuildError(
                    f"Compilation GitHub trop longue ; suivre {run.get('html_url', '')}"
                )
            self._sleep(poll)

    def download(
        self, run: dict[str, Any], request_id: str, firmware: FirmwareBuildConfig, directory: Path
    ) -> RemoteBuildResult:
        listing = (
            self._request("GET", self._repo_path(f"/actions/runs/{int(run['id'])}/artifacts")) or {}
        )
        name = ARTIFACT_PREFIX + request_id
        artifact = next(
            (
                item
                for item in listing.get("artifacts", [])
                if isinstance(item, dict) and item.get("name") == name
            ),
            None,
        )
        if artifact is None or artifact.get("expired"):
            raise RemoteBuildError(f"Artefact {name} absent ou expiré.")
        if int(artifact.get("size_in_bytes", 0)) > MAX_ARTIFACT_BYTES:
            raise RemoteBuildError("Artefact trop volumineux.")
        head_sha = run.get("head_sha")
        if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise RemoteBuildError("L'exécution GitHub n'identifie pas son commit source.")
        archive = self._request("GET", str(artifact["archive_download_url"]), raw=True)
        output = Path(directory) / f"firmware-{firmware.build_id:08x}-{request_id}"
        Path(directory).mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise RemoteBuildError(
                "Le dossier de cette compilation existe déjà ; il n'est pas écrasé."
            )
        # Keep an invalid/partial download out of the selectable build folders.
        with tempfile.TemporaryDirectory(prefix=".firmware-download-", dir=directory) as temporary:
            staging = Path(temporary)
            extract_firmware_archive(archive, staging)
            manifest = check_downloaded_firmware(staging, firmware, expected_commit=head_sha)
            staging.rename(output)
        return RemoteBuildResult(output / "arty_frame.bit", manifest, str(run.get("html_url", "")))

    def build(
        self, firmware: FirmwareBuildConfig, directory: Path, progress: Progress | None = None
    ) -> RemoteBuildResult:
        report = progress or (lambda message: None)
        request_id = self.dispatch(firmware)
        report(f"Compilation demandée à GitHub Actions ({firmware.summary()}).")
        run = self.find_run(request_id)
        report(f"Exécution GitHub : {run.get('html_url', '')}")
        run = self.wait(int(run["id"]), progress=report)
        report("Compilation réussie ; téléchargement du firmware.")
        return self.download(run, request_id, firmware, directory)


def extract_firmware_archive(archive: bytes, output: Path) -> None:
    """Extrait les seuls fichiers attendus, à plat, sans suivre de chemin."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            members = {}
            total = 0
            for info in bundle.infolist():
                if info.filename not in ARTIFACT_FILES or info.is_dir():
                    continue
                if info.filename in members:
                    raise RemoteBuildError(f"Fichier dupliqué dans l'artefact : {info.filename}.")
                total += info.file_size
                members[info.filename] = info
            if total > MAX_ARTIFACT_BYTES:
                raise RemoteBuildError("L'ensemble des fichiers de l'artefact est trop volumineux.")
            for name in ARTIFACT_FILES:
                member = members.get(name)
                if (
                    member is None
                    or member.is_dir()
                    or Path(member.filename).name != member.filename
                ):
                    continue
                if member.file_size > MAX_ARTIFACT_BYTES:
                    raise RemoteBuildError(f"{name} trop volumineux dans l'artefact.")
                (output / name).write_bytes(bundle.read(member))
    except zipfile.BadZipFile as exc:
        raise RemoteBuildError("Artefact GitHub illisible (ZIP invalide).") from exc


def check_downloaded_firmware(
    directory: Path, firmware: FirmwareBuildConfig, *, expected_commit: str | None = None
) -> dict[str, Any]:
    """Le .bit, son manifeste et la configuration demandée doivent concorder."""
    try:
        return validate_firmware_artifact(directory, firmware, expected_commit=expected_commit)
    except (FirmwareArtifactError, BitstreamError, OSError) as exc:
        raise RemoteBuildError(str(exc)) from exc
