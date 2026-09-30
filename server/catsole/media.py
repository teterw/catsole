"""Now-playing metadata: Windows' System Media Transport Controls, or
MPRIS over D-Bus on Linux.

SMTC is per-user-session, which is why the whole service has to run in the
interactive logon session and cannot be a Windows service.

Windows only pushes a timeline update when something changes, not
continuously, so a naive read of `position` sits still for tens of seconds
at a time. Every consumer here works from `extrapolate_position` instead.
"""

from __future__ import annotations

import asyncio
import logging
import sys
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
    from winrt.windows.storage.streams import Buffer, InputStreamOptions

    WINRT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only off Windows
    SessionManager = None
    PlaybackStatus = None
    WINRT_AVAILABLE = False

try:
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection
    from jeepney.wrappers import unwrap_msg

    JEEPNEY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the extra
    JEEPNEY_AVAILABLE = False


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


def is_music(
    now_playing: "NowPlaying | None",
    min_duration_s: float = 60.0,
    allow_apps: list[str] | None = None,
    block_apps: list[str] | None = None,
    require_artist: bool = False,
) -> bool:
    """Decide whether a session is music worth showing.

    Windows reports the *application*, not the site, so a YouTube tab and
    an Instagram tab in the same browser are indistinguishable by app id
    alone. What does separate them is shape: songs run for minutes and
    carry an artist, while stories and reels are seconds long and usually
    carry neither. So this filters on duration and metadata, using the app
    lists only for whole applications worth including or excluding.
    """
    if now_playing is None or now_playing.is_empty:
        return False

    app = (now_playing.app_id or "").casefold()

    if block_apps:
        if any(bad.casefold() in app for bad in block_apps if bad):
            return False

    if allow_apps:
        if not any(good.casefold() in app for good in allow_apps if good):
            return False

    # A clip too short to be a song is almost certainly a story or a reel.
    # Zero means the source never reported a length, which is common for
    # live streams, so it is not treated as a failure.
    if min_duration_s > 0 and now_playing.duration_ms:
        if now_playing.duration_ms < min_duration_s * 1000:
            return False

    if require_artist and not now_playing.artist.strip():
        return False

    return True


def netflix_kind(now_playing: "NowPlaying | None") -> str | None:
    """Whether a session is Netflix: "watch", "browse", or None if not.

    A browser reports only the tab's title for a site that publishes no
    media metadata, and Netflix publishes none: its player page is titled
    "Netflix" and nothing more, with no artist. Its browse pages ("Home -
    Netflix" and so on) autoplay trailers, which nobody is watching. The
    Netflix app is recognised by its id instead.
    """
    if now_playing is None:
        return None
    title = now_playing.title.strip().casefold()
    in_app = "netflix" in (now_playing.app_id or "").casefold()
    untagged = not now_playing.artist.strip()
    if title.endswith((" - netflix", " | netflix")) and (in_app or untagged):
        return "browse"
    if in_app or (untagged and title == "netflix"):
        return "watch"
    return None


def netflix_show(now_playing: "NowPlaying") -> str:
    """The show's name, or "" when the session says only "Netflix"."""
    title = now_playing.title.strip()
    return "" if title.casefold() == "netflix" else title


# ---- Linux: MPRIS ------------------------------------------------------------
#
# Linux players and browsers publish what they are playing over D-Bus as
# MPRIS, one bus name per player (org.mpris.MediaPlayer2.<app>). Unlike
# Windows there is no single "current" session, so every player is read and
# one that is playing wins. Position is live at the moment it is read, so
# there is nothing to extrapolate.

MPRIS_PREFIX = "org.mpris.MediaPlayer2."


_SIGNATURE_CHARS = frozenset("ybnqiuxtdhsogva(){}")


