"""The study timer.

Time is counted from wall-clock steps between ticks, so a session can be
saved and carried across a restart. A step longer than GAP_SUSPEND_S can only
mean the machine was asleep (the loop ticks every few milliseconds), and
sleep is never counted: the session is paused from the tick before it.

Every change is saved to the state file, and so is the running session every
few seconds. A restart within RESTART_GRACE_S is a crash or a flash, and the
gap counts; a longer one is treated like sleep.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from typing import Callable

from .studylog import LoggedSession, StudyLog, StudyStore

log = logging.getLogger(__name__)

GAP_SUSPEND_S = 30
RESTART_GRACE_S = 120
ASK_AFTER_S = 3 * 3600
ASK_TIMEOUT_S = 600
PAUSE_LIMIT_S = 1800
MIN_LOG_S = 60
DISTRACTION_MIN_S = 15
DISTRACTION_END_S = 3
SAVE_EVERY_S = 5
RETRY_PENDING_S = 60
MAX_COUNTDOWN_MIN = 600

TIMERS = ("countdown", "countup")


@dataclass
class _Session:
    subject: str
    timer: str
    planned_s: float | None
    started: float            # wall clock, epoch seconds
    last_tick: float
    studied_s: float = 0.0
    paused_s: float = 0.0
    state: str = "running"    # running | paused | asking
    # When the current pause began, for the end time of a session the
    # pause limit ends; and when its limit started counting, which is the
    # wake-up time after sleep rather than the moment before it.
    pause_began: float | None = None
    paused_since: float | None = None
    next_ask: float = ASK_AFTER_S
    asked_at: float | None = None
    studied_at_ask: float = 0.0
    distractions: int = 0
    distraction_s: float = 0.0
    dist_start: float | None = None
    dist_last: float | None = None
    dist_counted: bool = False
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "_Session | None":
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None


@dataclass
class Finished:
    session: LoggedSession | None
    reason: str
    studied_s: float


def _hms(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


class StudyTimer:
    def __init__(self, log: StudyLog, store: StudyStore,
                 clock: Callable[[], float] = time.time):
        self.log = log
        self.store = store
        self.clock = clock
        self._lock = threading.RLock()
        self._s: _Session | None = None
        self._pending: list[dict] = []
        self._last: Finished | None = None
        self._last_save = 0.0
        self._last_retry = 0.0

    # ---- persistence -----------------------------------------------------

    def _save(self) -> None:
        self.store.save({
            "active": asdict(self._s) if self._s else None,
            "pending": list(self._pending),
            "last": self._last.session.to_row() if self._last and self._last.session else None,
        })
        self._last_save = self.clock()

    def restore(self) -> None:
        with self._lock:
            state = self.store.load()
            self._pending = [r for r in state.get("pending") or [] if isinstance(r, dict)]
            last = state.get("last")
            if isinstance(last, dict):
                session = LoggedSession.from_row(last)
                if session is not None:
                    self._last = Finished(session, "stopped", session.studied_min * 60)
            active = state.get("active")
            s = _Session.from_dict(active) if isinstance(active, dict) else None
            if s is None:
                return
            now = self.clock()
            gap = max(0.0, now - s.last_tick)
            if gap <= RESTART_GRACE_S:
                # A crash or a flash: the user kept studying through it.
                if s.state in ("running", "asking"):
                    s.studied_s += gap
                else:
                    s.paused_s += gap
            else:
                self._suspend(s, now)
            s.last_tick = now
            self._s = s
            log.info("study session restored: %s (%s)", s.subject, s.state)
            self._save()

    # ---- state ------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._s is not None

    @property
    def last(self) -> Finished | None:
        return self._last

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def save_error(self) -> bool:
        return bool(self._pending)

    def view(self) -> dict | None:
        with self._lock:
            s = self._s
            if s is None:
                return None
            return {
                "subject": s.subject,
                "timer": s.timer,
                "planned_s": s.planned_s,
                "studied_s": s.studied_s,
                "left_s": max(0.0, s.planned_s - s.studied_s) if s.planned_s else None,
                "state": s.state,
                "distracted": s.dist_counted,
                "started": datetime.fromtimestamp(s.started).strftime("%H:%M"),
            }

    # ---- commands -----------------------------------------------------------

    def start(self, subject: str, timer: str, minutes: int | None) -> bool:
        with self._lock:
            subject = (subject or "").strip()
            if self._s is not None or not subject or timer not in TIMERS:
                return False
            planned = None
            if timer == "countdown":
                if not isinstance(minutes, (int, float)) or not 1 <= minutes <= MAX_COUNTDOWN_MIN:
                    return False
                planned = float(minutes) * 60
            now = self.clock()
            self._s = _Session(subject=subject, timer=timer, planned_s=planned,
                               started=now, last_tick=now)
            # A new session closes the last one's note box.
            self._last = None
            self._save()
            return True

    def pause(self) -> bool:
        with self._lock:
            s = self._s
            if s is None or s.state == "paused":
                return False
            now = self.clock()
            self._step(s, now, distracted=False)
            s.state = "paused"
            s.pause_began = s.paused_since = now
            s.asked_at = None
            self._save()
            return True

    def resume(self) -> bool:
        with self._lock:
            s = self._s
            if s is None or s.state != "paused":
                return False
            self._step(s, self.clock(), distracted=False)
            s.state = "running"
            s.pause_began = s.paused_since = None
            self._save()
            return True

    def add_minutes(self, n: int = 5) -> bool:
        with self._lock:
            s = self._s
            if s is None or s.timer != "countdown":
                return False
            s.planned_s += n * 60
            self._save()
            return True

    def answer_yes(self) -> bool:
        with self._lock:
            s = self._s
            if s is None or s.state != "asking":
                return False
            s.state = "running"
            s.asked_at = None
            s.next_ask += ASK_AFTER_S
            self._save()
            return True

    def stop(self) -> Finished | None:
        with self._lock:
            s = self._s
            if s is None:
                return None
            now = self.clock()
            self._step(s, now, distracted=False)
            return self._finish(s, "stopped", s.studied_s, now)

    def set_note(self, text: str) -> bool:
        with self._lock:
            text = text or ""
            if self._s is not None:
                self._s.note = text
                self._save()
                return True
            last = self._last
            if last is None or last.session is None:
                return False
            expected = last.session
            noted = replace(expected, note=text)
            row = expected.to_row()
            if row in self._pending:
                self._pending[self._pending.index(row)] = noted.to_row()
            elif not self.log.replace_last_note(expected, text):
                return False
            self._last = Finished(noted, last.reason, last.studied_s)
            self._save()
            return True

    # ---- the clock ----------------------------------------------------------

    def tick(self, distracted: bool = False) -> Finished | None:
        with self._lock:
            now = self.clock()
            if self._pending and now - self._last_retry >= RETRY_PENDING_S:
                self._retry_pending(now)
            s = self._s
            if s is None:
                return None
            self._step(s, now, distracted)

            if s.timer == "countdown" and s.studied_s >= s.planned_s:
                return self._finish(s, "done", s.planned_s, now)
            if s.state == "asking" and now - s.asked_at >= ASK_TIMEOUT_S:
                return self._finish(s, "no_answer", s.studied_at_ask, s.asked_at)
            if s.state == "paused" and now - s.paused_since >= PAUSE_LIMIT_S:
                return self._finish(s, "pause_limit", s.studied_s, s.pause_began)
            if s.timer == "countup" and s.state == "running" and s.studied_s >= s.next_ask:
                s.state = "asking"
                s.asked_at = now
                s.studied_at_ask = s.studied_s
                self._save()
            elif now - self._last_save >= SAVE_EVERY_S:
                self._save()
            return None

    def _step(self, s: _Session, now: float, distracted: bool) -> None:
        delta = now - s.last_tick
        if delta > GAP_SUSPEND_S:
            self._suspend(s, now)
            s.last_tick = now
            return
        # A clock set backwards counts nothing for the step, then carries on
        # from the new time.
        delta = max(0.0, delta)
        s.last_tick = now
        if s.state == "paused":
            s.paused_s += delta
            self._close_distraction(s)
            return
        s.studied_s += delta
        self._track_distraction(s, now, distracted)

    def _suspend(self, s: _Session, now: float) -> None:
        """The machine slept: pause from the last tick before it."""
        if s.state != "paused":
            s.pause_began = s.last_tick
            s.state = "paused"
            s.asked_at = None
        # The pause limit counts from waking, or a night's sleep would end
        # every session the moment the PC wakes.
        s.paused_since = now
        self._close_distraction(s)

    def _track_distraction(self, s: _Session, now: float, distracted: bool) -> None:
        if distracted:
            if s.dist_start is None:
                s.dist_start = now
            s.dist_last = now
            if not s.dist_counted and now - s.dist_start >= DISTRACTION_MIN_S:
                s.dist_counted = True
        elif s.dist_start is not None and now - s.dist_last >= DISTRACTION_END_S:
            self._close_distraction(s)

    def _close_distraction(self, s: _Session) -> None:
        if s.dist_start is not None and s.dist_counted:
            s.distractions += 1
            s.distraction_s += s.dist_last - s.dist_start
        s.dist_start = s.dist_last = None
        s.dist_counted = False

    def _finish(self, s: _Session, reason: str, studied_s: float, end: float) -> Finished:
        self._close_distraction(s)
        self._s = None
        session = None
        if studied_s >= MIN_LOG_S:
            started = datetime.fromtimestamp(s.started)
            session = LoggedSession(
                date=started.date(),
                start=_hms(s.started),
                end=_hms(end),
                subject=s.subject,
                timer=s.timer,
                planned_min=round(s.planned_s / 60, 1) if s.planned_s else None,
                studied_min=round(studied_s / 60, 1),
                paused_min=round(s.paused_s / 60, 1),
                distractions=s.distractions,
                distraction_min=round(s.distraction_s / 60, 1),
                note=s.note,
            )
            if not self.log.append(session):
                self._pending.append(session.to_row())
                self._last_retry = self.clock()
        finished = Finished(session, reason, studied_s)
        self._last = finished
        log.info("study session ended (%s): %s, %.1f min", reason, s.subject, studied_s / 60)
        self._save()
        return finished

    def _retry_pending(self, now: float) -> None:
        self._last_retry = now
        while self._pending:
            session = LoggedSession.from_row(self._pending[0])
            if session is not None and not self.log.append(session):
                break
            self._pending.pop(0)
        self._save()
