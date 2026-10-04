import binascii
import struct
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from arty_frame_studio.model import FrameConfig
from arty_frame_studio.protocol import Opcode, PacketDecoder, StatusCode
from arty_frame_studio.transport import (
    CONNECT_PING_ATTEMPTS,
    CommandTimeout,
    DemoDevice,
    DeviceError,
    SerialDevice,
    TransportError,
)


def response(packet, *, opcode=None, sequence=None, status=0, busy=0, completed=0):
    payload = struct.pack("<BBH", status, busy, completed)
    body = bytes(
        (1, opcode or (0x80 | packet.opcode), packet.sequence if sequence is None else sequence, 4)
    )
    body += payload
    return b"\xa7\x7a" + body + struct.pack("<H", binascii.crc_hqx(body, 0xFFFF))


class FakeSerial:
    def __init__(self, handler=None, **kwargs):
        self.settings = kwargs
        # Comme pyserial : port non ouvert, DTR/RTS actifs tant qu'on ne les change pas.
        self.port = kwargs.get("port")
        self.dtr = True
        self.rts = True
        self.opened_with = None
        self.is_open = False
        self.requests = []
        self._receive = bytearray()
        self.handler = handler or (lambda packet: response(packet))
        self.resets = 0

    @property
    def in_waiting(self):
        return len(self._receive)

    def reset_input_buffer(self):
        self._receive.clear()
        self.resets += 1

    def write(self, data):
        packet = PacketDecoder().feed(data)[0]
        self.requests.append(packet)
        self._receive.extend(self.handler(packet))
        return len(data)

    def read(self, count):
        if not self._receive:
            time.sleep(0.0005)
            return b""
        chunk = bytes(self._receive[: min(count, 3)])
        del self._receive[: len(chunk)]
        return chunk

    def open(self):
        self.opened_with = {"port": self.port, "dtr": self.dtr, "rts": self.rts}
        self.is_open = True

    def close(self):
        self.is_open = False


def make_serial(handler=None, timeout=0.015):
    endpoint = FakeSerial(handler)

    def factory(**kwargs):
        endpoint.settings = kwargs
        return endpoint

    device = SerialDevice("/dev/ttyUSB1", timeout=timeout, open_settle=0, serial_factory=factory)
    return device, endpoint


def test_connection_verifies_ping_and_full_serial_settings():
    device, endpoint = make_serial()
    status = device.connect()
    assert status.ok and device.connected
    assert [p.opcode for p in endpoint.requests] == [Opcode.PING]
    # JP2 relie DTR à ck_rst : DTR/RTS doivent être inactifs avant l'ouverture.
    assert endpoint.settings["port"] is None
    assert endpoint.opened_with == {"port": "/dev/ttyUSB1", "dtr": False, "rts": False}
    assert endpoint.settings["baudrate"] == 115200
    assert endpoint.settings["bytesize"] == 8
    assert endpoint.settings["parity"] == "N"
    assert endpoint.settings["stopbits"] == 1
    assert not endpoint.settings["xonxoff"]
    assert not endpoint.settings["rtscts"]
    assert not endpoint.settings["dsrdtr"]
    assert endpoint.resets == 1
    device.close()
    assert not device.connected and not endpoint.is_open
    with pytest.raises(TransportError, match="pas connectée"):
        device.status()


def test_connection_does_not_accept_an_unresponsive_port():
    device, endpoint = make_serial(lambda packet: b"")
    with pytest.raises(CommandTimeout, match="PING") as error:
        device.connect()
    assert error.value.received_bytes == 0
    assert f"{CONNECT_PING_ATTEMPTS} tentatives" in str(error.value)
    assert [p.opcode for p in endpoint.requests] == [Opcode.PING] * CONNECT_PING_ATTEMPTS
    assert "Le port USB/UART a été ouvert" in str(error.value)
    assert "bitstream" in str(error.value)
    assert not device.connected and not endpoint.is_open


