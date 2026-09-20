"""Now-playing metadata from Windows' System Media Transport Controls.

SMTC is per-user-session, which is why the whole service has to run in the
interactive logon session and cannot be a Windows service.

Windows only pushes a timeline update when something changes, not
continuously, so a naive read of `position` sits still for tens of seconds
at a time. Every consumer here works from `extrapolate_position` instead.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )

    WINRT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off Windows
    SessionManager = None
    PlaybackStatus = None
    WINRT_AVAILABLE = False


@dataclass
class NowPlaying:
    artist: str = ""
    title: str = ""
    album: str = ""
    duration_ms: int = 0
    position_ms: int = 0
    is_playing: bool = False
    app_id: str = ""

    @property
    def track_key(self) -> tuple[str, str]:
        """Identity of the track, ignoring playback position.

        Used to decide when a lyrics refetch is actually warranted.
        """
        return (self.artist.casefold(), self.title.casefold())

    @property
    def is_empty(self) -> bool:
        return not self.title

    @property
    def label(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or self.artist


def extrapolate_position(
    position_ms: int,
    last_updated: datetime | None,
    now: datetime,
    rate: float,
    is_playing: bool,
    duration_ms: int,
) -> int:
    """Project the reported position forward to the present moment.

    Paused playback is frozen at the reported value. A rate of zero is
    treated as normal speed, because some players report it that way while
    genuinely playing. Negative elapsed time is ignored so a clock skew or
    a stamp from the future cannot rewind the lyric.
    """
    if not is_playing or last_updated is None:
        return int(max(0, position_ms))

    elapsed_ms = (now - last_updated).total_seconds() * 1000.0
    if elapsed_ms < 0:
        elapsed_ms = 0.0

    effective_rate = rate if rate and rate > 0 else 1.0
    projected = position_ms + elapsed_ms * effective_rate

    if duration_ms and duration_ms > 0:
        projected = min(projected, duration_ms)
    return int(max(0, projected))


class MediaReader:
    """Polls the current SMTC session.

    WinRT's async calls are driven on one private event loop owned by this
    object, so callers stay synchronous and no loop is created per poll.
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._manager = None
        self._warned = False

    @staticmethod
    async def _await(operation):
        return await operation

    def _run(self, operation):
        return self._loop.run_until_complete(self._await(operation))

    def _ensure_manager(self):
        if self._manager is None and WINRT_AVAILABLE:
            self._manager = self._run(SessionManager.request_async())
        return self._manager

    def poll(self) -> NowPlaying | None:
        """Read the current session, or None if there is nothing playing."""
        if not WINRT_AVAILABLE:
            if not self._warned:
                log.warning("winrt is unavailable; lyrics mode will stay idle")
                self._warned = True
            return None

        try:
            manager = self._ensure_manager()
            if manager is None:
                return None

            session = manager.get_current_session()
            if session is None:
                return None

            props = self._run(session.try_get_media_properties_async())
            timeline = session.get_timeline_properties()
            playback = session.get_playback_info()

            is_playing = (
                playback is not None
                and playback.playback_status == PlaybackStatus.PLAYING
            )
            rate = 1.0
            if playback is not None and playback.playback_rate is not None:
                rate = float(playback.playback_rate)

            duration_ms = 0
            reported_ms = 0
            last_updated = None
            if timeline is not None:
                span = timeline.end_time - timeline.start_time
                duration_ms = max(0, int(span.total_seconds() * 1000))
                reported_ms = max(0, int(timeline.position.total_seconds() * 1000))
                last_updated = timeline.last_updated_time
                if last_updated is not None and last_updated.tzinfo is None:
                    last_updated = last_updated.replace(tzinfo=timezone.utc)
                # A never-updated timeline reports the epoch; treat as unknown.
                if last_updated is not None and last_updated.year < 2000:
                    last_updated = None

            position_ms = extrapolate_position(
                reported_ms,
                last_updated,
                datetime.now(timezone.utc),
                rate,
                is_playing,
                duration_ms,
            )

            return NowPlaying(
                artist=(props.artist or "").strip() if props else "",
                title=(props.title or "").strip() if props else "",
                album=(props.album_title or "").strip() if props else "",
                duration_ms=duration_ms,
                position_ms=position_ms,
                is_playing=is_playing,
                app_id=session.source_app_user_model_id or "",
            )
        except Exception as exc:  # WinRT raises a variety of OSError subclasses
            log.debug("SMTC poll failed: %s", exc)
            # Drop the cached manager so the next poll rebuilds it; the
            # session manager goes stale when the shell restarts.
            self._manager = None
            return None

    def close(self) -> None:
        try:
            self._loop.close()
        except Exception:
            pass
