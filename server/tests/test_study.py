"""Tests for the study timer. A fake clock stands in for time.time, so
hours of study run in milliseconds."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.study import StudyTimer
from catsole.studylog import StudyLog, StudyStore


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def log():
    return StudyLog(None)


@pytest.fixture
def store():
    return StudyStore(None)


@pytest.fixture
def timer(log, store, clock):
    return StudyTimer(log, store, clock)


def run(timer, clock, seconds, step=1, distracted=False):
    """Tick for `seconds`; returns the first Finished, which stops the run."""
    for _ in range(int(seconds / step)):
        clock.t += step
        finished = timer.tick(distracted)
        if finished is not None:
            return finished
    return None


def test_countdown_finishes_and_logs(timer, clock, log):
    timer.start("math", "countdown", 25)
    f = run(timer, clock, 25 * 60)
    assert f.reason == "done" and f.session.studied_min == 25.0 and f.session.planned_min == 25.0
    assert log.read()[-1].subject == "math" and not timer.active


def test_pause_time_not_counted(timer, clock):
    timer.start("math", "countup", None)
    run(timer, clock, 600)
    timer.pause()
    run(timer, clock, 300)
    timer.resume()
    run(timer, clock, 60)
    assert round(timer.view()["studied_s"]) == 660


def test_paused_minutes_logged(timer, clock):
    timer.start("math", "countup", None)
    run(timer, clock, 600)
    timer.pause()
    run(timer, clock, 300)
    timer.resume()
    assert timer.stop().session.paused_min == 5.0


def test_add_minutes_extends_countdown(timer):
    timer.start("math", "countdown", 25)
    assert timer.add_minutes(5)
    assert timer.view()["planned_s"] == 30 * 60


def test_add_minutes_refused_for_countup(timer):
    timer.start("math", "countup", None)
    assert not timer.add_minutes(5)


def test_under_a_minute_is_discarded(timer, clock, log):
    timer.start("math", "countup", None)
    run(timer, clock, 59)
    assert timer.stop().session is None and log.read() == []


def test_suspend_gap_pauses_at_last_tick(timer, clock):
    timer.start("math", "countup", None)
    run(timer, clock, 120)
    clock.t += 3600
    timer.tick()
    v = timer.view()
    assert v["state"] == "paused" and round(v["studied_s"]) == 120


def test_clock_backwards_counts_nothing(timer, clock):
    timer.start("math", "countup", None)
    run(timer, clock, 100)
    clock.t -= 50
    timer.tick()
    assert round(timer.view()["studied_s"]) == 100


def test_small_jump_still_counts(timer, clock):
    timer.start("math", "countup", None)
    clock.t += 20
    timer.tick()
    assert timer.view()["state"] == "running" and round(timer.view()["studied_s"]) == 20


def test_restore_after_short_restart_keeps_running(store, log, clock):
    a = StudyTimer(log, store, clock)
    a.start("math", "countup", None)
    run(a, clock, 100)
    clock.t += 60
    b = StudyTimer(log, store, clock)
    b.restore()
    assert b.view()["state"] == "running" and round(b.view()["studied_s"]) == 160


def test_restore_after_long_gap_comes_back_paused(store, log, clock):
    a = StudyTimer(log, store, clock)
    a.start("math", "countup", None)
    run(a, clock, 100)
    clock.t += 3 * 3600
    b = StudyTimer(log, store, clock)
    b.restore()
    assert b.view()["state"] == "paused" and round(b.view()["studied_s"]) == 100


def test_countup_asks_at_3h_and_stops_without_answer(timer, clock):
    timer.start("math", "countup", None)
    run(timer, clock, 3 * 3600, step=10)
    assert timer.view()["state"] == "asking"
    f = run(timer, clock, 600, step=10)
    assert f.reason == "no_answer" and f.session.studied_min == 180.0


def test_answer_yes_keeps_going_and_asks_again(timer, clock):
    timer.start("math", "countup", None)
    run(timer, clock, 3 * 3600, step=10)
    assert timer.answer_yes()
    run(timer, clock, 3 * 3600 - 20, step=10)
    assert timer.view()["state"] == "running"
    run(timer, clock, 30, step=10)
    assert timer.view()["state"] == "asking"


def test_pause_limit_ends_session(timer, clock):
    timer.start("math", "countup", None)
    run(timer, clock, 1200)
    timer.pause()
    f = run(timer, clock, 1800, step=10)
    assert f.reason == "pause_limit" and f.session.studied_min == 20.0


def test_distraction_counted_after_15s(timer, clock):
    timer.start("math", "countup", None)
    for _ in range(20):
        clock.t += 1
        timer.tick(distracted=True)
    assert timer.view()["distracted"]
    for _ in range(5):
        clock.t += 1
        timer.tick(distracted=False)
    assert not timer.view()["distracted"]


def test_distraction_logged_on_a_real_session(timer, clock):
    timer.start("math", "countup", None)
    run(timer, clock, 120)
    for _ in range(20):
        clock.t += 1
        timer.tick(distracted=True)
    for _ in range(5):
        clock.t += 1
        timer.tick(distracted=False)
    f = timer.stop()
    assert f.session.distractions == 1 and 0.3 <= f.session.distraction_min <= 0.4


def test_short_distraction_not_counted(timer, clock):
    timer.start("math", "countup", None)
    run(timer, clock, 120)
    for _ in range(10):
        clock.t += 1
        timer.tick(distracted=True)
    run(timer, clock, 10)
    assert timer.stop().session.distractions == 0


def test_failed_write_kept_pending_and_retried(timer, clock, log, monkeypatch):
    ok = {"v": False}
    real = log.append
    monkeypatch.setattr(log, "append", lambda s: ok["v"] and real(s))
    timer.start("math", "countup", None)
    run(timer, clock, 120)
    timer.stop()
    assert timer.pending_count == 1 and timer.save_error
    ok["v"] = True
    run(timer, clock, 61)
    assert timer.pending_count == 0 and len(log.read()) == 1 and not timer.save_error


def test_late_note_updates_last_row(timer, clock, log):
    timer.start("math", "countup", None)
    run(timer, clock, 120)
    timer.stop()
    assert timer.set_note("did ch.3") and log.read()[-1].note == "did ch.3"
    assert timer.set_note("did ch.3 and 4") and log.read()[-1].note == "did ch.3 and 4"


def test_note_while_running_is_saved_with_session(timer, clock, log):
    timer.start("math", "countup", None)
    assert timer.set_note("chapter 5")
    run(timer, clock, 120)
    timer.stop()
    assert log.read()[-1].note == "chapter 5"


def test_cannot_start_twice_or_blank(timer):
    assert timer.start("math", "countup", None)
    assert not timer.start("x", "countup", None)
    timer.stop()
    assert not timer.start("  ", "countup", None)
    assert not timer.start("math", "countdown", 0)
    assert not timer.start("math", "countdown", 601)


def test_view_none_when_idle(timer):
    assert timer.view() is None and not timer.active


def test_counting_resumes_right_after_clock_goes_back(timer, clock):
    timer.start("math", "countup", None)
    run(timer, clock, 100)
    clock.t -= 50
    timer.tick()
    run(timer, clock, 10)
    assert round(timer.view()["studied_s"]) == 110
