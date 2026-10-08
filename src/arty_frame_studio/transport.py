"""Liaison USB/UART et carte de démonstration, sans dépendance à Vivado."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .model import REFERENCE_HZ, FrameConfig, check_core_hz
from .protocol import (
    CAPABILITY_CONTINUOUS,
    CAPABILITY_FREE_CLOCK,
    CAPABILITY_INFO,
    CAPABILITY_LED,
    CAPABILITY_TR,
    INFO_PAGES,
    DeviceStatus,
    FirmwareInfo,
    Opcode,
    PacketDecoder,
    ProtocolError,
    StatusCode,
    decode_response,
    encode_request,
    info_from_pages,
    led_argument,
    tr_argument,
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
        if self.received_sample:
            readable = "".join(
                chr(value) if 32 <= value < 127 else "." for value in self.received_sample
            )
            message += (
                f"\nDébut RX (32 octets max.) : {self.received_sample.hex(' ')}"
                f" · ASCII : {readable}\n"
            )
        if opcode == Opcode.PING:
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


# PING, STATUS et INFO ne modifient pas la carte, et répéter un même motif LED ou
# un même niveau de TR donne le même état : une réponse perdue peut être
# redemandée. SEND et STOP ne sont jamais répétés automatiquement.
_REPEATABLE = (Opcode.PING, Opcode.STATUS, Opcode.LED, Opcode.INFO, Opcode.TR)
# Firmware antérieur à INFO : référence à 200 MHz, sans test LED.
LEGACY_FIRMWARE = FirmwareInfo(revision=1, core_hz=REFERENCE_HZ, capabilities=0, build_id=0)
CONNECT_PING_ATTEMPTS = 2
STATUS_ATTEMPTS = 2
# Une réponse plus lente que le délai arrive pendant la requête suivante. Elle
# est reconnue (opcode et séquence d'une requête déjà envoyée dans la session)
# pour mesurer le retard et allonger l'attente des requêtes suivantes ; elle
# n'est jamais acceptée à la place de la réponse attendue.
LATE_REPLY_MARGIN = 1.5
MAX_ADAPTIVE_TIMEOUT = 5.0
_SENT_HISTORY = 32


def _opcode_name(value: int) -> str:
    try:
        return Opcode(value & 0x7F).name
    except ValueError:
        return f"0x{value:02X}"


class SerialDevice:
    """Une requête à la fois, confirmation avec numéro de séquence et opcode.

    PING (à la connexion), STATUS, LED, TR et INFO sont redemandés une fois après
    un délai dépassé ; SEND et STOP ne le sont jamais. ``serial_factory`` reçoit
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
        self._reset_link_statistics()
        # Lu par info() ; SEND refuse une trame calculée pour une autre horloge.
        self.firmware: FirmwareInfo | None = None

    def _reset_link_statistics(self) -> None:
        # Requêtes de la session sans réponse : séquence -> (opcode, instant d'envoi).
        self._sent: dict[int, tuple[int, float]] = {}
        self.reply_delay: float | None = None
        self.late_replies = 0
        # Réponses tardives sorties dès l'envoi de la requête suivante.
        self.released_by_next = 0
        self.foreign_replies = 0
        # Écart de séquence (reçue - attendue) des réponses étrangères.
        self.foreign_offsets: list[int] = []
        self.stale_at_open = 0
        self.board_reset = False
        self.last_round_trip: float | None = None

    @property
    def effective_timeout(self) -> float:
        """Délai par requête, allongé si la session a déjà reçu des réponses tardives."""
        if self.reply_delay is None:
            return self.timeout
        return min(
            MAX_ADAPTIVE_TIMEOUT,
            max(self.timeout, LATE_REPLY_MARGIN * self.reply_delay + 0.2),
        )

    def link_notes(self) -> list[str]:
        """Constats sur la liaison à reporter dans le journal."""
        notes = []
        if self.reply_delay is not None:
            notes.append(
                f"Réponses de la carte en retard : jusqu'à {self.reply_delay * 1000:.0f} ms "
                f"pour {self.timeout * 1000:.0f} ms prévus ({self.late_replies} réponse(s) "
                f"tardive(s)). Attente portée à {self.effective_timeout * 1000:.0f} ms. "
                "Une carte saine répond en quelques millisecondes : fermer les logiciels "
                "qui surveillent les ports série (Keysight Connection Expert, terminaux), "
                "vérifier le Latency Timer du port FTDI (16 ms par défaut) et le câble USB."
            )
        if self.released_by_next and self.released_by_next == self.late_replies:
            notes.append(
                "Chaque réponse tardive est arrivée dès l'envoi de la requête suivante : "
                "la carte semble rendre la réponse précédente avec une requête de retard. "
                "Recharger le firmware vérifié, puis lancer arty-frame diagnose --port "
                "<COM> --timeout 3 et conserver sa sortie."
            )
        if self.stale_at_open:
            notes.append(
                f"{self.stale_at_open} paquet(s) d'une session précédente ignoré(s) à "
                "l'ouverture du port : des réponses étaient restées dans le convertisseur USB."
            )
        offsets = self.foreign_offsets
        if len(offsets) >= 2 and len(set(offsets)) == 1:
            notes.append(
                f"Décalage constant de {offsets[0]} requête(s) : à chaque requête, la carte "
                "renvoie la réponse d'une requête plus ancienne, même d'une session "
                "précédente. Le firmware répond (CRC valide) mais sa file de réponses est "
                "désynchronisée ; ce n'est pas un problème de port COM. Utiliser "
                "« Réinitialiser la carte puis connecter » (cavalier JP2) ou appuyer sur le "
                "bouton RESET rouge de l'Arty (pas PROG), puis reconnecter."
            )
        if self.foreign_replies:
            notes.append(
                f"{self.foreign_replies} réponse(s) sans requête correspondante dans cette "
                "session : reste d'une session précédente ou autre logiciel sur le port."
            )
        return notes

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._serial is not None and bool(getattr(self._serial, "is_open", True))

    def connect(self, *, reset_board: bool = False) -> DeviceStatus:
        """Ouvre le port puis vérifie PING.

        ``reset_board`` envoie une impulsion DTR : avec le cavalier JP2, elle
        réinitialise la logique du FPGA (file de réponses, émission en cours)
        sans recharger le firmware. Sans JP2, elle est sans effet.
        """
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
                self._reset_link_statistics()
                if reset_board:
                    self._serial.dtr = True
                    time.sleep(0.05)
                    self._serial.dtr = False
                    # PLL verrouillée et reset synchronisé bien avant 250 ms.
                    time.sleep(0.25)
                    self.board_reset = True
                if hasattr(self._serial, "reset_input_buffer"):
                    self._serial.reset_input_buffer()
                self._drain_stale(self.open_settle)
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

    def _drain_stale(self, duration: float) -> None:
        """Jette ce qui arrive juste après l'ouverture : réponses d'une session close.

        Le convertisseur USB peut livrer après la purge des réponses arrivées
        pendant que le port était fermé ; elles décaleraient le premier PING.
        """
        deadline = time.monotonic() + duration
        decoder = PacketDecoder()
        while True:
            available = int(getattr(self._serial, "in_waiting", 0))
            if available:
                self.stale_at_open += len(decoder.feed(self._serial.read(min(available, 256))))
            # Borné même si des octets arrivent sans arrêt (autre firmware bavard).
            if time.monotonic() >= deadline:
                break
            if not available:
                time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))

    def ping(self) -> DeviceStatus:
        return self._exchange(Opcode.PING)

    def send(self, config: FrameConfig) -> DeviceStatus:
        firmware = self.firmware or LEGACY_FIRMWARE
        if isinstance(config, FrameConfig):
            check_firmware_accepts(config, firmware)
        return self._exchange(Opcode.SEND, config)

    def led(self, pattern: int | None) -> DeviceStatus:
        """Affiche ``pattern`` (bit 0 = LD4) quelques secondes, ou ``None`` : état."""
        return self._exchange(Opcode.LED, argument=led_argument(pattern), attempts=2)

    def tr(self, level: int) -> DeviceStatus:
        """Fixe la broche TR à 3,3 V (1) ou 0 V (0) ; elle garde ce niveau jusqu'au suivant."""
        tr_argument(level)
        firmware = self.firmware or LEGACY_FIRMWARE
        if not firmware.tr:
            raise TransportError(
                f"Ce firmware (révision {firmware.revision}) n'a pas de broche TR : "
                "charger le firmware révision 5 ou plus (onglet FPGA)."
            )
        return self._exchange(Opcode.TR, argument=level, attempts=2)

    def info(self) -> FirmwareInfo:
        """Lit l'identité du firmware ; DeviceError UNKNOWN_OPCODE avant la révision 2."""
        with self._lock:
            words = [
                self._exchange(Opcode.INFO, argument=page, attempts=2).completed
                for page in range(INFO_PAGES)
            ]
            info = info_from_pages(words)
            if not info.capabilities & CAPABILITY_INFO:
                raise TransportError("Réponse INFO incohérente : capacité INFO absente.")
            try:
                check_core_hz(info.core_hz)
            except ValueError as exc:
                raise TransportError(f"Horloge de cœur annoncée invalide : {exc}") from exc
            self.firmware = info
            return info

    def identify(self) -> FirmwareInfo:
        """INFO si le firmware le connaît, sinon l'identité du firmware historique."""
        try:
            return self.info()
        except DeviceError as exc:
            if exc.device_status.status != StatusCode.UNKNOWN_OPCODE:
                raise
            self.firmware = LEGACY_FIRMWARE
            return LEGACY_FIRMWARE

    def stop(self) -> DeviceStatus:
        return self._exchange(Opcode.STOP)

    def status(self) -> DeviceStatus:
        return self._exchange(Opcode.STATUS, attempts=STATUS_ATTEMPTS)

    def close(self) -> None:
        with self._lock:
            self.firmware = None
            connection, self._serial = self._serial, None
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    # L'état local doit rester fermé même si le périphérique a disparu.
                    pass

    def _exchange(
        self,
        opcode: Opcode,
        config: FrameConfig | None = None,
        *,
        argument: int | None = None,
        attempts: int = 1,
    ) -> DeviceStatus:
        if type(attempts) is not int or attempts < 1:
            raise ValueError("Le nombre de tentatives doit être un entier positif.")
        if attempts > 1 and opcode not in _REPEATABLE:
            raise ValueError(
                "Seuls PING, STATUS, LED et INFO peuvent être redemandés automatiquement."
            )
        with self._lock:
            timeouts: list[CommandTimeout] = []
            for attempt in range(attempts):
                try:
                    return self._transaction(opcode, config, argument)
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

    def _transaction(
        self, opcode: Opcode, config: FrameConfig | None, argument: int | None
    ) -> DeviceStatus:
        with self._lock:
            if not self.connected:
                raise TransportError("La carte n'est pas connectée.")
            sequence = self._sequence
            request = encode_request(opcode, sequence, config, argument=argument)
            self._sequence = (sequence + 1) & 0xFF
            crc_errors_before = self._decoder.crc_errors
            length_errors_before = self._decoder.length_errors
            unmatched_sample: list[str] = []
            late_sample: list[str] = []
            timeout = self.effective_timeout
            received_bytes = 0
            received_sample = bytearray()
            try:
                written = self._serial.write(request)
                if written != len(request):
                    raise TransportError(
                        "La commande UART n'a pas été transmise entièrement."
                        " Vérifiez l'état de la carte avant de la renvoyer."
                    )
                sent_at = time.monotonic()
                self._sent.pop(sequence, None)
                self._sent[sequence] = (int(opcode), sent_at)
                while len(self._sent) > _SENT_HISTORY:
                    self._sent.pop(next(iter(self._sent)))
                deadline = sent_at + timeout
                while time.monotonic() < deadline:
                    available = int(getattr(self._serial, "in_waiting", 0))
                    data = self._serial.read(max(1, min(available, 256)))
                    received_bytes += len(data)
                    received_sample.extend(data[: max(0, 32 - len(received_sample))])
                    for packet in self._decoder.feed(data):
                        if packet.sequence != sequence or packet.opcode != (0x80 | opcode):
                            now = time.monotonic()
                            earlier = self._sent.get(packet.sequence)
                            if (
                                packet.sequence != sequence
                                and earlier is not None
                                and packet.opcode == 0x80 | earlier[0]
                            ):
                                # Réponse d'une requête précédente de cette session.
                                delay = now - earlier[1]
                                del self._sent[packet.sequence]
                                self.late_replies += 1
                                if now - sent_at < 0.03:
                                    self.released_by_next += 1
                                self.reply_delay = max(self.reply_delay or 0.0, delay)
                                # La réponse attendue sera sans doute aussi tardive.
                                deadline = max(deadline, sent_at + self.effective_timeout)
                                if len(late_sample) < 4:
                                    late_sample.append(
                                        f"{_opcode_name(packet.opcode)} séquence "
                                        f"{packet.sequence} après {delay * 1000:.0f} ms "
                                        f"({(now - sent_at) * 1000:.0f} ms après l'envoi de "
                                        f"{opcode.name} séquence {sequence})"
                                    )
                                continue
                            self.foreign_replies += 1
                            self.foreign_offsets.append((packet.sequence - sequence) & 0xFF)
                            if len(unmatched_sample) < 4:
                                unmatched_sample.append(
                                    f"op 0x{packet.opcode:02X} "
                                    f"({_opcode_name(packet.opcode)}), "
                                    f"séquence {packet.sequence}"
                                )
                            continue
                        self._sent.pop(sequence, None)
                        self.last_round_trip = time.monotonic() - sent_at
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
                # Un port peut rester is_open après débranchement ou erreur
                # du pilote. Fermer l'objet évite d'annoncer une liaison active.
                self.close()
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
            if self._decoder.length_errors > length_errors_before:
                details += " Des paquets avec une longueur invalide ont été ignorés."
            if late_sample:
                details += (
                    " Réponse(s) tardive(s) à une requête précédente, ignorée(s) : "
                    f"{' ; '.join(late_sample)}. La carte répond, mais au-delà du délai."
                )
            if unmatched_sample:
                details += (
                    " Des réponses ne correspondant pas à la commande ni à une requête en"
                    f" attente de cette session ont été ignorées : {' ; '.join(unmatched_sample)}."
                )
            raise CommandTimeout(
                opcode,
                sequence,
                details,
                received_bytes=received_bytes,
                received_sample=bytes(received_sample),
            )


