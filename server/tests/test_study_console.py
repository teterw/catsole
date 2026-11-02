"""Study mode inside the console: frames, the done screen, the panel block."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.app import DeskConsole
from catsole.config import Config
from catsole.link import NullLink
from catsole.media import NowPlaying
from catsole.study import StudyTimer
from catsole.textbitmap import render as render_bitmap

from test_app import StubHardware, StubLyrics, StubMedia


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


def make_console(tmp_path):
    return DeskConsole(Config(cache_dir=tmp_path, data_dir=tmp_path), link=NullLink(echo=False),
                       media=StubMedia(), hardware=StubHardware(), lyrics_provider=StubLyrics())


@pytest.fixture
def console(tmp_path, clock):
    c = make_console(tmp_path)
    c.study = StudyTimer(c.study.log, c.study.store, clock)
    c.chimes = []
    c.chime = lambda: c.chimes.append(clock())
    return c


def advance(console, clock, seconds):
    for _ in range(seconds):
        clock.t += 1
        console.tick()


def playing_song():
    return NowPlaying(artist="An Artist", title="A Song", duration_ms=200_000,
                      position_ms=1_000, is_playing=True, app_id="spotify")


def test_study_frame_countdown(console):
    assert console.study_start("math", "countdown", 25)
    f = console.build_frame()
    assert (f["mode"], f["meta"], f["time"], f["sec"], f["rt"], f["pg"], f["st"]) == (
        "study", "math", "25:00", "", "25m", 0, "run")
    assert f["wig"] == 0 and f["eye"] == 0


def test_study_frame_over_an_hour(console, clock):
    console.study_start("math", "countup", None)
    advance(console, clock, 3725)
    f = console.build_frame()
    assert (f["time"], f["sec"], f["rt"]) == ("1:02", "05", "goal 1h")
    assert f["pg"] == 1000


def test_study_frame_countup_progress_toward_goal(console, clock):
    console.settings.daily_goal_min = 120
    console.study_start("math", "countup", None)
    advance(console, clock, 30 * 60)
    f = console.build_frame()
    assert f["pg"] == 250 and f["rt"] == "goal 2h"


def test_study_frame_paused(console, clock):
    console.study_start("math", "countdown", 25)
    advance(console, clock, 10)
    console.study_control("pause")
    f = console.build_frame()
    assert f["st"] == "pause" and f["rt"] == "paused" and f["time"] == "24:50"


def test_study_frame_fish_wiggles_in_last_minute(console, clock):
    console.study_start("math", "countdown", 2)
    advance(console, clock, 61)
    assert console.build_frame()["wig"] == 1


def test_thai_subject_gets_strip_bitmap(console):
    if render_bitmap("ฟิสิกส์") is None:
        pytest.skip("no Thai font for the strip bitmap here")
    console.study_start("ฟิสิกส์", "countup", None)
    console.push_frame()
    assert any(m.get("t") == "mbmp" for m in console.link.frames)
    assert console.link.frames[-1]["mb"] > 0


def test_done_screen_then_back(console, clock):
    # An idle machine, so the load rule doesn't jump to stats afterwards.
    console.hardware.stats = {"cpu": {"load": 5.0}, "gpu": {"load": 5.0}}
    console.mode = "clock"
    console.study_start("math", "countdown", 1)
    advance(console, clock, 60)
    f = console.build_frame()
    assert (f["st"], f["l1"], f["mode"]) == ("done", "1 min", "study")
    assert f["l2"] == "streak 1d" and len(console.chimes) == 1
    advance(console, clock, 8)
    assert console.build_frame()["mode"] == "clock"


def test_no_chime_when_turned_off(console, clock):
    console.settings.chime = False
    console.study_start("math", "countdown", 1)
    advance(console, clock, 60)
    assert console.chimes == []


def test_asking_frame(console, clock):
    console.study_start("math", "countup", None)
    advance(console, clock, 3 * 3600)
    f = console.build_frame()
    assert (f["st"], f["rt"], f["l1"], f["l2"]) == ("ask", "still?", "still studying?", "ctrl+f12 = yes")


def test_music_does_not_take_over_during_study(console):
    console.study_start("math", "countup", None)
    console.media.now_playing = playing_song()
    console.tick()
    assert console.build_frame()["mode"] == "study"
    assert not any(m.get("t") == "eq" for m in console.link.frames)


def test_study_start_remembers_subject(console, tmp_path):
    console.study_start("math", "countdown", 45)
    assert console.settings.recent[0] == {"subject": "math", "timer": "countdown", "minutes": 45}
    assert (tmp_path / "settings.json").exists()


def test_snapshot_study_block(console, clock):
    console.study_start("math", "countup", None)
    advance(console, clock, 120)
    console.study_control("stop")
    s = console.snapshot()["study"]
    assert s["active"] is None and s["last"]["subject"] == "math" and s["last"]["note_editable"]
    assert s["today_min"] == 2.0 and s["streak"] == 1 and s["goal_min"] == 60
    assert s["pending"] == 0 and s["save_error"] is False and s["discarded"] is False


def test_snapshot_says_short_session_discarded(console, clock):
    console.study_start("math", "countup", None)
    advance(console, clock, 30)
    console.study_control("stop")
    assert console.snapshot()["study"]["discarded"] is True


def test_session_survives_a_new_console(console, clock, tmp_path):
    console.study_start("math", "countup", None)
    advance(console, clock, 30)
    assert make_console(tmp_path).study.active
