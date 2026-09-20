"""Tests for the serial transport, using a fake port so no board is needed."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk_console.link import ARDUINO_PID, ARDUINO_VID, SerialLink, find_port


class FakePort:
    def __init__(self, inbound: bytes = b""):
        self.written = b""
        self._in = inbound
        self.is_open = True

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def flush(self):
        pass

    def readline(self) -> bytes:
        line, sep, rest = self._in.partition(b"\n")
        if not sep:
            return b""
        self._in = rest
        return line + b"\n"

    def close(self):
        self.is_open = False


def port_info(device, vid=None, pid=None, description=""):
    return SimpleNamespace(device=device, vid=vid, pid=pid, description=description)


def test_find_port_prefers_arduino_vid_pid():
    ports = [
        port_info("COM1", vid=0x1234, pid=0x5678, description="Some Adapter"),
        port_info("COM3", vid=ARDUINO_VID, pid=ARDUINO_PID, description="USB Serial Device"),
    ]
    assert find_port(ports=ports) == "COM3"


def test_find_port_falls_back_to_usb_serial_description():
    ports = [port_info("COM7", vid=0x1111, pid=0x2222, description="USB Serial Device")]
    assert find_port(ports=ports) == "COM7"


def test_find_port_honours_explicit_preference():
    ports = [port_info("COM3", vid=ARDUINO_VID, pid=ARDUINO_PID)]
    assert find_port(preferred="COM9", ports=ports) == "COM9"


def test_find_port_returns_none_when_nothing_matches():
    assert find_port(ports=[port_info("COM1", vid=1, pid=2, description="Bluetooth")]) is None


def test_send_writes_ndjson():
    link = SerialLink()
    link._port = FakePort()
    assert link.send({"t": "frame", "mode": "stats"}) is True
    assert link._port.written.endswith(b"\n")
    assert b'"mode"' in link._port.written


def test_send_returns_false_when_disconnected():
    link = SerialLink()
    assert link.send({"t": "frame"}) is False


def test_send_survives_a_port_that_dies_mid_write():
    class DeadPort(FakePort):
        def write(self, data):
            raise OSError("device disconnected")

    link = SerialLink()
    link._port = DeadPort()
    assert link.send({"t": "frame"}) is False
    # The dead port must be dropped so the reader thread reconnects.
    assert link._port is None


def test_inbound_line_dispatches_event():
    seen = []
    link = SerialLink(on_event=seen.append)
    link._handle_line('{"t":"tap","kind":"short"}')
    assert seen == [{"t": "tap", "kind": "short"}]


def test_inbound_garbage_is_dropped_not_raised():
    seen = []
    link = SerialLink(on_event=seen.append)
    link._handle_line("<<noise>>")
    link._handle_line("")
    assert seen == []


def test_event_handler_exception_does_not_kill_the_reader():
    def boom(_event):
        raise RuntimeError("handler blew up")

    link = SerialLink(on_event=boom)
    link._handle_line('{"t":"tap"}')  # must not raise


def test_connected_reflects_port_state():
    link = SerialLink()
    assert link.connected is False
    link._port = FakePort()
    assert link.connected is True
