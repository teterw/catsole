"""Tests for mode selection and frame shaping.

All dependencies are stubbed, so these run with no board, no network and
no media session. Lyric fixtures are invented placeholder text.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole import app as app_module
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

    def latest(self):
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


def test_synced_frame_carries_how_long_the_line_holds(console):
    console.mode = "lyrics"
    console.now_playing = NowPlaying(
        artist="An Artist", title="A Title", duration_ms=200_000,
        position_ms=2_000, is_playing=True,
    )
    console.lyrics = Lyrics(
        kind="synced",
        synced=[(1000, "placeholder line one"), (4500, "placeholder line two")],
    )
    assert console.build_frame()["hold_ms"] == 2500


def test_thai_line_is_shown_not_replaced_by_title_card(console):
    console.mode = "lyrics"
    console.now_playing = NowPlaying(
        artist="An Artist", title="A Title", duration_ms=200_000,
        position_ms=5_000, is_playing=True,
    )
    console.lyrics = Lyrics(kind="synced", synced=[(1000, "ทดสอบ ข้อความ")])
    frame = console.build_frame()
    assert frame["main"] == "ทดสอบ ข้อความ"
    assert frame["lyr"] == "synced"


def test_line_in_a_script_with_no_font_falls_back_to_title_card(console):
    console.mode = "lyrics"
    console.now_playing = NowPlaying(
        artist="An Artist", title="A Title", duration_ms=200_000,
        position_ms=5_000, is_playing=True,
    )
    console.lyrics = Lyrics(kind="synced", synced=[(1000, "你好")])
    frame = console.build_frame()
    assert frame["main"] == "A Title"
    assert frame["lyr"] == "script"


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


def load_stats(cpu=0.0, gpu=0.0):
    return {
        "cpu": {"temp": None, "load": cpu, "clock": None},
        "gpu": {"temp": None, "load": gpu, "vram_used": None, "vram_total": None},
        "ram": {"used": None, "total": None, "percent": None},
        "fans": [],
    }


def test_heavy_load_jumps_to_stats(console):
    console.mode = "lyrics"
    console.hardware.stats = load_stats(cpu=95.0)
    console.tick()
    assert console.mode == "stats"


def test_gpu_load_alone_is_enough(console):
    console.mode = "lyrics"
    console.hardware.stats = load_stats(gpu=88.0)
    console.tick()
    assert console.mode == "stats"


def test_quiet_machine_does_not_jump(console):
    # Rotation off, so this tests the load rule rather than the timer.
    console.config.idle_rotate_s = 0
    console.mode = "lyrics"
    console.hardware.stats = load_stats(cpu=12.0, gpu=5.0)
    console.tick()
    assert console.mode == "lyrics"


def test_start_mode_is_not_rotated_past_immediately(console):
    # The first tick used to rotate straight away, so the configured
    # starting screen was never seen.
    assert console.mode == "lyrics"
    console.hardware.stats = load_stats(cpu=5.0)
    console.tick()
    assert console.mode == "lyrics"


def test_busy_holds_until_load_falls_well_back(console):
    console.hardware.stats = load_stats(cpu=95.0)
    console.tick()
    assert console._busy is True

    # Still above the release threshold, so it stays busy rather than
    # flapping the moment load dips.
    console.hardware.stats = load_stats(cpu=65.0)
    console._next_stats = 0.0
    console.tick()
    assert console._busy is True

    console.hardware.stats = load_stats(cpu=20.0)
    console._next_stats = 0.0
    console.tick()
    assert console._busy is False


def test_a_hand_picked_mode_survives_a_load_spike(console):
    console.set_mode("clock")          # starts the manual hold
    console.hardware.stats = load_stats(cpu=99.0)
    console.tick()
    assert console.mode == "clock"


def test_clock_rests_longer_than_the_others(console):
    assert console._hold_for("clock") == 20.0
    assert console._hold_for("lyrics") == console.config.idle_rotate_s
    assert console._hold_for("stats") == console.config.idle_rotate_s


def test_unknown_screen_falls_back_to_the_default_dwell(console):
    assert console._hold_for("nonesuch") == console.config.idle_rotate_s


# ---- lookups that must not be lost ------------------------------------

class TrackChangesMidFetch:
    """A provider during whose first lookup the next track starts."""

    def __init__(self, console, next_track):
        self.console = console
        self.next_track = next_track
        self.calls = []

    def fetch(self, artist, title, album="", duration_s=None):
        self.calls.append(title)
        if len(self.calls) == 1:
            self.console._on_media(self.next_track)
        return Lyrics(kind="synced", synced=[(0, f"a line of {title}")])


def playing_song(title="One", position_ms=30_000, playing=True):
    return NowPlaying(
        artist="An Artist", title=title, duration_ms=210_000,
        position_ms=position_ms, is_playing=playing,
    )


def test_a_track_change_during_a_lookup_still_looks_up_the_new_track(console):
    # The in-flight guard used to skip the new track's lookup outright, and
    # the old result was then discarded: the song ran with no lyrics at all.
    provider = TrackChangesMidFetch(console, playing_song("Two"))
    console.lyrics_provider = provider
    console._on_media(playing_song("One"))
    assert provider.calls == ["One", "Two"]
    assert console.lyrics.synced == [(0, "a line of Two")]


# ---- Netflix ------------------------------------------------------------

def netflix(**kw):
    # What Brave reports for Netflix's player: the tab title, nothing else.
    base = dict(
        artist="", title="Netflix", duration_ms=1_434_000,
        position_ms=1_113_000, is_playing=True, app_id="Brave",
    )
    base.update(kw)
    return NowPlaying(**base)


def test_netflix_gets_a_video_card_instead_of_lyrics(console):
    console._on_media(netflix())
    assert console.lyrics_provider.calls == []
    frame = console.build_frame()
    assert frame["lyr"] == "video"
    assert frame["meta"] == "Netflix"
    assert frame["main"] == ""
    assert frame["pos"] == 1_113_000
    assert frame["dur"] == 1_434_000
    assert frame["state"] == "playing"


def test_netflix_card_names_the_show_when_the_session_does(console):
    console._on_media(netflix(title="A Show", app_id="4DF9E0F8.Netflix_mcm4njqhnhss8!Netflix.App"))
    assert console.build_frame()["main"] == "A Show"


def test_netflix_trailers_on_the_browse_page_are_ignored(console):
    console._on_media(netflix(title="Home - Netflix", duration_ms=90_000))
    assert console.now_playing is None
    assert console.lyrics_provider.calls == []


def test_netflix_is_shown_even_when_an_artist_is_required(console):
    console.config.require_artist = True
    console._on_media(netflix())
    assert console.now_playing is not None


# ---- another tab taking the session for a moment -------------------------

@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(app_module.time, "monotonic", lambda: now[0])
    return now


def test_a_reel_taking_the_session_does_not_drop_the_song(console, clock):
    console._on_media(playing_song())
    console._on_media(NowPlaying(artist="", title="a clip", duration_ms=15_000, is_playing=True))
    assert console.now_playing.title == "One"


def test_a_trailer_taking_the_session_does_not_drop_the_song(console, clock):
    console._on_media(playing_song())
    console._on_media(netflix(title="Home - Netflix", duration_ms=90_000))
    assert console.now_playing.title == "One"


def test_a_paused_tab_surfacing_does_not_replace_a_playing_song(console, clock):
    # A paused video cannot be what is making the sound.
    console._on_media(playing_song())
    paused = NowPlaying(artist="A Channel", title="A Video", duration_ms=600_000, is_playing=False)
    console._on_media(paused)
    assert console.now_playing.title == "One"


def test_the_held_song_keeps_its_place(console, clock):
    console._on_media(playing_song(position_ms=30_000))
    clock[0] += 4
    console._on_media(None)
    assert console.now_playing.position_ms == 34_000


def test_the_song_coming_back_costs_no_second_lookup(console, clock):
    console._on_media(playing_song())
    console._on_media(None)
    console._on_media(playing_song(position_ms=31_000))
    assert console.lyrics_provider.calls == [("An Artist", "One")]
    assert console.now_playing.position_ms == 31_000


def test_a_new_playing_track_takes_over_at_once(console, clock):
    console._on_media(playing_song("One"))
    console._on_media(playing_song("Two"))
    assert console.now_playing.title == "Two"


def test_the_hold_runs_out(console, clock):
    console._on_media(playing_song())
    clock[0] += app_module.INTERRUPTION_HOLD_S + 1
    console._on_media(None)
    assert console.now_playing is None


def test_the_hold_ends_with_the_song(console, clock):
    console._on_media(playing_song(position_ms=208_000))
    clock[0] += 3
    console._on_media(None)
    assert console.now_playing is None


def test_a_paused_song_is_not_held(console, clock):
    console._on_media(playing_song(playing=False))
    console._on_media(None)
    assert console.now_playing is None


# ---- Thai word breaks -----------------------------------------------------

def test_thai_lines_go_out_with_their_word_breaks(console):
    pytest.importorskip("pythainlp")
    console.mode = "lyrics"
    console.now_playing = playing_song(position_ms=5_000)
    console.lyrics = Lyrics(kind="synced", synced=[(1000, "ทดสอบข้อความ")])
    assert console.build_frame()["main"] == "ทดสอบ\u200bข้อความ"


def test_panel_reports_a_show_rather_than_missing_lyrics(console):
    console._on_media(netflix())
    assert console.snapshot()["lyrics_kind"] == "video"


# ---- the moment a song starts ----------------------------------------------

def test_no_title_card_flashes_while_the_lyrics_are_still_coming(console, clock):
    # The title used to show for a moment and then give way to the lyrics,
    # which read as some other line flashing up first.
    song = playing_song()
    console.lyrics_provider = StubLyrics()
    console._inflight.add(song.track_key)       # a lookup still under way
    console.now_playing = song
    console._track_key = song.track_key
    console._track_started = clock[0]
    console.mode = "lyrics"
    assert console.build_frame()["main"] == ""


def test_the_title_card_shows_if_the_lookup_takes_too_long(console, clock):
    song = playing_song()
    console._inflight.add(song.track_key)
    console.now_playing = song
    console._track_key = song.track_key
    console._track_started = clock[0]
    console.mode = "lyrics"
    clock[0] += app_module.LOOKUP_GRACE_S + 0.1
    assert console.build_frame()["main"] == "One"
