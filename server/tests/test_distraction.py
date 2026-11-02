"""Which sessions count as a distraction during study, and the side-eye."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.media import NowPlaying, is_distraction

from test_study_console import Clock, advance, clock, console  # noqa: F401  (fixtures)


def np(**kw):
    kw.setdefault("is_playing", True)
    kw.setdefault("app_id", "Brave.Browser")
    return NowPlaying(**kw)


WIDE = (1280, 720)


@pytest.mark.parametrize("session, artwork", [
    (np(title="Instagram"), None),
    (np(title="(3) Facebook"), None),
    (np(title="Netflix"), None),
    (np(title="A video", artist="A channel", duration_ms=600_000), WIDE),
])
def test_video_sites_are_distractions(session, artwork):
    assert is_distraction(session, artwork)


@pytest.mark.parametrize("session, artwork", [
    (np(title="Instagram", is_playing=False), None),
    (np(title="A Song", artist="A Band", album="An Album", duration_ms=200_000), (500, 500)),
    (np(title="Home - Netflix"), None),
    (None, None),
])
def test_other_sessions_are_not(session, artwork):
    assert not is_distraction(session, artwork)


def test_side_eye_and_count_during_study(console, clock):
    console.config.media_poll_s = 0
    console.study_start("math", "countup", None)
    advance(console, clock, 70)
    console.media.now_playing = np(title="Instagram")
    advance(console, clock, 20)
    assert console.build_frame()["eye"] == 1
    console.media.now_playing = None
    advance(console, clock, 5)
    assert console.build_frame()["eye"] == 0
    session = console.study.stop().session
    assert session.distractions == 1 and session.distraction_min > 0.2
