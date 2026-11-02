"""The menu the two shortcuts drive on the OLED.

The board has no buttons, so two global shortcuts stand in for them: one
opens the menu and moves down it, the other picks. Starting a new subject
still needs the panel; the menu offers the recent ones.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .protocol import fold_ascii
from .textbitmap import needs_bitmap

MENU_IDLE_S = 6.0
RECENT_IN_MENU = 3
# What fits inside the highlight bar at 6px a character.
LABEL_MAX = 15


def _label(subject: str, suffix: str) -> str:
    """Subject and timer, the subject cut short so the timer -- the one
    thing telling two entries apart -- always shows."""
    room = LABEL_MAX - len(suffix) - 1
    folded = fold_ascii(subject)
    if needs_bitmap(subject) or len(folded) <= room:
        return f"{subject} {suffix}"
    return f"{folded[:room - 1]}~ {suffix}"


@dataclass(frozen=True)
class MenuItem:
    label: str
    action: str           # start | pause | resume | add5 | stop | close
    subject: str = ""
    timer: str = ""
    minutes: int | None = None


def build_items(view: dict | None, recent: list[dict]) -> list[MenuItem]:
    if view is None:
        items = []
        for r in recent[:RECENT_IN_MENU]:
            timer = r.get("timer") or "countup"
            minutes = r.get("minutes") if timer == "countdown" else None
            suffix = f"{minutes}m" if minutes else "up"
            items.append(MenuItem(_label(r.get("subject", ""), suffix), "start",
                                  r.get("subject", ""), timer, minutes))
        return items + [MenuItem("close", "close")]

    items = [MenuItem("resume", "resume") if view.get("state") == "paused"
             else MenuItem("pause", "pause")]
    if view.get("timer") == "countdown":
        items.append(MenuItem("+5 min", "add5"))
    return items + [MenuItem("stop", "stop"), MenuItem("close", "close")]


class StudyMenu:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.items: list[MenuItem] = []
        self.sel = 0
        self._pressed: float | None = None

    def visible(self) -> bool:
        return (
            self._pressed is not None
            and bool(self.items)
            and self.clock() - self._pressed < MENU_IDLE_S
        )

    def press_next(self, items: list[MenuItem]) -> None:
        """Open the menu, or move down it. The items are taken fresh each
        press, so a session paused from the panel meanwhile shows right."""
        if self.visible():
            self.sel = (self.sel + 1) % len(items)
        else:
            self.sel = 0
        self.items = items
        self._pressed = self.clock()

    def press_pick(self) -> MenuItem | None:
        if not self.visible():
            return None
        item = self.items[min(self.sel, len(self.items) - 1)]
        self.close()
        return item

    def close(self) -> None:
        self._pressed = None