def test_unrelated_uart_text_is_reported_as_data_without_a_valid_ping():
    text = b"Arty factory demo\r\n"
    device, endpoint = make_serial(lambda packet: text)
    with pytest.raises(CommandTimeout) as error:
        device.connect()
    assert error.value.received_bytes == len(text) * CONNECT_PING_ATTEMPTS
    assert error.value.received_sample == text
    assert "ASCII : Arty factory demo.." in str(error.value)
    assert "Aucun octet reçu" not in str(error.value)
    assert [p.opcode for p in endpoint.requests] == [Opcode.PING] * CONNECT_PING_ATTEMPTS
    assert not endpoint.is_open


def test_ping_timeout_preview_is_bounded_and_never_emits_raw_controls():
    raw = b"\x1b[31m\x00" + b"X" * 458
    device, endpoint = make_serial(lambda packet: raw, timeout=0.05)
    with pytest.raises(CommandTimeout) as error:
        device.connect()
    assert error.value.received_bytes == len(raw) * CONNECT_PING_ATTEMPTS
    assert error.value.received_sample == raw[:32]
    assert "1b 5b 33 31 6d 00" in str(error.value)
    assert "\x1b" not in str(error.value)
    assert "\x00" not in str(error.value)
    assert "ASCII : .[31m." in str(error.value)
    assert [p.opcode for p in endpoint.requests] == [Opcode.PING] * CONNECT_PING_ATTEMPTS
    assert not endpoint.is_open


def test_windows_port_name_is_trimmed_before_opening():
    endpoint = FakeSerial()

    def factory(**settings):
        endpoint.settings = settings
        return endpoint

    device = SerialDevice(" COM7 ", open_settle=0, serial_factory=factory)
    try:
        assert device.connect().ok
        assert device.port == "COM7"
        assert endpoint.opened_with == {"port": "COM7", "dtr": False, "rts": False}
    finally:
        device.close()


def test_connection_open_error_is_french_and_keeps_disconnected():
    def unavailable(**kwargs):
        raise OSError("port unavailable")

    device = SerialDevice("COM42", serial_factory=unavailable)
    with pytest.raises(TransportError, match="Impossible d'ouvrir le port COM42"):
        device.connect()
    assert not device.connected


def test_send_ack_and_device_busy_error():
    def handler(packet):
        if packet.opcode == Opcode.SEND:
            return response(packet, status=3, busy=1, completed=5)
        return response(packet)

    device, endpoint = make_serial(handler)
    device.connect()
    with pytest.raises(DeviceError, match="déjà en cours") as error:
        device.send(FrameConfig())
    assert error.value.device_status.status == StatusCode.BUSY
    assert error.value.device_status.completed == 5
    assert len([p for p in endpoint.requests if p.opcode == Opcode.SEND]) == 1
    assert device.connected


def test_send_status_ping_and_stop_sequence_while_busy():
    state = {"busy": False, "completed": 0}

    def handler(packet):
        if packet.opcode == Opcode.SEND:
            assert len(packet.payload) == 14
            state["busy"] = True
        elif packet.opcode == Opcode.STOP:
            state["busy"] = False
        return response(packet, busy=int(state["busy"]), completed=state["completed"])

    device, endpoint = make_serial(handler)
    device.connect()
    assert device.send(FrameConfig()).busy
    state["completed"] = 4
    assert device.ping().busy
    assert device.status().completed == 4
    stopped = device.stop()
    assert not stopped.busy and stopped.completed == 4
    assert [p.opcode for p in endpoint.requests] == [1, 2, 1, 4, 3]


def test_stale_sequences_wrong_opcodes_and_bad_crc_are_ignored():
    def handler(packet):
        bad_crc = bytearray(response(packet))
        bad_crc[-1] ^= 1
        return (
            response(packet, sequence=(packet.sequence + 1) % 256, completed=41)
            + response(packet, opcode=0x84 if packet.opcode == Opcode.PING else 0x81, completed=42)
            + bad_crc
            + response(packet, completed=7)
        )

    device, _ = make_serial(handler)
    assert device.connect().completed == 7
    assert device.status().completed == 7