def check_firmware_accepts(config: FrameConfig, firmware: FirmwareInfo) -> None:
    """Refuse localement une trame que ce firmware exécuterait autrement."""
    if config.core_hz != firmware.core_hz:
        raise ValueError(
            f"Trame calculée pour un cœur à {config.core_hz / 1e6:g} MHz ; le firmware "
            f"connecté fonctionne à {firmware.core_hz / 1e6:g} MHz. Recalculer la trame."
        )
    if config.continuous and not firmware.continuous:
        # Une révision 2 refuserait repeat_count 0 (paramètres invalides) ;
        # le message indique la cause plutôt qu'un refus générique.
        raise ValueError(
            f"Le firmware connecté (révision {firmware.revision}) ne gère pas l'émission "
            "continue. Charger le firmware fourni (révision 3 ou plus) ou choisir un "
            "nombre de répétitions."
        )
    if config.free_clock and not firmware.free_clock:
        # Une révision 3 refuse le bit 2 des flags (paramètres invalides).
        raise ValueError(
            f"Le firmware connecté (révision {firmware.revision}) ne gère pas la CLK "
            "libre. Charger le firmware fourni (révision 4 ou plus) ou désactiver "
            "CLK libre."
        )


class DemoDevice:
    """Simulation des commandes selon le temps monotone et les durées idéales.

    Le nombre de trames terminées inclut le latch et l'intervalle. Les temps ne
    sont pas ralentis : une émission courte peut finir avant le prochain poll.
    La démo simule un firmware de révision 6 à l'horloge ``core_hz`` ; le motif
    des LED virtuelles est exposé par ``led_pattern`` (``None`` : état) et le
    niveau de la broche TR par ``tr_level`` (0 V au départ). Une
    émission continue reste active jusqu'à STOP, son compteur modulo 65 536.
    """

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, core_hz: int = REFERENCE_HZ
    ) -> None:
        self.firmware = FirmwareInfo(
            revision=6,
            core_hz=core_hz,
            capabilities=(
                CAPABILITY_LED
                | CAPABILITY_INFO
                | CAPABILITY_CONTINUOUS
                | CAPABILITY_FREE_CLOCK
                | CAPABILITY_TR
            ),
            build_id=0,
        )
        self.led_pattern: int | None = None
        self.tr_level = 0
        self.tr_history: list[int] = []
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

    def connect(self, *, reset_board: bool = False) -> DeviceStatus:
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
            if config.core_hz != self.firmware.core_hz:
                raise ValueError(
                    f"Trame calculée pour un cœur à {config.core_hz / 1e6:g} MHz ; la démo "
                    f"simule {self.firmware.core_hz / 1e6:g} MHz. Recalculer la trame."
                )
            check_firmware_accepts(config, self.firmware)
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

    def led(self, pattern: int | None) -> DeviceStatus:
        with self._lock:
            self._require_connected()
            led_argument(pattern)
            self.led_pattern = pattern
            return self._snapshot()

    def tr(self, level: int) -> DeviceStatus:
        with self._lock:
            self._require_connected()
            self.tr_level = tr_argument(level)
            self.tr_history.append(level)
            return self._snapshot()

    def info(self) -> FirmwareInfo:
        with self._lock:
            self._require_connected()
            return self.firmware

    def identify(self) -> FirmwareInfo:
        return self.info()

    def close(self) -> None:
        with self._lock:
            self.led_pattern = None
            self.tr_level = 0  # la carte remet TR à 0 V à la réinitialisation
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
        if self._config.continuous:
            # Comme le firmware : pas de fin, compteur 16 bits qui reboucle.
            self._completed = int(elapsed / duration) & 0xFFFF
            return
        if elapsed >= duration * self._config.repeat_count:
            self._completed = self._config.repeat_count
            self._busy = False
            self._started_at = None
            return
        self._completed = min(self._config.repeat_count, int(elapsed / duration))

    def _snapshot(self) -> DeviceStatus:
        self._update()
        return DeviceStatus(StatusCode.OK, self._busy, self._completed)


