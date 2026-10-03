"""Liaison USB/UART et carte de démonstration, sans dépendance à Vivado."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .model import FrameConfig
from .protocol import (
    DeviceStatus,
    Opcode,
    PacketDecoder,
    ProtocolError,
    StatusCode,
    decode_response,
    encode_request,
)


class TransportError(RuntimeError):
    """Erreur de connexion, de réponse ou d'exécution sur la carte."""


class DeviceError(TransportError):
    def __init__(self, device_status: DeviceStatus) -> None:
        self.device_status = device_status
        super().__init__(f"La carte a rejeté la commande : {device_status.status.message}.")


class CommandTimeout(TransportError):
    def __init__(
        self,
        opcode: Opcode,
        sequence: int,
        details: str = "",
        *,
        received_bytes: int = 0,
        received_sample: bytes = b"",
    ) -> None:
        self.opcode = opcode
        self.sequence = sequence
        self.details = details
        self.received_bytes = received_bytes
        self.received_sample = received_sample[:32]
        message = f"Délai de réponse dépassé pour {opcode.name} (séquence {sequence})."
        if details:
            message += f" {details}"
        if opcode == Opcode.PING:
            if self.received_sample:
                readable = "".join(
                    chr(value) if 32 <= value < 127 else "." for value in self.received_sample
                )
                message += (
                    f"\nDébut RX (32 octets max.) : {self.received_sample.hex(' ')}"
                    f" · ASCII : {readable}\n"
                )
            message += (
                " Le port USB/UART a été ouvert, mais aucune confirmation compatible"
                " n'a été reçue. Un port COM détecté ne suffit pas : le bitstream"
                " UART du projet doit être chargé dans le FPGA. Vérifiez aussi le"
                " port choisi, le reset et le verrouillage de l'horloge."
            )
        if opcode == Opcode.SEND:
            message += (
                " La commande peut avoir été exécutée ; consultez l'état de la carte"
                " avant de la renvoyer."
            )
        super().__init__(message)


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str
    hwid: str = ""


def list_ports() -> list[PortInfo]:
    try:
        from serial.tools import list_ports as serial_ports
    except ImportError as exc:
        raise TransportError(
            "PySerial est absent. Installez les dépendances de l'application."
        ) from exc
    try:
        ports = [
            PortInfo(port.device, port.description or port.device, port.hwid or "")
            for port in serial_ports.comports()
        ]
    except Exception as exc:
        raise TransportError(f"Impossible de lister les ports série : {exc}") from exc
    return sorted(ports, key=lambda port: port.device)


# PING et STATUS ne modifient pas la carte : une réponse perdue peut être
# redemandée. SEND et STOP ne sont jamais répétés automatiquement.
_REPEATABLE = (Opcode.PING, Opcode.STATUS)
CONNECT_PING_ATTEMPTS = 2
STATUS_ATTEMPTS = 2


