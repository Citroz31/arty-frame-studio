"""Independent IEEE 1149.1 TAP / FTDI MPSSE wire model; no hardware access."""

import ctypes

import pytest

from arty_frame_studio import windows_jtag as jtag

# The two destinations in each row correspond to TMS=0 and TMS=1.
TAP = {
    "reset": ("idle", "reset"),
    "idle": ("idle", "select_dr"),
    "select_dr": ("capture_dr", "select_ir"),
    "capture_dr": ("shift_dr", "exit1_dr"),
    "shift_dr": ("shift_dr", "exit1_dr"),
    "exit1_dr": ("pause_dr", "update_dr"),
    "pause_dr": ("pause_dr", "exit2_dr"),
    "exit2_dr": ("shift_dr", "update_dr"),
    "update_dr": ("idle", "select_dr"),
    "select_ir": ("capture_ir", "reset"),
    "capture_ir": ("shift_ir", "exit1_ir"),
    "shift_ir": ("shift_ir", "exit1_ir"),
    "exit1_ir": ("pause_ir", "update_ir"),
    "pause_ir": ("pause_ir", "exit2_ir"),
    "exit2_ir": ("shift_ir", "update_ir"),
    "update_ir": ("idle", "select_dr"),
}


class Tap:
    def __init__(self, identifier=0x03631093, status=0x00004010):
        self.state = "reset"
        self.instruction = 0x09  # Xilinx 7-series IDCODE after Test-Logic-Reset.
        self.identifier = identifier
        self.status = status
        self.done = True
        self.ir = 0
        self.dr = []
        self.shifted = []
        self.instructions = []
        self.dr_transactions = []

    def clock(self, tms, tdi):
        tdo = 0
        if self.state == "capture_ir":
            # Mandatory low bits 01, plus Xilinx INIT_COMPLETE and DONE.
            self.ir = 0x11 | (int(self.done) << 5)
        elif self.state == "shift_ir":
            tdo = self.ir & 1
            self.ir = (self.ir >> 1) | (tdi << 5)
        elif self.state == "capture_dr":
            if self.instruction == 0x09:
                self.dr = [(self.identifier >> index) & 1 for index in range(32)]
            elif self.instruction == 0x04:  # CFG_OUT serializes config words MSB first.
                self.dr = [(self.status >> index) & 1 for index in range(31, -1, -1)]
            else:
                self.dr = [0]
            self.shifted = []
        elif self.state == "shift_dr":
            tdo = self.dr.pop(0)
            self.dr.append(tdi)
            self.shifted.append(tdi)
        self.state = TAP[self.state][tms]
        if self.state == "reset":
            self.instruction = 0x09
        elif self.state == "update_ir":
            self.instruction = self.ir
            self.instructions.append(self.instruction)
            if self.instruction == 0x0B:
                self.done = False
            elif self.instruction == 0x0C:
                self.done = True
        elif self.state == "update_dr":
            self.dr_transactions.append((self.instruction, self.shifted.copy()))
        return tdo


