"""Traces D2XX simulées : aucun périphérique Windows n'est utilisé."""

import ctypes
import os
from types import SimpleNamespace

import pytest

from arty_frame_studio import windows_jtag as jtag


def put_dword(pointer, value):
    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = value


class FakeD2xx:
    def __init__(self, *, idcode=0x03631093, synchronize=True, status=0x4010):
        self.devices = [
            ("ARTY123A", "Digilent USB Device A", 6, 0x04036010),
            ("ARTY123B", "Digilent USB Device B", 6, 0x04036010),
        ]
        self.idcode = idcode
        self.configuration_status = status
        self.captures = [0x11, 0x31]
        self.synchronize = synchronize
        self.pending = bytearray()
        self.commands = []
        self.calls = []
        self.failures = {}

    def status(self, name, *arguments):
        self.calls.append((name, arguments))
        return self.failures.get(name, 0)

    def FT_CreateDeviceInfoList(self, count):
        put_dword(count, len(self.devices))
        return self.status("FT_CreateDeviceInfoList")

    def FT_GetDeviceInfoDetail(
        self, index, flags, kind, identifier, location, serial, desc, handle
    ):
        device_serial, description, device_kind, device_id = self.devices[index]
        put_dword(flags, 0)
        put_dword(kind, device_kind)
        put_dword(identifier, device_id)
        put_dword(location, index + 10)
        serial.value = device_serial.encode("ascii")
        desc.value = description.encode("ascii")
        return self.status("FT_GetDeviceInfoDetail", index)

    def FT_OpenEx(self, serial, mode, handle):
        ctypes.cast(handle, ctypes.POINTER(ctypes.c_void_p))[0] = 1234
        return self.status("FT_OpenEx", serial.value.decode("ascii"), mode)

    def FT_Write(self, handle, pointer, length, written):
        data = ctypes.string_at(pointer, length)
        self.commands.append(data)
        put_dword(written, length)
        if data == b"\xaa\x87" and self.synchronize:
            self.pending.extend(b"\xfa\xaa")
        if b"\x28\x02\x00\x2a\x06" in data:
            self.pending.extend(self.idcode.to_bytes(4, "little")[:3])
            self.pending.extend(((self.idcode >> 24 & 0x7F) << 1, self.idcode >> 31 << 7))
        if b"\x3b\x04" in data:
            capture = self.captures.pop(0) if len(self.captures) > 1 else self.captures[0]
            self.pending.extend(((capture & 0x1F) << 3, (capture >> 5) << 7))
        if b"\x20\x02\x00\x22\x06" in data:
            self.pending.extend(self.configuration_status.to_bytes(4, "big")[:3])
            self.pending.extend(
                ((self.configuration_status & 0xFF) >> 1, (self.configuration_status & 1) << 7)
            )
        return self.status("FT_Write")

    def FT_GetQueueStatus(self, handle, queue):
        put_dword(queue, len(self.pending))
        return self.status("FT_GetQueueStatus")

    def FT_Read(self, handle, buffer, requested, received):
        data = bytes(self.pending[:requested])
        del self.pending[:requested]
        ctypes.memmove(buffer, data, len(data))
        put_dword(received, len(data))
        return self.status("FT_Read")

    def __getattr__(self, operation):
        if operation.startswith("FT_"):
            return lambda handle, *arguments: self.status(operation, *arguments)
        raise AttributeError(operation)


@pytest.fixture(autouse=True)
def skip_device_wait(monkeypatch):
    monkeypatch.setattr(jtag.time, "sleep", lambda seconds: None)


def test_enumeration_rejects_uart_and_wrong_device_types():
    driver = FakeD2xx()
    driver.devices.extend(
        [
            ("OTHERAA", "Other A", 8, 0x04036010),
            ("WRONGAA", "Other A", 6, 0x04036011),
            ("UNKNOWN", "Unidentified", 6, 0x04036010),
            ("BAD123A", "Conflicting B", 6, 0x04036010),
        ]
    )
    assert jtag.list_ftdi_devices(dll=driver) == [
        jtag.FtdiDevice("ARTY123A", "Digilent USB Device A", 0x04036010, 10)
    ]
    assert not any(name == "FT_OpenEx" for name, _ in driver.calls)


