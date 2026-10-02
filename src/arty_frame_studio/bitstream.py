"""Bounded reader for uncompressed 7-Series .bit files targeting the Arty A7-100T.

The payload retains file byte order: its bits must be sent MSB first to CFG_IN.
This validates the container and declared target, not configuration CRC, the FPGA
design, timing, or hardware operation. The programmer must verify DONE/STAT.
Protocol reference: AMD UG470, bitstream composition and configuration packets.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from struct import unpack_from

MAX_BITSTREAM_SIZE = 40 * 1024 * 1024
TARGET_IDCODE = 0x03631093
IDCODE_PART_MASK = 0x0FFFFFFF

_MAGIC = bytes.fromhex("0ff00ff00ff00ff000")
_PART = re.compile(r"(?:xc)?7a100tcsg324(?:-1)?", re.IGNORECASE)
_SYNC_WORD = 0xAA995566
_PREAMBLE_WORDS = {0xFFFFFFFF, 0x000000BB, 0x11220044}


class BitstreamError(RuntimeError):
    """The file cannot be accepted by the bounded SRAM programmer."""


@dataclass(frozen=True)
class BitstreamImage:
    path: Path
    part: str
    design: str
    payload: bytes
    sha256: str
    idcode: int


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def take(self, size: int, label: str) -> bytes:
        end = self.position + size
        if size < 0 or end > len(self.data):
            raise BitstreamError(f"Fichier .bit tronqué : {label}.")
        result = self.data[self.position : end]
        self.position = end
        return result

    def integer(self, size: int, label: str) -> int:
        return int.from_bytes(self.take(size, label), "big")


def _text_field(cursor: _Cursor, label: str) -> str:
    size = cursor.integer(2, f"longueur {label}")
    raw = cursor.take(size, label)
    if raw.endswith(b"\0"):
        raw = raw[:-1]
    try:
        result = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BitstreamError(f"Métadonnée {label} : texte invalide.") from error
    if not result or any(ord(character) < 32 for character in result):
        raise BitstreamError(f"Métadonnée {label} vide ou contenant un caractère de contrôle.")
    return result


def _word(payload: bytes, position: int) -> int:
    return int(unpack_from(">I", payload, position)[0])


def _check_write(register: int, count: int, payload: bytes, position: int) -> int | None:
    """Check control writes; frame data are never searched for instruction motifs."""
    if register == 0x0C:
        if count != 1:
            raise BitstreamError("Paquet IDCODE invalide : un seul mot est requis.")
        idcode = _word(payload, position)
        if idcode & IDCODE_PART_MASK != TARGET_IDCODE:
            raise BitstreamError(
                f"IDCODE du bitstream 0x{idcode:08X} incompatible avec l'Arty A7-100T "
                f"(0x{TARGET_IDCODE:08X}, révision ignorée)."
            )
        return idcode
    if register == 0x0A:
        raise BitstreamError("Bitstream compressé (MFWR) non pris en charge.")
    if register == 0x0B:
        raise BitstreamError("Bitstream chiffré (CBC) non pris en charge.")
    if register == 0x05:
        if count != 1:
            raise BitstreamError("Paquet CTL0 invalide.")
        if _word(payload, position) & (1 << 6):
            raise BitstreamError("Bitstream chiffré (CTL0.DEC) non pris en charge.")
    if register == 0x04:
        if count != 1:
            raise BitstreamError("Paquet CMD invalide.")
        command = _word(payload, position)
        if command == 0x02:
            raise BitstreamError("Bitstream compressé (commande MFW) non pris en charge.")
        if command == 0x0F:
            raise BitstreamError("Commande IPROG non prise en charge pour un chargement SRAM.")
    return None


def _validate_payload(payload: bytes) -> int:
    if not payload or len(payload) % 4:
        raise BitstreamError("Payload .bit vide ou non aligné sur des mots de 32 bits.")
    position = 0
    while position < min(len(payload), 1024):
        word = _word(payload, position)
        position += 4
        if word == _SYNC_WORD:
            break
        if word not in _PREAMBLE_WORDS:
            raise BitstreamError("Préambule .bit invalide : mot de synchronisation attendu.")
    else:
        raise BitstreamError("Mot de synchronisation AA995566 absent du préambule .bit.")

    idcode: int | None = None
    has_frames = False
    pending_register: int | None = None
    while position < len(payload):
        header = _word(payload, position)
        position += 4
        if header == 0xFFFFFFFF:
            if pending_register is not None:
                raise BitstreamError("Paquet Type 2 manquant après son en-tête Type 1.")
            continue
        packet_type = header >> 29
        opcode = (header >> 27) & 3
        if packet_type == 1:
            if pending_register is not None:
                raise BitstreamError("Paquet Type 2 manquant après son en-tête Type 1.")
            if header & 0x1800:
                raise BitstreamError("Bits réservés non nuls dans un paquet Type 1.")
            register = (header >> 13) & 0x3FFF
            count = header & 0x7FF
            if opcode == 0 and header == 0x20000000:
                continue
            if opcode != 2:
                raise BitstreamError("Seuls les paquets de configuration WRITE/NOP sont acceptés.")
            if count == 0:
                pending_register = register
                continue
        elif packet_type == 2:
            if pending_register is None or opcode != 2:
                raise BitstreamError("Paquet Type 2 sans en-tête WRITE Type 1 correspondant.")
            register = pending_register
            pending_register = None
            count = header & 0x7FFFFFF
            if count == 0:
                raise BitstreamError("Paquet Type 2 vide non pris en charge.")
        else:
            raise BitstreamError("Type de paquet de configuration invalide ou non pris en charge.")

        size = count * 4
        if size > len(payload) - position:
            raise BitstreamError("Payload tronqué : données du paquet de configuration manquantes.")
        declared_idcode = _check_write(register, count, payload, position)
        if declared_idcode is not None:
            idcode = declared_idcode
        if register == 0x02:
            has_frames = True
        position += size
    if pending_register is not None:
        raise BitstreamError("Paquet Type 2 manquant après son en-tête Type 1.")
    if idcode is None:
        raise BitstreamError("Aucun paquet WRITE IDCODE dans le bitstream.")
    if not has_frames:
        raise BitstreamError("Aucune donnée de configuration FDRI dans le bitstream.")
    return idcode


def read_bitstream(path: Path) -> BitstreamImage:
    """Read one .bit container, verifying its package and embedded silicon target.

    Accepts only uncompressed, unencrypted WRITE/NOP packet streams. The returned
    IDCODE includes its revision nibble; target checks ignore that nibble only.
    The digest covers the complete file, including its metadata. No hardware I/O.
    """
    path = Path(path)
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_BITSTREAM_SIZE + 1)
    except OSError as error:
        raise BitstreamError(f"Impossible de lire le fichier .bit {path} : {error}") from error
    if len(data) > MAX_BITSTREAM_SIZE:
        raise BitstreamError("Fichier .bit trop volumineux : limite de 40 Mio.")
    cursor = _Cursor(data)
    magic_size = cursor.integer(2, "longueur de signature")
    if magic_size != len(_MAGIC) or cursor.take(magic_size, "signature") != _MAGIC:
        raise BitstreamError("Signature du conteneur Xilinx .bit invalide.")
    if cursor.integer(2, "longueur du marqueur a") != 1:
        raise BitstreamError("Marqueur de champ a invalide dans le conteneur .bit.")
    fields: dict[str, str] = {}
    for tag, label in (("a", "design"), ("b", "part"), ("c", "date"), ("d", "heure")):
        if cursor.take(1, f"marqueur {tag}") != tag.encode("ascii"):
            raise BitstreamError(f"Champ {tag} attendu dans le conteneur .bit.")
        fields[tag] = _text_field(cursor, label)
    if _PART.fullmatch(fields["b"]) is None:
        raise BitstreamError(f"Part {fields['b']!r} incompatible : xc7a100tcsg324-1 attendu.")
    if re.search(
        r"(?:^|;)\s*(?:COMPRESS|ENCRYPT|ENCRYPTION)\s*=\s*(?:TRUE|YES|1)(?:;|$)",
        fields["a"],
        re.IGNORECASE,
    ):
        raise BitstreamError("Bitstream compressé ou chiffré déclaré non pris en charge.")
    if cursor.take(1, "marqueur e") != b"e":
        raise BitstreamError("Champ e attendu dans le conteneur .bit.")
    payload_size = cursor.integer(4, "longueur du payload")
    payload = cursor.take(payload_size, "payload")
    if cursor.position != len(data):
        raise BitstreamError("Données supplémentaires après le payload du conteneur .bit.")
    return BitstreamImage(
        path=path,
        part=fields["b"],
        design=fields["a"],
        payload=payload,
        sha256=hashlib.sha256(data).hexdigest(),
        idcode=_validate_payload(payload),
    )
