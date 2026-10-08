from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from arty_frame_studio import local_tools
from arty_frame_studio.toolchain import TARGET_PART, ToolchainError


def archive_spec(data: bytes = b"verified archive") -> local_tools.ToolArchive:
    return local_tools.ToolArchive(
        "fixture.tgz",
        "https://example.invalid/fixture.tgz",
        hashlib.sha256(data).hexdigest(),
        len(data),
    )


class Response(io.BytesIO):
    url = "https://example.invalid/fixture.tgz"


def populate_installation(path: Path) -> None:
    for required in local_tools._required_files(path):
        required.parent.mkdir(parents=True, exist_ok=True)
        required.write_bytes(b"portable tool fixture")
    (path / "openxc7/BUILD-INFO.json").write_text(
        json.dumps(
            {
                "release-tag": "2026-09-30",
                "yosys-release-tag": "2026-03-24",
                "target-platform": "windows-amd64",
            }
        )
    )


def mock_installation(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    actions: list[str] = []
    monkeypatch.setattr(local_tools, "_check_windows", lambda: None)
    monkeypatch.setattr(
        local_tools.shutil, "disk_usage", lambda _: SimpleNamespace(free=5 * 1024**3)
    )

    def download(spec: local_tools.ToolArchive, cache: Path, log: object) -> Path:
        actions.append("download:" + spec.filename)
        return cache / spec.filename

    def extract_suite(archive: Path, destination: Path, log: object) -> None:
        actions.append("extract suite")
        populate_installation(destination)

    monkeypatch.setattr(local_tools, "_download_verified", download)
    monkeypatch.setattr(local_tools, "_extract_suite", extract_suite)
    monkeypatch.setattr(local_tools, "_extract_tar", lambda *_: actions.append("extract xc7"))
    monkeypatch.setattr(local_tools, "_validate_runtime", lambda *_: actions.append("runtime"))
    return actions


def test_download_checks_hash_and_reuses_verified_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"verified archive"
    calls = []

    def download(request: object, timeout: int) -> Response:
        calls.append(request)
        return Response(data)

    monkeypatch.setattr(local_tools.urllib.request, "urlopen", download)
    spec = archive_spec(data)
    target = local_tools._download_verified(spec, tmp_path, None)
    assert target.read_bytes() == data
    assert local_tools._download_verified(spec, tmp_path, None) == target
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.download"))


@pytest.mark.parametrize("data", [b"corrupt archive!", b"short", b"overlarge archive payload"])
def test_bad_download_is_rejected_and_partial_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data: bytes,
) -> None:
    spec = archive_spec()
    monkeypatch.setattr(local_tools.urllib.request, "urlopen", lambda *_a, **_k: Response(data))
    with pytest.raises(ToolchainError):
        local_tools._download_verified(spec, tmp_path, None)
    assert not (tmp_path / spec.filename).exists()
    assert not list(tmp_path.glob("*.download"))


def test_failed_download_keeps_old_cache_and_reports_network_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = archive_spec()
    old = tmp_path / spec.filename
    old.write_bytes(b"invalid cached file")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(local_tools.urllib.request, "urlopen", fail)
    with pytest.raises(ToolchainError, match="Téléchargement impossible"):
        local_tools._download_verified(spec, tmp_path, None)
    assert old.read_bytes() == b"invalid cached file"


def test_http_redirect_is_rejected_before_writing_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = Response(b"verified archive")
    response.url = "http://example.invalid/fixture.tgz"
    monkeypatch.setattr(local_tools.urllib.request, "urlopen", lambda *_a, **_k: response)
    with pytest.raises(ToolchainError, match="HTTPS"):
        local_tools._download_verified(archive_spec(), tmp_path, None)
    assert not list(tmp_path.glob("*.download"))


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/escape",
        "C:/escape",
        "C:escape",
        "safe/../../escape",
        "safe\\escape",
        "safe/stream:payload",
        "safe/NUL.txt",
        "safe/path. ",
    ],
)
def test_windows_archive_paths_cannot_escape_or_change_meaning(tmp_path: Path, name: str) -> None:
    with pytest.raises(ToolchainError, match="Chemin"):
        local_tools._safe_member(name, tmp_path)


