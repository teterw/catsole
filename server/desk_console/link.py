"""Serial transport to the device: autodetect, reconnect, NDJSON both ways.

The board is permanently tethered, but "permanently" still includes the PC
sleeping, the cable being knocked, and Windows renumbering the COM port. A
disconnect must never propagate an exception into the render loop, so every
failure here degrades to `connected == False` and a reconnect attempt.
"""

from __future__ import annotations

import logging
import threading
import time

import serial
from serial.tools import list_ports

from .protocol import decode_line, encode_frame

log = logging.getLogger(__name__)

# Arduino UNO R4 WiFi, CDC interface. Confirmed on this machine as
# USB\VID_2341&PID_1002.
ARDUINO_VID = 0x2341
ARDUINO_PID = 0x1002

DEFAULT_BAUD = 115200
RECONNECT_DELAY_S = 2.0
READ_TIMEOUT_S = 0.2


def find_port(preferred: str | None = None, ports=None) -> str | None:
    """Locate the device's COM port.

    An explicit preference always wins, so a user with two R4s can pin one.
    Otherwise match on USB VID/PID, then fall back to any generic USB serial
    device — on Windows the R4 enumerates as a bare "USB Serial Device".
    """
    if preferred:
        return preferred

    candidates = list(ports if ports is not None else list_ports.comports())

    for info in candidates:
        if info.vid == ARDUINO_VID and info.pid == ARDUINO_PID:
            return info.device

    for info in candidates:
        description = (info.description or "").lower()
        if "usb serial" in description or "arduino" in description:
            return info.device

    return None


class SerialLink:
    """Full-duplex NDJSON link to the device, with its reader on a thread."""

    def __init__(
        self,
        port: str | None = None,
        baud: int = DEFAULT_BAUD,
        on_event=None,
        on_connect=None,
    ):
        self.preferred_port = port
        self.baud = baud
        self.on_event = on_event
        self.on_connect = on_connect

        self._port = None
        self._thread = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self.last_error = ""
        self.port_name = ""

    @property
    def connected(self) -> bool:
        return self._port is not None and getattr(self._port, "is_open", False)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._reader_loop, name="serial-reader", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._close()

    def send(self, obj: dict) -> bool:
        """Write one frame. Returns False rather than raising when down."""
        port = self._port
        if port is None:
            return False
        try:
            with self._write_lock:
                port.write(encode_frame(obj))
                port.flush()
            return True
        except Exception as exc:
            log.info("serial write failed: %s", exc)
            self.last_error = str(exc)
            # Drop the port so the reader loop reopens it.
            self._close()
            return False

    def _handle_line(self, line: str) -> None:
        event = decode_line(line)
        if event is None:
            return
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:
            # A bad handler must not take the reader thread down with it.
            log.exception("event handler raised")

    def _open(self) -> bool:
        device = find_port(self.preferred_port)
        if device is None:
            self.last_error = "no serial port found"
            return False
        try:
            self._port = serial.Serial(device, self.baud, timeout=READ_TIMEOUT_S)
            self.port_name = device
            self.last_error = ""
            log.info("connected to %s", device)
            if self.on_connect is not None:
                try:
                    self.on_connect()
                except Exception:
                    log.exception("on_connect handler raised")
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self._port = None
            return False

    def _close(self) -> None:
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            if not self.connected:
                if not self._open():
                    self._stop.wait(RECONNECT_DELAY_S)
                    continue

            try:
                raw = self._port.readline()
            except Exception as exc:
                log.info("serial read failed: %s", exc)
                self.last_error = str(exc)
                self._close()
                self._stop.wait(RECONNECT_DELAY_S)
                continue

            if not raw:
                continue  # Idle read timeout, which is normal.

            try:
                self._handle_line(raw.decode("ascii", "ignore"))
            except Exception:
                log.exception("failed handling inbound line")


class NullLink:
    """Stand-in used by --no-serial, so the pipeline runs with no board."""

    def __init__(self, echo: bool = True):
        self.echo = echo
        self.frames: list[dict] = []
        self.port_name = "(none)"
        self.last_error = ""

    connected = False

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, obj: dict) -> bool:
        self.frames.append(obj)
        if self.echo:
            print(encode_frame(obj).decode("ascii", "ignore").rstrip())
        return True