class SerialDevice:
    """Une requête à la fois, confirmation avec numéro de séquence et opcode.

    Seuls PING (à la connexion) et STATUS sont redemandés une fois après un
    délai dépassé ; SEND et STOP ne le sont jamais. ``serial_factory`` reçoit
    ``port=None`` et retourne un port non ouvert, ouvert ensuite par ``open()`` :
    il permet de vérifier le protocole sans matériel.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        *,
        timeout: float = 0.75,
        open_settle: float = 0.05,
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(port, str) or not port.strip() or any(char in port for char in "\r\n\0"):
            raise ValueError("Choisissez un nom de port série valide (COM3, /dev/ttyUSB1…).")
        if type(baudrate) is not int or baudrate <= 0:
            raise ValueError("La vitesse UART doit être un entier positif.")
        if not 0 < timeout <= 60:
            raise ValueError("Le délai UART doit être compris entre 0 et 60 secondes.")
        if not 0 <= open_settle <= 5:
            raise ValueError("L'attente après ouverture doit être comprise entre 0 et 5 secondes.")
        self.port = port.strip()
        self.baudrate = baudrate
        self.timeout = timeout
        self.open_settle = open_settle
        self._serial_factory = serial_factory
        self._serial: Any = None
        self._lock = threading.RLock()
        self._decoder = PacketDecoder()
        self._sequence = 0

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._serial is not None and bool(getattr(self._serial, "is_open", True))

    def connect(self) -> DeviceStatus:
        with self._lock:
            if self.connected:
                return self.ping()
            factory = self._serial_factory
            if factory is None:
                try:
                    import serial
                except ImportError as exc:
                    raise TransportError(
                        "PySerial est absent. Installez les dépendances de l'application."
                    ) from exc
                factory = serial.Serial
            try:
                self._serial = factory(
                    port=None,
                    baudrate=self.baudrate,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    timeout=min(0.02, self.timeout),
                    write_timeout=self.timeout,
                    xonxoff=False,
                    rtscts=False,
                    dsrdtr=False,
                )
                # Le cavalier JP2 de l'Arty A7 relie DTR du FT2232 à ck_rst, le
                # reset du FPGA. pyserial active DTR et RTS à l'ouverture par
                # défaut : les désactiver avant open() évite de réinitialiser
                # la carte (garanti sous Windows ; Linux peut émettre une brève
                # impulsion à l'ouverture, d'où l'attente et le second PING).
                self._serial.dtr = False
                self._serial.rts = False
                self._serial.port = self.port
                self._serial.open()
                self._decoder = PacketDecoder()
                if self.open_settle:
                    time.sleep(self.open_settle)
                if hasattr(self._serial, "reset_input_buffer"):
                    self._serial.reset_input_buffer()
                return self._exchange(Opcode.PING, attempts=CONNECT_PING_ATTEMPTS)
            except Exception as exc:
                self.close()
                if isinstance(exc, TransportError):
                    raise
                raise TransportError(
                    f"Impossible d'ouvrir le port {self.port} : {exc}."
                    " Fermez les autres logiciels utilisant ce port, puis vérifiez"
                    " son nom et le pilote USB série dans le Gestionnaire de périphériques."
                ) from exc

    def ping(self) -> DeviceStatus:
        return self._exchange(Opcode.PING)

    def send(self, config: FrameConfig) -> DeviceStatus:
        return self._exchange(Opcode.SEND, config)

    def stop(self) -> DeviceStatus:
        return self._exchange(Opcode.STOP)

    def status(self) -> DeviceStatus:
        return self._exchange(Opcode.STATUS, attempts=STATUS_ATTEMPTS)

    def close(self) -> None:
        with self._lock:
            connection, self._serial = self._serial, None
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    # L'état local doit rester fermé même si le périphérique a disparu.
                    pass

    def _exchange(
        self, opcode: Opcode, config: FrameConfig | None = None, *, attempts: int = 1
    ) -> DeviceStatus:
        if type(attempts) is not int or attempts < 1:
            raise ValueError("Le nombre de tentatives doit être un entier positif.")
        if attempts > 1 and opcode not in _REPEATABLE:
            raise ValueError("Seuls PING et STATUS peuvent être redemandés automatiquement.")
        with self._lock:
            timeouts: list[CommandTimeout] = []
            for attempt in range(attempts):
                try:
                    return self._transaction(opcode, config)
                except CommandTimeout as exc:
                    if attempts == 1:
                        raise
                    timeouts.append(exc)
                    if attempt == attempts - 1:
                        sample = next(
                            (
                                failure.received_sample
                                for failure in timeouts
                                if failure.received_sample
                            ),
                            b"",
                        )
                        details = " ".join(
                            f"Tentative {index}: {failure.details}"
                            for index, failure in enumerate(timeouts, start=1)
                        )
                        raise CommandTimeout(
                            opcode,
                            exc.sequence,
                            f"{attempts} tentatives sans réponse compatible. {details}",
                            received_bytes=sum(failure.received_bytes for failure in timeouts),
                            received_sample=sample,
                        ) from exc
            raise AssertionError("Une transaction doit réussir ou lever une exception.")

    def _transaction(self, opcode: Opcode, config: FrameConfig | None) -> DeviceStatus:
        with self._lock:
            if not self.connected:
                raise TransportError("La carte n'est pas connectée.")
            sequence = self._sequence
            request = encode_request(opcode, sequence, config)
            self._sequence = (sequence + 1) & 0xFF
            crc_errors_before = self._decoder.crc_errors
            unmatched = 0
            received_bytes = 0
            received_sample = bytearray()
            try:
                written = self._serial.write(request)
                if written != len(request):
                    raise TransportError(
                        "La commande UART n'a pas été transmise entièrement."
                        " Vérifiez l'état de la carte avant de la renvoyer."
                    )
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    available = int(getattr(self._serial, "in_waiting", 0))
                    data = self._serial.read(max(1, min(available, 256)))
                    received_bytes += len(data)
                    if opcode == Opcode.PING:
                        received_sample.extend(data[: max(0, 32 - len(received_sample))])
                    for packet in self._decoder.feed(data):
                        if packet.sequence != sequence or packet.opcode != (0x80 | opcode):
                            unmatched += 1
                            continue
                        try:
                            status = decode_response(packet)
                        except ProtocolError as exc:
                            raise TransportError(str(exc)) from exc
                        if not status.ok:
                            raise DeviceError(status)
                        return status
            except TransportError:
                raise
            except Exception as exc:
                raise TransportError(
                    f"Erreur de liaison UART pendant {opcode.name} : {exc}"
                ) from exc
            details = (
                f"{received_bytes} octet(s) reçus sans réponse compatible."
                if received_bytes
                else "Aucun octet reçu pendant le délai."
            )
            if self._decoder.crc_errors > crc_errors_before:
                details += " Des réponses avec un CRC invalide ont été ignorées."
            elif unmatched:
                details += " Des réponses ne correspondant pas à la commande ont été ignorées."
            raise CommandTimeout(
                opcode,
                sequence,
                details,
                received_bytes=received_bytes,
                received_sample=bytes(received_sample),
            )


class DemoDevice:
    """Simulation des commandes selon le temps monotone et les durées idéales.

    Le nombre de trames terminées inclut le latch et l'intervalle. Les temps ne
    sont pas ralentis : une émission courte peut finir avant le prochain poll.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._connected = False
        self._busy = False
        self._completed = 0
        self._config: FrameConfig | None = None
        self._started_at: float | None = None

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def connect(self) -> DeviceStatus:
        with self._lock:
            self._connected = True
            return self._snapshot()

    def ping(self) -> DeviceStatus:
        with self._lock:
            self._require_connected()
            return self._snapshot()

    def send(self, config: FrameConfig) -> DeviceStatus:
        with self._lock:
            self._require_connected()
            if not isinstance(config, FrameConfig):
                raise ValueError("La configuration de trame est invalide.")
            self._update()
            if self._busy:
                raise DeviceError(DeviceStatus(StatusCode.BUSY, True, self._completed))
            self._config = config
            self._started_at = self._clock()
            self._completed = 0
            self._busy = True
            return self._snapshot()

    def stop(self) -> DeviceStatus:
        with self._lock:
            self._require_connected()
            self._update()
            self._busy = False
            self._started_at = None
            return self._snapshot()

    def status(self) -> DeviceStatus:
        return self.ping()

    def close(self) -> None:
        with self._lock:
            self._update()
            self._busy = False
            self._started_at = None
            self._connected = False

    def _require_connected(self) -> None:
        if not self._connected:
            raise TransportError("La carte de démonstration n'est pas connectée.")

    def _update(self) -> None:
        if not self._busy or self._config is None or self._started_at is None:
            return
        elapsed = max(0.0, self._clock() - self._started_at)
        duration = self._config.frame_duration_ns / 1_000_000_000
        if elapsed >= duration * self._config.repeat_count:
            self._completed = self._config.repeat_count
            self._busy = False
            self._started_at = None
            return
        self._completed = min(self._config.repeat_count, int(elapsed / duration))

    def _snapshot(self) -> DeviceStatus:
        self._update()
        return DeviceStatus(StatusCode.OK, self._busy, self._completed)
