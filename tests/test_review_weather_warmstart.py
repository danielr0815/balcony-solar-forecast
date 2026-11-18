"""An unusable restored image cannot masquerade as an available forecast."""

from datetime import timedelta

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.balcony_solar_forecast import coordinator as coord_mod
from custom_components.balcony_solar_forecast.const import (
    MAX_PAYLOAD_AGE_HOURS,
    STATUS_CACHED,
    STATUS_PHYSICS_FALLBACK,
)
from custom_components.balcony_solar_forecast.fetcher import FetchError
from tests.helpers.weather import NOW, _coord, _FakeFetcher, _om_payload, _PayloadStore


def _warm_coordinator(monkeypatch, payload, age):
    store = _PayloadStore()
    store.set_last_payload(payload, (NOW - age).isoformat())
    coord = _coord(store)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: NOW)
    coord._fetcher = _FakeFetcher([FetchError("provider down", retryable=True)])
    return coord


@pytest.mark.parametrize("kind", ["expired_intervals", "missing_components"])
async def test_warm_start_requires_usable_weather_after_failed_fetch(monkeypatch, kind):
    if kind == "expired_intervals":
        # Last slot ends precisely now. The payload is young, but every slot
        # lies wholly in the past after applying the backward-average shift.
        payload = _om_payload(start_iso=NOW.isoformat(), n_quarters=1)
    else:
        payload = _om_payload()
        payload["minutely_15"]["direct_normal_irradiance"] = [
            None for _ in payload["minutely_15"]["time"]
        ]
    coord = _warm_coordinator(monkeypatch, payload, timedelta(minutes=20))
    with pytest.raises(UpdateFailed, match="no usable future intervals"):
        await coord._async_update_data()
    assert coord._last_result is None
    assert coord._last_error == "provider down"


@pytest.mark.parametrize(("age", "status"), [
    (timedelta(minutes=20), STATUS_CACHED),
    (timedelta(hours=MAX_PAYLOAD_AGE_HOURS, minutes=1), STATUS_PHYSICS_FALLBACK),
])
async def test_in_progress_usable_slot_preserves_age_ladder(monkeypatch, age, status):
    payload = _om_payload(start_iso=(NOW + timedelta(minutes=15)).isoformat(), n_quarters=1)
    # Five minutes after the start of the still-usable backward-average slot.
    coord = _warm_coordinator(monkeypatch, payload, age)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: NOW + timedelta(minutes=5))
    data = await coord._async_update_data()
    assert data["status"] == status
    assert data["degraded"]
    assert data["weather_age_seconds"] == (age + timedelta(minutes=5)).total_seconds()


async def test_current_response_exports_raw_curve_publication_and_weather_mask(monkeypatch):
    from custom_components.balcony_solar_forecast._forecast_presenter import (
        _build_forecast_response,
    )
    from custom_components.balcony_solar_forecast.const import DOMAIN

    payload = _om_payload(start_iso=(NOW + timedelta(minutes=15)).isoformat())
    payload["minutely_15"]["diffuse_radiation"][0] = None
    coord = _warm_coordinator(monkeypatch, payload, timedelta(minutes=20))
    coord.data = await coord._async_update_data()
    coord.last_update_success = True
    coord.hass.data = {DOMAIN: {"synthetic": coord}}
    entry = _build_forecast_response(coord.hass, "synthetic")["entries"]["synthetic"]
    assert entry.get("published_at") == NOW.isoformat()
    assert entry.get("raw_hourly_wh") == coord.data["raw_hourly_wh"]
    assert entry.get("invalid_weather_slots") == [NOW.isoformat()]


async def test_opt_in_capture_metadata_binds_published_result_and_confirms_night(monkeypatch):
    from custom_components.balcony_solar_forecast._forecast_presenter import (
        _build_forecast_response,
    )
    from custom_components.balcony_solar_forecast.const import DOMAIN
    from scripts.validation.capture_lead_time import _binding_sha256

    night = NOW.replace(hour=0)
    payload = _om_payload(start_iso=(night + timedelta(minutes=15)).isoformat(), n_quarters=4)
    m15 = payload["minutely_15"]
    m15.update({field: [0] * 4 for field in ("shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation")})
    m15["direct_normal_irradiance"][1] = None
    coord = _warm_coordinator(monkeypatch, payload, timedelta(minutes=20))
    coord._last_fetched_at = night - timedelta(minutes=20)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: night)
    coord.data = await coord._async_update_data()
    coord.last_update_success = True
    eid = coord.entry.entry_id
    coord.hass.data = {DOMAIN: {eid: coord}}
    default = _build_forecast_response(coord.hass, eid)["entries"][eid]
    assert "dc_bindings_sha256" not in default
    meta = _build_forecast_response(coord.hass, eid, include_capture_metadata=True)["entries"][eid]
    expected = _binding_sha256({
        "entry_id": eid,
        "dc_sources": {p.name: p.actual_entity for p in coord._site.planes if p.actual_entity},
    })
    assert meta["dc_bindings_sha256"] == meta["forecast_dc_bindings_sha256"] == expected
    assert meta["night_zero_slots"] == [
        (night + timedelta(minutes=15 * i)).isoformat() for i in (0, 2, 3)
    ]
    assert meta["invalid_weather_slots"] == [(night + timedelta(minutes=15)).isoformat()]
