import binascii
import struct

import pytest

from arty_frame_studio.model import FrameConfig
from arty_frame_studio.protocol import (
    DeviceStatus,
    Opcode,
    Packet,
    PacketDecoder,
    ProtocolError,
    StatusCode,
    crc16,
    decode_response,
    encode_request,
)


def wire_response(op=Opcode.PING, seq=17, status=0, busy=0, completed=0):
    payload = struct.pack("<BBH", status, busy, completed)
    body = bytes((1, 0x80 | op, seq, len(payload))) + payload
    return b"\xa7\x7a" + body + struct.pack("<H", binascii.crc_hqx(body, 0xFFFF))


def test_standard_crc_vector():
    assert crc16(b"123456789") == 0x29B1


def test_send_binary_layout_and_flags():
    config = FrameConfig(
        word=0x12345,
        bit_count=17,
        divider=4,
        latch_ticks=3,
        gap_ticks=5,
        repeat_count=2,
        lsb_first=True,
        latch_active_low=True,
    )
    body = bytes.fromhex("01 02 ab 0e 45 23 01 00 11 04 00 03 00 05 00 02 00 03")
    expected = b"\xa7\x7a" + body + struct.pack("<H", binascii.crc_hqx(body, 0xFFFF))
    assert encode_request(Opcode.SEND, 171, config) == expected


@pytest.mark.parametrize("op", [Opcode.PING, Opcode.STOP, Opcode.STATUS])
def test_zero_payload_commands(op):
    wire = encode_request(op, 255)
    assert len(wire) == 8
    packet = PacketDecoder().feed(wire)[0]
    assert (packet.version, packet.opcode, packet.sequence, packet.payload) == (1, op, 255, b"")


@pytest.mark.parametrize("split", range(13))
def test_incremental_decoder_at_every_boundary(split):
    wire = wire_response(completed=512, busy=1)
    decoder = PacketDecoder()
    packets = decoder.feed(wire[:split]) + decoder.feed(wire[split:])
    assert packets == [Packet(1, 0x81, 17, struct.pack("<BBH", 0, 1, 512))]


def test_decoder_resynchronizes_after_noise_oversize_and_bad_crc():
    valid = wire_response(seq=9, completed=3)
    corrupt = bytearray(wire_response(seq=8))
    corrupt[-1] ^= 0x20
    oversize = b"\xa7\x7a\x01\x81\x01\xff"
    decoder = PacketDecoder()
    decoded = decoder.feed(b"garbage\xa7" + oversize + corrupt + valid)
    assert [packet.sequence for packet in decoded] == [9]
    assert decoder.length_errors == 1
    assert decoder.crc_errors == 1


def test_decoder_handles_magic_inside_a_valid_payload():
    wire = wire_response(completed=0x7AA7)
    assert decode_response(PacketDecoder().feed(wire)[0]).completed == 0x7AA7


def test_decoder_recovers_without_waiting_for_a_corrupt_length():
    # Une réponse dont len est corrompu à 32 ne doit pas bloquer une bonne
    # réponse courte arrivée ensuite, alors que le tampon n'a que 24 octets.
    corrupt = bytearray(wire_response(seq=8))
    corrupt[5] = 32
    decoder = PacketDecoder()
    decoded = decoder.feed(corrupt + wire_response(seq=9))
    assert [packet.sequence for packet in decoded] == [9]


def test_decoder_discards_unbounded_noise():
    decoder = PacketDecoder()
    assert decoder.feed(b"x" * 100_000 + b"\xa7") == []
    assert decoder.feed(wire_response()[1:])[0].sequence == 17


def test_decode_response_fields():
    packet = PacketDecoder().feed(wire_response(status=3, busy=1, completed=123))[0]
    result = decode_response(packet)
    assert result == DeviceStatus(StatusCode.BUSY, True, 123)
    assert not result.ok
    assert StatusCode.BUSY.message == "Une émission est déjà en cours"


@pytest.mark.parametrize(
    ("wire", "sequence"),
    [
        ("a7 7a 01 86 01 04 00 00 04 00 53 7c", 1),
        ("a7 7a 01 86 02 04 00 00 04 00 b3 b2", 2),
    ],
)
def test_board_captured_info_reply_is_revision_four(wire, sequence):
    packets = PacketDecoder().feed(bytes.fromhex(wire))
    assert packets == [Packet(1, 0x86, sequence, b"\x00\x00\x04\x00")]
    assert decode_response(packets[0]) == DeviceStatus(StatusCode.OK, False, 4)


