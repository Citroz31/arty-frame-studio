"""Protocole UART borné, identique à celui du firmware Arty A7.

Un paquet contient ``A7 7A version op seq len payload crc_lo crc_hi``.
Le CRC couvre les octets de ``version`` à la fin du payload.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from .model import FrameConfig

SYNC = b"\xa7\x7a"
VERSION = 1
MAX_PAYLOAD = 32
SEND_FORMAT = struct.Struct("<IBHHHHB")
RESPONSE_FORMAT = struct.Struct("<BBH")


class Opcode(IntEnum):
    PING = 1
    SEND = 2
    STOP = 3
    STATUS = 4
    # Révision 2 du firmware : test des LED et identification.
    LED = 5
    INFO = 6
    # Révision 5 du firmware : niveau statique de la broche TR (3,3 V ou 0 V).
    TR = 7


LED_MANUAL = 0x80
INFO_PAGES = 6
CAPABILITY_LED = 0x0001
CAPABILITY_INFO = 0x0002
# Révision 3 : SEND avec repeat_count 0 répète la trame jusqu'à STOP.
CAPABILITY_CONTINUOUS = 0x0004
# Révision 4 : bit 2 des flags de SEND, CLK libre pendant LATCH et pause.
CAPABILITY_FREE_CLOCK = 0x0008
# Révision 5 : commande TR, sortie statique pour le mode émission/réception.
CAPABILITY_TR = 0x0010


class StatusCode(IntEnum):
    OK = 0
    UNKNOWN_OPCODE = 1
    INVALID_PAYLOAD = 2
    BUSY = 3
    BAD_CRC = 4
    BAD_VERSION = 5

    @property
    def message(self) -> str:
        return {
            self.OK: "Commande acceptée",
            self.UNKNOWN_OPCODE: "Commande inconnue",
            self.INVALID_PAYLOAD: "Paramètres de trame invalides",
            self.BUSY: "Une émission est déjà en cours",
            self.BAD_CRC: "CRC invalide",
            self.BAD_VERSION: "Version du protocole incompatible",
        }[self]


class ProtocolError(ValueError):
    """Un paquet reçu ne respecte pas le contrat du protocole."""


@dataclass(frozen=True)
class Packet:
    version: int
    opcode: int
    sequence: int
    payload: bytes

    @property
    def op(self) -> int:
        return self.opcode

    @property
    def seq(self) -> int:
        return self.sequence


@dataclass(frozen=True)
class FirmwareInfo:
    """Identité lue par INFO ; ``build_id`` 0 désigne le firmware de référence."""

    revision: int
    core_hz: int
    capabilities: int
    build_id: int

    @property
    def led_test(self) -> bool:
        return bool(self.capabilities & CAPABILITY_LED)

    @property
    def continuous(self) -> bool:
        return bool(self.capabilities & CAPABILITY_CONTINUOUS)

    @property
    def free_clock(self) -> bool:
        return bool(self.capabilities & CAPABILITY_FREE_CLOCK)

    @property
    def tr(self) -> bool:
        return bool(self.capabilities & CAPABILITY_TR)


def led_argument(pattern: int | None) -> int:
    """``None`` rend les LED à l'état de la carte ; sinon motif LD4..LD7 sur 4 bits."""
    if pattern is None:
        return 0
    if type(pattern) is not int or not 0 <= pattern <= 0x0F:
        raise ProtocolError("Le motif des LED doit être un entier de 0 à 15.")
    return LED_MANUAL | pattern


def tr_argument(level: int) -> int:
    """Niveau de la broche TR : 1 = 3,3 V, 0 = 0 V."""
    if type(level) is not int or level not in (0, 1):
        raise ProtocolError("Le niveau de TR doit valoir 0 (0 V) ou 1 (3,3 V).")
    return level


def info_from_pages(words: list[int]) -> FirmwareInfo:
    if len(words) != INFO_PAGES or any(type(word) is not int for word in words):
        raise ProtocolError("INFO doit fournir six mots de 16 bits.")
    return FirmwareInfo(
        revision=words[0],
        core_hz=words[1] | words[2] << 16,
        capabilities=words[3],
        build_id=words[4] | words[5] << 16,
    )


@dataclass(frozen=True)
class DeviceStatus:
    status: StatusCode
    busy: bool
    completed: int

    @property
    def ok(self) -> bool:
        return self.status == StatusCode.OK


