"""Parsed-weather ownership tests, independent of the Home Assistant runtime.

The coordinator delegates weather reads to WeatherCache. Payload adoption and
parsed-image invalidation belong to that owner, so the tests construct it
normally instead of relying on an incomplete ``Coordinator.__new__`` object.
"""

from __future__ import annotations

import pytest
from balcony_solar_forecast import _weather_cache as cache_mod
from balcony_solar_forecast._weather_cache import WeatherCache
from balcony_solar_forecast.core.types import WeatherSeries
from balcony_solar_forecast.fetcher import FetchError


def _stored(payload):
    return {"payload": payload, "fetched_at": "2026-07-10T00:00:00+00:00"}


@pytest.fixture
def parse_spy(monkeypatch):
    calls = {"n": 0}

    def spy(payload):
        calls["n"] += 1
        return WeatherSeries(slots=())

    monkeypatch.setattr(cache_mod, "parse_weather", spy)
    return calls


def test_recompute_without_new_fetch_does_not_reparse(parse_spy):
    cache = WeatherCache()
    cache.restore(_stored({"minutely_15": {}, "hourly": {}}))
    first = cache.weather()
    assert cache.weather() is first
    assert cache.weather() is first
    assert parse_spy["n"] == 1


def test_new_payload_invalidates_cache(parse_spy):
    cache = WeatherCache()
    stored = _stored({"minutely_15": {}, "hourly": {}})
    cache.restore(stored)
    first = cache.weather()
    cache.restore(stored)
    assert cache.weather() is first
    assert parse_spy["n"] == 1
    cache.restore(_stored({"minutely_15": {}, "hourly": {}, "generation": 2}))
    second = cache.weather()
    assert second is not first
    assert cache.weather() is second
    assert parse_spy["n"] == 2


def test_no_payload_returns_none_without_parsing(parse_spy):
    assert WeatherCache().weather() is None
    assert parse_spy["n"] == 0


def test_unparseable_payload_returns_none_and_is_not_cached(monkeypatch):
    calls = {"n": 0}

    def boom(payload):
        calls["n"] += 1
        raise FetchError("stored payload no longer parses", retryable=False)

    monkeypatch.setattr(cache_mod, "parse_weather", boom)
    cache = WeatherCache()
    cache.restore(_stored({"bad": 1}))
    assert cache.weather() is None
    assert cache.weather() is None
    assert calls["n"] == 2
