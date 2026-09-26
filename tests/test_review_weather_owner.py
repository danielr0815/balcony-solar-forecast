"""Weather-owner transitions and frozen pre-extraction scheduling contract."""

from datetime import UTC, datetime, timedelta
from random import Random

import pytest
from balcony_solar_forecast._weather_cache import WeatherCache
from balcony_solar_forecast.const import (
    FAILED_FETCH_MIN_INTERVAL_SECONDS,
    MAX_PAYLOAD_AGE_HOURS,
    MAX_PHYSICS_FALLBACK_AGE_HOURS,
    STATUS_CACHED,
    STATUS_FRESH,
    STATUS_PHYSICS_FALLBACK,
    STATUS_UNAVAILABLE,
)
from balcony_solar_forecast.fetcher import FetchError

NOW = datetime(2026, 7, 5, 12, tzinfo=UTC)


def _payload(count):
    # Provider timestamps mark the END of each preceding radiation interval.
    times = [(NOW + timedelta(minutes=15 * (i + 1))).isoformat()
             for i in range(count)]
    return {
        "minutely_15": {
            "time": times,
            "shortwave_radiation": [500.0] * count,
            "direct_normal_irradiance": [600.0] * count,
            "diffuse_radiation": [150.0] * count,
            "temperature_2m": [22.0] * count,
        },
        "hourly": {
            "time": [NOW.isoformat()],
            "cloud_cover_low": [10.0], "cloud_cover_mid": [0.0],
            "cloud_cover_high": [0.0], "visibility": [30000.0],
            "snowfall": [0.0], "snow_depth": [0.0],
        },
    }


async def test_failed_and_poorer_refreshes_preserve_owned_image_and_actual_age():
    original = _payload(4)
    old_at = NOW - timedelta(hours=1)
    cache = WeatherCache()
    cache.restore({"payload": original, "fetched_at": old_at.isoformat()})
    parsed = cache.weather()
    persisted = []

    async def fail():
        raise FetchError("provider offline")

    await cache.async_refresh(NOW, fetch=fail, persist=lambda *args: persisted.append(args))
    assert cache.payload is original and cache.weather() is parsed
    assert cache.fetched_at == old_at and cache.attempted_at == NOW
    assert cache.last_error == "provider offline" and not cache.fetch_ok

    async def poorer():
        return _payload(2)

    retry = NOW + timedelta(minutes=1)
    await cache.async_refresh(retry, fetch=poorer, persist=lambda *args: persisted.append(args))
    assert cache.payload is original and cache.weather() is parsed
    assert cache.fetched_at == old_at and cache.attempted_at == retry
    assert cache.age_seconds(retry) == 3660
    assert cache.fetch_ok and cache.last_error is None
    assert persisted == []

    replacement = _payload(8)

    async def richer():
        return replacement

    await cache.async_refresh(retry, fetch=richer, persist=lambda *args: persisted.append(args))
    assert cache.payload is replacement and cache.weather() is not parsed
    assert len(cache.weather().slots) == 8
    assert cache.fetched_at == retry and cache.age_seconds(retry) == 0
    assert persisted == [(replacement, retry.isoformat())]


@pytest.mark.parametrize("stamp", ["2026-07-05T12:00:00", "2026-07-05T14:00:00+02:00"])
def test_restore_normalizes_time_without_claiming_a_live_attempt(stamp):
    cache = WeatherCache()
    cache.restore({"payload": _payload(4), "fetched_at": stamp})
    assert cache.fetched_at == NOW
    assert cache.attempted_at is None and not cache.fetch_ok
    assert cache.due(NOW)
    assert cache.status_for_age(timedelta(0)) == STATUS_CACHED


def _old_due(attempted_at, fetch_ok, interval, now):
    """Frozen BalconySolarCoordinator._due_for_fetch from parent a799162."""
    if attempted_at is None:
        return True
    if not fetch_ok:
        return now - attempted_at >= min(
            interval, timedelta(seconds=FAILED_FETCH_MIN_INTERVAL_SECONDS),
        )
    return now - attempted_at >= interval


def _old_status(age, fetch_ok, interval):
    """Frozen BalconySolarCoordinator._status_for_age from parent a799162."""
    if age < timedelta(0):
        age = timedelta(0)
    if fetch_ok and age < interval:
        return STATUS_FRESH
    if age <= timedelta(hours=MAX_PAYLOAD_AGE_HOURS):
        return STATUS_CACHED
    if age <= timedelta(hours=MAX_PHYSICS_FALLBACK_AGE_HOURS):
        return STATUS_PHYSICS_FALLBACK
    return STATUS_UNAVAILABLE


def test_extracted_due_and_availability_match_frozen_policy():
    rng = Random(20260926)
    boundaries = [timedelta(0), timedelta(hours=MAX_PAYLOAD_AGE_HOURS),
                  timedelta(hours=MAX_PHYSICS_FALLBACK_AGE_HOURS)]
    for _ in range(2000):
        interval = timedelta(seconds=rng.randint(1, 7200))
        fetch_ok = rng.choice([True, False])
        attempted_at = rng.choice([None, NOW - timedelta(seconds=rng.randint(-3600, 7200))])
        age = rng.choice(boundaries + [interval, timedelta(seconds=rng.randint(-3600, 200000))])
        cache = WeatherCache(fetch_interval=interval, attempted_at=attempted_at, fetch_ok=fetch_ok)
        assert cache.due(NOW) == _old_due(attempted_at, fetch_ok, interval, NOW)
        assert cache.status_for_age(age) == _old_status(age, fetch_ok, interval)