@dataclass(frozen=True)
class LedTestResult:
    commands: int
    mean_ms: float
    max_ms: float


# Chenillard LD4 → LD7, toutes allumées, toutes éteintes, puis retour à l'état.
LED_TEST_SEQUENCE: tuple[int, ...] = (0b0001, 0b0010, 0b0100, 0b1000) * 2 + (0b1111, 0b0000)


def run_led_test(
    device: SerialDevice | DemoDevice,
    *,
    step: float = 0.25,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_step: Callable[[int | None], None] | None = None,
) -> LedTestResult:
    """Fait défiler un motif visible sur LD4..LD7 et mesure chaque aller-retour UART.

    Chaque motif est une commande confirmée : l'œil vérifie le chemin PC → FPGA →
    LED, la réponse vérifie le retour. Les LED reviennent toujours à l'état.
    """
    info = device.firmware if device.firmware is not None else device.identify()
    if not info.led_test:
        raise TransportError(
            "Ce firmware ne connaît pas la commande LED (révision 1). Charger le firmware "
            "fourni à jour ; PING suffit à tester la liaison avec l'ancien firmware."
        )
    pause = sleep or time.sleep
    durations = []
    try:
        for pattern in LED_TEST_SEQUENCE:
            started = clock()
            device.led(pattern)
            durations.append(clock() - started)
            if on_step:
                on_step(pattern)
            pause(step)
    finally:
        try:
            device.led(None)
        finally:
            if on_step:
                on_step(None)
    return LedTestResult(
        len(durations), 1000 * sum(durations) / len(durations), 1000 * max(durations)
    )