def test_unmatched_ack_is_never_accepted():
    device, _ = make_serial(
        lambda p: response(p) if p.opcode == Opcode.PING else response(p, sequence=0)
    )
    device.connect()
    with pytest.raises(CommandTimeout, match="ne correspondant pas"):
        device.status()


def test_send_timeout_is_not_retried_and_explains_unknown_execution():
    def handler(packet):
        return response(packet) if packet.opcode == Opcode.PING else b""

    device, endpoint = make_serial(handler)
    device.connect()
    with pytest.raises(CommandTimeout, match="peut avoir été exécutée") as error:
        device.send(FrameConfig())
    assert error.value.opcode == Opcode.SEND
    assert len([p for p in endpoint.requests if p.opcode == Opcode.SEND]) == 1


def test_crc_corruption_timeout_reports_cause():
    def handler(packet):
        result = bytearray(response(packet))
        if packet.opcode != Opcode.PING:
            result[-1] ^= 1
        return result

    device, _ = make_serial(handler)
    device.connect()
    with pytest.raises(CommandTimeout, match="CRC invalide"):
        device.status()


def test_sequence_rollover_and_concurrent_requests_remain_matched():
    device, endpoint = make_serial()
    device.connect()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: device.status(), range(260)))
    assert all(result.ok for result in results)
    assert [p.sequence for p in endpoint.requests] == [index % 256 for index in range(261)]


def test_connection_repeats_a_lost_ping_once():
    def handler(packet):
        return b"" if len(endpoint.requests) == 1 else response(packet)

    device, endpoint = make_serial(handler)
    assert device.connect().ok
    assert [p.opcode for p in endpoint.requests] == [Opcode.PING, Opcode.PING]
    assert [p.sequence for p in endpoint.requests] == [0, 1]


def test_status_is_repeated_once_but_stop_and_send_never_are():
    lost = {Opcode.STATUS: 1, Opcode.STOP: 99, Opcode.SEND: 99}

    def handler(packet):
        if lost.get(packet.opcode, 0):
            lost[packet.opcode] -= 1
            return b""
        return response(packet, completed=3)

    device, endpoint = make_serial(handler)
    device.connect()
    assert device.status().completed == 3
    with pytest.raises(CommandTimeout):
        device.stop()
    with pytest.raises(CommandTimeout):
        device.send(FrameConfig())
    assert [p.opcode for p in endpoint.requests] == [1, 4, 4, 3, 2]
    with pytest.raises(ValueError, match="PING, STATUS, LED et INFO"):
        device._exchange(Opcode.SEND, FrameConfig(), attempts=2)


def test_open_settle_is_bounded():
    with pytest.raises(ValueError, match="attente"):
        SerialDevice("COM7", open_settle=-1)
    assert SerialDevice("COM7").open_settle > 0


def test_failed_retry_preserves_first_attempt_uart_diagnostics():
    text = b"\x1b[2JArty factory demo\r\n"

    def handler(packet):
        return text if len(endpoint.requests) == 1 else b""

    device, endpoint = make_serial(handler)
    with pytest.raises(CommandTimeout) as error:
        device.connect()
    assert error.value.received_bytes == len(text)
    assert error.value.received_sample == text
    assert "Arty factory demo" in str(error.value)
    assert [p.opcode for p in endpoint.requests] == [Opcode.PING, Opcode.PING]
    assert not endpoint.is_open


def test_partial_write_is_reported_without_retry():
    device, endpoint = make_serial()
    device.connect()
    endpoint.write = lambda data: len(data) - 1
    with pytest.raises(TransportError, match="pas été transmise entièrement"):
        device.send(FrameConfig())


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_demo_requires_connection():
    demo = DemoDevice()
    with pytest.raises(TransportError, match="pas connectée"):
        demo.send(FrameConfig())
    demo.connect()
    assert demo.connected
    demo.close()
    assert not demo.connected


