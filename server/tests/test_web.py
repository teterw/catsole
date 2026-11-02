"""Tests for the panel's HTTP API, through Flask's test client."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.app import DeskConsole
from catsole.config import Config
from catsole.link import NullLink
from catsole.web import create_app

from test_app import StubHardware, StubLyrics, StubMedia


@pytest.fixture
def console(tmp_path):
    c = DeskConsole(Config(cache_dir=tmp_path, data_dir=tmp_path), link=NullLink(echo=False),
                    media=StubMedia(), hardware=StubHardware(), lyrics_provider=StubLyrics())
    c.chime = lambda: None
    return c


@pytest.fixture
def client(console):
    return create_app(console).test_client()


def test_api_start_and_control(client):
    r = client.post("/api/study/start", json={"subject": "math", "timer": "countdown", "minutes": 25})
    assert r.status_code == 200 and r.json["ok"]
    assert client.post("/api/study/start", json={"subject": "", "timer": "countup"}).status_code == 400
    assert client.post("/api/study/control", json={"action": "pause"}).json["ok"]
    assert client.get("/api/state").json["study"]["active"]["state"] == "paused"
    assert client.post("/api/study/control", json={"action": "fly"}).status_code == 400


def test_api_start_rejects_bad_minutes(client):
    r = client.post("/api/study/start", json={"subject": "math", "timer": "countdown", "minutes": "x"})
    assert r.status_code == 400


def test_api_note(client):
    client.post("/api/study/start", json={"subject": "math", "timer": "countup"})
    assert client.post("/api/study/note", json={"note": "ch.3"}).json["ok"]


def test_summary_endpoint_empty(client):
    assert client.get("/api/study/summary").json["total_min"] == 0


def test_study_page_served(client):
    r = client.get("/study")
    assert r.status_code == 200 and b'id="heat"' in r.data


def test_summary_with_mangled_log(client, tmp_path):
    (tmp_path / "study.csv").write_text(
        "date,studied_min\n2026-11-01,abc\n\n2026-11-01,30\n", encoding="utf-8")
    assert client.get("/api/study/summary").json["total_min"] == 30


def test_settings_hotkeys(client, console):
    r = client.post("/api/settings", json={"hotkey_menu": "Shift+Ctrl+F9"})
    assert r.status_code == 200 and console.settings.hotkey_menu == "ctrl+shift+f9"
    assert "hotkeys" in r.json
    assert client.post("/api/settings", json={"hotkey_pick": "q"}).status_code == 400
    assert client.post("/api/settings", json={"hotkey_pick": "ctrl+shift+f9"}).status_code == 400


def test_settings_endpoint(client, console):
    r = client.post("/api/settings", json={"daily_goal_min": 90, "chime": False})
    assert r.status_code == 200 and r.json["settings"]["daily_goal_min"] == 90
    assert console.settings.chime is False
    assert client.post("/api/settings", json={"daily_goal_min": 0}).status_code == 400
