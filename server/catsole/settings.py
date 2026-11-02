"""Settings the user changes from the panel.

Kept apart from config.json, which holds hand-tuned service settings: these
are written by the service itself, every time the panel changes one, so a
hand edit to config.json is never overwritten.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

# How many recent subjects the panel and the shortcut menu remember.
RECENT_MAX = 6


@dataclass
class UserSettings:
    daily_goal_min: int = 60
    chime: bool = True
    # Newest first: {"subject", "timer", "minutes"}.
    recent: list = field(default_factory=list)
    hotkey_menu: str = "ctrl+f11"
    hotkey_pick: str = "ctrl+f12"
    # {"city", "lat", "lon"}, or None until a city is set.
    weather: dict | None = None

    @classmethod
    def load(cls, path: Path | None) -> "UserSettings":
        settings = cls()
        if path is None or not Path(path).exists():
            return settings
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable settings %s: %s", path, exc)
            return settings
        if not isinstance(raw, dict):
            return settings
        known = {f.name for f in fields(cls)}
        for key, value in raw.items():
            if key in known:
                setattr(settings, key, value)
        return settings

    def save(self, path: Path | None) -> None:
        if path is None:
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)

    def touch_recent(self, subject: str, timer: str, minutes: int | None) -> None:
        """Put a subject at the front, with the timer it was last run with."""
        key = subject.casefold()
        kept = [r for r in self.recent if str(r.get("subject", "")).casefold() != key]
        entry = {"subject": subject, "timer": timer, "minutes": minutes}
        self.recent = [entry] + kept[: RECENT_MAX - 1]