def test_probe_trace_reads_idcode_only_and_closes():
    driver = FakeD2xx()
    result = jtag.probe_arty(dll=driver)
    assert result == jtag.JtagProbeResult("ARTY123A", "Digilent USB Device A", 0x03631093)
    assert ("FT_OpenEx", ("ARTY123A", 1)) in driver.calls
    assert driver.commands == [
        b"\xaa\x87",
        b"\x8a\x97\x8d\x85\x80\xe8\xeb\x82\x00\x60\x86\x1d\x00",
        b"\x4b\x05\x1f",
        b"\x4b\x02\x01\x28\x02\x00\x2a\x06\x6b\x00\x01\x4b\x01\x01\x87",
        b"\x4b\x05\x1f",
    ]
    assert driver.calls[-2:] == [("FT_SetBitMode", (0, 0)), ("FT_Close", ())]


@pytest.mark.parametrize("idcode", [0, 0xFFFFFFFF])
def test_constant_tdo_is_reported_as_a_jtag_path_problem(idcode):
    with pytest.raises(jtag.WindowsJtagError, match="lecture TDO est constante") as failure:
        jtag.probe_arty(dll=FakeD2xx(idcode=idcode))
    assert "USB PROG/UART" in str(failure.value)
    assert "profil Digilent Arty" in str(failure.value)
    assert "ARTY123A" in str(failure.value)


def test_revision_is_ignored():
    assert jtag.probe_arty(dll=FakeD2xx(idcode=0x13631093)).idcode == 0x13631093


@pytest.mark.parametrize("idcode", [0, 0xFFFFFFFF, 0x0362D093])
def test_incorrect_fpga_still_closes(idcode):
    driver = FakeD2xx(idcode=idcode)
    with pytest.raises(jtag.WindowsJtagError, match="IDCODE"):
        jtag.probe_arty(dll=driver)
    assert driver.calls[-2:] == [("FT_SetBitMode", (0, 0)), ("FT_Close", ())]


def test_timeout_is_bounded_and_still_closes(monkeypatch):
    ticks = iter(index * 0.2 for index in range(100))
    monkeypatch.setattr(jtag.time, "monotonic", lambda: next(ticks))
    driver = FakeD2xx(synchronize=False)
    with pytest.raises(jtag.WindowsJtagError, match="Délai dépassé"):
        jtag.probe_arty(dll=driver)
    assert driver.calls[-1] == ("FT_Close", ())


def test_device_ambiguity_requires_serial_and_never_opens_uart():
    driver = FakeD2xx()
    driver.devices.append(("OTHER123A", "Other A", 6, 0x04036010))
    with pytest.raises(jtag.WindowsJtagError, match="Plusieurs"):
        jtag.probe_arty(dll=driver)
    with pytest.raises(jtag.WindowsJtagError, match="Aucune interface"):
        jtag.probe_arty(serial="ARTY123B", dll=driver)
    assert not any(name == "FT_OpenEx" for name, _ in driver.calls)
    assert jtag.probe_arty(serial="OTHER123A", dll=driver).serial == "OTHER123A"


def test_primary_failure_is_preserved_when_cleanup_fails():
    driver = FakeD2xx(idcode=0)
    driver.failures["FT_Close"] = 1
    with pytest.raises(jtag.WindowsJtagError, match="IDCODE"):
        jtag.probe_arty(dll=driver)
    assert driver.calls[-1] == ("FT_Close", ())


def test_cleanup_failure_after_success_is_reported():
    driver = FakeD2xx()
    driver.failures["FT_Close"] = 1
    with pytest.raises(jtag.WindowsJtagError, match="FT_Close"):
        jtag.probe_arty(dll=driver)