def make_tar(path: Path, members: list[tarfile.TarInfo]) -> None:
    with tarfile.open(path, "w:gz") as package:
        for member in members:
            package.addfile(member, io.BytesIO(b"x" * member.size) if member.isfile() else None)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_tar_validates_every_member_before_extracting(
    tmp_path: Path,
    kind: bytes,
) -> None:
    good = tarfile.TarInfo("./bin/nextpnr.exe")
    good.size = 1
    malicious = tarfile.TarInfo("./link")
    malicious.type = kind
    malicious.linkname = "../escape"
    archive = tmp_path / "tools.tgz"
    make_tar(archive, [good, malicious])
    destination = tmp_path / "extract"
    with pytest.raises(ToolchainError, match="Lien/fichier spécial"):
        local_tools._extract_tar(archive, destination)
    assert not (destination / "bin/nextpnr.exe").exists()


def test_tar_extracts_normal_root_dot_layout(tmp_path: Path) -> None:
    directory = tarfile.TarInfo("./bin")
    directory.type = tarfile.DIRTYPE
    executable = tarfile.TarInfo("./bin/nextpnr-xilinx.exe")
    executable.size = 4
    archive = tmp_path / "tools.tgz"
    make_tar(archive, [directory, executable])
    destination = tmp_path / "extract"
    local_tools._extract_tar(archive, destination)
    assert (destination / "bin/nextpnr-xilinx.exe").read_bytes() == b"xxxx"


def test_non_windows_fails_before_any_files_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_tools.platform, "system", lambda: "Linux")
    with pytest.raises(ToolchainError, match="Windows x64"):
        local_tools.ensure_local_toolchain(tmp_path, tools_dir=tmp_path / "tools")
    assert not list(tmp_path.iterdir())


def test_install_publishes_only_validated_tools_and_resolved_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions = mock_installation(monkeypatch)
    project = tmp_path / "project with spaces"
    project.mkdir()
    config = local_tools.ensure_local_toolchain(project, tools_dir=tmp_path / "tools")
    settings = json.loads((project / "toolchain.json").read_text())
    installation = tmp_path / "tools" / local_tools.TOOLS_VERSION
    assert config.chipdb == installation / "openxc7/chipdb/chipdb-xc7a100t.bin"
    assert config.part == TARGET_PART
    assert config.nextpnr_backend == "himbaechel"
    assert config.yosys_mapping == "abc9"
    assert config.nextpnr_seeds == tuple(range(1, 17))
    assert "nextpnr_seeds" not in settings
    assert config.build_dir.parent == tmp_path / "builds"
    assert len(config.build_dir.name) == 12
    assert not config.build_dir.is_relative_to(project)
    assert all(path.is_absolute() for path in config.tool_dirs + config.python_path)
    assert settings["fasm2frames"][1] == str(installation / "openxc7/libexec/fasm2frames")
    assert (installation / "arty-tools.json").is_file()
    assert actions[-1] == "runtime"
    assert not list((tmp_path / "tools").glob(".install*"))


def test_reuse_performs_runtime_check_without_redownload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions = mock_installation(monkeypatch)
    tools = tmp_path / "tools"
    local_tools.ensure_local_toolchain(tmp_path, tools_dir=tools)
    actions.clear()
    local_tools.ensure_local_toolchain(tmp_path, tools_dir=tools)
    assert actions == ["runtime"]


@pytest.mark.parametrize("failure", ["download", "extract", "runtime"])
def test_failure_preserves_user_config_and_does_not_publish_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    mock_installation(monkeypatch)
    config = tmp_path / "custom.json"
    config.write_bytes(b"existing user configuration")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise ToolchainError("intentional failure")

    target = {
        "download": "_download_verified",
        "extract": "_extract_suite",
        "runtime": "_validate_runtime",
    }[failure]
    monkeypatch.setattr(local_tools, target, fail)
    tools = tmp_path / "tools"
    with pytest.raises(ToolchainError, match="intentional"):
        local_tools.ensure_local_toolchain(tmp_path, tools_dir=tools, config_path=config)
    assert config.read_bytes() == b"existing user configuration"
    assert not (tools / local_tools.TOOLS_VERSION).exists()
    assert not list(tools.glob(".install*"))


