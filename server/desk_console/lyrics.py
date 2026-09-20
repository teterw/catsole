"""Synced lyrics: lrclib lookup, LRC parsing, and current-line selection.

Nothing fetched here is committed. Responses cache to a gitignored
directory so replaying a track does not re-hit the API.

The fallback chain is deliberate: synced lyrics, then plain lyrics shown
statically, then a title/artist card. Showing the wrong line is worse than
showing no line, so an uncertain match degrades rather than guesses.
"""

from __future__ import annotations

import bisect
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API_BASE = "https://lrclib.net/api"
REQUEST_TIMEOUT = 6.0

# How long the last line of a track stays on screen once nothing follows it.
TRAILING_HOLD_MS = 5000

# A duration this far from the reported track length is a different edit.
DURATION_TOLERANCE_S = 3.0

_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_UNSAFE = re.compile(r"[^a-z0-9]+")


@dataclass
class Lyrics:
    """A resolved lyrics lookup.

    kind is "synced" (timed lines available), "plain" (text but no timing)
    or "none" (nothing found).
    """

    kind: str = "none"
    synced: list[tuple[int, str]] = field(default_factory=list)
    plain: str = ""
    source: str = ""

    @property
    def is_synced(self) -> bool:
        return self.kind == "synced" and bool(self.synced)


def parse_lrc(text: str) -> list[tuple[int, str]]:
    """Parse LRC text into sorted (milliseconds, line) pairs.

    Handles repeated timestamps on one line, both `.cc` and `:cc` separators,
    and two- or three-digit fractional parts. Metadata tags such as `[ar:]`
    carry no numeric timestamp and are skipped. Blank timed lines are kept:
    they are how an LRC marks an instrumental gap, and dropping them would
    leave the previous line on screen through the whole break.
    """
    if not text:
        return []

    out: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        stamps = list(_TIMESTAMP.finditer(raw_line))
        if not stamps:
            continue
        # The lyric is whatever follows the final timestamp on the line.
        content = raw_line[stamps[-1].end():].strip()
        for stamp in stamps:
            minutes, seconds, fraction = stamp.groups()
            total = int(minutes) * 60_000 + int(seconds) * 1000
            if fraction:
                # One digit is tenths, two are centiseconds, three are millis.
                scale = {1: 100, 2: 10, 3: 1}[len(fraction)]
                total += int(fraction) * scale
            out.append((total, content))

    out.sort(key=lambda pair: pair[0])
    return out


def select_line(lines: list[tuple[int, str]], position_ms: int) -> tuple[str, int]:
    """Return the line active at position_ms and how long it stays valid.

    The hold is what lets the device draw a progress hairline and notice a
    late frame, rather than sitting on a stale line without knowing it.
    """
    if not lines:
        return "", 0

    timestamps = [stamp for stamp, _ in lines]
    index = bisect.bisect_right(timestamps, position_ms) - 1

    if index < 0:
        # Still in the intro, before the first timed line.
        return "", max(0, timestamps[0] - position_ms)

    text = lines[index][1]
    if index + 1 < len(lines):
        hold = max(0, timestamps[index + 1] - position_ms)
    else:
        hold = TRAILING_HOLD_MS
    return text, hold


def pick_best(results: list[dict], duration_s: float | None) -> dict | None:
    """Choose the best lrclib result, preferring synced and matching length.

    Search returns every edit, remaster and live version under one title, so
    duration is the only reliable discriminator available.
    """
    if not results:
        return None

    def distance(entry: dict) -> float:
        if duration_s is None:
            return 0.0
        entry_duration = entry.get("duration")
        if not entry_duration:
            return float("inf")
        return abs(float(entry_duration) - float(duration_s))

    synced = [entry for entry in results if entry.get("syncedLyrics")]
    if synced:
        best = min(synced, key=distance)
        if duration_s is None or distance(best) <= DURATION_TOLERANCE_S:
            return best
        # Nothing close enough: a synced file for the wrong edit drifts badly.
        return best if distance(best) != float("inf") else None

    plain = [entry for entry in results if entry.get("plainLyrics")]
    if plain:
        return min(plain, key=distance)

    return None


