"""Tests for mode selection and frame shaping.

All dependencies are stubbed, so these run with no board, no network and
no media session. Lyric fixtures are invented placeholder text.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.app import MODES, DeskConsole
from catsole.config import Config
from catsole.link import NullLink
from catsole.lyrics import Lyrics
from catsole.media import NowPlaying


class StubMedia:
    def __init__(self, now_playing=None):
        self.now_playing = now_playing
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self.now_playing

    def close(self):
        pass


class StubHardware:
    def __init__(self, stats=None):
        self.stats = stats or {
            "cpu": {"temp": 61.0, "load": 12.0, "clock": 4850.0},
            "gpu": {"temp": 68.0, "load": 99.0, "vram_used": 4211.0, "vram_total": 8188.0},
        }
        self.polls = 0

    def poll(self):
        self.polls += 1
        return self.stats

    lhm_available = True


class StubLyrics:
    def __init__(self, lyrics=None):
        self.lyrics = lyrics or Lyrics(kind="none")
        self.calls = []

    def fetch(self, artist, title, album="", duration_s=None):
        self.calls.append((artist, title))
        return self.lyrics


@pytest.fixture
def console(tmp_path):
    config = Config(cache_dir=tmp_path)
    return DeskConsole(
        config,
        link=NullLink(echo=False),
        media=StubMedia(),
        hardware=StubHardware(),
        lyrics_provider=StubLyrics(),
    )


def test_set_mode_switches(console):
    console.set_mode("stats")
    assert console.mode == "stats"


def test_set_mode_ignores_unknown_mode(console):
    console.set_mode("lyrics")
    console.set_mode("bogus")
    assert console.mode == "lyrics"


def test_force_refresh_flags_without_changing_mode(console):
    console.mode = "stats"
    console.force_refresh()
    assert console.mode == "stats"
    assert console.refresh_requested is True


def test_hello_records_firmware_and_answers(console):
    console.handle_event({"t": "hello", "fw": "1.0.0", "variant": 0})
    assert console.device_firmware == "1.0.0"
    # The reply is what lets the display leave its waiting state.
    assert console.link.frames


def test_non_hello_events_are_ignored(console):
    console.handle_event({"t": "somethingelse"})
    console.handle_event({})
    assert console.mode == "lyrics"
    assert console.device_firmware == ""


def test_stats_frame_passes_none_through_for_missing_fields(console):
    console.mode = "stats"
    console.stats = {"cpu": {"temp": None, "load": 12, "clock": 4200}, "gpu": {}}
    frame = console.build_frame()
    assert frame["mode"] == "stats"
    assert frame["cpu"]["temp"] is None


def test_lyrics_frame_shows_current_synced_line(console):
    console.mode = "lyrics"
    console.now_playing = NowPlaying(
        artist="An Artist", title="A Title", duration_ms=200_000,
        position_ms=5_000, is_playing=True,
    )
    console.lyrics = Lyrics(
        kind="synced",
        synced=[(1000, "placeholder line one"), (4500, "placeholder line two")],
    )
    frame = console.build_frame()
    assert frame["main"] == "placeholder line two"
    assert frame["meta"] == "An Artist - A Title"
    assert frame["lyr"] == "synced"
    assert frame["hold_ms"] > 0


def test_lyric_offset_shifts_line_selection(console):
    console.config.lyric_offset_ms = -4000
    console.mode = "lyrics"
    console.now_playing = NowPlaying(
        artist="An Artist", title="A Title", duration_ms=200_000,
        position_ms=5_000, is_playing=True,
    )
    console.lyrics = Lyrics(
        kind="synced",
        synced=[(1000, "placeholder line one"), (4500, "placeholder line two")],
    )
    # Pulling the clock back 4s should land on the earlier line.
    assert console.build_frame()["main"] == "placeholder line one"


def test_lyrics_frame_falls_back_to_title_card(console):
    console.mode = "lyrics"
    console.now_playing = NowPlaying(
        artist="An Artist", title="A Title", is_playing=True
    )
    console.lyrics = Lyrics(kind="none")
    frame = console.build_frame()
    assert frame["lyr"] == "none"
    assert frame["meta"] == "An Artist"
    assert frame["main"] == "A Title"


def test_lyrics_frame_is_idle_when_nothing_playing(console):
    console.mode = "lyrics"
    console.now_playing = None
    frame = console.build_frame()
    assert frame["state"] == "idle"
    assert frame["eq"] == 0


def test_paused_playback_stops_the_equalizer(console):
    console.mode = "lyrics"
    console.now_playing = NowPlaying(artist="A", title="B", is_playing=False)
    frame = console.build_frame()
    assert frame["state"] == "paused"
    assert frame["eq"] == 0


def test_track_change_triggers_one_lyrics_fetch(console):
    # Durations must be song-length: the source filter rejects anything
    # short enough to be a story or a reel.
    first = NowPlaying(artist="An Artist", title="One", duration_ms=210_000)
    console._on_media(first)
    console._on_media(first)
    assert len(console.lyrics_provider.calls) == 1

    console._on_media(
        NowPlaying(artist="An Artist", title="Two", duration_ms=195_000)
    )
    assert len(console.lyrics_provider.calls) == 2


def test_short_clips_are_ignored_entirely(console):
    # A reel should not become now-playing, nor cost a lyrics lookup.
    console._on_media(
        NowPlaying(artist="", title="some clip", duration_ms=17_000)
    )
    assert console.now_playing is None
    assert console.lyrics_provider.calls == []


def test_blocked_app_is_ignored(console):
    console.config.block_apps = ["instagram"]
    console._on_media(
        NowPlaying(
            artist="x", title="y", duration_ms=200_000, app_id="Instagram.Desktop"
        )
    )
    assert console.now_playing is None


def test_frame_always_carries_a_mode(console):
    for mode in MODES:
        console.mode = mode
        assert console.build_frame()["mode"] == mode