def test_concurrent_installation_is_rejected_without_changing_existing_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_installation(monkeypatch)
    tools = tmp_path / "tools"
    tools.mkdir()
    lock = tools / ".install.lock"
    lock.write_text("123")
    with pytest.raises(ToolchainError, match="déjà active"):
        local_tools.ensure_local_toolchain(tmp_path, tools_dir=tools)
    assert lock.read_text() == "123"
    assert not (tmp_path / "toolchain.json").exists()


def test_incomplete_installation_is_not_silently_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions = mock_installation(monkeypatch)
    installation = tmp_path / "tools" / local_tools.TOOLS_VERSION
    installation.mkdir(parents=True)
    sentinel = installation / "keep"
    sentinel.write_text("user file")
    with pytest.raises(ToolchainError, match="Installation incomplète"):
        local_tools.ensure_local_toolchain(tmp_path, tools_dir=tmp_path / "tools")
    assert sentinel.read_text() == "user file"
    assert actions == []


def test_runtime_check_isolated_from_host_python_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONHOME", "other Python")
    monkeypatch.setenv("PYTHONPATH", "other modules")
    monkeypatch.setenv("PATH", "host binaries")
    calls = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "ready")

    monkeypatch.setattr(local_tools.subprocess, "run", run)
    local_tools._validate_runtime(tmp_path, None)
    assert len(calls) >= 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert "PYTHONHOME" not in env
        assert "other modules" not in env["PYTHONPATH"]
        assert env["PATH"].endswith(os.pathsep + "host binaries")
    assert os.environ["PYTHONHOME"] == "other Python"


def test_runtime_failure_is_reported_before_config_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        local_tools.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(args, 1, "DLL missing"),
    )
    with pytest.raises(ToolchainError, match="DLL missing"):
        local_tools._validate_runtime(tmp_path, None)


def test_insufficient_disk_fails_before_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions = mock_installation(monkeypatch)
    monkeypatch.setattr(
        local_tools.shutil, "disk_usage", lambda _: SimpleNamespace(free=4 * 1024**3 - 1)
    )
    with pytest.raises(ToolchainError, match="4 Go"):
        local_tools.ensure_local_toolchain(tmp_path, tools_dir=tmp_path / "tools")
    assert actions == []
    assert not (tmp_path / "toolchain.json").exists()


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {"release-tag": "older"},
        {
            "release-tag": "2026-09-30",
            "yosys-release-tag": "2026-03-24",
            "target-platform": "linux-x86-64",
        },
    ],
)
def test_wrong_metadata_rejects_installation(tmp_path: Path, metadata: object) -> None:
    populate_installation(tmp_path)
    (tmp_path / "openxc7/BUILD-INFO.json").write_text(json.dumps(metadata))
    with pytest.raises(ToolchainError, match="incompatibles"):
        local_tools._validate_files(tmp_path)


def test_default_tools_and_builds_avoid_project_sync_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local appdata"))
    project = tmp_path / "OneDrive/project with spaces"
    tools = local_tools.default_tools_dir(project)
    settings = local_tools._settings(tools / local_tools.TOOLS_VERSION, project.resolve())
    assert tools == tmp_path / "local appdata/ArtyFrameStudio/fpga-tools"
    assert Path(settings["build_dir"]).parent == tmp_path / "local appdata/ArtyFrameStudio/builds"
    other = local_tools._settings(tools / local_tools.TOOLS_VERSION, project.with_name("other"))
    assert settings["build_dir"] != other["build_dir"]


def test_self_extractor_uses_private_destination_without_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "extracted")

    monkeypatch.setattr(local_tools.subprocess, "run", run)
    archive = tmp_path / "oss suite.exe"
    destination = tmp_path / "private staging"
    destination.mkdir()
    local_tools._extract_suite(archive, destination, None)
    args, kwargs = calls[0]
    assert args == [str(archive), "-y", f"-o{destination}"]
    assert kwargs["cwd"] == destination
    assert not kwargs.get("shell")
