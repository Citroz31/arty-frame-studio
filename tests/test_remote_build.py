"""GitHub Actions builds, against an in-memory GitHub API."""

import hashlib
import io
import json
import urllib.error
import zipfile
from pathlib import Path

import pytest

from arty_frame_studio.firmware_config import FirmwareBuildConfig
from arty_frame_studio.remote_build import (
    ARTIFACT_PREFIX,
    GitHubBuildClient,
    RemoteBuildError,
    RemoteBuildTarget,
    extract_firmware_archive,
)

ROOT = Path(__file__).resolve().parents[1]
BIT = (ROOT / "firmware/prebuilt/arty_frame.bit").read_bytes()
FIRMWARE = FirmwareBuildConfig(
    core_hz=150_000_000, data_pin="JC3", clock_pin="JC1", latch_pin="JC7"
)
SOURCE_COMMIT = "a" * 40


def artifact(firmware=FIRMWARE, *, sha256=None, extra=None, manifest_changes=None, omit=()):
    manifest = {
        "sha256": sha256 or hashlib.sha256(BIT).hexdigest(),
        "build_id": firmware.build_id,
        "firmware_config": firmware.to_dict(),
        "routed_core_fmax_mhz": firmware.core_hz / 1e6 + 10,
        "timing_requirement_mhz": firmware.core_hz / 1e6,
        "source_commit": SOURCE_COMMIT,
        "part": "xc7a100tcsg324-1",
        "idcode": "0x03631093",
    }
    manifest.update(manifest_changes or {})
    files = {
        "arty_frame.bit": BIT,
        "firmware-manifest.json": json.dumps(manifest),
        "timing.json": json.dumps(
            {
                "fmax": {
                    "core_clock": {
                        "achieved": firmware.core_hz / 1e6 + 10,
                        "constraint": firmware.core_hz / 1e6,
                    }
                }
            }
        ),
        "successful-build.json": json.dumps(
            {
                "firmware_config": firmware.to_dict(),
                "build_id": firmware.build_id,
                "bitstream_sha256": hashlib.sha256(BIT).hexdigest(),
                "part": "xc7a100tcsg324-1",
                "timing_clock": "core_clock",
                "timing_requirement_mhz": firmware.core_hz / 1e6,
            }
        ),
        "../escape.txt": "outside",
    }
    files.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in files.items():
            if name not in omit:
                bundle.writestr(name, data)
    return buffer.getvalue()


class Response:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.body[:limit]


class FakeGitHub:
    def __init__(self, *, archive=None, conclusion="success", polls_before_run=1):
        self.requests = []
        self.archive = archive if archive is not None else artifact()
        self.conclusion = conclusion
        self.polls_before_run = polls_before_run
        self.request_id = None

    def __call__(self, request, timeout):
        url = request.full_url
        self.requests.append((request.get_method(), url, request))
        assert request.unredirected_hdrs["Authorization"] == "Bearer secret-token"
        if url.endswith("/dispatches"):
            body = json.loads(request.data)
            assert body["ref"] == "main"
            self.request_id = body["inputs"]["request_id"]
            self.config = body["inputs"]["firmware_config"]
            return Response(b"")
        if "/workflows/firmware.yml/runs" in url:
            if self.polls_before_run:
                self.polls_before_run -= 1
                return Response(json.dumps({"workflow_runs": []}).encode())
            run = {
                "id": 77,
                "display_title": f"Firmware build {self.request_id}",
                "html_url": "https://github.com/x/runs/77",
                "head_sha": SOURCE_COMMIT,
            }
            other = {"id": 76, "display_title": "Firmware build 0123456789abcdef"}
            return Response(json.dumps({"workflow_runs": [other, run]}).encode())
        if url.endswith("/actions/runs/77"):
            run = {
                "id": 77,
                "status": "completed",
                "conclusion": self.conclusion,
                "html_url": "https://github.com/x/runs/77",
                "head_sha": SOURCE_COMMIT,
            }
            return Response(json.dumps(run).encode())
        if url.endswith("/actions/runs/77/artifacts"):
            listing = {
                "artifacts": [
                    {
                        "name": ARTIFACT_PREFIX + self.request_id,
                        "size_in_bytes": len(self.archive),
                        "archive_download_url": "https://api.github.com/repos/o/r/actions/artifacts/9/zip",
                    }
                ]
            }
            return Response(json.dumps(listing).encode())
        if url.endswith("/artifacts/9/zip"):
            return Response(self.archive)
        raise AssertionError(url)


def client(fake):
    return GitHubBuildClient(
        RemoteBuildTarget(), "secret-token", opener=fake, clock=lambda: 0.0, sleep=lambda _: None
    )


def test_remote_build_dispatches_finds_waits_and_verifies_the_artifact(tmp_path):
    fake = FakeGitHub()
    messages = []
    result = client(fake).build(FIRMWARE, tmp_path, progress=messages.append)
    assert FirmwareBuildConfig.from_json(fake.config) == FIRMWARE
    assert len(fake.request_id) == 16
    assert result.bitstream.read_bytes() == BIT
    assert result.manifest["build_id"] == FIRMWARE.build_id
    assert result.bitstream.parent.name == f"firmware-{FIRMWARE.build_id:08x}-{fake.request_id}"
    assert not (tmp_path / "escape.txt").exists()
    assert sorted(path.name for path in result.bitstream.parent.iterdir()) == [
        "arty_frame.bit",
        "firmware-manifest.json",
        "successful-build.json",
        "timing.json",
    ]
    assert any("runs/77" in message for message in messages)
    assert all(url.startswith("https://api.github.com/") for _, url, _ in fake.requests)


