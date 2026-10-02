from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from arty_frame_studio import bitstream
from arty_frame_studio.bitstream import BitstreamError, read_bitstream

TARGET = 0x03631093


def words(*values: int) -> bytes:
    return b"".join(struct.pack(">I", value) for value in values)


def write_packet(register: int, *data: int) -> bytes:
    return words(0x30000000 | register << 13 | len(data), *data)


def payload(idcode: int = TARGET, extra: bytes = b"") -> bytes:
    # Representative uncompressed 7-Series layout, not a routed hardware image.
    return (
        words(0xFFFFFFFF, 0x000000BB, 0x11220044, 0xFFFFFFFF, 0xAA995566, 0x20000000)
        + write_packet(0x04, 0x07)  # CMD RCRC
        + write_packet(0x0C, idcode)
        + extra
        + write_packet(0x04, 0x01)  # CMD WCFG
        + words(0x30004000, 0x50000003, 0x12345678, 0, 0xFFFFFFFF)  # Type 2 FDRI
        + write_packet(0x04, 0x05)  # CMD START
        + write_packet(0x04, 0x0D)  # CMD DESYNC
        + words(0x20000000, 0xFFFFFFFF)
    )


def container(
    data: bytes | None = None,
    part: str = "7a100tcsg324",
    design: str = "arty_top;UserID=0XFFFFFFFF;Version=2024.1;COMPRESS=FALSE",
) -> bytes:
    if data is None:
        data = payload()
    result = b"\x00\x09" + bytes.fromhex("0ff00ff00ff00ff000") + b"\x00\x01"
    for tag, value in (
        (b"a", design),
        (b"b", part),
        (b"c", "2026/10/01"),
        (b"d", "12:00:00"),
    ):
        encoded = value.encode("utf-8") + b"\0"
        result += tag + struct.pack(">H", len(encoded)) + encoded
    return result + b"e" + struct.pack(">I", len(data)) + data


def image_path(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "arty frame.bit"
    path.write_bytes(data)
    return path


@pytest.mark.parametrize(
    "part", ["7a100tcsg324", "xc7a100tcsg324", "7a100tcsg324-1", "XC7A100TCSG324-1"]
)
def test_valid_container_preserves_payload_and_file_digest(tmp_path: Path, part: str) -> None:
    data = payload()
    raw = container(data, part=part)
    path = image_path(tmp_path, raw)
    image = read_bitstream(path)
    assert image.path == path
    assert image.part == part
    assert image.design.startswith("arty_top;")
    assert image.payload == data
    assert image.sha256 == hashlib.sha256(raw).hexdigest()
    assert image.idcode == TARGET


def test_revision_nibble_is_accepted_and_retained(tmp_path: Path) -> None:
    assert read_bitstream(image_path(tmp_path, container(payload(0xF3631093)))).idcode == 0xF3631093


@pytest.mark.parametrize(
    "part",
    ["xc7a35tcsg324-1", "xc7a100tfgg484-1", "xc7a100tcsg324-2", "7a100tcsg324junk", ""],
)
def test_wrong_package_or_speed_grade_is_rejected(tmp_path: Path, part: str) -> None:
    with pytest.raises(BitstreamError, match="Part|Métadonnée"):
        read_bitstream(image_path(tmp_path, container(part=part)))


def test_wrong_embedded_idcode_despite_matching_part(tmp_path: Path) -> None:
    with pytest.raises(BitstreamError, match="IDCODE.*incompatible"):
        read_bitstream(image_path(tmp_path, container(payload(0x0362D093))))


def test_every_declared_idcode_is_checked(tmp_path: Path) -> None:
    data = payload(extra=write_packet(0x0C, 0x0362D093))
    with pytest.raises(BitstreamError, match="IDCODE.*incompatible"):
        read_bitstream(image_path(tmp_path, container(data)))


def test_type2_frame_data_are_skipped_without_interpreting_false_idcode(tmp_path: Path) -> None:
    data = (
        words(0xAA995566)
        + write_packet(0x0C, TARGET)
        + words(0x30004000, 0x50000004, 0x30018001, 0x0362D093, 0x30016001, 0x30014001)
        + words(0x20000000)
    )
    assert read_bitstream(image_path(tmp_path, container(data))).idcode == TARGET


def test_type1_frame_data_are_also_skipped(tmp_path: Path) -> None:
    data = words(0xAA995566) + write_packet(0x0C, TARGET) + write_packet(2, 0x30018001, 0)
    assert read_bitstream(image_path(tmp_path, container(data))).payload == data


@pytest.mark.parametrize("end", [0, 1, 2, 5, 10, 11, 12, 13, 14, 16, 25, 35, -4, -1])
def test_truncated_container_is_reported(tmp_path: Path, end: int) -> None:
    with pytest.raises(BitstreamError):
        read_bitstream(image_path(tmp_path, container()[:end]))


@pytest.mark.parametrize("offset,replacement", [(0, b"\x00\x08"), (2, b"bad"), (11, b"\x00\x02")])
def test_malformed_container_signature(tmp_path: Path, offset: int, replacement: bytes) -> None:
    raw = bytearray(container())
    raw[offset : offset + len(replacement)] = replacement
    with pytest.raises(BitstreamError, match="Signature|Marqueur"):
        read_bitstream(image_path(tmp_path, bytes(raw)))


def test_wrong_metadata_field_order(tmp_path: Path) -> None:
    raw = bytearray(container())
    raw[13] = ord("b")
    with pytest.raises(BitstreamError, match="Champ a"):
        read_bitstream(image_path(tmp_path, bytes(raw)))


@pytest.mark.parametrize("design", ["", "design\0embedded", "design\ncontrol"])
def test_invalid_metadata_text(tmp_path: Path, design: str) -> None:
    with pytest.raises(BitstreamError, match="Métadonnée"):
        read_bitstream(image_path(tmp_path, container(design=design)))


def test_payload_declared_size_cannot_overrun_file(tmp_path: Path) -> None:
    raw = bytearray(container())
    offset = len(raw) - len(payload()) - 4
    raw[offset : offset + 4] = struct.pack(">I", 0xFFFFFFFF)
    with pytest.raises(BitstreamError, match="tronqué"):
        read_bitstream(image_path(tmp_path, bytes(raw)))


def test_container_trailing_garbage_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(BitstreamError, match="supplémentaires"):
        read_bitstream(image_path(tmp_path, container() + b"garbage"))


def test_file_size_is_bounded_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bitstream, "MAX_BITSTREAM_SIZE", 64)
    with pytest.raises(BitstreamError, match="volumineux"):
        read_bitstream(image_path(tmp_path, b"x" * 65))


