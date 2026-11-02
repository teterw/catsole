"""Fixes from the final review: bad input that must not crash or corrupt
anything, and a few behaviours a person would expect."""

import json
import math
import os
import socket
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.claude import ClaudeMonitor
from catsole.hotkeys import parse_combo
from catsole.menu import build_items
from catsole.study import StudyTimer
from catsole.studylog import LoggedSession, StudyLog, StudyStore

from test_claude import SAMPLE, SCRIPTS, run_script
from test_study_console import Clock, clock, console  # noqa: F401  (fixtures)

S = LoggedSession(date(2026, 11, 2), "14:05:00", "14:30:00", "ฟิสิกส์", "countdown",
                  25.0, 25.0, 0.0, 0, 0.0)


# ---- 1. Claude endpoint input ---------------------------------------------------

def test_bad_reset_values_are_ignored_or_converted(clock):
    mon = ClaudeMonitor(None, clock)
    mon.on_status({"session_id": "a", "rate_limits": {
        "five_hour": {"used_percentage": float("nan"), "resets_at": float("nan")},
        "seven_day": {"used_percentage": 250, "resets_at": 1_800_300_000_000}}})
    limits = mon.limits()
    assert limits["p5"] is None and limits["r5"] is None
    assert limits["p7"] == 100 and limits["r7"] == 1_800_300_000.0


def test_absurd_reset_time_is_rejected(clock):
    mon = ClaudeMonitor(None, clock)
    mon.on_status({"session_id": "a", "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 5e10}}})
    assert mon.limits()["r5"] is None


def test_status_endpoint_ignores_non_json_posts(console, clock):
    from catsole.web import create_app

    console.claude = ClaudeMonitor(None, clock)
    c = create_app(console).test_client()
    c.post("/api/claude/status", data=json.dumps(SAMPLE), content_type="text/plain")
    assert console.claude.limits()["p5"] is None


def test_a_failing_tick_does_not_stop_the_service(console, monkeypatch):
    def boom():
        raise ValueError("bad frame")

    monkeypatch.setattr(console, "build_frame", boom)
    console.tick_safely()   # logs, does not raise


# ---- 2. study.csv after Excel ------------------------------------------------------

def test_reads_excel_us_dates(tmp_path):
    p = tmp_path / "study.csv"
    p.write_text("date,start,end,subject,timer,planned_min,studied_min\n"
                 "11/2/2026,14:05,14:30,math,countdown,25,25\n", encoding="utf-8")
    assert [r.date for r in StudyLog(p).read()] == [date(2026, 11, 2)]


def test_reads_thai_buddhist_dates(tmp_path):
    p = tmp_path / "study.csv"
    p.write_text("date,studied_min\n2/11/2569,25\n", encoding="utf-8-sig")
    assert [r.date for r in StudyLog(p).read()] == [date(2026, 11, 2)]


def test_day_first_when_month_impossible(tmp_path):
    p = tmp_path / "study.csv"
    p.write_text("date,studied_min\n25/10/2026,25\n", encoding="utf-8")
    assert [r.date for r in StudyLog(p).read()] == [date(2026, 10, 25)]


def test_ansi_file_converted_before_append(tmp_path):
    import locale

    if locale.getpreferredencoding(False).lower() != "cp1252":
        pytest.skip("Excel's ANSI save here would use another code page")
    p = tmp_path / "study.csv"
    p.write_bytes("date,studied_min,subject\r\n2026-11-01,30,café\r\n".encode("cp1252"))
    log = StudyLog(p)
    assert log.append(S)
    rows = log.read()
    assert [r.subject for r in rows] == ["café", "ฟิสิกส์"]
    p.read_bytes().decode("utf-8-sig")   # the whole file is UTF-8 now


# ---- 3. Claude "working" after Esc -----------------------------------------------

def test_status_pings_do_not_extend_working(clock):
    mon = ClaudeMonitor(None, clock)
    mon.on_event({"session_id": "a", "hook_event_name": "UserPromptSubmit"})
    for _ in range(6):
        clock.t += 300
        mon.on_status({"session_id": "a"})
    assert mon.state() == "wait"


def test_notification_means_your_turn(clock):
    mon = ClaudeMonitor(None, clock)
    mon.on_event({"session_id": "a", "hook_event_name": "UserPromptSubmit"})
    mon.on_event({"session_id": "a", "hook_event_name": "Notification"})
    assert mon.state() == "wait"


# ---- 4. long subjects in the OLED menu ---------------------------------------------

def test_long_subject_label_keeps_its_timer():
    items = build_items(None, [{"subject": "linear algebra", "timer": "countdown", "minutes": 25},
                               {"subject": "organic chemistry", "timer": "countup", "minutes": None}])
    assert [i.label for i in items[:2]] == ["linear alg~ 25m", "organic che~ up"]
    assert items[0].subject == "linear algebra"


# ---- 5. shortcuts that would eat ordinary keys ----------------------------------------

@pytest.mark.parametrize("bad", ["delete", "home", "end", "insert", "pageup", "pagedown", "numpad3", "f5"])
def test_everyday_keys_need_a_modifier(bad):
    with pytest.raises(ValueError):
        parse_combo(bad)


@pytest.mark.parametrize("ok", ["pause", "scrolllock", "f13", "ctrl+delete", "alt+home", "ctrl+f5"])
def test_rare_keys_alone_or_with_modifier(ok):
    parse_combo(ok)


# ---- 6. the scripts only knock when catsole is up -----------------------------------------

@pytest.fixture
def listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(3)
    hits = []

    def accept():
        try:
            conn, _ = srv.accept()
            hits.append(conn.recv(65536))
            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield srv.getsockname()[1], hits, t
    srv.close()


def test_script_skips_network_without_port_file(tmp_path, listener):
    port, hits, t = listener
    env = {**os.environ, "CATSOLE_DIR": str(tmp_path), "CATSOLE_PORT": str(port)}
    subprocess.run([sys.executable, str(SCRIPTS / "hook.py")], input="{}", capture_output=True,
                   text=True, timeout=5, env=env)
    t.join(0.5)
    assert hits == []


def test_script_posts_to_the_port_in_the_port_file(tmp_path, listener):
    port, hits, t = listener
    (tmp_path / "catsole.port").write_text(str(port), encoding="utf-8")
    r = run_script("statusline.py", SAMPLE, data_dir=tmp_path)
    t.join(3)
    assert r.stdout.strip() == "5h 42% · wk 18%"
    assert hits and b"/api/claude/status" in hits[0]


def test_service_writes_its_port_file(console, tmp_path):
    from catsole.web import write_port_file

    write_port_file(console)
    assert (tmp_path / "catsole.port").read_text(encoding="utf-8").strip() == "8730"


# ---- 8. a damaged state file -----------------------------------------------------------

def test_malformed_state_file_is_dropped(tmp_path, clock):
    (tmp_path / "s.json").write_text(json.dumps({"active": {
        "subject": "x", "timer": "countup", "planned_s": None,
        "started": "abc", "last_tick": "abc"}, "pending": [], "last": None}), encoding="utf-8")
    t = StudyTimer(StudyLog(None), StudyStore(tmp_path / "s.json"), clock)
    t.restore()
    assert not t.active