def test_reference_firmware_is_requested_with_an_empty_configuration(tmp_path):
    fake = FakeGitHub(archive=artifact(FirmwareBuildConfig()))
    client(fake).build(FirmwareBuildConfig(), tmp_path)
    assert fake.config == ""


@pytest.mark.parametrize(
    "fake,message",
    [
        (FakeGitHub(conclusion="failure"), "échec"),
        (FakeGitHub(archive=artifact(sha256="0" * 64)), "manifeste"),
        (FakeGitHub(archive=artifact(FirmwareBuildConfig(core_hz=100_000_000))), "configuration"),
        (FakeGitHub(archive=b"not a zip"), "ZIP"),
    ],
)
def test_failed_or_mismatched_builds_are_refused(tmp_path, fake, message):
    with pytest.raises(RemoteBuildError, match=message):
        client(fake).build(FIRMWARE, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_http_errors_explain_token_permissions():
    def forbidden(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO())

    with pytest.raises(RemoteBuildError, match="Actions en écriture"):
        client(forbidden).dispatch(FIRMWARE)


def test_token_is_never_sent_outside_the_github_api():
    fake = FakeGitHub()
    with pytest.raises(RemoteBuildError, match="inattendue"):
        client(fake)._request("GET", "https://example.com/zip", raw=True)
    assert fake.requests == []


@pytest.mark.parametrize(
    "repository,ref", [("owner", "main"), ("o/r;x", "main"), ("o/r", "../main"), ("o/r", "a b")]
)
def test_invalid_targets_and_tokens_are_rejected(repository, ref):
    with pytest.raises(ValueError):
        RemoteBuildTarget(repository, ref)
    with pytest.raises(ValueError, match="Jeton"):
        GitHubBuildClient(RemoteBuildTarget(), "two words")


def test_missing_run_times_out_with_guidance():
    fake = FakeGitHub(polls_before_run=10**6)
    times = iter(range(0, 10_000, 10))
    patient = GitHubBuildClient(
        RemoteBuildTarget(),
        "secret-token",
        opener=fake,
        clock=lambda: next(times),
        sleep=lambda _: None,
    )
    with pytest.raises(RemoteBuildError, match="n'apparaît pas"):
        patient.find_run("feedfacecafebeef")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "160", True, 10**400])
def test_nonfinite_or_untyped_timing_is_refused_without_leaving_a_download(tmp_path, value):
    fake = FakeGitHub(archive=artifact(manifest_changes={"routed_core_fmax_mhz": value}))
    with pytest.raises(RemoteBuildError, match="numériques finies"):
        client(fake).build(FIRMWARE, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["timing.json", "successful-build.json"])
def test_missing_build_evidence_is_refused(tmp_path, name):
    fake = FakeGitHub(archive=artifact(omit=[name]))
    with pytest.raises(RemoteBuildError, match=name):
        client(fake).build(FIRMWARE, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"source_commit": "b" * 40}, "commit"),
        ({"source_commit": "unknown"}, "commit"),
        ({"routed_core_fmax_mhz": 175}, "timing"),
        ({"idcode": "0x03636093"}, "IDCODE"),
    ],
)
def test_manifest_must_match_run_target_and_timing_report(tmp_path, changes, message):
    with pytest.raises(RemoteBuildError, match=message):
        client(FakeGitHub(archive=artifact(manifest_changes=changes))).build(FIRMWARE, tmp_path)


def test_timing_constraint_cannot_be_weaker_than_the_requested_core(tmp_path):
    report = {"fmax": {"core_clock": {"achieved": 160, "constraint": 149.99}}}
    fake = FakeGitHub(archive=artifact(extra={"timing.json": json.dumps(report)}))
    with pytest.raises(RemoteBuildError, match="timing"):
        client(fake).build(FIRMWARE, tmp_path)


def test_archive_limits_the_total_extracted_size(tmp_path, monkeypatch):
    from arty_frame_studio import remote_build

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("build.log", "a" * 600)
        bundle.writestr("rtl-tests.log", "b" * 600)
    monkeypatch.setattr(remote_build, "MAX_ARTIFACT_BYTES", 1000)
    with pytest.raises(RemoteBuildError, match="ensemble"):
        extract_firmware_archive(buffer.getvalue(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_duplicate_files_are_rejected(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("timing.json", "{}")
        with pytest.warns(UserWarning, match="Duplicate"):
            bundle.writestr("timing.json", "{}")
    with pytest.raises(RemoteBuildError, match="dupliqué"):
        extract_firmware_archive(buffer.getvalue(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_programming_revalidates_downloaded_firmware_after_metadata_changes(tmp_path):
    from arty_frame_studio.bitstream import BitstreamError, read_bitstream
    from arty_frame_studio.prebuilt import validate_programming_image

    result = client(FakeGitHub()).build(FIRMWARE, tmp_path)
    image = read_bitstream(result.bitstream)
    assert validate_programming_image(image, ROOT)["build_id"] == FIRMWARE.build_id
    (result.bitstream.parent / "timing.json").write_text("{}")
    with pytest.raises(BitstreamError, match="timing"):
        validate_programming_image(image, ROOT)
