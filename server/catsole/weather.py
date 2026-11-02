"""Weather for the clock screen, from Open-Meteo (free, no account).

The city is set on the panel and looked up once with Open-Meteo's
geocoding; until then nothing is fetched. A failed fetch keeps the last
reading for up to STALE_S, after which the clock screen goes back to
showing the board's own numbers.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)

POLL_S = 900
STALE_S = 3600
TIMEOUT_S = 10

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Icons the board draws.
CLEAR, CLOUD, RAIN, STORM = 1, 2, 3, 4


def icon_for(code: int) -> int:
    """A WMO weather code, as Open-Meteo reports it, to one of four icons."""
    if code in (0, 1):
        return CLEAR
    if 51 <= code <= 67 or 80 <= code <= 82:
        return RAIN
    if 95 <= code <= 99:
        return STORM
    return CLOUD


def parse_forecast(data: dict) -> dict:
    current = data.get("current") or {}
    rain = (data.get("daily") or {}).get("precipitation_probability_max") or [None]
    return {
        "temp": int(round(float(current["temperature_2m"]))),
        "icon": icon_for(int(current.get("weather_code") or 0)),
        "rain": int(rain[0]) if rain[0] is not None else None,
    }


def geocode(city: str, user_agent: str = "catsole") -> dict | None:
    try:
        r = requests.get(GEOCODE_URL, params={"name": city, "count": 1},
                         headers={"User-Agent": user_agent}, timeout=TIMEOUT_S)
        r.raise_for_status()
        results = r.json().get("results") or []
    except (requests.RequestException, ValueError) as exc:
        log.warning("city lookup failed: %s", exc)
        return None
    if not results:
        return None
    place = results[0]
    return {"city": place.get("name", city), "lat": place["latitude"], "lon": place["longitude"]}


class WeatherReader:
    def __init__(self, user_agent: str, clock: Callable[[], float] = time.monotonic):
        self.user_agent = user_agent
        self.clock = clock
        self._lock = threading.Lock()
        self._location: dict | None = None
        self._reading: dict | None = None
        self._read_at = 0.0
        self._tried_at: float | None = None
        self._thread: threading.Thread | None = None

    def set_location(self, location: dict | None) -> None:
        with self._lock:
            if location != self._location:
                self._location = location
                self._reading = None
                self._tried_at = None   # fetch the new place straight away

    def latest(self) -> dict | None:
        with self._lock:
            if self._location is None or self._reading is None:
                return None
            if self.clock() - self._read_at > STALE_S:
                return None
            return dict(self._reading)

    def poll_once(self) -> bool:
        with self._lock:
            location = self._location
            self._tried_at = self.clock()
        if location is None:
            return False
        try:
            r = requests.get(FORECAST_URL, params={
                "latitude": location["lat"],
                "longitude": location["lon"],
                "current": "temperature_2m,weather_code",
                "daily": "precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": 1,
            }, headers={"User-Agent": self.user_agent}, timeout=TIMEOUT_S)
            r.raise_for_status()
            reading = parse_forecast(r.json())
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            log.warning("weather fetch failed: %s", exc)
            return False
        with self._lock:
            if self._location == location:
                self._reading = reading
                self._read_at = self.clock()
        return True

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop():
            while True:
                due = self._tried_at is None or self.clock() - self._tried_at >= POLL_S
                if self._location is not None and due:
                    self.poll_once()
                time.sleep(5)

        self._thread = threading.Thread(target=loop, name="weather", daemon=True)
        self._thread.start()
