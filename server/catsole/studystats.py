"""Numbers for the study page, the panel and the cat, from the study log."""

from __future__ import annotations

import math
from datetime import date, timedelta

from .studylog import LoggedSession

MILESTONES_H = (10, 25, 50, 100, 250, 500, 1000)

HEATMAP_DAYS = 371      # 53 weeks
WEEK_BARS = 12
SUBJECTS_SHOWN = 7
RECENT_SHOWN = 5


def daily_minutes(sessions: list[LoggedSession]) -> dict[date, float]:
    days: dict[date, float] = {}
    for s in sessions:
        if s.studied_min > 0:
            days[s.date] = days.get(s.date, 0.0) + s.studied_min
    return days


def streak(days: dict[date, float], today: date) -> int:
    """Days in a row with study, back from today -- or from yesterday, since
    a streak isn't broken until a whole day passes without study."""
    day = today if days.get(today, 0) > 0 else today - timedelta(days=1)
    count = 0
    while days.get(day, 0) > 0:
        count += 1
        day -= timedelta(days=1)
    return count


def best_streak(days: dict[date, float]) -> int:
    best = run = 0
    previous = None
    for day in sorted(d for d, m in days.items() if m > 0):
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        best = max(best, run)
        previous = day
    return best


def mood(days: dict[date, float], today: date) -> int:
    """0 sulky, 1 as usual, 2 content, 3 proud."""
    current = streak(days, today)
    if current == 0:
        return 0 if any(m > 0 for d, m in days.items() if d < today) else 1
    if current >= 7:
        return 3
    if current >= 3:
        return 2
    return 1


def _window(days: dict[date, float], end: date, length: int) -> float:
    return sum(days.get(end - timedelta(days=i), 0.0) for i in range(length))


def summarize(sessions: list[LoggedSession], today: date, goal_min: int) -> dict:
    days = daily_minutes(sessions)
    total = sum(s.studied_min for s in sessions)
    week = _window(days, today, 7)

    first = today - timedelta(days=HEATMAP_DAYS - 1)
    heat = {d.isoformat(): round(m, 1) for d, m in days.items() if first <= d <= today}

    weeks = [round(_window(days, today - timedelta(days=7 * i), 7), 1)
             for i in reversed(range(WEEK_BARS))]

    # Grouped without regard to case, shown as last typed.
    by_subject: dict[str, list] = {}
    for s in sorted(sessions, key=lambda s: (s.date, s.start)):
        entry = by_subject.setdefault(s.subject.casefold(), [s.subject, 0.0])
        entry[0] = s.subject
        entry[1] += s.studied_min
    ranked = sorted(by_subject.values(), key=lambda e: -e[1])
    subjects = [[name, round(m, 1)] for name, m in ranked[:SUBJECTS_SHOWN]]
    rest = sum(m for _, m in ranked[SUBJECTS_SHOWN:])
    if rest:
        subjects.append(["other", round(rest, 1)])

    hours = total / 60
    reached = [h for h in MILESTONES_H if h <= hours]
    upcoming = [h for h in MILESTONES_H if h > hours]
    nxt = upcoming[0] if upcoming else None
    eta = math.ceil((nxt - hours) / (week / 60)) if nxt and week > 0 else None

    recent = sorted(sessions, key=lambda s: (s.date, s.start), reverse=True)[:RECENT_SHOWN]

    return {
        "total_min": round(total, 1),
        "sessions": len(sessions),
        "today_min": round(days.get(today, 0.0), 1),
        "goal_min": goal_min,
        "week_min": round(week, 1),
        "last_week_min": round(_window(days, today - timedelta(days=7), 7), 1),
        "streak": streak(days, today),
        "best_streak": best_streak(days),
        "days": heat,
        "weeks": weeks,
        "subjects": subjects,
        "milestone": {"reached": reached, "next": nxt, "eta_weeks": eta},
        "recent": [s.to_row() for s in recent],
    }