def cache_key(artist: str, title: str) -> str:
    """Stable, case-insensitive, filesystem-safe cache filename stem."""
    raw = f"{artist}__{title}"
    folded = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    return _UNSAFE.sub("-", folded.lower()).strip("-") or "unknown"


class LyricsProvider:
    """Fetches and caches lyrics from lrclib."""

    def __init__(self, cache_dir: Path, user_agent: str):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        # lrclib asks clients to identify themselves rather than send a
        # default library UA.
        self.session.headers.update({"User-Agent": user_agent})

    def fetch(
        self,
        artist: str,
        title: str,
        album: str = "",
        duration_s: float | None = None,
    ) -> Lyrics:
        """Resolve lyrics for a track, using the cache when possible."""
        if not title:
            return Lyrics(kind="none")

        cached = self._read_cache(artist, title)
        if cached is not None:
            return cached

        entry = self._query_exact(artist, title, album, duration_s)
        if entry is None:
            entry = self._query_search(artist, title, duration_s)

        lyrics = self._to_lyrics(entry)
        self._write_cache(artist, title, lyrics)
        return lyrics

    def _query_exact(
        self, artist: str, title: str, album: str, duration_s: float | None
    ) -> dict | None:
        """Try the exact-match endpoint, which needs a duration to be useful."""
        if duration_s is None:
            return None
        params = {
            "artist_name": artist,
            "track_name": title,
            "album_name": album or title,
            "duration": int(round(duration_s)),
        }
        data = self._get("/get", params)
        if isinstance(data, dict) and (
            data.get("syncedLyrics") or data.get("plainLyrics")
        ):
            return data
        return None

    def _query_search(
        self, artist: str, title: str, duration_s: float | None
    ) -> dict | None:
        data = self._get("/search", {"track_name": title, "artist_name": artist})
        if isinstance(data, list):
            return pick_best(data, duration_s)
        return None

    def _get(self, path: str, params: dict):
        try:
            response = self.session.get(
                API_BASE + path, params=params, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            log.warning("lrclib request failed: %s", exc)
            return None
        if response.status_code == 404:
            return None
        if not response.ok:
            log.warning("lrclib returned %s for %s", response.status_code, path)
            return None
        try:
            return response.json()
        except ValueError:
            return None

    @staticmethod
    def _to_lyrics(entry: dict | None) -> Lyrics:
        if not entry:
            return Lyrics(kind="none")
        synced_text = entry.get("syncedLyrics") or ""
        if synced_text:
            parsed = parse_lrc(synced_text)
            if parsed:
                return Lyrics(kind="synced", synced=parsed, source="lrclib")
        plain = (entry.get("plainLyrics") or "").strip()
        if plain:
            return Lyrics(kind="plain", plain=plain, source="lrclib")
        return Lyrics(kind="none")

    def _cache_path(self, artist: str, title: str) -> Path:
        return self.cache_dir / (cache_key(artist, title) + ".json")

    def _read_cache(self, artist: str, title: str) -> Lyrics | None:
        path = self._cache_path(artist, title)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A truncated or corrupt cache file should look like a miss.
            return None
        return Lyrics(
            kind=raw.get("kind", "none"),
            synced=[(int(ms), text) for ms, text in raw.get("synced", [])],
            plain=raw.get("plain", ""),
            source=raw.get("source", "cache"),
        )

    def _write_cache(self, artist: str, title: str, lyrics: Lyrics) -> None:
        payload = {
            "kind": lyrics.kind,
            "synced": [[ms, text] for ms, text in lyrics.synced],
            "plain": lyrics.plain,
            "source": lyrics.source,
        }
        try:
            self._cache_path(artist, title).write_text(
                json.dumps(payload), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("could not write lyrics cache: %s", exc)
