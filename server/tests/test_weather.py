"""Tests for the weather on the clock screen (Open-Meteo, stubbed)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole import weather
from catsole.weather import WeatherReader, geocode, icon_for, parse_forecast

from test_study_console import Clock, clock, console  # noqa: F401  (fixtures)

FORECAST = {
    "current": {"time": "2026-11-02T21:00", "interval": 900, "temperature_2m": 27.6, "weather_code": 61},
    "daily": {"time": ["2026-11-02"], "precipitation_probability_max": [60]},
}
PLACE = {"results": [{"name": "Bangkok", "latitude": 13.75, "longitude": 100.5, "country": "Thailand"}]}


class Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


@pytest.mark.parametrize("code, icon", [
    (0, 1), (1, 1), (2, 2), (3, 2), (45, 2), (48, 2), (71, 2), (77, 2), (85, 2), (86, 2),
    (51, 3), (61, 3), (67, 3), (80, 3), (82, 3), (95, 4), (99, 4), (5, 2),
])
def test_icon_for(code, icon):
    assert icon_for(code) == icon


def test_parse_forecast():
    assert parse_forecast(FORECAST) == {"temp": 28, "icon": 3, "rain": 60}


def test_parse_forecast_without_rain_chance():
    data = {"current": {"temperature_2m": 20.2, "weather_code": 0}, "daily": {}}
    assert parse_forecast(data) == {"temp": 20, "icon": 1, "rain": None}


def test_geocode(monkeypatch):
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: Response(PLACE))
    assert geocode("bangkok") == {"city": "Bangkok", "lat": 13.75, "lon": 100.5}
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: Response({"generationtime_ms": 1}))
    assert geocode("nowhere") is None


def test_no_location_no_weather(clock):
    reader = WeatherReader("ua", clock)
    assert reader.latest() is None and reader.poll_once() is False


def test_fetch_failure_keeps_last_then_expires(clock, monkeypatch):
    reader = WeatherReader("ua", clock)
    reader.set_location({"city": "Bangkok", "lat": 13.75, "lon": 100.5})
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: Response(FORECAST))
    assert reader.poll_once()

    def offline(*a, **k):
        raise weather.requests.ConnectionError("offline")

    monkeypatch.setattr(weather.requests, "get", offline)
    clock.t += 3599
    assert reader.poll_once() is False
    assert reader.latest() == {"temp": 28, "icon": 3, "rain": 60}
    clock.t += 2
    assert reader.latest() is None


def test_clearing_location_clears_weather(clock, monkeypatch):
    reader = WeatherReader("ua", clock)
    reader.set_location({"city": "Bangkok", "lat": 13.75, "lon": 100.5})
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: Response(FORECAST))
    reader.poll_once()
    reader.set_location(None)
    assert reader.latest() is None


def test_clock_frame_carries_weather_only_when_known(console, clock, monkeypatch):
    console.weather = WeatherReader("ua", clock)
    console.mode = "clock"
    assert "wi" not in console.build_frame()
    console.weather.set_location({"city": "Bangkok", "lat": 13.75, "lon": 100.5})
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: Response(FORECAST))
    console.weather.poll_once()
    f = console.build_frame()
    assert (f["wt"], f["wi"], f["wr"]) == (28, 3, 60)


def test_settings_weather_city(console, monkeypatch):
    from catsole.web import create_app

    c = create_app(console).test_client()
    monkeypatch.setattr(weather, "geocode", lambda city, *a: {"city": "Bangkok", "lat": 13.75, "lon": 100.5})
    r = c.post("/api/settings", json={"weather_city": "bangkok"})
    assert r.status_code == 200 and r.json["settings"]["weather"]["city"] == "Bangkok"
    monkeypatch.setattr(weather, "geocode", lambda city, *a: None)
    assert c.post("/api/settings", json={"weather_city": "zzz"}).status_code == 400
    r = c.post("/api/settings", json={"weather_city": ""})
    assert r.json["settings"]["weather"] is None
