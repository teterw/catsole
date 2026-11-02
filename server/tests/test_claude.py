"""Tests for Claude usage: the status-line data, the hooks, today's counts,
the Claude screen's frame, and the scripts Claude Code runs."""

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.app import MODES, next_mode
from catsole.claude import ClaudeMonitor

from test_study_console import Clock, clock, console  # noqa: F401  (fixtures)

SCRIPTS = Path(__file__).resolve().parents[1] / "claude"
SAMPLE = {
    "session_id": "abc",
    "rate_limits": {
        "five_hour": {"used_percentage": 42.4, "resets_at": 1_800_004_320},
        "seven_day": {"used_percentage": 18.0, "resets_at": 1_800_300_000},
    },
}


@pytest.fixture
def mon(clock):
    return ClaudeMonitor(None, clock)


def test_status_keeps_last_limits(mon):
    mon.on_status(SAMPLE)
    mon.on_status({"session_id": "abc"})
    limits = mon.limits()
    assert limits["p5"] == 42 and limits["r5"] == 1_800_004_320 and limits["p7"] == 18


def test_iso_reset_time(mon):
    mon.on_status({"session_id": "a", "rate_limits": {
        "seven_day": {"used_percentage": 18, "resets_at": "2027-01-15T08:00:00Z"}}})
    assert mon.limits()["r7"] == datetime(2027, 1, 15, 8, tzinfo=timezone.utc).timestamp()


def test_status_line_marks_session_seen(mon, clock):
    assert mon.state() == "idle"
    mon.on_status(SAMPLE)
    assert mon.state() == "wait" and mon.active()
    clock.t += 601
    assert mon.state() == "idle"


def test_working_goes_stale_without_stop(mon, clock):
    mon.on_event({"session_id": "a", "hook_event_name": "UserPromptSubmit"})
    assert mon.state() == "work"
    clock.t += 601
    assert mon.state() == "idle"


def test_any_working_session_wins(mon):
    mon.on_event({"session_id": "a", "hook_event_name": "UserPromptSubmit"})
    mon.on_event({"session_id": "b", "hook_event_name": "Stop"})
    assert mon.state() == "work"
    mon.on_event({"session_id": "a", "hook_event_name": "SessionEnd"})
    assert mon.state() == "wait"


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def asst(msg_id, ts, inp=0, out=0, cw=0, cr=0):
    return {"type": "assistant", "timestamp": ts, "message": {"id": msg_id, "usage": {
        "input_tokens": inp, "output_tokens": out,
        "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}}}


def write_lines(path, rows, end="\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + end)


def test_scan_today_dedupes_and_filters(tmp_path):
    now = datetime.now().astimezone()
    write_lines(tmp_path / "p" / "s.jsonl", [
        asst("m1", _iso(now), inp=10, out=5, cw=100, cr=9999),
        asst("m1", _iso(now), inp=10, out=5, cw=100, cr=9999),
        asst("m2", _iso(now - timedelta(hours=30)), inp=1, out=1, cw=1, cr=1),
        {"type": "user"},
    ])
    assert ClaudeMonitor(tmp_path).scan_today(now.date()) == (1, 115)


def test_scan_holds_back_partial_line(tmp_path):
    now = datetime.now().astimezone()
    path = tmp_path / "p" / "s.jsonl"
    line = json.dumps(asst("m2", _iso(now), out=7))
    write_lines(path, [asst("m1", _iso(now), out=3)])
    with open(path, "a", encoding="utf-8") as f:
        f.write(line[:20])
    mon = ClaudeMonitor(tmp_path)
    assert mon.scan_today(now.date()) == (1, 3)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line[20:] + "\n")
    assert mon.scan_today(now.date()) == (2, 10)


def test_scan_with_no_folder_is_zero():
    assert ClaudeMonitor(None).scan_today(date.today()) == (0, 0)


def test_next_mode_skips_claude_unless_available():
    assert "claude" in MODES
    assert next_mode("clock") == "lyrics"
    assert next_mode("clock", {"claude"}) == "claude"
    assert next_mode("claude", {"claude"}) == "lyrics"


def test_claude_frame_and_rotation(console, clock):
    console.claude = ClaudeMonitor(None, clock)
    assert "claude" not in console._available_modes()
    console.claude.on_status(SAMPLE)
    console.claude.on_event({"session_id": "abc", "hook_event_name": "Stop"})
    assert "claude" in console._available_modes()
    console.mode = "claude"
    f = console.build_frame()
    assert (f["mode"], f["cs"], f["p5"], f["p7"]) == ("claude", "wait", 42, 18)
    assert f["r5"] == "1h12" and f["td"] == "0 msgs 0 tok"


def test_claude_web_routes(console, clock):
    from catsole.web import create_app

    console.claude = ClaudeMonitor(None, clock)
    c = create_app(console).test_client()
    assert c.post("/api/claude/status", json=SAMPLE).json["ok"]
    assert c.post("/api/claude/event", json={"session_id": "abc", "hook_event_name": "UserPromptSubmit"}).json["ok"]
    assert c.get("/api/state").json["claude"]["state"] == "work"


def run_script(name, payload, data_dir=None):
    # A data folder of its own, so the live service's port file is never found.
    env = {**os.environ, "CATSOLE_DIR": str(data_dir or Path(__file__).parent / "no-such-dir")}
    return subprocess.run(
        [sys.executable, str(SCRIPTS / name)], input=json.dumps(payload), capture_output=True,
        text=True, encoding="utf-8", timeout=5, env=env)


def test_statusline_script_without_service():
    r = run_script("statusline.py", SAMPLE)
    assert r.returncode == 0 and r.stdout.strip() == "5h 42% · wk 18%"


def test_statusline_script_without_limits():
    r = run_script("statusline.py", {"session_id": "abc"})
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_hook_script_is_silent_without_service():
    r = run_script("hook.py", {"session_id": "abc", "hook_event_name": "Stop"})
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
