"""Tests for media position extrapolation.

Only the pure half is unit-tested. The WinRT half needs a live session and
is exercised by the --no-serial smoke run instead.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.media import (
    NowPlaying,
    extrapolate_position,
    is_music,
    netflix_kind,
    netflix_show,
)

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


def song(**kw):
    base = dict(artist="An Artist", title="A Song", duration_ms=210_000, app_id="Brave")
    base.update(kw)
    return NowPlaying(**base)


def test_is_music_accepts_a_normal_track():
    assert is_music(song())


def test_is_music_rejects_short_clips():
    # Stories and reels are seconds long; songs are minutes.
    assert not is_music(song(duration_ms=17_000))


def test_is_music_allows_unknown_duration():
    # Live streams report no length, which is not a reason to hide them.
    assert is_music(song(duration_ms=0))


def test_is_music_rejects_nothing_playing():
    assert not is_music(None)
    assert not is_music(song(title=""))


def test_is_music_blocks_listed_apps_by_substring():
    assert not is_music(song(app_id="Instagram.Desktop"), block_apps=["instagram"])
    assert is_music(song(app_id="Brave"), block_apps=["instagram"])


def test_is_music_allowlist_excludes_everything_else():
    assert is_music(song(app_id="Spotify.exe"), allow_apps=["spotify"])
    assert not is_music(song(app_id="Brave"), allow_apps=["spotify"])


def test_is_music_empty_allowlist_permits_all():
    assert is_music(song(app_id="anything"), allow_apps=[])


def test_is_music_can_require_an_artist():
    assert not is_music(song(artist="   "), require_artist=True)
    assert is_music(song(artist="   "), require_artist=False)


# Brave, as observed: one session for the whole browser, carrying only the
# tab's title. Netflix's player page is titled just "Netflix".
def test_netflix_player_in_a_browser_is_a_watch_session():
    watching = song(artist="", title="Netflix", duration_ms=1_434_000)
    assert netflix_kind(watching) == "watch"


def test_netflix_browse_pages_are_trailers_not_shows():
    # The home page autoplays previews, and each one grabs the session.
    assert netflix_kind(song(artist="", title="Home - Netflix", duration_ms=90_000)) == "browse"
    assert netflix_kind(song(artist="", title="My List - Netflix")) == "browse"


def test_netflix_app_is_recognised_by_its_id():
    app = song(artist="", title="A Show", app_id="4DF9E0F8.Netflix_mcm4njqhnhss8!Netflix.App")
    assert netflix_kind(app) == "watch"


def test_a_song_called_netflix_is_still_a_song():
    assert netflix_kind(song(artist="An Artist", title="Netflix")) is None
    assert netflix_kind(song()) is None


def test_netflix_show_is_empty_when_only_the_page_title_is_known():
    assert netflix_show(song(artist="", title="Netflix")) == ""


def test_netflix_show_uses_a_real_title_when_there_is_one():
    app = song(artist="", title="A Show", app_id="4DF9E0F8.Netflix_mcm4njqhnhss8!Netflix.App")
    assert netflix_show(app) == "A Show"
