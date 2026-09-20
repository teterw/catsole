"""Tests for media position extrapolation.

Only the pure half is unit-tested. The WinRT half needs a live session and
is exercised by the --no-serial smoke run instead.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk_console.media import NowPlaying, extrapolate_position

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_extrapolate_advances_while_playing():
    assert extrapolate_position(10_000, T0, T0 + timedelta(seconds=3), 1.0, True, 200_000) == 13_000


def test_extrapolate_frozen_while_paused():
    assert extrapolate_position(10_000, T0, T0 + timedelta(seconds=3), 1.0, False, 200_000) == 10_000


def test_extrapolate_respects_playback_rate():
    assert extrapolate_position(0, T0, T0 + timedelta(seconds=10), 1.5, True, 200_000) == 15_000


def test_extrapolate_clamps_to_duration():
    assert extrapolate_position(0, T0, T0 + timedelta(seconds=600), 1.0, True, 200_000) == 200_000


def test_extrapolate_ignores_clock_skew_backwards():
    # A last_updated stamp in the future must not rewind the position.
    assert extrapolate_position(10_000, T0, T0 - timedelta(seconds=5), 1.0, True, 200_000) == 10_000


def test_extrapolate_treats_zero_rate_as_normal_speed():
    # Some players report rate 0 even while playing; trust the status instead.
    assert extrapolate_position(0, T0, T0 + timedelta(seconds=4), 0.0, True, 200_000) == 4_000


def test_extrapolate_without_known_duration_does_not_clamp():
    assert extrapolate_position(0, T0, T0 + timedelta(seconds=600), 1.0, True, 0) == 600_000


def test_extrapolate_handles_missing_timestamp():
    assert extrapolate_position(7_000, None, T0, 1.0, True, 200_000) == 7_000


def test_nowplaying_track_key_changes_with_track():
    first = NowPlaying(artist="A", title="One", album="", duration_ms=1000)
    same = NowPlaying(artist="A", title="One", album="", duration_ms=1000, position_ms=90_000)
    other = NowPlaying(artist="A", title="Two", album="", duration_ms=1000)
    assert first.track_key == same.track_key
    assert first.track_key != other.track_key


def test_nowplaying_is_empty_without_title():
    assert NowPlaying(artist="A", title="", album="", duration_ms=0).is_empty
    assert not NowPlaying(artist="A", title="One", album="", duration_ms=0).is_empty
