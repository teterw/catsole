"""Mode state machine and the main poll loop.

All content decisions live here rather than on the microcontroller. The
device is a renderer: it receives finished strings and numbers, and owns
only animation and link state.

Lyrics are fetched on a worker thread. An lrclib call takes a few hundred
milliseconds on a good day and can hang until timeout on a bad one; doing
it inline would stall frame output and visibly freeze the marquee.
"""

from __future__ import annotations

import logging
import threading
import time

from .audio import AudioLevels
from .config import Config
from .hardware import HardwareReader, empty_stats
from .link import NullLink, SerialLink
from .lyrics import Lyrics, LyricsProvider, select_line
from .media import MediaReader, NowPlaying

log = logging.getLogger(__name__)

MODES = ("lyrics", "stats")


class DeskConsole:
    """Owns device state and drives the poll/render cycle."""

    def __init__(
        self,
        config: Config,
        link=None,
        media=None,
        hardware=None,
        lyrics_provider=None,
    ):
        self.config = config
        self.link = link if link is not None else SerialLink(
            port=config.serial_port, baud=config.baud, on_event=self.handle_event
        )
        self.media = media if media is not None else MediaReader()
        self.hardware = hardware if hardware is not None else HardwareReader()
        self.lyrics_provider = (
            lyrics_provider
            if lyrics_provider is not None
            else LyricsProvider(config.cache_dir, config.user_agent)
        )

        self.mode = config.start_mode if config.start_mode in MODES else MODES[0]
        self.now_playing: NowPlaying | None = None
        self.stats: dict = empty_stats()
        self.lyrics = Lyrics(kind="none")
        self.refresh_requested = False
        self.device_firmware = ""
        self.audio = AudioLevels()

        self._track_key = None
        self._fetching = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._next_media = 0.0
        self._next_stats = 0.0
        self._next_frame = 0.0
        self._next_eq = 0.0

    # ---- events from the device -----------------------------------------

    def handle_event(self, event: dict) -> None:
        """Handle one inbound event from the device.

        The device is display-only, so the one thing it ever sends is its
        boot announcement. Anything else is ignored rather than trusted.
        """
        if event.get("t") != "hello":
            return

        self.device_firmware = str(event.get("fw", ""))
        log.info("device announced firmware %s", self.device_firmware)
        # Answer immediately so the display leaves its waiting state.
        self.push_frame()

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            return
        self.mode = mode
        self.push_frame()  # Reflect the change now, not on the next tick.

    def force_refresh(self) -> None:
        self.refresh_requested = True
        self._next_media = 0.0
        self._next_stats = 0.0

    # ---- polling ---------------------------------------------------------

    def _on_media(self, now_playing: NowPlaying | None) -> None:
        """Record the current track, refetching lyrics only on a real change."""
        self.now_playing = now_playing

        if now_playing is None or now_playing.is_empty:
            self._track_key = None
            self.lyrics = Lyrics(kind="none")
            return

        if now_playing.track_key == self._track_key:
            return

        self._track_key = now_playing.track_key
        self.lyrics = Lyrics(kind="none")
        self._fetch_lyrics(now_playing)

    def _fetch_lyrics(self, now_playing: NowPlaying) -> None:
        if self._fetching:
            return
        self._fetching = True

        def worker():
            try:
                found = self.lyrics_provider.fetch(
                    now_playing.artist,
                    now_playing.title,
                    now_playing.album,
                    now_playing.duration_ms / 1000.0 if now_playing.duration_ms else None,
                )
                with self._lock:
                    # Guard against a track change while the fetch was in flight.
                    if self._track_key == now_playing.track_key:
                        self.lyrics = found
                        log.info(
                            "lyrics for %s: %s", now_playing.label, found.kind
                        )
            except Exception:
                log.exception("lyrics fetch failed")
            finally:
                self._fetching = False

        # In tests the provider is a stub; running inline keeps them
        # deterministic without a thread join.
        if isinstance(self.lyrics_provider, LyricsProvider):
            threading.Thread(target=worker, name="lyrics-fetch", daemon=True).start()
        else:
            worker()

    # ---- frame building --------------------------------------------------

    def build_frame(self) -> dict:
        if self.mode == "stats":
            return self._stats_frame()
        return self._lyrics_frame()

    def _stats_frame(self) -> dict:
        cpu = self.stats.get("cpu", {})
        gpu = self.stats.get("gpu", {})
        ram = self.stats.get("ram", {})
        return {
            "t": "frame",
            "mode": "stats",
            "cpu": {
                "temp": cpu.get("temp"),
                "load": cpu.get("load"),
                "clock": cpu.get("clock"),
            },
            "gpu": {
                "temp": gpu.get("temp"),
                "load": gpu.get("load"),
                "vram_used": gpu.get("vram_used"),
                "vram_total": gpu.get("vram_total"),
            },
            "ram": {
                "used": ram.get("used"),
                "total": ram.get("total"),
                "percent": ram.get("percent"),
            },
        }

    def _lyrics_frame(self) -> dict:
        playing = self.now_playing

        if playing is None or playing.is_empty:
            return {
                "t": "frame",
                "mode": "lyrics",
                "meta": "",
                "main": "",
                "lyr": "none",
                "state": "idle",
                "eq": 0,
                "hold_ms": 0,
            }

        state = "playing" if playing.is_playing else "paused"

        if self.lyrics.is_synced:
            position = playing.position_ms + self.config.lyric_offset_ms
            line, hold_ms = select_line(self.lyrics.synced, position)
            return {
                "t": "frame",
                "mode": "lyrics",
                "meta": playing.label,
                "main": line,
                "lyr": "synced",
                "state": state,
                "eq": 1 if playing.is_playing else 0,
                "hold_ms": hold_ms,
            }

        # No timing available: show the track itself as the headline rather
        # than a lyric line we cannot place.
        return {
            "t": "frame",
            "mode": "lyrics",
            "meta": playing.artist,
            "main": playing.title,
            "lyr": self.lyrics.kind,
            "state": state,
            "eq": 1 if playing.is_playing else 0,
            "hold_ms": 0,
        }

    def push_frame(self) -> None:
        self.link.send(self.build_frame())
        self._next_frame = time.monotonic() + self.config.frame_interval_s

    # ---- main loop -------------------------------------------------------

    def tick(self) -> None:
        """One pass of the poll/render cycle."""
        now = time.monotonic()

        if now >= self._next_media:
            self._on_media(self.media.poll())
            self._next_media = now + self.config.media_poll_s

        if now >= self._next_stats:
            self.stats = self.hardware.poll()
            self._next_stats = now + self.config.stats_poll_s

        # Spectrum goes out far more often than full frames. A meter that
        # lags the music reads as broken, and the payload is tiny.
        if now >= self._next_eq:
            playing = (
                self.mode == "lyrics"
                and self.now_playing is not None
                and self.now_playing.is_playing
            )
            if playing and self.audio.available:
                self.link.send({"t": "eq", "b": self.audio.hex_levels()})
            self._next_eq = now + self.config.eq_interval_s

        if self.refresh_requested:
            self.refresh_requested = False
            # A hold on a track whose lyrics were not found is worth a retry.
            if (
                self.now_playing is not None
                and not self.now_playing.is_empty
                and self.lyrics.kind == "none"
            ):
                self._fetch_lyrics(self.now_playing)
            self.push_frame()
            return

        if now >= self._next_frame:
            self.push_frame()

    def run(self) -> None:
        self.link.start()
        self.audio.start()
        log.info("catsole running; mode=%s", self.mode)
        try:
            while not self._stop.is_set():
                self.tick()
                time.sleep(0.05)
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.link.stop()
        except Exception:
            pass
        try:
            self.media.close()
        except Exception:
            pass
        try:
            self.audio.stop()
        except Exception:
            pass

    # ---- introspection for the web panel ---------------------------------

    def snapshot(self) -> dict:
        playing = self.now_playing
        return {
            "mode": self.mode,
            "modes": list(MODES),
            "link": {
                "connected": bool(getattr(self.link, "connected", False)),
                "port": getattr(self.link, "port_name", ""),
                "error": getattr(self.link, "last_error", ""),
                "firmware": self.device_firmware,
            },
            "now_playing": None
            if playing is None or playing.is_empty
            else {
                "artist": playing.artist,
                "title": playing.title,
                "album": playing.album,
                "position_ms": playing.position_ms,
                "duration_ms": playing.duration_ms,
                "is_playing": playing.is_playing,
                "app": playing.app_id,
            },
            "lyrics_kind": self.lyrics.kind,
            "stats": self.stats,
            "lhm": bool(getattr(self.hardware, "lhm_available", False)),
            "audio": {
                "available": self.audio.available,
                "device": self.audio.device_name,
                "levels": self.audio.levels(),
                "error": self.audio.last_error,
            },
        }
