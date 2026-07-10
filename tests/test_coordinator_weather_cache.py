"""Parsed-weather cache tests (Phase-D optimisation #3, audit #31).

``coordinator._cached_weather`` re-read the stored Open-Meteo payload and
re-parsed it into a ``WeatherSeries`` on EVERY 15-min recompute (and on the
nightly snapshot). Parsing is pure and the payload object is only replaced when
a new fetch lands, so the result is now memoised keyed by the payload OBJECT
IDENTITY: recomputes between fetches reuse the parsed (immutable) series, and a
new payload invalidates it. These tests spy on the parse function to prove both.

The coordinator is built bare via ``__new__`` (only ``_store`` + ``_weather_cache``
are needed by ``_cached_weather``), mirroring the other coordinator glue tests.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast import (  # noqa: E402
    coordinator as coord_mod,
)
from custom_components.balcony_solar_forecast.coordinator import (  # noqa: E402
    BalconySolarCoordinator,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    WeatherSeries,
)
from custom_components.balcony_solar_forecast.fetcher import FetchError  # noqa: E402


class _Store:
    """Minimal last-payload store: returns a STABLE wrapper until replaced."""

    def __init__(self) -> None:
        self._wrapper: dict | None = None

    def set_payload(self, payload: dict) -> None:
        # Mirror the real store: a new fetch replaces the wrapper (and thus the
        # payload object identity) wholesale.
        self._wrapper = {"payload": payload, "fetched_at": "2026-07-10T00:00:00+00:00"}

    def get_last_payload(self):
        return self._wrapper


def _bare_coordinator(store: _Store) -> BalconySolarCoordinator:
    c = BalconySolarCoordinator.__new__(BalconySolarCoordinator)
    c._store = store
    c._weather_cache = None
    return c


@pytest.fixture
def parse_spy(monkeypatch):
    """Count parse_weather calls; return a DISTINCT series object each call."""
    calls = {"n": 0}

    def spy(payload):
        calls["n"] += 1
        return WeatherSeries(slots=())  # fresh object per parse -> identity check

    monkeypatch.setattr(coord_mod, "parse_weather", spy)
    return calls


def test_recompute_without_new_fetch_does_not_reparse(parse_spy):
    store = _Store()
    store.set_payload({"minutely_15": {}, "hourly": {}})
    c = _bare_coordinator(store)

    w1 = c._cached_weather()
    assert parse_spy["n"] == 1  # first call parses
    w2 = c._cached_weather()
    assert parse_spy["n"] == 1  # cache hit: NOT re-parsed
    w3 = c._cached_weather()
    assert parse_spy["n"] == 1
    # Same immutable series object served across recomputes.
    assert w1 is w2 is w3


def test_new_payload_invalidates_cache(parse_spy):
    store = _Store()
    store.set_payload({"minutely_15": {}, "hourly": {}})
    c = _bare_coordinator(store)

    w1 = c._cached_weather()
    assert parse_spy["n"] == 1
    c._cached_weather()
    assert parse_spy["n"] == 1  # still cached

    # A new fetch lands -> new payload object -> identity miss -> re-parse.
    store.set_payload({"minutely_15": {}, "hourly": {}, "gen": 2})
    w2 = c._cached_weather()
    assert parse_spy["n"] == 2
    assert w2 is not w1
    # And the fresh series is now the cached one.
    assert c._cached_weather() is w2
    assert parse_spy["n"] == 2


def test_no_payload_returns_none_without_parsing(parse_spy):
    c = _bare_coordinator(_Store())
    assert c._cached_weather() is None
    assert parse_spy["n"] == 0


def test_unparseable_payload_returns_none_and_is_not_cached(monkeypatch):
    """A payload that fails to parse yields None and leaves the cache empty, so a
    later good payload still parses (the failure is not memoised)."""
    calls = {"n": 0}

    def boom(payload):
        calls["n"] += 1
        raise FetchError("stored payload no longer parses", retryable=False)

    monkeypatch.setattr(coord_mod, "parse_weather", boom)
    store = _Store()
    store.set_payload({"bad": 1})
    c = _bare_coordinator(store)

    assert c._cached_weather() is None
    assert calls["n"] == 1
    assert c._weather_cache is None  # failure not cached
    # A second attempt re-tries the parse (still None, but proves no stale cache).
    assert c._cached_weather() is None
    assert calls["n"] == 2
