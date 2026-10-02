"""Accès JTAG Windows via le pilote FTDI D2XX déjà installé.

Le diagnostic lit uniquement l'IDCODE. Il ne charge aucun firmware. Le pilote
FTDI et sa DLL restent des composants externes, distribués sous leur licence.
Les tests utilisent une DLL simulée ; ils ne remplacent pas un essai matériel.
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_FT2232H = 6
_DEVICE_ID = 0x04036010
_ARTY_IDCODE = 0x03631093
_REVISION_MASK = 0x0FFFFFFF


class WindowsJtagError(RuntimeError):
    """Le pilote, l'interface JTAG ou le FPGA n'a pas répondu correctement."""


@dataclass(frozen=True)
class FtdiDevice:
    serial: str
    description: str
    device_id: int
    location_id: int


@dataclass(frozen=True)
class JtagProbeResult:
    serial: str
    description: str
    idcode: int


@dataclass(frozen=True)
class JtagProgramResult:
    serial: str
    description: str
    idcode: int
    status: int


def _load_dll(dll_path: Path | None, injected: Any) -> Any:
    if injected is not None:
        return injected
    if os.name != "nt":
        raise WindowsJtagError("Cet accès FTDI D2XX est réservé à Windows.")
    if dll_path is not None and not isinstance(dll_path, Path):
        raise ValueError("Le chemin de la DLL doit être un objet Path.")
    name = str(dll_path.resolve()) if dll_path is not None else "ftd2xx.dll"
    try:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise OSError("Chargeur Windows indisponible.")
        library = loader(name)
    except (OSError, AttributeError) as exc:
        raise WindowsJtagError(
            "La DLL FTDI D2XX est inaccessible. Utilisez le pilote FTDI CDM"
            " existant et une DLL de même architecture que Python (32 ou 64 bits)."
            " Vous pouvez fournir son chemin avec --ftdi-dll."
        ) from exc
    declarations = {
        "FT_CreateDeviceInfoList": [ctypes.POINTER(_DWORD)],
        "FT_GetDeviceInfoDetail": [
            _DWORD,
            ctypes.POINTER(_DWORD),
            ctypes.POINTER(_DWORD),
            ctypes.POINTER(_DWORD),
            ctypes.POINTER(_DWORD),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_HANDLE),
        ],
        "FT_OpenEx": [ctypes.c_void_p, _DWORD, ctypes.POINTER(_HANDLE)],
        "FT_Close": [_HANDLE],
        "FT_ResetDevice": [_HANDLE],
        "FT_Purge": [_HANDLE, _DWORD],
        "FT_SetUSBParameters": [_HANDLE, _DWORD, _DWORD],
        "FT_SetTimeouts": [_HANDLE, _DWORD, _DWORD],
        "FT_SetLatencyTimer": [_HANDLE, ctypes.c_ubyte],
        "FT_SetBitMode": [_HANDLE, ctypes.c_ubyte, ctypes.c_ubyte],
        "FT_GetQueueStatus": [_HANDLE, ctypes.POINTER(_DWORD)],
        "FT_Write": [_HANDLE, ctypes.c_void_p, _DWORD, ctypes.POINTER(_DWORD)],
        "FT_Read": [_HANDLE, ctypes.c_void_p, _DWORD, ctypes.POINTER(_DWORD)],
    }
    for operation, arguments in declarations.items():
        try:
            function = getattr(library, operation)
        except AttributeError as exc:
            raise WindowsJtagError(f"La DLL FTDI ne fournit pas {operation}.") from exc
        function.argtypes = arguments
        function.restype = _DWORD
    return library


def _checked(library: Any, operation: str, *arguments: Any) -> None:
    try:
        status = int(getattr(library, operation)(*arguments))
    except (AttributeError, OSError) as exc:
        raise WindowsJtagError(f"Échec FTDI pendant {operation}.") from exc
    if status:
        raise WindowsJtagError(f"{operation} a échoué (code FTDI {status}).")


def list_ftdi_devices(dll_path: Path | None = None, *, dll: Any = None) -> list[FtdiDevice]:
    """Liste uniquement les interfaces A des FT2232H 0403:6010.

    Le canal B est le port UART/COM de l'Arty. Une interface sans indication
    explicite de canal A est ignorée pour éviter de détourner le port série.
    """
    library = _load_dll(dll_path, dll)
    count = _DWORD()
    _checked(library, "FT_CreateDeviceInfoList", ctypes.byref(count))
    if count.value > 256:
        raise WindowsJtagError("Le pilote a retourné un nombre de périphériques incohérent.")
    devices = []
    for index in range(count.value):
        flags, kind, identifier, location = (_DWORD() for _ in range(4))
        serial = ctypes.create_string_buffer(16)
        description = ctypes.create_string_buffer(64)
        handle = _HANDLE()
        _checked(
            library,
            "FT_GetDeviceInfoDetail",
            index,
            ctypes.byref(flags),
            ctypes.byref(kind),
            ctypes.byref(identifier),
            ctypes.byref(location),
            serial,
            description,
            ctypes.byref(handle),
        )
        if kind.value != _FT2232H or identifier.value != _DEVICE_ID:
            continue
        serial_text = serial.value.decode("ascii", errors="replace").strip()
        description_text = description.value.decode("ascii", errors="replace").strip()
        serial_channel = serial_text[-1:].upper()
        description_channel = description_text.rsplit(" ", 1)[-1].upper()
        if serial_channel == "B" or description_channel == "B":
            continue
        if serial_channel != "A" and description_channel != "A":
            continue
        if not serial_text or not serial_text.isascii():
            continue
        devices.append(FtdiDevice(serial_text, description_text, identifier.value, location.value))
    return devices


def _select(library: Any, serial: str | None) -> FtdiDevice:
    if serial is not None and (not isinstance(serial, str) or not serial or "\0" in serial):
        raise ValueError("Choisissez le numéro de série complet du canal JTAG A.")
    devices = list_ftdi_devices(dll=library)
    selected = (
        devices if serial is None else [device for device in devices if device.serial == serial]
    )
    if not selected:
        raise WindowsJtagError(
            "Aucune interface FT2232H JTAG A correspondante. Le port COM appartient"
            " au canal B : branchez l'Arty par son connecteur USB PROG/UART."
        )
    if len(selected) != 1:
        raise WindowsJtagError(
            "Plusieurs interfaces JTAG A sont présentes. Précisez leur numéro de série."
        )
    return selected[0]


class _Mpsse:
    def __init__(self, library: Any, handle: _HANDLE) -> None:
        self.library = library
        self.handle = handle
        self.initialized = False

    def write(self, commands: bytes) -> None:
        buffer = ctypes.create_string_buffer(commands)
        offset = 0
        while offset < len(commands):
            written = _DWORD()
            _checked(
                self.library,
                "FT_Write",
                self.handle,
                ctypes.byref(buffer, offset),
                len(commands) - offset,
                ctypes.byref(written),
            )
            if not 0 < written.value <= len(commands) - offset:
                raise WindowsJtagError("Le pilote FTDI n'a pas écrit les commandes JTAG.")
            offset += written.value

    def read(self, length: int, timeout: float = 1.0) -> bytes:
        deadline = time.monotonic() + timeout
        output = bytearray()
        while len(output) < length:
            if time.monotonic() >= deadline:
                raise WindowsJtagError("Délai dépassé pendant la lecture JTAG FTDI.")
            queued = _DWORD()
            _checked(self.library, "FT_GetQueueStatus", self.handle, ctypes.byref(queued))
            if queued.value == 0:
                time.sleep(0.001)
                continue
            if queued.value > 65536:
                raise WindowsJtagError("Le pilote FTDI a retourné une file de lecture incohérente.")
            requested = min(queued.value, length - len(output))
            buffer = ctypes.create_string_buffer(requested)
            read = _DWORD()
            _checked(
                self.library,
                "FT_Read",
                self.handle,
                buffer,
                requested,
                ctypes.byref(read),
            )
            if read.value > requested:
                raise WindowsJtagError("Le pilote FTDI a retourné une longueur incohérente.")
            output.extend(buffer.raw[: read.value])
        return bytes(output)

    def initialize(self) -> None:
        for operation, arguments in (
            ("FT_ResetDevice", ()),
            ("FT_Purge", (3,)),
            ("FT_SetUSBParameters", (65536, 65536)),
            ("FT_SetTimeouts", (100, 1000)),
            ("FT_SetLatencyTimer", (2,)),
            ("FT_SetBitMode", (0, 0)),
        ):
            _checked(self.library, operation, self.handle, *arguments)
        time.sleep(0.02)
        _checked(self.library, "FT_SetBitMode", self.handle, 0x0B, 2)
        time.sleep(0.02)
        self.write(b"\xaa\x87")
        if self.read(2) != b"\xfa\xaa":
            raise WindowsJtagError("Le canal FTDI n'a pas confirmé son mode MPSSE.")
        # 60 MHz / (2 * (29 + 1)) = 1 MHz ; TCK/TDI/TMS sorties, TDO entrée.
        self.write(b"\x8a\x97\x8d\x85\x80\x08\x0b\x86\x1d\x00")
        self.initialized = True

    def reset_idle(self) -> None:
        # Cinq TMS=1 imposent Test-Logic-Reset ; TMS=0 rejoint Run-Test/Idle.
        self.write(b"\x4b\x05\x1f")

    def idcode(self) -> int:
        self.reset_idle()
        # RTI -> Select-DR -> Capture-DR -> Shift-DR (TMS=1,0,0).
        # Le reset TAP sélectionne IDCODE sur les FPGA Xilinx 7 series.
        # 24 + 7 bits, puis le 32e bit avec TMS=1 pour quitter Shift-DR.
        self.write(b"\x4b\x02\x01\x28\x02\x00\x2a\x06\x6b\x00\x01\x4b\x01\x01\x87")
        data = self.read(5)
        identifier = int.from_bytes(data[:3], "little")
        identifier |= (data[3] >> 1) << 24
        identifier |= (data[4] >> 7) << 31
        if identifier & _REVISION_MASK != _ARTY_IDCODE:
            raise WindowsJtagError(
                f"IDCODE JTAG 0x{identifier:08X} : un XC7A100T était attendu"
                f" (0x{_ARTY_IDCODE:08X}, révision ignorée)."
            )
        return identifier

    def instruction(self, instruction: int, *, capture: bool = False) -> int:
        # IR Xilinx 7 series = six bits, premier bit LSB. Le sixième sort d'IR.
        last = ((instruction >> 5) & 1) << 7 | 1
        commands = b"\x4b\x03\x03"
        commands += bytes((0x3B if capture else 0x1B, 4, instruction & 0x1F))
        commands += bytes((0x6B if capture else 0x4B, 0, last))
        commands += b"\x4b\x01\x01"
        if capture:
            commands += b"\x87"
        self.write(commands)
        if capture:
            data = self.read(2)
            return (data[0] >> 3) | ((data[1] >> 7) << 5)
        return 0

    def idle_clocks(self, count: int) -> None:
        # 0x8F : octets de huit TCK, sans modification TDI/TMS (RTI).
        whole, remainder = divmod(count, 8)
        while whole:
            chunk = min(whole, 65536)
            self.write(b"\x8f" + (chunk - 1).to_bytes(2, "little"))
            whole -= chunk
        if remainder:
            self.write(bytes((0x8E, remainder - 1)))

    def data_write_msb(self, payload: bytes) -> None:
        self.write(b"\x4b\x02\x01")
        # Écriture TDI sur front descendant : stable avant l'échantillonnage
        # Xilinx sur front montant. Les octets de configuration sont MSB-first.
        remaining = memoryview(payload)[:-1]
        for offset in range(0, len(remaining), 65536):
            chunk = remaining[offset : offset + 65536]
            self.write(b"\x11" + (len(chunk) - 1).to_bytes(2, "little") + chunk.tobytes())
        last_byte = payload[-1]
        self.write(
            bytes((0x13, 6, last_byte & 0xFE, 0x4B, 0, ((last_byte & 1) << 7) | 1))
            + b"\x4b\x01\x01"
        )

    def data_read_msb(self) -> int:
        self.write(b"\x4b\x02\x01\x20\x02\x00\x22\x06\x6b\x00\x01\x4b\x01\x01\x87")
        data = self.read(5)
        return (int.from_bytes(data[:3], "big") << 8) | ((data[3] & 0x7F) << 1) | (data[4] >> 7)

    def program(self, payload: bytes) -> int:
        self.instruction(0x0B)  # JPROGRAM : effacement SRAM, sans accès flash.
        deadline = time.monotonic() + 5.0
        while True:
            capture = self.instruction(0x3F, capture=True)  # BYPASS/Capture-IR.
            if capture & 0x03 != 0x01:
                raise WindowsJtagError("La chaîne JTAG a retourné une Capture-IR incohérente.")
            if capture & 0x10:
                break
            if time.monotonic() >= deadline:
                raise WindowsJtagError("Le FPGA n'a pas terminé son initialisation après JPROGRAM.")
            time.sleep(0.01)
        self.idle_clocks(120000)
        self.instruction(0x05)  # CFG_IN.
        self.data_write_msb(payload)
        self.instruction(0x0C)  # JSTART.
        self.idle_clocks(2000)
        self.reset_idle()
        capture = self.instruction(0x3F, capture=True)
        if capture & 0x33 != 0x31:
            raise WindowsJtagError("Le FPGA n'a pas confirmé DONE/INIT après le chargement SRAM.")
        self.instruction(0x05)
        # Synchronisation, NOOP, lecture type-1 du registre STAT, deux NOOP.
        self.data_write_msb(bytes.fromhex("AA995566200000002800E0012000000020000000"))
        self.instruction(0x04)  # CFG_OUT.
        status = self.data_read_msb()
        if status & ((1 << 0) | (1 << 15) | (1 << 16)):
            raise WindowsJtagError(
                f"Le FPGA signale une erreur de configuration (STAT=0x{status:08X})."
            )
        if status & ((1 << 14) | (1 << 4)) != ((1 << 14) | (1 << 4)):
            raise WindowsJtagError(f"DONE/EOS non confirmés (STAT=0x{status:08X}).")
        self.reset_idle()
        return status


def _cleanup(
    library: Any, handle: _HANDLE, primary_failure: bool, engine: _Mpsse | None = None
) -> None:
    if primary_failure and engine is not None and engine.initialized:
        try:
            engine.reset_idle()
        except WindowsJtagError:
            # Une erreur de liaison peut aussi empêcher le reset TAP. L'erreur
            # initiale reste prioritaire ; reset BitMode et fermeture suivent.
            pass
    failure = None
    for operation, arguments in (("FT_SetBitMode", (0, 0)), ("FT_Close", ())):
        try:
            _checked(library, operation, handle, *arguments)
        except WindowsJtagError as exc:
            failure = failure or exc
    if failure is not None and not primary_failure:
        raise failure


def probe_arty(
    serial: str | None = None, dll_path: Path | None = None, *, dll: Any = None
) -> JtagProbeResult:
    """Lit l'IDCODE du XC7A100T, sans instruction de reconfiguration.

    Une réponse correcte prouve l'accès à la chaîne JTAG. Elle ne prouve pas
    que le firmware UART de cette application est chargé dans le FPGA.
    """
    library = _load_dll(dll_path, dll)
    device = _select(library, serial)
    handle = _HANDLE()
    _checked(
        library,
        "FT_OpenEx",
        ctypes.c_char_p(device.serial.encode("ascii")),
        1,
        ctypes.byref(handle),
    )
    failed = True
    engine = _Mpsse(library, handle)
    try:
        engine.initialize()
        identifier = engine.idcode()
        engine.reset_idle()
        failed = False
        return JtagProbeResult(device.serial, device.description, identifier)
    finally:
        _cleanup(library, handle, failed, engine)


def program_arty(
    payload: bytes,
    serial: str | None = None,
    dll_path: Path | None = None,
    *,
    dll: Any = None,
) -> JtagProgramResult:
    """Charge en SRAM le payload d'un .bit préalablement validé par l'appelant.

    Cette opération remplace le circuit actif et ne programme pas la flash.
    L'IDCODE doit correspondre au XC7A100T avant JPROGRAM. Une panne après
    l'effacement laisse potentiellement le FPGA sans circuit utilisable ; aucun
    rechargement implicite ni aucune nouvelle tentative automatique n'a lieu.
    """
    if not isinstance(payload, bytes) or not 4 <= len(payload) <= 20 * 1024 * 1024:
        raise ValueError("Le payload FPGA doit contenir entre 4 octets et 20 Mio.")
    if b"\xaa\x99\x55\x66" not in payload:
        raise ValueError("Le payload FPGA ne contient pas le mot de synchronisation Xilinx.")
    library = _load_dll(dll_path, dll)
    device = _select(library, serial)
    handle = _HANDLE()
    _checked(
        library,
        "FT_OpenEx",
        ctypes.c_char_p(device.serial.encode("ascii")),
        1,
        ctypes.byref(handle),
    )
    failed = True
    engine = _Mpsse(library, handle)
    try:
        engine.initialize()
        identifier = engine.idcode()
        status = engine.program(payload)
        failed = False
        return JtagProgramResult(device.serial, device.description, identifier, status)
    finally:
        _cleanup(library, handle, failed, engine)