def test_demo_progress_stop_and_restart_obey_whole_frame_timing():
    clock = Clock()
    demo = DemoDevice(clock=clock)
    demo.connect()
    config = FrameConfig(
        word=1, bit_count=1, divider=100, latch_ticks=4, gap_ticks=80, repeat_count=3
    )
    duration = config.frame_duration_ns / 1e9
    assert demo.send(config).busy
    with pytest.raises(DeviceError) as error:
        demo.send(config)
    assert error.value.device_status.status == StatusCode.BUSY
    clock.now = duration * 0.9
    assert demo.status().completed == 0
    clock.now = duration * 1.1
    assert demo.status().completed == 1
    assert demo.stop().completed == 1
    clock.now = duration * 5
    assert demo.status().completed == 1
    assert not demo.status().busy
    assert demo.send(config).completed == 0
    clock.now += duration * 3.1
    final = demo.status()
    assert not final.busy and final.completed == 3


def test_demo_gap_remains_busy_and_incomplete_until_end():
    clock = Clock()
    demo = DemoDevice(clock=clock)
    demo.connect()
    config = FrameConfig(
        word=0, bit_count=1, divider=1, latch_ticks=1, gap_ticks=100, repeat_count=1
    )
    demo.send(config)
    clock.now = (3 + 1 + 50) * 2.5e-9
    assert demo.status().busy and demo.status().completed == 0
    clock.now = config.frame_duration_ns / 1e9 * 1.001
    assert not demo.status().busy and demo.status().completed == 1


def info_handler(words, *, revision_known=True):
    def handler(packet):
        if packet.opcode == Opcode.INFO:
            if not revision_known:
                return response(packet, status=1)
            return response(packet, completed=words[packet.payload[0]])
        return response(packet)

    return handler


def test_identify_reads_six_info_pages_and_guards_send_core_clock():
    words = [2, 150_000_000 & 0xFFFF, 150_000_000 >> 16, 3, 0x1E2D, 0xA5C3]
    device, endpoint = make_serial(info_handler(words))
    device.connect()
    info = device.identify()
    assert info.revision == 2 and info.core_hz == 150_000_000
    assert info.build_id == 0xA5C31E2D and info.led_test
    assert [p.payload for p in endpoint.requests if p.opcode == Opcode.INFO] == [
        bytes((page,)) for page in range(6)
    ]
    with pytest.raises(ValueError, match="Recalculer"):
        device.send(FrameConfig())
    assert device.send(FrameConfig(core_hz=150_000_000)).ok
    device.close()
    assert device.firmware is None


def test_legacy_firmware_is_identified_without_led_test():
    device, endpoint = make_serial(info_handler([], revision_known=False))
    device.connect()
    info = device.identify()
    assert info.revision == 1 and info.core_hz == 200_000_000 and not info.led_test
    assert device.send(FrameConfig()).ok
    assert len([p for p in endpoint.requests if p.opcode == Opcode.INFO]) == 1


def test_led_command_encodes_manual_pattern_and_automatic_mode():
    lost = {"count": 1}

    def handler(packet):
        if packet.opcode == Opcode.LED and lost["count"]:
            lost["count"] -= 1
            return b""
        return response(packet)

    device, endpoint = make_serial(handler)
    device.connect()
    assert device.led(0b0101).ok
    assert device.led(None).ok
    led = [p.payload for p in endpoint.requests if p.opcode == Opcode.LED]
    # The first LED reply is lost: the same idempotent pattern is sent again.
    assert led == [b"\x85", b"\x85", b"\x00"]
    with pytest.raises(ValueError):
        device.led(16)


def test_demo_simulates_revision_two_firmware_and_virtual_leds():
    demo = DemoDevice(core_hz=100_000_000)
    demo.connect()
    assert demo.identify().core_hz == 100_000_000
    demo.led(0b1000)
    assert demo.led_pattern == 0b1000
    demo.led(None)
    assert demo.led_pattern is None
    with pytest.raises(ValueError, match="Recalculer"):
        demo.send(FrameConfig())
    assert demo.send(FrameConfig(core_hz=100_000_000)).busy


