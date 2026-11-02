"""The study log, a CSV the user can open in Excel, and the timer's state file.

Rows are only ever appended, so the user can fix a row by hand while the
service runs; the one exception is a note added after a session ended,
which rewrites the last row only if it is still exactly what was written.
Excel locks a CSV it has open, so every write can fail, and callers keep
what they could not write.
"""

from __future__ import annotations

import codecs
import copy
import csv
import io
import json
import locale
import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

COLUMNS = [
    "date", "start", "end", "subject", "timer", "planned_min", "studied_min",
    "paused_min", "distractions", "distraction_min", "note",
]


_SHORT_DATE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$")


def _parse_date(text) -> date | None:
    """ISO as written, or the short date Excel puts back when it saves.

    Excel rewrites dates in the system's own format: 11/2/2026 (month
    first) on an English system, 2/11/2569 (day first, Buddhist year) on a
    Thai one. A day over 12 settles the order whatever the year.
    """
    text = str(text or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    m = _SHORT_DATE.match(text)
    if not m:
        return None
    a, b, year = (int(g) for g in m.groups())
    if year > 2400:
        year -= 543
        day, month = a, b
    elif a > 12:
        day, month = a, b
    else:
        month, day = a, b
    try:
        return date(year, month, day)
    except ValueError:
        return None


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
        day = _parse_date(row.get("date"))
        if day is None:
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


def _decode(raw: bytes) -> tuple[str, bool]:
    """The file's text, and whether all of it was UTF-8.

    Excel can save in the system code page, and rows appended after that
    are UTF-8 again, so each line is decoded on its own."""
    try:
        return raw.decode("utf-8-sig"), True
    except UnicodeDecodeError:
        pass
    fallback = locale.getpreferredencoding(False)
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8):]
    lines = []
    for line in raw.split(b"\n"):
        try:
            lines.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            lines.append(line.decode(fallback, errors="replace"))
    return "\n".join(lines), False


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
            fieldnames = COLUMNS
            if size:
                # Never add UTF-8 to a file in another encoding: convert it.
                text, utf8 = _decode(self.path.read_bytes())
                # Rows go in under the file's own header, so columns moved
                # or removed by hand stay lined up.
                header = next(csv.reader(io.StringIO(text, newline="")), [])
                header = [h.strip() for h in header]
                if "date" in header and "studied_min" in header:
                    fieldnames = header
                if not utf8:
                    tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                    tmp.write_text(text, encoding="utf-8-sig", newline="")
                    os.replace(tmp, self.path)
                    size = self.path.stat().st_size
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
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
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
        text, _ = _decode(self.path.read_bytes())
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
