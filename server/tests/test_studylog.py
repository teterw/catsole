"""Tests for the study log CSV and the study state file."""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.studylog import COLUMNS, LoggedSession, StudyLog, StudyStore

S = LoggedSession(date(2026, 11, 2), "14:05:00", "14:30:00", "ฟิสิกส์", "countdown",
                  25.0, 25.0, 0.0, 0, 0.0)


def test_columns_match_spec():
    assert COLUMNS == ["date", "start", "end", "subject", "timer", "planned_min",
                       "studied_min", "paused_min", "distractions", "distraction_min",
                       "note"]


def test_to_row_formats_minutes():
    row = S.to_row()
    assert row["studied_min"] == "25.0" and row["date"] == "2026-11-02"
    countup = LoggedSession(date(2026, 11, 2), "", "", "math", "countup", None, 3.25, 0.0, 0, 0.0)
    assert countup.to_row()["planned_min"] == "" and countup.to_row()["studied_min"] == "3.2"


def test_first_append_writes_bom_and_header(tmp_path):
    log = StudyLog(tmp_path / "study.csv")
    assert log.append(S)
    raw = (tmp_path / "study.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbfdate,start,end,subject")
    log.append(S)
    assert (tmp_path / "study.csv").read_bytes().count(b"date,start") == 1


def test_round_trip_thai(tmp_path):
    log = StudyLog(tmp_path / "study.csv")
    log.append(S)
    log.append(S)
    assert log.read() == [S, S]


def test_append_after_hand_edit_without_newline(tmp_path):
    p = tmp_path / "study.csv"
    StudyLog(p).append(S)
    p.write_bytes(p.read_bytes().rstrip(b"\r\n"))
    StudyLog(p).append(S)
    assert len(StudyLog(p).read()) == 2


def test_read_skips_bad_rows(tmp_path):
    p = tmp_path / "study.csv"
    p.write_text(
        "date,start,end,subject,timer,planned_min,studied_min\n\n"
        "2026-11-01,,,math,countup,,abc\n"
        "not-a-date,,,x,countup,,5\n"
        "2026-11-01,,,math,countup,,30\n",
        encoding="utf-8",
    )
    rows = StudyLog(p).read()
    assert [(r.subject, r.studied_min) for r in rows] == [("math", 30.0)]


def test_read_missing_file_is_empty(tmp_path):
    assert StudyLog(tmp_path / "nope.csv").read() == []


def test_locked_file_returns_false(tmp_path, monkeypatch):
    log = StudyLog(tmp_path / "study.csv")

    def locked(*args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr("builtins.open", locked)
    assert log.append(S) is False


def test_memory_log_when_no_path():
    log = StudyLog(None)
    assert log.append(S) and log.read() == [S]


def test_replace_last_note(tmp_path):
    log = StudyLog(tmp_path / "study.csv")
    log.append(S)
    log.append(S)
    assert log.replace_last_note(S, "finished ch.3")
    rows = log.read()
    assert rows[-1].note == "finished ch.3" and rows[0].note == ""


def test_replace_last_note_refuses_after_hand_edit(tmp_path):
    p = tmp_path / "study.csv"
    log = StudyLog(p)
    log.append(S)
    p.write_text(p.read_text(encoding="utf-8-sig").replace("25.0,25.0", "25.0,26.0"),
                 encoding="utf-8-sig")
    assert log.replace_last_note(S, "x") is False


def test_store_round_trip_and_none(tmp_path):
    st = StudyStore(tmp_path / "s.json")
    st.save({"active": {"a": 1}, "pending": [], "last": None})
    assert StudyStore(tmp_path / "s.json").load()["active"] == {"a": 1}
    assert StudyStore(None).load() == {"active": None, "pending": [], "last": None}


def test_store_corrupt_gives_defaults(tmp_path):
    (tmp_path / "s.json").write_text("{", encoding="utf-8")
    assert StudyStore(tmp_path / "s.json").load() == {"active": None, "pending": [], "last": None}
