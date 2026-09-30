"""A stand-in MPRIS player on the D-Bus session bus, for Linux tests.

Answers Properties.GetAll the way a browser or music player does, with
invented placeholder metadata. Run it directly to keep it up until killed:

    python fake_mpris.py
"""

import threading

from jeepney import new_method_return
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import open_dbus_connection
from jeepney.low_level import HeaderFields, MessageType

NAME = "org.mpris.MediaPlayer2.catsoletest"
TITLE = "A Song"
ARTIST = "An Artist"

PROPERTIES = {
    "PlaybackStatus": ("s", "Playing"),
    "Position": ("x", 83_000_000),
    "Metadata": ("a{sv}", {
        "xesam:title": ("s", TITLE),
        "xesam:artist": ("as", [ARTIST]),
        "mpris:length": ("x", 210_000_000),
    }),
}


def serve(ready: threading.Event, stop: threading.Event) -> None:
    conn = open_dbus_connection(bus="SESSION")
    try:
        conn.send_and_get_reply(message_bus.RequestName(NAME), timeout=5)
        ready.set()
        while not stop.is_set():
            try:
                msg = conn.receive(timeout=0.2)
            except TimeoutError:
                continue
            fields = msg.header.fields
            if (msg.header.message_type == MessageType.method_call
                    and fields.get(HeaderFields.member) == "GetAll"):
                conn.send(new_method_return(msg, "a{sv}", (PROPERTIES,)))
    finally:
        conn.close()


if __name__ == "__main__":
    serve(threading.Event(), threading.Event())
