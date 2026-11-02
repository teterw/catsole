"""Tests for the user settings the panel edits, and the data folder."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.config import Config
from catsole.settings import UserSettings


def test_defaults_when_missing(tmp_path):
    s = UserSettings.load(tmp_path / "settings.json")
    assert (s.daily_goal_min, s.chime, s.hotkey_menu, s.hotkey_pick) == (
        60, True, "ctrl+f11", "ctrl+f12")
    assert s.recent == [] and s.weather is None


def test_round_trip_keeps_thai(tmp_path):
    p = tmp_path / "settings.json"
    s = UserSettings.load(p)
    s.touch_recent("ฟิสิกส์", "countdown", 25)
    s.save(p)
    assert UserSettings.load(p).recent[0] == {
        "subject": "ฟิสิกส์", "timer": "countdown", "minutes": 25}


def test_corrupt_file_gives_defaults(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{nope", encoding="utf-8")
    assert UserSettings.load(p).daily_goal_min == 60


def test_unknown_keys_ignored(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"daily_goal_min": 90, "mystery": 1}', encoding="utf-8")
    assert UserSettings.load(p).daily_goal_min == 90


def test_touch_recent_dedupes_and_caps():
    s = UserSettings()
    for name in ["a", "b", "c", "d", "e", "f", "g"]:
        s.touch_recent(name, "countup", None)
    s.touch_recent("C", "countdown", 45)
    assert [r["subject"] for r in s.recent] == ["C", "g", "f", "e", "d", "b"]
    assert s.recent[0]["minutes"] == 45


def test_save_none_path_is_noop():
    UserSettings().save(None)


def test_config_data_dir_from_json(tmp_path):
    (tmp_path / "config.json").write_text('{"data_dir": "C:/x"}', encoding="utf-8")
    assert Config.load(tmp_path / "config.json").data_dir == Path("C:/x")


def test_config_data_dir_defaults_to_none():
    assert Config().data_dir is None
