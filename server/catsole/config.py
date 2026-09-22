"""Runtime configuration, overridable from config.json next to run.py."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / "cache"


@dataclass
class Config:
    # Serial
    serial_port: str | None = None  # None means autodetect by USB VID/PID
    baud: int = 115200

    # Web control panel
    web_host: str = "127.0.0.1"
    web_port: int = 8730

    # Poll cadence, in seconds
    media_poll_s: float = 0.25
    stats_poll_s: float = 1.0
    frame_interval_s: float = 0.25
    # 20Hz: fast enough that the bars track transients.
    eq_interval_s: float = 0.05

    # The spectrum reaches us already late: a 1024-sample buffer at
    # 48kHz is 21ms before analysis even starts, then transit and a
    # frame of render on top. The beat phase is advanced by this much
    # so the bob lands with what you hear rather than after it.
    beat_lead_ms: float = 70.0

    # With nothing playing the device cycles screens on its own.
    # Zero disables it.
    idle_rotate_s: float = 9.0

    # The screen jumps to stats when the machine starts working hard,
    # and holds there until it settles. Two thresholds rather than
    # one, so load hovering on the line does not flap the display.
    busy_enter_pct: float = 80.0
    busy_exit_pct: float = 55.0
    # How long a hand-picked mode sticks before rotation resumes.
    manual_hold_s: float = 90.0

    # Positive values push lyrics later, negative pull them earlier. Some
    # players report position ahead of what you actually hear.
    lyric_offset_ms: int = 0

    # Which sessions count as music. Windows reports the app, not the
    # site, so a YouTube tab and an Instagram tab in the same browser look
    # identical here -- the duration rule is what actually separates them.
    # Anything shorter than this is treated as a story or a reel.
    min_duration_s: float = 60.0
    # Empty allows every app. Add e.g. "brave", "chrome", "spotify" to
    # restrict it. Blocked apps are matched as substrings.
    allow_apps: list = field(default_factory=list)
    block_apps: list = field(default_factory=lambda: ["instagram"])
    require_artist: bool = False

    start_mode: str = "lyrics"
    cache_dir: Path = field(default_factory=lambda: DEFAULT_CACHE_DIR)
    user_agent: str = "catsole/1.0 (https://github.com/teterw/catsole)"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Load config.json if present; fall back to defaults otherwise."""
        config = cls()
        if path is None or not Path(path).exists():
            return config
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable config %s: %s", path, exc)
            return config

        for key, value in raw.items():
            if not hasattr(config, key):
                log.warning("ignoring unknown config key: %s", key)
                continue
            if key == "cache_dir":
                value = Path(value)
            setattr(config, key, value)
        return config

    def to_dict(self) -> dict:
        data = asdict(self)
        data["cache_dir"] = str(self.cache_dir)
        return data
