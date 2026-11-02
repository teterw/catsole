"""Claude Code usage, for the Claude screen.

Three sources, all from Claude Code on this PC:

- its status line, which is handed the plan's 5-hour and weekly limits
  (rate_limits.five_hour / seven_day: used_percentage, resets_at) and is
  forwarded here by server/claude/statusline.py;
- its hooks, forwarded by server/claude/hook.py: UserPromptSubmit means a
  session is working, Stop that it is waiting for you, SessionEnd that it
  is gone;
- its transcripts under ~/.claude/projects, which hold every reply's token
  usage. One reply is written as several lines, so they are counted by
  message id.

A turn cut short with Esc fires no Stop hook, so "working" lapses on its
own after WORK_STALE_S without news from that session.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

ACTIVE_S = 600
WORK_STALE_S = 600
SCAN_EVERY_S = 60


def _epoch(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


class ClaudeMonitor:
    def __init__(self, projects_dir: Path | None, clock: Callable[[], float] = time.time):
        self.projects_dir = Path(projects_dir) if projects_dir else None
        self.clock = clock
        self._lock = threading.Lock()
        # session id -> {"working": bool, "seen": epoch}
        self._sessions: dict[str, dict] = {}
        self._limits = {"p5": None, "r5": None, "p7": None, "r7": None}
        # Today's transcript reading, carried between scans.
        self._scan_day: date | None = None
        self._offsets: dict[Path, int] = {}
        self._ids: set[str] = set()
        self._msgs = 0
        self._tokens = 0
        self._thread: threading.Thread | None = None

    # ---- what Claude Code tells us -------------------------------------------

    def _session(self, session_id: str) -> dict:
        return self._sessions.setdefault(session_id or "?", {"working": False, "seen": 0.0})

    def on_status(self, payload: dict) -> None:
        with self._lock:
            self._session(str(payload.get("session_id") or "")).update(seen=self.clock())
            limits = payload.get("rate_limits")
            if not isinstance(limits, dict):
                return
            for key, name in (("five_hour", "5"), ("seven_day", "7")):
                block = limits.get(key)
                if not isinstance(block, dict):
                    continue
                pct = block.get("used_percentage")
                if isinstance(pct, (int, float)):
                    self._limits["p" + name] = int(round(pct))
                reset = _epoch(block.get("resets_at"))
                if reset is not None:
                    self._limits["r" + name] = reset

    def on_event(self, payload: dict) -> None:
        session_id = str(payload.get("session_id") or "")
        event = payload.get("hook_event_name")
        with self._lock:
            if event == "SessionEnd":
                self._sessions.pop(session_id or "?", None)
                return
            session = self._session(session_id)
            session["seen"] = self.clock()
            if event == "UserPromptSubmit":
                session["working"] = True
            elif event == "Stop":
                session["working"] = False

    # ---- what the screen asks -------------------------------------------------

    def state(self) -> str:
        now = self.clock()
        with self._lock:
            sessions = list(self._sessions.values())
        if any(s["working"] and now - s["seen"] < WORK_STALE_S for s in sessions):
            return "work"
        if any(now - s["seen"] < ACTIVE_S for s in sessions):
            return "wait"
        return "idle"

    def active(self) -> bool:
        return self.state() != "idle"

    def limits(self) -> dict:
        with self._lock:
            return dict(self._limits)

    def today(self) -> tuple[int, int]:
        with self._lock:
            return self._msgs, self._tokens

    # ---- today's transcripts ----------------------------------------------------

    def scan_today(self, today: date | None = None) -> tuple[int, int]:
        """Messages and tokens (input + output + cache writes) for today."""
        today = today or date.today()
        if self._scan_day != today:
            self._scan_day = today
            self._offsets.clear()
            self._ids.clear()
            self._msgs = self._tokens = 0
        if self.projects_dir is None or not self.projects_dir.is_dir():
            return self._msgs, self._tokens
        for path in self.projects_dir.rglob("*.jsonl"):
            try:
                if date.fromtimestamp(path.stat().st_mtime) != today:
                    continue
                self._read_new(path, today)
            except OSError:
                continue
        return self._msgs, self._tokens

    def _read_new(self, path: Path, today: date) -> None:
        offset = self._offsets.get(path, 0)
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        # A line still being written has no newline yet: leave it for next time.
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self._offsets[path] = offset + end + 1
        msgs, tokens = 0, 0
        for raw in chunk[:end].splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            msg_id = message.get("id")
            if not msg_id or msg_id in self._ids:
                continue
            stamp = _epoch(entry.get("timestamp"))
            if stamp is None or date.fromtimestamp(stamp) != today:
                continue
            self._ids.add(msg_id)
            usage = message.get("usage") or {}
            msgs += 1
            tokens += sum(int(usage.get(k) or 0) for k in (
                "input_tokens", "output_tokens", "cache_creation_input_tokens"))
        with self._lock:
            self._msgs += msgs
            self._tokens += tokens

    def start(self) -> None:
        if self.projects_dir is None or self._thread is not None:
            return

        def loop():
            while True:
                try:
                    self.scan_today()
                except Exception:
                    log.exception("reading Claude transcripts failed")
                time.sleep(SCAN_EVERY_S)

        self._thread = threading.Thread(target=loop, name="claude-scan", daemon=True)
        self._thread.start()
