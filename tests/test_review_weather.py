"""Review regressions at the last-good weather and public availability boundary."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.balcony_solar_forecast.const import DOMAIN
from custom_components.balcony_solar_forecast.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.balcony_solar_forecast.energy import async_get_solar_forecast
from custom_components.balcony_solar_forecast.sensor import _build_forecast_response
from tests.helpers.weather import (
    NOW,
    _coord,
    _FakeFetcher,
    _om_payload,
    _PayloadStore,
)


@pytest.mark.parametrize("corruption", ["invalid", "duplicate", "reverse", "gap"])
async def test_invalid_time_axis_never_replaces_last_good_weather(corruption):
    store = _PayloadStore()
    good = _om_payload(start_iso="2026-07-05T12:15")
    store.set_last_payload(good, (NOW - timedelta(hours=1)).isoformat())
    coord = _coord(store)
    coord._last_fetched_at = NOW - timedelta(hours=1)
    before = coord._cached_weather()
    bad = deepcopy(good)
    times = bad["minutely_15"]["time"]
    if corruption == "invalid":
        times[1] = "not-a-date"
    elif corruption == "duplicate":
        times[1] = times[0]
    elif corruption == "reverse":
        times.reverse()
    else:
        times[1] = "2026-07-05T12:31"
    coord._fetcher = _FakeFetcher([bad])

    await coord._async_try_fetch(NOW)

    assert store.last_payload["payload"] is good
    assert coord._cached_weather() is before
    assert coord._last_fetched_at == NOW - timedelta(hours=1)
    assert coord._last_attempt_at == NOW
    assert not coord._last_fetch_ok


async def test_expired_long_payload_cannot_block_shorter_future_forecast():
    store = _PayloadStore()
    old = _om_payload(n_quarters=96, start_iso="2026-07-03T00:15")
    store.set_last_payload(old, (NOW - timedelta(days=2)).isoformat())
    coord = _coord(store)
    coord._last_fetched_at = NOW - timedelta(days=2)
    fresh = _om_payload(n_quarters=8, start_iso="2026-07-05T12:15")
    coord._fetcher = _FakeFetcher([fresh])

    await coord._async_try_fetch(NOW)

    assert store.last_payload["payload"] is fresh
    assert coord._last_fetched_at == NOW
    assert coord._last_fetch_ok


async def test_failed_update_hides_old_curves_and_reports_live_provenance():
    old = {"slot_starts": [NOW.isoformat()], "plane_watts": {"M1": [400]},
           "watts": {NOW.isoformat(): 400}, "hourly_wh": {NOW.isoformat(): 100},
           "hourly_wh_ac": {NOW.isoformat(): 90}, "status": "fresh",
           "degraded": False, "weather_age_seconds": 60, "last_error": None}
    coord = SimpleNamespace(data=old, last_update_success=False,
                            weather_age_seconds_live=200000,
                            forecast_provenance={"available": False,
                                "source_status": "unavailable", "degraded": True,
                                "weather_age_seconds": 200000,
                                "last_error": "weather expired"})
    hass = SimpleNamespace(data={DOMAIN: {"entry": coord}})
    entry = SimpleNamespace(entry_id="entry", data={}, options={}, title="Test")

    response = _build_forecast_response(hass, "entry")["entries"]["entry"]
    assert response["slot_starts"] == []
    assert await async_get_solar_forecast(hass, "entry") is None
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["state"]["source_status"] == "unavailable"
    assert diagnostics["state"]["weather_age_seconds"] == 200000
    assert diagnostics["state"]["last_error"] == "weather expired"
    assert diagnostics["forecast"] is None


@pytest.mark.parametrize("missing", ["past", "temperature", "nonfinite"])
async def test_payload_without_usable_future_cannot_become_fresh(missing):
    store = _PayloadStore()
    good = _om_payload(start_iso="2026-07-05T12:15")
    store.set_last_payload(good, (NOW - timedelta(hours=1)).isoformat())
    coord = _coord(store)
    coord._last_fetched_at = NOW - timedelta(hours=1)
    bad = _om_payload(start_iso="2026-07-04T12:15" if missing == "past" else "2026-07-05T12:15")
    if missing == "temperature":
        bad["minutely_15"]["temperature_2m"] = [None] * 8
    if missing == "nonfinite":
        bad["minutely_15"]["shortwave_radiation"] = [float("inf")] * 8
    coord._fetcher = _FakeFetcher([bad])
    await coord._async_try_fetch(NOW)
    assert store.last_payload["payload"] is good
    assert coord._last_fetch_ok is False


async def test_richer_future_cache_loses_veto_after_normal_age_limit():
    store = _PayloadStore()
    old = _om_payload(n_quarters=96, start_iso="2026-07-05T12:15")
    store.set_last_payload(old, (NOW - timedelta(days=2)).isoformat())
    coord = _coord(store)
    coord._last_fetched_at = NOW - timedelta(days=2)
    fresh = _om_payload(n_quarters=8, start_iso="2026-07-05T12:15")
    coord._fetcher = _FakeFetcher([fresh])
    await coord._async_try_fetch(NOW)
    assert store.last_payload["payload"] is fresh