@pytest.mark.parametrize(
    "packet",
    [
        Packet(2, 0x81, 0, b"\x00\x00\x00\x00"),
        Packet(1, 0x01, 0, b"\x00\x00\x00\x00"),
        Packet(1, 0x81, 0, b"\x00"),
        Packet(1, 0x81, 0, b"\x00\x02\x00\x00"),
        Packet(1, 0x81, 0, b"\xff\x00\x00\x00"),
    ],
)
def test_decode_rejects_malformed_responses(packet):
    with pytest.raises(ProtocolError):
        decode_response(packet)


@pytest.mark.parametrize("seq", [-1, 256, 1.5, True])
def test_encoder_rejects_bad_sequence(seq):
    with pytest.raises(ProtocolError):
        encode_request(Opcode.PING, seq)


def test_encoder_requires_exact_command_configuration():
    with pytest.raises(ProtocolError):
        encode_request(Opcode.SEND, 0)
    with pytest.raises(ProtocolError):
        encode_request(Opcode.PING, 0, FrameConfig())
    with pytest.raises(ProtocolError):
        encode_request(99, 0)


def test_led_and_info_requests_carry_one_argument_byte():
    from arty_frame_studio.protocol import PacketDecoder, ProtocolError, led_argument

    led = PacketDecoder().feed(encode_request(Opcode.LED, 3, argument=led_argument(0b1010)))[0]
    assert led.opcode == 5 and led.payload == b"\x8a"
    assert PacketDecoder().feed(encode_request(Opcode.LED, 4, argument=0))[0].payload == b"\x00"
    info = PacketDecoder().feed(encode_request(Opcode.INFO, 5, argument=5))[0]
    assert info.opcode == 6 and info.payload == b"\x05"
    for opcode, argument in ((Opcode.LED, 0x10), (Opcode.INFO, 6), (Opcode.INFO, None)):
        with pytest.raises(ProtocolError):
            encode_request(opcode, 1, argument=argument)
    with pytest.raises(ProtocolError):
        encode_request(Opcode.PING, 1, argument=0)
    with pytest.raises(ProtocolError):
        led_argument(16)


def test_info_pages_assemble_core_clock_and_build_identity():
    from arty_frame_studio.protocol import info_from_pages

    info = info_from_pages([2, 0xC200, 0x0BEB, 3, 0, 0])
    assert info.core_hz == 200_000_000 and info.build_id == 0 and info.led_test
    with pytest.raises(ValueError):
        info_from_pages([2, 0, 0])


def test_continuous_send_encodes_repeat_zero_and_is_a_declared_capability():
    from arty_frame_studio.protocol import CAPABILITY_CONTINUOUS, info_from_pages

    config = FrameConfig(word=1, bit_count=1, divider=1, latch_ticks=1, gap_ticks=0, repeat_count=0)
    request = encode_request(Opcode.SEND, 3, config)
    # repeat_count is the uint16 at payload offset 11, just before the flags.
    assert request[6 + 11 : 6 + 13] == b"\x00\x00"
    assert CAPABILITY_CONTINUOUS == 0x0004
    assert info_from_pages([3, 0xC200, 0x0BEB, 7, 0, 0]).continuous
    assert not info_from_pages([2, 0xC200, 0x0BEB, 3, 0, 0]).continuous


def test_free_clock_sets_flag_bit_two_and_capability():
    from arty_frame_studio.protocol import CAPABILITY_FREE_CLOCK, info_from_pages

    config = FrameConfig(
        divider=20, latch_ticks=20, gap_ticks=40, latch_active_low=True, free_clock=True
    )
    assert encode_request(Opcode.SEND, 1, config)[6 + 13] == 0b110
    assert CAPABILITY_FREE_CLOCK == 0x0008
    assert info_from_pages([4, 0xC200, 0x0BEB, 15, 0, 0]).free_clock
    assert not info_from_pages([3, 0xC200, 0x0BEB, 7, 0, 0]).free_clock
