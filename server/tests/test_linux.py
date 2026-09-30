"""Linux integration: a real D-Bus session and a real PulseAudio monitor.

Skipped anywhere these are not available. CI runs them with both, and sets
CATSOLE_LINUX_IT=1 so that a skip there fails instead of passing quietly.
"""

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

REQUIRED = os.environ.get("CATSOLE_LINUX_IT") == "1"


def need(condition: bool, reason: str) -> None:
    if not condition:
        if REQUIRED:
            pytest.fail(reason)
        pytest.skip(reason)


def test_mpris_reader_sees_a_playing_player():
    need(sys.platform.startswith("linux"), "Linux only")
    need(bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")), "no D-Bus session")
    import fake_mpris
    from catsole.media import MediaReader, MprisMediaReader

    ready, stop = threading.Event(), threading.Event()
    thread = threading.Thread(target=fake_mpris.serve, args=(ready, stop), daemon=True)
    thread.start()
    try:
        assert ready.wait(5), "the fake player never got its bus name"
        reader = MediaReader()
        assert isinstance(reader, MprisMediaReader)
        found = None
        for _ in range(20):
            found = reader.poll()
            if found is not None:
                break
            time.sleep(0.1)
        reader.close()
    finally:
        stop.set()
        thread.join(2)
    assert found is not None
    assert found.title == fake_mpris.TITLE
    assert found.artist == fake_mpris.ARTIST
    assert found.is_playing
    assert found.duration_ms == 210_000
    assert found.app_id == "catsoletest"


def pulse_running() -> bool:
    if not shutil.which("pactl"):
        return False
    return subprocess.run(["pactl", "info"], capture_output=True).returncode == 0


def test_parec_delivers_frames_from_the_output_monitor():
    need(sys.platform.startswith("linux"), "Linux only")
    need(bool(shutil.which("parec")), "parec is not installed")
    need(pulse_running(), "no PulseAudio or PipeWire server")
    from catsole.audio import CHUNK, ParecSource

    source = ParecSource()
    try:
        data = source.read(CHUNK)
    finally:
        source.close()
    assert len(data) == CHUNK * source.channels * 2


def test_audio_levels_come_up_on_linux():
    need(sys.platform.startswith("linux"), "Linux only")
    need(pulse_running(), "no PulseAudio or PipeWire server")
    pytest.importorskip("numpy")
    from catsole.audio import AUDIO_AVAILABLE, AudioLevels

    assert AUDIO_AVAILABLE
    levels = AudioLevels()
    levels.start()
    try:
        for _ in range(30):
            if levels.available:
                break
            time.sleep(0.1)
        assert levels.available, levels.last_error
    finally:
        levels.stop()
