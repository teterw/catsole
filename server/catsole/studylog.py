"""The study log, a CSV the user can open in Excel, and the timer's state file.

Rows are only ever appended, so the user can fix a row by hand while the
service runs; the one exception is a note added after a session ended,
which rewrites the last row only if it is still exactly what was written.
Excel locks a CSV it has open, so every write can fail, and callers keep
what they could not write.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import locale
import logging
import os
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

COLUMNS = [
    "date", "start", "end", "subject", "timer", "planned_min", "studied_min",
    "paused_min", "distractions", "distraction_min", "note",
]


def _minutes(value: float) -> str:
    return f"{value:.1f}"


def _float(text, default: float | None = 0.0) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class LoggedSession:
    date: date
    start: str
    end: str
    subject: str
    timer: str
    planned_min: float | None
    studied_min: float
    paused_min: float
    distractions: int
    distraction_min: float
    note: str = ""

    def to_row(self) -> dict[str, str]:
        return {
            "date": self.date.isoformat(),
            "start": self.start,
            "end": self.end,
            "subject": self.subject,
            "timer": self.timer,
            "planned_min": "" if self.planned_min is None else _minutes(self.planned_min),
            "studied_min": _minutes(self.studied_min),
            "paused_min": _minutes(self.paused_min),
            "distractions": str(self.distractions),
            "distraction_min": _minutes(self.distraction_min),
            "note": self.note,
        }

    @classmethod
    def from_row(cls, row: dict) -> "LoggedSession | None":
        """A row read back from the file, or None if it can't be used.

        Only the date and the minutes studied are essential; a hand edit
        that mangles anything else still leaves a row worth counting.
        """
        try:
            day = date.fromisoformat(str(row.get("date") or "").strip())
        except ValueError:
            return None
        studied = _float(row.get("studied_min"), None)
        if studied is None:
            return None
        try:
            distractions = int(_float(row.get("distractions")) or 0)
        except (OverflowError, ValueError):
            distractions = 0
        return cls(
            date=day,
            start=str(row.get("start") or ""),
            end=str(row.get("end") or ""),
            subject=str(row.get("subject") or ""),
            timer=str(row.get("timer") or ""),
            planned_min=_float(row.get("planned_min"), None),
            studied_min=studied,
            paused_min=_float(row.get("paused_min")) or 0.0,
            distractions=distractions,
            distraction_min=_float(row.get("distraction_min")) or 0.0,
            note=str(row.get("note") or ""),
        )


def _decode(raw: bytes) -> str:
    """UTF-8 as written, or the system code page if Excel re-saved it."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


class StudyLog:
    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self._memory: list[LoggedSession] = []

    def append(self, session: LoggedSession) -> bool:
        if self.path is None:
            self._memory.append(session)
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            size = self.path.stat().st_size if self.path.exists() else 0
            needs_newline = False
            if size:
                with open(self.path, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    needs_newline = f.read(1) != b"\n"
            # utf-8-sig writes its BOM on every open, so only the open that
            # starts the file may use it.
            encoding = "utf-8" if size else "utf-8-sig"
            with open(self.path, "a", encoding=encoding, newline="") as f:
                if needs_newline:
                    f.write("\r\n")
                writer = csv.DictWriter(f, fieldnames=COLUMNS)
                if not size:
                    writer.writeheader()
                writer.writerow(session.to_row())
            return True
        except OSError as exc:
            log.warning("could not write %s: %s", self.path, exc)
            return False

    def signature(self) -> tuple:
        """Changes whenever the log does, hand edits included."""
        if self.path is None:
            return ("memory", len(self._memory), id(self._memory[-1]) if self._memory else 0)
        try:
            st = self.path.stat()
        except OSError:
            return ("missing",)
        return (st.st_mtime_ns, st.st_size)

    def _rows(self) -> list[list[str]]:
        text = _decode(self.path.read_bytes())
        return list(csv.reader(io.StringIO(text, newline="")))

    def read(self) -> list[LoggedSession]:
        if self.path is None:
            return list(self._memory)
        try:
            rows = self._rows()
        except OSError:
            return []
        if not rows:
            return []
        header = [h.strip() for h in rows[0]]
        sessions = []
        for row in rows[1:]:
            if not any(cell.strip() for cell in row):
                continue
            session = LoggedSession.from_row(dict(zip(header, row)))
            if session is not None:
                sessions.append(session)
        return sessions

    def replace_last_note(self, expected: LoggedSession, note: str) -> bool:
        """Set the note on the last row, if it is still the row expected."""
        if self.path is None:
            if self._memory and self._memory[-1] == expected:
                self._memory[-1] = _with_note(expected, note)
                return True
            return False
        try:
            rows = self._rows()
        except OSError:
            return False
        if len(rows) < 2:
            return False
        header = [h.strip() for h in rows[0]]
        if "note" not in header:
            return False
        last = len(rows) - 1
        while last > 0 and not any(cell.strip() for cell in rows[last]):
            last -= 1
        if last == 0 or LoggedSession.from_row(dict(zip(header, rows[last]))) != expected:
            return False
        row = rows[last] + [""] * (len(header) - len(rows[last]))
        row[header.index("note")] = note
        rows[last] = row
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
                csv.writer(f).writerows(rows)
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.warning("could not rewrite %s: %s", self.path, exc)
            return False


def _with_note(session: LoggedSession, note: str) -> LoggedSession:
    return replace(session, note=note)


def _empty_state() -> dict:
    return {"active": None, "pending": [], "last": None}


class StudyStore:
    """The running session and unwritten rows, so neither dies with the service."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self._memory = _empty_state()

    def load(self) -> dict:
        if self.path is None:
            return copy.deepcopy(self._memory)
        state = _empty_state()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return state
        if isinstance(raw, dict):
            for key in state:
                if key in raw:
                    state[key] = raw[key]
        return state

    def save(self, state: dict) -> None:
        if self.path is None:
            self._memory = copy.deepcopy(state)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not save study state: %s", exc)