def crc16(data: bytes | bytearray | memoryview) -> int:
    """CRC16 CCITT-FALSE (polynôme 0x1021, valeur initiale 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if crc & 0x8000 else 0)) & 0xFFFF
    return crc


def encode_request(
    op: Opcode, seq: int, config: FrameConfig | None = None, *, argument: int | None = None
) -> bytes:
    """``config`` sert à SEND ; ``argument`` est l'octet de LED, d'INFO (page) et de TR (niveau)."""
    try:
        opcode = Opcode(op)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("Commande UART inconnue.") from exc
    if type(seq) is not int or not 0 <= seq <= 255:
        raise ProtocolError("Le numéro de séquence doit être compris entre 0 et 255.")
    if opcode in (Opcode.LED, Opcode.INFO, Opcode.TR):
        if config is not None:
            raise ProtocolError("Seule la commande SEND accepte une configuration.")
        if type(argument) is not int or not 0 <= argument <= 255:
            raise ProtocolError(f"{opcode.name} exige un octet d'argument.")
        if opcode == Opcode.LED and argument & 0x70:
            raise ProtocolError("LED : les bits 4 à 6 de l'argument sont réservés.")
        if opcode == Opcode.INFO and argument >= INFO_PAGES:
            raise ProtocolError(f"INFO : page entre 0 et {INFO_PAGES - 1}.")
        if opcode == Opcode.TR:
            tr_argument(argument)
        payload = bytes((argument,))
    elif argument is not None:
        raise ProtocolError("Seules LED, INFO et TR acceptent un octet d'argument.")
    elif opcode == Opcode.SEND:
        if not isinstance(config, FrameConfig):
            raise ProtocolError("La commande SEND exige une configuration de trame.")
        flags = (
            int(config.lsb_first)
            | (int(config.latch_active_low) << 1)
            | (int(config.free_clock) << 2)
        )
        payload = SEND_FORMAT.pack(
            config.word,
            config.bit_count,
            config.divider,
            config.latch_ticks,
            config.gap_ticks,
            config.repeat_count,
            flags,
        )
    else:
        if config is not None:
            raise ProtocolError("Seule la commande SEND accepte une configuration.")
        payload = b""
    body = bytes((VERSION, opcode, seq, len(payload))) + payload
    return SYNC + body + struct.pack("<H", crc16(body))


class PacketDecoder:
    """Décodage incrémental et resynchronisation après bruit ou CRC invalide.

    Le tampon résiduel est borné à un paquet (40 octets). Les paquets corrompus
    sont ignorés ; les compteurs permettent au transport de préciser un timeout.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.crc_errors = 0
        self.length_errors = 0

    def feed(self, data: bytes) -> list[Packet]:
        self._buffer.extend(data)
        packets: list[Packet] = []
        while self._buffer:
            start = self._buffer.find(SYNC)
            if start < 0:
                # Conserver le premier octet de synchro s'il est fragmenté.
                self._buffer[:] = b"\xa7" if self._buffer[-1] == SYNC[0] else b""
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 6:
                break
            size = self._buffer[5]
            if size > MAX_PAYLOAD:
                self.length_errors += 1
                del self._buffer[0]
                continue
            total = 8 + size
            if len(self._buffer) < total:
                # Une longueur corrompue peut masquer une réponse complète qui
                # suit. Ne resynchroniser que sur un candidat au CRC vérifié.
                candidate = self._buffer.find(SYNC, 2)
                recovered = False
                while candidate >= 0 and len(self._buffer) - candidate >= 6:
                    candidate_size = self._buffer[candidate + 5]
                    candidate_end = candidate + 8 + candidate_size
                    if candidate_size <= MAX_PAYLOAD and candidate_end <= len(self._buffer):
                        candidate_crc = struct.unpack_from("<H", self._buffer, candidate_end - 2)[0]
                        if (
                            crc16(memoryview(self._buffer)[candidate + 2 : candidate_end - 2])
                            == candidate_crc
                        ):
                            del self._buffer[:candidate]
                            recovered = True
                            break
                    candidate = self._buffer.find(SYNC, candidate + 2)
                if recovered:
                    continue
                break
            expected = struct.unpack_from("<H", self._buffer, total - 2)[0]
            if crc16(memoryview(self._buffer)[2 : total - 2]) != expected:
                self.crc_errors += 1
                del self._buffer[0]
                continue
            packets.append(
                Packet(
                    self._buffer[2],
                    self._buffer[3],
                    self._buffer[4],
                    bytes(self._buffer[6 : total - 2]),
                )
            )
            del self._buffer[:total]
        return packets


def decode_response(packet: Packet) -> DeviceStatus:
    if packet.version != VERSION:
        raise ProtocolError("La carte utilise une version de protocole incompatible.")
    if packet.opcode not in tuple(0x80 | op for op in Opcode):
        raise ProtocolError("Le paquet reçu n'est pas une réponse UART reconnue.")
    if len(packet.payload) != RESPONSE_FORMAT.size:
        raise ProtocolError("La réponse UART a une longueur invalide.")
    status, busy, completed = RESPONSE_FORMAT.unpack(packet.payload)
    if busy not in (0, 1):
        raise ProtocolError("Le champ d'activité de la réponse UART est invalide.")
    try:
        status_code = StatusCode(status)
    except ValueError as exc:
        raise ProtocolError("Le code d'état de la réponse UART est inconnu.") from exc
    return DeviceStatus(status_code, bool(busy), completed)
