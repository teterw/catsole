"""Tests for the OLED menu the two shortcuts drive."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.menu import MenuItem, StudyMenu, build_items
from catsole.textbitmap import render as render_bitmap

from test_study_console import Clock, advance, console, clock  # noqa: F401  (fixtures)

RECENT = [
    {"subject": "math", "timer": "countdown", "minutes": 25},
    {"subject": "thai", "timer": "countup", "minutes": None},
]


def test_items_idle():
    items = build_items(None, RECENT)
    assert [i.label for i in items] == ["math 25m", "thai up", "close"]
    assert items[0] == MenuItem("math 25m", "start", "math", "countdown", 25)


def test_items_idle_at_most_three_subjects():
    recent = [{"subject": f"s{i}", "timer": "countup", "minutes": None} for i in range(5)]
    assert len(build_items(None, recent)) == 4


def test_items_active():
    view = {"timer": "countdown", "state": "running"}
    assert [i.label for i in build_items(view, RECENT)] == ["pause", "+5 min", "stop", "close"]
    assert build_items({"timer": "countup", "state": "paused"}, RECENT)[0].label == "resume"
    assert [i.label for i in build_items({"timer": "countup", "state": "running"}, RECENT)] == [
        "pause", "stop", "close"]


def test_menu_wraps_and_times_out(clock):
    m = StudyMenu(clock)
    items = build_items(None, [])
    m.press_next(items)
    m.press_next(items)
    assert m.visible() and m.sel == 0
    clock.t += 6.1
    assert not m.visible()


def test_next_moves_down(clock):
    m = StudyMenu(clock)
    items = build_items(None, RECENT)
    m.press_next(items)
    m.press_next(items)
    assert m.sel == 1


def test_pick_closes_and_returns(clock):
    m = StudyMenu(clock)
    m.press_next(build_items(None, [{"subject": "math", "timer": "countup", "minutes": None}]))
    assert m.press_pick().subject == "math" and not m.visible()


def test_pick_when_closed_returns_none(clock):
    assert StudyMenu(clock).press_pick() is None


def test_console_menu_starts_recent_subject(console):
    console.settings.recent = list(RECENT)
    console.menu_press("menu")
    assert console.build_frame()["mi"] == ["math 25m", "thai up", "close"]
    console.menu_press("pick")
    assert console.study.view()["subject"] == "math" and "mi" not in console.build_frame()


def test_console_menu_pauses(console):
    console.study_start("math", "countdown", 25)
    console.menu_press("menu")
    console.menu_press("pick")
    assert console.study.view()["state"] == "paused"


def test_pick_answers_still_studying(console, clock):
    console.study_start("math", "countup", None)
    advance(console, clock, 3 * 3600)
    assert console.study.view()["state"] == "asking"
    console.menu_press("pick")
    assert console.study.view()["state"] == "running"


def test_menu_with_thai_subject_sends_item_bitmap(console):
    if render_bitmap("ฟิสิกส์ up") is None:
        pytest.skip("no Thai font for item bitmaps here")
    console.settings.touch_recent("ฟิสิกส์", "countup", None)
    console.menu_press("menu")
    console.push_frame()
    frame = [m for m in console.link.frames if m.get("t") == "frame"][-1]
    assert frame["mi"][0] == "" and frame["ms"] == 0
    bitmaps = [m for m in console.link.frames if m.get("t") == "ibmp"]
    assert bitmaps and bitmaps[-1]["i"] == 0 and bitmaps[-1]["w"] <= 104 and bitmaps[-1]["h"] == 11
    assert len(bitmaps[-1]["d"]) == ((bitmaps[-1]["w"] + 7) // 8) * 11 * 2
