"""The cat's mood in every frame follows the study streak."""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.studylog import LoggedSession

from test_study_console import Clock, clock, console  # noqa: F401  (fixtures)


def log_days(console, *days_ago):
    today = console._today()
    for d in days_ago:
        console.study.log.append(LoggedSession(today - timedelta(days=d), "10:00:00", "11:00:00",
                                               "math", "countup", None, 60.0, 0.0, 0, 0.0))


def test_mood_proud_after_a_week(console):
    log_days(console, *range(1, 8))
    assert console.build_frame()["md"] == 3


def test_mood_as_usual_with_no_history(console):
    assert console.build_frame()["md"] == 1


def test_mood_sulky_after_missed_days(console):
    log_days(console, 3, 4)
    assert console.build_frame()["md"] == 0


def test_mood_rides_on_study_frames_too(console):
    log_days(console, 1, 2, 3)
    console.study_start("math", "countup", None)
    assert console.build_frame()["md"] == 2