def test_non_windows_without_injection_is_rejected():
    if os.name == "nt":
        pytest.skip("Validation spécifique à l'environnement de tests non Windows.")
    with pytest.raises(jtag.WindowsJtagError, match="réservé à Windows"):
        jtag.list_ftdi_devices()


def test_failed_open_does_not_close_an_unopened_device():
    driver = FakeD2xx()
    driver.failures["FT_OpenEx"] = 2
    with pytest.raises(jtag.WindowsJtagError, match="FT_OpenEx"):
        jtag.probe_arty(dll=driver)
    assert driver.calls[-1][0] == "FT_OpenEx"


def test_sram_program_trace_and_status():
    driver = FakeD2xx()
    payload = bytes.fromhex("FFFFFFFFAA99556620000000")
    result = jtag.program_arty(payload, dll=driver)
    assert result == jtag.JtagProgramResult("ARTY123A", "Digilent USB Device A", 0x03631093, 0x4010)

    def ir_write(value):
        return (
            b"\x4b\x03\x03\x1b\x04"
            + bytes((value & 0x1F, 0x4B, 0, ((value >> 5) << 7) | 1))
            + b"\x4b\x01\x01"
        )

    instructions = [command for command in driver.commands if b"\x1b\x04" in command]
    assert instructions == [ir_write(value) for value in (0x0B, 0x05, 0x0C, 0x05, 0x04)]
    assert b"\x8f\x97\x3a" in driver.commands  # 15000 octets = 120000 TCK.
    assert b"\x8f\xf9\x00" in driver.commands  # 250 octets = 2000 TCK.
    assert b"\x11\x0a\x00" + payload[:-1] in driver.commands
    assert bytes((0x13, 6, payload[-1] & 0xFE, 0x4B, 0, 1)) + b"\x4b\x01\x01" in driver.commands
    assert driver.calls[-2:] == [("FT_SetBitMode", (0, 0)), ("FT_Close", ())]


def test_wrong_idcode_is_rejected_before_jprogram():
    driver = FakeD2xx(idcode=0x0362D093)
    with pytest.raises(jtag.WindowsJtagError, match="IDCODE"):
        jtag.program_arty(bytes.fromhex("AA995566"), dll=driver)
    assert not any(b"\x1b\x04\x0b" in command for command in driver.commands)


@pytest.mark.parametrize("status", [0x4011, 0xC010, 0x14010])
def test_program_rejects_configuration_error_status(status):
    driver = FakeD2xx(status=status)
    with pytest.raises(jtag.WindowsJtagError, match="erreur de configuration"):
        jtag.program_arty(bytes.fromhex("AA995566"), dll=driver)
    assert driver.calls[-1] == ("FT_Close", ())


@pytest.mark.parametrize("status", [0, 0x4000, 0x10])
def test_program_requires_done_and_eos(status):
    with pytest.raises(jtag.WindowsJtagError, match="DONE/EOS"):
        jtag.program_arty(bytes.fromhex("AA995566"), dll=FakeD2xx(status=status))


@pytest.mark.parametrize("capture", [0, 0x21, 0x11])
def test_program_requires_valid_done_capture(capture):
    driver = FakeD2xx()
    driver.captures = [0x11, capture]
    with pytest.raises(jtag.WindowsJtagError, match="DONE/INIT"):
        jtag.program_arty(bytes.fromhex("AA995566"), dll=driver)
    assert driver.calls[-1] == ("FT_Close", ())


def test_init_timeout_is_bounded(monkeypatch):
    ticks = iter(index * 0.5 for index in range(100))
    monkeypatch.setattr(jtag.time, "monotonic", lambda: next(ticks))
    driver = FakeD2xx()
    driver.captures = [0x01]
    with pytest.raises(jtag.WindowsJtagError, match="initialisation après JPROGRAM"):
        jtag.program_arty(bytes.fromhex("AA995566"), dll=driver)
    assert driver.calls[-1] == ("FT_Close", ())


