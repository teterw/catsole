"""Tests for streaks, mood, milestones and the study page summary."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.studylog import LoggedSession
from catsole.studystats import best_streak, daily_minutes, mood, streak, summarize

T = date(2026, 11, 2)


def days_of(*offsets):
    return {T - timedelta(d): 30.0 for d in offsets}


def mk(day, subject, minutes, start="10:00:00"):
    return LoggedSession(day, start, "11:00:00", subject, "countup", None, float(minutes), 0.0, 0, 0.0)


def test_daily_minutes_adds_up_a_day():
    assert daily_minutes([mk(T, "a", 20), mk(T, "b", 15)]) == {T: 35.0}


def test_streak_counts_from_yesterday_when_today_empty():
    assert streak(days_of(1, 2, 3), T) == 3
    assert streak(days_of(0, 1), T) == 2
    assert streak(days_of(2, 3), T) == 0


def test_best_streak():
    assert best_streak(days_of(0, 5, 6, 7, 8)) == 4
    assert best_streak({}) == 0


def test_mood_thresholds():
    assert mood({}, T) == 1
    assert mood(days_of(2, 3), T) == 0
    assert mood(days_of(0, 1), T) == 1
    assert mood(days_of(*range(3)), T) == 2
    assert mood(days_of(*range(7)), T) == 3


def test_summarize_empty():
    s = summarize([], T, 60)
    assert s["total_min"] == 0 and s["sessions"] == 0 and s["days"] == {}
    assert s["weeks"] == [0] * 12 and s["goal_min"] == 60
    assert s["milestone"] == {"reached": [], "next": 10, "eta_weeks": None}
    assert s["recent"] == [] and s["subjects"] == []


def test_summarize_numbers():
    rows = [mk(T, "Math", 90), mk(T - timedelta(1), "math", 30), mk(T - timedelta(8), "thai", 60)]
    s = summarize(rows, T, 60)
    assert s["today_min"] == 90 and s["week_min"] == 120 and s["last_week_min"] == 60
    assert s["subjects"][0] == ["Math", 120] and s["recent"][0]["subject"] == "Math"
    assert s["milestone"] == {"reached": [], "next": 10, "eta_weeks": 4}
    assert s["days"]["2026-11-02"] == 90 and s["weeks"][-1] == 120 and s["weeks"][-2] == 60
    assert s["streak"] == 2


def test_summarize_ignores_days_outside_a_year_for_heatmap():
    s = summarize([mk(T - timedelta(400), "old", 30)], T, 60)
    assert s["days"] == {} and s["total_min"] == 30


def test_subjects_fold_into_other_past_seven():
    rows = [mk(T, f"s{i}", 10 + i) for i in range(9)]
    names = [name for name, _ in summarize(rows, T, 60)["subjects"]]
    assert len(names) == 8 and names[-1] == "other"


def test_milestones_reached():
    s = summarize([mk(T, "a", 26 * 60)], T, 60)
    assert s["milestone"]["reached"] == [10, 25] and s["milestone"]["next"] == 50