def test_led_test_walks_the_pattern_and_always_restores_status():
    from arty_frame_studio.transport import LED_TEST_SEQUENCE, run_led_test

    demo = DemoDevice()
    demo.connect()
    seen = []
    result = run_led_test(demo, sleep=lambda _: None, on_step=seen.append)
    assert seen == [*LED_TEST_SEQUENCE, None]
    assert result.commands == len(LED_TEST_SEQUENCE) and demo.led_pattern is None

    class Failing(DemoDevice):
        def led(self, pattern):
            if pattern == 0b0100:
                raise TransportError("lost")
            return super().led(pattern)

    broken = Failing()
    broken.connect()
    with pytest.raises(TransportError, match="lost"):
        run_led_test(broken, sleep=lambda _: None)
    assert broken.led_pattern is None


def test_led_test_refuses_legacy_firmware_without_sending_led():
    from arty_frame_studio.transport import run_led_test

    device, endpoint = make_serial(info_handler([], revision_known=False))
    device.connect()
    with pytest.raises(TransportError, match="révision 1"):
        run_led_test(device, sleep=lambda _: None)
    assert not [p for p in endpoint.requests if p.opcode == Opcode.LED]


def test_continuous_send_requires_the_capability_before_writing():
    continuous = FrameConfig(repeat_count=0)
    revision_two = [2, 200_000_000 & 0xFFFF, 200_000_000 >> 16, 3, 0, 0]
    device, endpoint = make_serial(info_handler(revision_two))
    device.connect()
    device.identify()
    with pytest.raises(ValueError, match="émission continue"):
        device.send(continuous)
    assert not [p for p in endpoint.requests if p.opcode == Opcode.SEND]
    # Without INFO the firmware is treated as legacy: refused as well.
    legacy, legacy_endpoint = make_serial()
    legacy.connect()
    with pytest.raises(ValueError, match="révision 1"):
        legacy.send(continuous)
    assert not [p for p in legacy_endpoint.requests if p.opcode == Opcode.SEND]

    revision_three = [3, 200_000_000 & 0xFFFF, 200_000_000 >> 16, 7, 0, 0]
    device, endpoint = make_serial(info_handler(revision_three))
    device.connect()
    assert device.identify().continuous
    assert device.send(continuous).ok
    send = [p for p in endpoint.requests if p.opcode == Opcode.SEND][0]
    assert send.payload[11:13] == b"\x00\x00"


def test_demo_continuous_emission_runs_until_stop_with_a_wrapping_counter():
    clock = Clock()
    demo = DemoDevice(clock=clock)
    demo.connect()
    assert demo.identify().continuous and demo.identify().revision == 4
    config = FrameConfig(word=1, bit_count=1, divider=1, latch_ticks=1, gap_ticks=0, repeat_count=0)
    duration = config.frame_duration_ns / 1e9
    assert demo.send(config).busy
    clock.now = duration * 70_000.5
    status = demo.status()
    assert status.busy and status.completed == 70_000 % 65_536
    with pytest.raises(DeviceError) as error:
        demo.send(config)
    assert error.value.device_status.status == StatusCode.BUSY
    stopped = demo.stop()
    assert not stopped.busy
    clock.now *= 2
    assert not demo.status().busy and demo.status().completed == stopped.completed


def test_free_clock_requires_revision_four_before_writing():
    free = FrameConfig(divider=20, latch_ticks=20, gap_ticks=40, free_clock=True)
    revision_three = [3, 200_000_000 & 0xFFFF, 200_000_000 >> 16, 7, 0, 0]
    device, endpoint = make_serial(info_handler(revision_three))
    device.connect()
    device.identify()
    with pytest.raises(ValueError, match="CLK libre"):
        device.send(free)
    assert not [p for p in endpoint.requests if p.opcode == Opcode.SEND]
    revision_four = [4, 200_000_000 & 0xFFFF, 200_000_000 >> 16, 15, 0, 0]
    device, endpoint = make_serial(info_handler(revision_four))
    device.connect()
    assert device.identify().free_clock
    assert device.send(free).ok
    assert [p for p in endpoint.requests if p.opcode == Opcode.SEND][0].payload[13] == 4
    demo = DemoDevice()
    demo.connect()
    assert demo.identify().free_clock and demo.send(free).busy