def test_missing_file_uses_application_exception(tmp_path: Path) -> None:
    with pytest.raises(BitstreamError, match="Impossible de lire"):
        read_bitstream(tmp_path / "missing.bit")


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "vide"),
        (words(0xAA995566) + b"x", "aligné"),
        (words(0xFFFFFFFF), "synchronisation"),
        (words(0, 0xAA995566), "Préambule"),
        (words(0xAA995566, 0x20000000), "IDCODE"),
        (words(0xAA995566) + write_packet(0x0C, TARGET), "FDRI"),
        (words(0xAA995566, 0x30018002, TARGET, TARGET), "IDCODE invalide"),
        (words(0xAA995566, 0x30018001), "tronqué"),
        (words(0xAA995566, 0x50000001, TARGET), "Type 2"),
        (words(0xAA995566, 0x30004000), "Type 2 manquant"),
        (words(0xAA995566, 0x30004000, 0x20000000), "Type 2 manquant"),
        (words(0xAA995566, 0x30004000, 0x50000000), "Type 2 vide"),
        (words(0xAA995566, 0x30004000, 0x57FFFFFF), "tronqué"),
        (words(0xAA995566, 0x30019801, TARGET), "réservés"),
        (words(0xAA995566, 0x28018001), "WRITE/NOP"),
        (words(0xAA995566, 0x38018001, TARGET), "WRITE/NOP"),
        (words(0xAA995566, 0x60000000), "Type de paquet"),
    ],
)
def test_malformed_or_unsupported_packets(tmp_path: Path, data: bytes, message: str) -> None:
    with pytest.raises(BitstreamError, match=message):
        read_bitstream(image_path(tmp_path, container(data)))


@pytest.mark.parametrize(
    "extra,message",
    [
        (write_packet(0x0A, 0), "compressé"),
        (write_packet(0x04, 2), "compressé"),
        (write_packet(0x0B, 0), "chiffré"),
        (write_packet(0x05, 1 << 6), "chiffré"),
        (write_packet(0x04, 0x0F), "IPROG"),
    ],
)
def test_compression_encryption_and_reboot_commands_are_rejected(
    tmp_path: Path, extra: bytes, message: str
) -> None:
    with pytest.raises(BitstreamError, match=message):
        read_bitstream(image_path(tmp_path, container(payload(extra=extra))))


@pytest.mark.parametrize("flag", ["COMPRESS=TRUE", "encrypt=1", "ENCRYPTION=YES"])
def test_unsupported_encoding_declared_by_metadata(tmp_path: Path, flag: str) -> None:
    with pytest.raises(BitstreamError, match="compressé ou chiffré"):
        read_bitstream(image_path(tmp_path, container(design="arty_top;" + flag)))