@pytest.mark.parametrize("payload", [b"", b"ABCD", bytearray.fromhex("AA995566")])
def test_invalid_payload_is_rejected_before_loading_driver(payload):
    driver = FakeD2xx()
    with pytest.raises(ValueError):
        jtag.program_arty(payload, dll=driver)
    assert not driver.calls


def test_payload_is_chunked_with_exact_final_msb_bits():
    driver = FakeD2xx()
    payload = bytes.fromhex("AA995566") + b"\xff" * 65534
    jtag.program_arty(payload, dll=driver)
    chunks = [command for command in driver.commands if command[:1] == b"\x11"]
    assert chunks[0][:3] == b"\x11\xff\xff"
    assert chunks[0][3:] == payload[:65536]
    assert chunks[1] == b"\x11\x00\x00\xff"
    assert b"\x13\x06\xfe\x4b\x00\x81\x4b\x01\x01" in driver.commands


def test_loading_default_dll_binds_d2xx_abi(monkeypatch):
    operations = (
        "FT_CreateDeviceInfoList",
        "FT_GetDeviceInfoDetail",
        "FT_OpenEx",
        "FT_Close",
        "FT_ResetDevice",
        "FT_Purge",
        "FT_SetUSBParameters",
        "FT_SetTimeouts",
        "FT_SetLatencyTimer",
        "FT_SetBitMode",
        "FT_GetQueueStatus",
        "FT_Write",
        "FT_Read",
    )
    library = SimpleNamespace(**dict.fromkeys(operations))
    for operation in operations:
        setattr(library, operation, lambda *arguments: 0)
    loaded = []

    def load(name):
        loaded.append(name)
        return library

    monkeypatch.setattr(jtag, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(jtag.ctypes, "WinDLL", load, raising=False)
    assert jtag._load_dll(None, None) is library
    assert loaded == ["ftd2xx.dll"]
    assert library.FT_OpenEx.argtypes == [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    assert all(getattr(library, operation).restype == ctypes.c_uint32 for operation in operations)


def test_absent_dll_has_actionable_message(monkeypatch):
    def load(name):
        raise OSError("absent")

    monkeypatch.setattr(jtag, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(jtag.ctypes, "WinDLL", load, raising=False)
    with pytest.raises(jtag.WindowsJtagError, match="même architecture que Python"):
        jtag._load_dll(None, None)


def test_missing_d2xx_export_is_reported(monkeypatch):
    monkeypatch.setattr(jtag, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(jtag.ctypes, "WinDLL", lambda name: SimpleNamespace(), raising=False)
    with pytest.raises(jtag.WindowsJtagError, match="FT_CreateDeviceInfoList"):
        jtag._load_dll(None, None)


def test_program_failure_resets_tap_before_closing():
    driver = FakeD2xx(status=0)
    with pytest.raises(jtag.WindowsJtagError, match="DONE/EOS"):
        jtag.program_arty(bytes.fromhex("AA995566"), dll=driver)
    assert driver.commands[-1] == b"\x4b\x05\x1f"
    assert driver.calls[-2:] == [("FT_SetBitMode", (0, 0)), ("FT_Close", ())]


def test_failed_recovery_write_preserves_configuration_failure():
    driver = FakeD2xx(status=0)
    normal_write = driver.FT_Write

    def write(handle, pointer, length, written):
        command = ctypes.string_at(pointer, length)
        if command == b"\x4b\x05\x1f" and b"\x20\x02\x00" in b"".join(driver.commands):
            driver.failures["FT_Write"] = 4
        return normal_write(handle, pointer, length, written)

    driver.FT_Write = write
    with pytest.raises(jtag.WindowsJtagError, match="DONE/EOS"):
        jtag.program_arty(bytes.fromhex("AA995566"), dll=driver)
    assert driver.calls[-1] == ("FT_Close", ())