def unwrap_variants(value):
    """Strip D-Bus variant wrappers, which jeepney returns as (signature, value)."""
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and value[0]
        and set(value[0]) <= _SIGNATURE_CHARS
    ):
        return unwrap_variants(value[1])
    if isinstance(value, dict):
        return {key: unwrap_variants(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unwrap_variants(item) for item in value]
    return value


def mpris_now_playing(bus_name: str, props: dict) -> "NowPlaying | None":
    """A player's MPRIS Player properties as NowPlaying, or None if idle."""
    meta = props.get("Metadata") or {}
    title = str(meta.get("xesam:title") or "").strip()
    if not title:
        return None
    artists = meta.get("xesam:artist") or []
    if isinstance(artists, (list, tuple)):
        artist = ", ".join(str(a).strip() for a in artists if str(a).strip())
    else:
        artist = str(artists).strip()
    duration_ms = max(0, int(meta.get("mpris:length") or 0) // 1000)
    position_ms = max(0, int(props.get("Position") or 0) // 1000)
    if duration_ms:
        position_ms = min(position_ms, duration_ms)
    return NowPlaying(
        artist=artist,
        title=title,
        album=str(meta.get("xesam:album") or "").strip(),
        duration_ms=duration_ms,
        position_ms=position_ms,
        is_playing=props.get("PlaybackStatus") == "Playing",
        app_id=bus_name[len(MPRIS_PREFIX):] if bus_name.startswith(MPRIS_PREFIX) else bus_name,
    )


def pick_mpris(players: "list[NowPlaying]") -> "NowPlaying | None":
    """The player to show: the first one playing, else the first paused."""
    for player in players:
        if player.is_playing:
            return player
    return players[0] if players else None


class MprisMediaReader:
    """Polls every MPRIS player on the session bus."""

    def __init__(self):
        self._conn = None
        self._warned = False

    def _call(self, address, method, signature=None, body=()):
        if self._conn is None:
            self._conn = open_dbus_connection(bus="SESSION")
        message = new_method_call(address, method, signature, body)
        return unwrap_msg(self._conn.send_and_get_reply(message, timeout=1.0))

    def poll(self) -> "NowPlaying | None":
        if not JEEPNEY_AVAILABLE:
            if not self._warned:
                log.warning("jeepney is unavailable; lyrics mode will stay idle")
                self._warned = True
            return None
        try:
            bus = DBusAddress(
                "/org/freedesktop/DBus",
                bus_name="org.freedesktop.DBus",
                interface="org.freedesktop.DBus",
            )
            names = self._call(bus, "ListNames")[0]
            players = []
            for name in sorted(names):
                if not name.startswith(MPRIS_PREFIX):
                    continue
                player = DBusAddress(
                    "/org/mpris/MediaPlayer2",
                    bus_name=name,
                    interface="org.freedesktop.DBus.Properties",
                )
                try:
                    props = self._call(
                        player, "GetAll", "s", ("org.mpris.MediaPlayer2.Player",)
                    )[0]
                except Exception:  # a player quitting mid-poll
                    continue
                found = mpris_now_playing(name, unwrap_variants(props))
                if found is not None:
                    players.append(found)
            return pick_mpris(players)
        except Exception as exc:
            log.debug("MPRIS poll failed: %s", exc)
            self.close()
            return None

    def fetch_thumbnail(self) -> bytes | None:
        return None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


class WindowsMediaReader:
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

    def fetch_thumbnail(self) -> bytes | None:
        """Read the current session's cover art.

        This costs a stream read and an allocation, so it is called on
        track change rather than on every poll. Not every source supplies
        one; None simply means there is no cover to show.
        """
        if not WINRT_AVAILABLE:
            return None
        try:
            manager = self._ensure_manager()
            if manager is None:
                return None
            session = manager.get_current_session()
            if session is None:
                return None

            props = self._run(session.try_get_media_properties_async())
            thumbnail = props.thumbnail if props else None
            if thumbnail is None:
                return None

            stream = self._run(thumbnail.open_read_async())
            size = stream.size
            if not size:
                return None

            buffer = Buffer(size)
            self._run(
                stream.read_async(buffer, size, InputStreamOptions.READ_AHEAD)
            )
            return bytes(buffer)
        except Exception as exc:
            log.debug("thumbnail fetch failed: %s", exc)
            return None

    def close(self) -> None:
        try:
            self._loop.close()
        except Exception:
            pass


# Whichever this platform has. Both poll() to a NowPlaying or None.
MediaReader = WindowsMediaReader if sys.platform == "win32" else MprisMediaReader