class MpsseWire:
    """Decode MPSSE flags, optionally gating JTAG with the upstream Digilent profile.

    The GPIO requirements model openFPGALoader's Arty cable configuration, not
    a separately verified electrical schematic for every Arty board revision.
    """

    def __init__(self, tap=None, digilent=False):
        self.tap = tap or Tap()
        self.digilent = digilent
        self.commands = bytearray()
        self.rx = bytearray()
        self.tms = 1
        self.tdi = 0
        self.low_value = 0
        self.low_direction = 0
        self.high_value = 0
        self.high_direction = 0

    def _clock(self):
        # Upstream Digilent setup enables the low-bank buffers and selects
        # the JTAG paths through the high-bank mux controls. Without it this
        # model leaves TDO pulled high and does not deliver clocks to the TAP.
        ready = (
            self.low_value & 0xA0 == 0xA0
            and self.low_direction & 0xAB == 0xAB
            and self.high_value & 0x60 == 0
            and self.high_direction & 0x60 == 0x60
        )
        if self.digilent and not ready:
            return 1
        return self.tap.clock(self.tms, self.tdi)

    def feed(self, data):
        self.commands.extend(data)
        while self.commands:
            opcode = self.commands[0]
            if opcode == 0xAA:
                self.rx.extend((0xFA, 0xAA))
                del self.commands[0]
            elif opcode in (0x85, 0x87, 0x8A, 0x8D, 0x97):
                del self.commands[0]
            elif opcode in (0x80, 0x82, 0x86):
                if len(self.commands) < 3:
                    return
                if opcode == 0x80:
                    value, direction = self.commands[1:3]
                    assert not direction & 0x04, "TDO must remain an input"
                    self.low_value, self.low_direction = value, direction
                    self.tms, self.tdi = (value >> 3) & 1, (value >> 1) & 1
                elif opcode == 0x82:
                    self.high_value, self.high_direction = self.commands[1:3]
                del self.commands[:3]
            elif opcode in (0x8E, 0x8F):
                header = 2 if opcode == 0x8E else 3
                if len(self.commands) < header:
                    return
                count = int.from_bytes(self.commands[1:header], "little") + 1
                for _ in range(count * (1 if opcode == 0x8E else 8)):
                    self._clock()
                del self.commands[:header]
            else:
                if not self._shift(opcode):
                    return

    def _shift(self, opcode):
        assert opcode < 0x80 and opcode & 0x70, f"Unknown MPSSE opcode {opcode:#x}"
        bits_mode, lsb = bool(opcode & 0x02), bool(opcode & 0x08)
        write_tdi, read_tdo, write_tms = (
            bool(opcode & 0x10),
            bool(opcode & 0x20),
            bool(opcode & 0x40),
        )
        header = 2 if bits_mode else 3
        if len(self.commands) < header:
            return False
        count = int.from_bytes(self.commands[1:header], "little") + 1
        body = (1 if bits_mode else count) if write_tdi or write_tms else 0
        if len(self.commands) < header + body:
            return False
        outgoing = self.commands[header : header + body]
        if write_tdi or write_tms:
            assert opcode & 0x01, "TDI/TMS must change on the falling clock edge"
        if read_tdo:
            assert not opcode & 0x04, "TDO must be sampled on the rising clock edge"
        bit_count = count if bits_mode else count * 8
        if write_tms:
            assert bits_mode and lsb and bit_count <= 7
        received = 0
        for index in range(bit_count):
            if write_tms:
                self.tms, self.tdi = (outgoing[0] >> index) & 1, outgoing[0] >> 7
            elif write_tdi:
                position = index % 8 if lsb else 7 - index % 8
                self.tdi = (outgoing[index // 8] >> position) & 1
            value = self._clock()
            # Partial LSB reads occupy high bits; partial MSB reads occupy low bits.
            received = (received >> 1) | (value << 7) if lsb else ((received << 1) | value)
            if read_tdo and ((index + 1) % 8 == 0 or index + 1 == bit_count):
                self.rx.append(received & 0xFF)
                received = 0
        del self.commands[: header + body]
        return True


def put_dword(pointer, value):
    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = value


class WireDriver:
    def __init__(self, tap=None):
        self.wire = MpsseWire(tap, digilent=True)
        self.closed = False

    def FT_CreateDeviceInfoList(self, count):
        put_dword(count, 1)
        return 0

    def FT_GetDeviceInfoDetail(
        self, index, flags, kind, identifier, location, serial, desc, handle
    ):
        put_dword(kind, 6)
        put_dword(identifier, 0x04036010)
        serial.value, desc.value = b"ARTY123A", b"Digilent USB Device A"
        return 0

    def FT_OpenEx(self, serial, flags, handle):
        ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p))[0] = 1234
        return 0

    def FT_Write(self, handle, pointer, length, written):
        self.wire.feed(ctypes.string_at(pointer, length))
        put_dword(written, length)
        return 0

    def FT_GetQueueStatus(self, handle, queue):
        put_dword(queue, len(self.wire.rx))
        return 0

    def FT_Read(self, handle, buffer, requested, received):
        data = bytes(self.wire.rx[:requested])
        del self.wire.rx[:requested]
        ctypes.memmove(buffer, data, len(data))
        put_dword(received, len(data))
        return 0

    def FT_Close(self, handle):
        self.closed = True
        return 0

    def __getattr__(self, operation):
        if operation.startswith("FT_"):
            return lambda *arguments: 0
        raise AttributeError(operation)


@pytest.fixture(autouse=True)
def no_usb_wait(monkeypatch):
    monkeypatch.setattr(jtag.time, "sleep", lambda seconds: None)


@pytest.mark.parametrize("identifier", [0x03631093, 0x13631093, 0xF3631093])
def test_probe_against_tap_model(identifier):
    driver = WireDriver(Tap(identifier))
    result = jtag.probe_arty(dll=driver)
    assert result.idcode == identifier
    assert driver.closed
    assert not driver.wire.tap.instructions, "Probe must not shift reconfiguration instructions"
    assert driver.wire.tap.state == "idle"


@pytest.mark.parametrize(
    "old_gpio",
    [
        b"\x80\x08\x0b",  # Original generic FTDI profile.
        b"\x80\xe8\xeb",  # Digilent low bank alone leaves mux pins floating.
        b"\x80\x08\x0b\x82\x00\x60",  # Mux setup alone leaves buffers disabled.
        b"\x80\xe8\xeb\x82\x60\x60",  # High mux controls select another path.
    ],
)
def test_digilent_gpio_gate_reproduces_all_ones_and_restores_idcode(old_gpio):
    wire = MpsseWire(digilent=True)
    # Reset, enter Shift-DR, read 32 bits LSB first, then return to Idle.
    scan = b"\x4b\x05\x1f\x4b\x02\x01\x28\x03\x00\x4b\x02\x03\x87"
    wire.feed(old_gpio + scan)
    assert wire.rx == b"\xff" * 4
    assert wire.tap.state == "reset", "Disabled GPIO path must not clock the FPGA"
    assert not wire.tap.instructions

    wire.rx.clear()
    wire.feed(b"\x80\xe8\xeb\x82\x00\x60" + scan)
    assert int.from_bytes(wire.rx, "little") == 0x03631093
    assert wire.tap.state == "idle"
    assert not wire.tap.instructions, "Read-only scan must never issue JPROGRAM"


def test_mpsse_partial_read_alignment_is_independent_of_backend():
    lsb_wire = MpsseWire()
    lsb_wire.tap.state = "shift_ir"
    lsb_wire.tap.ir = 0b10101
    lsb_wire.tms = 0
    lsb_wire.feed(b"\x2a\x04")
    assert lsb_wire.rx == b"\xa8"  # Five LSB-first samples in bits 7..3.
    msb_wire = MpsseWire()
    msb_wire.tap.state = "shift_dr"
    msb_wire.tap.dr = [1, 0, 1, 0, 1, 0, 1]
    msb_wire.tms = 0
    msb_wire.feed(b"\x22\x06")
    assert msb_wire.rx == b"\x55"  # Seven MSB-first samples in bits 6..0.


def bits_to_bytes(bits):
    assert len(bits) % 8 == 0, "Extra clock entered the CFG_IN data register"
    return bytes(
        sum(bit << (7 - position) for position, bit in enumerate(bits[index : index + 8]))
        for index in range(0, len(bits), 8)
    )


def test_program_stream_crosses_usb_chunk_boundary_without_extra_shift_clock():
    payload = bytes.fromhex("AA995566") + bytes(range(256)) * 257 + b"\xa5\xfe\x00\x01"
    expected_status = 0x02404412
    driver = WireDriver(Tap(status=expected_status))
    result = jtag.program_arty(payload, dll=driver)
    assert result.status == expected_status
    assert driver.closed
    assert driver.wire.tap.state == "idle"
    transactions = driver.wire.tap.dr_transactions
    assert len(transactions[0][1]) == 32, "IDCODE must have exactly 32 Shift-DR clocks"
    config_writes = [
        bits_to_bytes(bits) for instruction, bits in transactions if instruction == 0x05
    ]
    assert config_writes == [
        payload,
        bytes.fromhex("AA995566200000002800E0012000000020000000"),
    ]
    assert driver.wire.tap.instructions == [0x0B, 0x3F, 0x05, 0x0C, 0x3F, 0x05, 0x04]
    assert len(transactions[-1][1]) == 32, "STAT must have exactly 32 Shift-DR clocks"


@pytest.mark.parametrize("status", [0x00004011, 0x0000C010, 0x00014010, 0x00004000, 0x10])
def test_configuration_status_failure_is_not_reported_as_success(status):
    driver = WireDriver(Tap(status=status))
    with pytest.raises(jtag.WindowsJtagError, match="STAT"):
        jtag.program_arty(bytes.fromhex("AA99556620000000"), dll=driver)
    assert driver.closed


def test_wrong_target_never_receives_jprogram():
    driver = WireDriver(Tap(identifier=0x0362D093))
    with pytest.raises(jtag.WindowsJtagError, match="IDCODE"):
        jtag.program_arty(bytes.fromhex("AA99556620000000"), dll=driver)
    assert driver.closed
    assert 0x0B not in driver.wire.tap.instructions


def test_transport_failure_during_cfg_in_resets_tap_and_preserves_error():
    class InterruptedDriver(WireDriver):
        def __init__(self):
            super().__init__()
            self.payload_writes = 0

        def FT_Write(self, handle, pointer, length, written):
            data = ctypes.string_at(pointer, length)
            if data[0] == 0x11:
                self.payload_writes += 1
                if self.payload_writes == 2:
                    put_dword(written, 0)
                    return 4  # FT_IO_ERROR, after one complete payload chunk.
            return super().FT_Write(handle, pointer, length, written)

    driver = InterruptedDriver()
    payload = bytes.fromhex("AA995566") + bytes(range(256)) * 513
    with pytest.raises(jtag.WindowsJtagError, match="FT_Write.*4"):
        jtag.program_arty(payload, dll=driver)
    assert driver.closed
    assert driver.wire.tap.state == "idle"
    assert driver.wire.tap.instructions.count(0x0B) == 1
    assert 0x0C not in driver.wire.tap.instructions, "Interrupted upload must not issue JSTART"
