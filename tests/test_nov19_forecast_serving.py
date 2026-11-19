"""Public forecasts preserve coverage and fetch provenance."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("homeassistant")

from homeassistant.util import dt as dt_util

from custom_components.balcony_solar_forecast._forecast_presenter import (
    _build_forecast_response,
    _hourly_from_slots,
)
from custom_components.balcony_solar_forecast.const import DOMAIN
from custom_components.balcony_solar_forecast.core.types import (
    BiasCell,
    BiasState,
    InverterGroup,
    LearnerConfig,
    PlaneConfig,
    SiteConfig,
)
from custom_components.balcony_solar_forecast.energy import async_get_solar_forecast
from custom_components.balcony_solar_forecast.fetcher import (
    FetchError,
    OpenMeteoFetcher,
    parse_weather,
)
from custom_components.balcony_solar_forecast.sensor import (
    EnergyProductionDcSensor,
    PowerNowDcSensor,
    PowerNowSensor,
)
from tests.helpers.weather import NOW, _coord, _FakeFetcher, _om_payload, _PayloadStore


def _clock(monkeypatch, tz: str = "UTC"):
    local = ZoneInfo(tz)
    monkeypatch.setattr(dt_util, "as_local", lambda stamp: stamp.astimezone(local))
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)


def _diffuse_payload(start: datetime, quarters: int, irradiance) -> dict:
    payload = _om_payload(n_quarters=quarters, start_iso=start.strftime("%Y-%m-%dT%H:%M"))
    weather = payload["minutely_15"]
    ghi = [irradiance(start + timedelta(minutes=15 * (i - 1))) for i in range(quarters)]
    weather.update(
        shortwave_radiation=ghi, diffuse_radiation=ghi.copy(),
        direct_normal_irradiance=[0.0] * quarters,
        temperature_2m=[25.0] * quarters,
    )
    return payload


def _reference_site(lat: float = 40.7128, lon: float = -74.006) -> SiteConfig:
    return SiteConfig(
        latitude=lat, longitude=lon,
        planes=(PlaneConfig("panel", 180.0, 0.0, 400.0, efficiency=1.0, ross_coeff=0.0),),
        groups=(InverterGroup("mic", ("panel",), 1000.0, 0.95),),
    )


def _power_sensors(coord):
    source = SimpleNamespace(entry=coord.entry, data=coord.data, last_update_success=coord.last_update_success)
    return PowerNowSensor(source), PowerNowDcSensor(source)


async def test_gap_does_not_publish_zero_or_a_partial_hour(monkeypatch):
    _clock(monkeypatch)
    coord = _coord()
    coord._site = _reference_site()
    payload = _diffuse_payload(
        NOW.replace(hour=0) + timedelta(minutes=15), 96,
        lambda stamp: 0.0 if stamp.hour == 0 else 500.0,
    )
    payload["minutely_15"]["direct_normal_irradiance"][48] = None
    coord._fetcher = _FakeFetcher([payload])
    coord.data = await coord._async_update_data()
    coord.last_update_success = True
    ac, dc = _power_sensors(coord)
    assert (ac.native_value, dc.native_value) == (None, None)
    assert coord.data["energy_today_kwh_ac"] is None
    assert coord.data["energy_today_kwh"] is None

    hass = SimpleNamespace(data={DOMAIN: {coord.entry.entry_id: coord}})
    start = NOW.isoformat()
    following = (NOW + timedelta(hours=1)).isoformat()
    night = NOW.replace(hour=0).isoformat()
    energy = (await async_get_solar_forecast(hass, coord.entry.entry_id))["wh_hours"]
    assert start not in energy
    assert energy[night] == 0.0
    assert energy[following] == pytest.approx(190.0)

    entry = _build_forecast_response(hass, coord.entry.entry_id)["entries"][coord.entry.entry_id]
    position = entry["slot_starts"].index(start)
    assert entry["total_15min"][position] is None
    assert entry["planes"]["panel"][position] is None
    assert entry["total_15min"][0] == 0.0
    assert start not in entry["total_hourly"]
    assert start not in entry["raw_hourly_wh"]
    assert entry["total_hourly"][night] == 0.0


async def test_complete_night_zero_is_known_to_ac_and_dc_sensors(monkeypatch):
    _clock(monkeypatch)
    coord = _coord()
    coord._site = _reference_site(0.0, 180.0)
    coord._fetcher = _FakeFetcher([_diffuse_payload(
        NOW + timedelta(minutes=15), 4, lambda _stamp: 0.0,
    )])
    coord.data = await coord._async_update_data()
    coord.last_update_success = True
    ac, dc = _power_sensors(coord)
    assert (ac.native_value, dc.native_value) == (0.0, 0.0)
    assert coord.data["hourly_wh_ac"] == {NOW.isoformat(): 0.0}


async def test_short_edge_omits_energy_hour(monkeypatch):
    _clock(monkeypatch)
    coord = _coord()
    coord._site = _reference_site()
    coord._fetcher = _FakeFetcher([_diffuse_payload(
        NOW + timedelta(minutes=15), 5, lambda _stamp: 500.0,
    )])
    coord.data = await coord._async_update_data()
    coord.last_update_success = True
    hass = SimpleNamespace(data={DOMAIN: {coord.entry.entry_id: coord}})
    energy = (await async_get_solar_forecast(hass, coord.entry.entry_id))["wh_hours"]
    assert energy == {NOW.isoformat(): pytest.approx(190.0)}


def test_hourly_band_omits_gaps_and_preserves_four_valid_zeros():
    values = {
        (NOW + timedelta(minutes=15 * i)).isoformat(): wh
        for i, wh in enumerate((0.0, 0.0, 0.0, 0.0, 100.0, 100.0, 100.0))
    }
    assert _hourly_from_slots(values) == {NOW.isoformat(): 0.0}


@pytest.mark.parametrize(("month", "day", "expected_hours"), [(3, 29, 23), (10, 25, 25)])
async def test_daily_headline_uses_dst_calendar_day(monkeypatch, month, day, expected_hours):
    local = ZoneInfo("Europe/Berlin")
    _clock(monkeypatch, local.key)
    coord = _coord()
    coord.hass.config.time_zone = local.key
    coord._site = _reference_site()
    start = datetime(2026, month, day, tzinfo=local).astimezone(UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: start + timedelta(hours=2))
    coord._fetcher = _FakeFetcher([_diffuse_payload(
        start + timedelta(minutes=15), expected_hours * 4, lambda _stamp: 500.0,
    )])
    data = await coord._async_update_data()
    assert data["energy_today_kwh"] == pytest.approx(expected_hours * 0.200)
    assert data["energy_today_kwh_ac"] == pytest.approx(expected_hours * 0.190)


async def test_new_york_today_stays_complete_across_utc_midnight(monkeypatch):
    local = ZoneInfo("America/New_York")
    _clock(monkeypatch, local.key)
    coord = _coord()
    coord.hass.config.time_zone = local.key
    coord._site = _reference_site()
    now = datetime(2026, 11, 19, 23, 45, tzinfo=UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    fetcher = OpenMeteoFetcher(None)
    coord._fetcher = fetcher
    requests = []

    def ghi(stamp):
        local_stamp = stamp.astimezone(local)
        hours = local_stamp.hour + local_stamp.minute / 60
        return max(0.0, 300.0 * (1 - abs(hours - 12) / 4))

    async def fetch(params, validate):
        requests.append(params)
        prior = int(params.get("past_days", "0"))
        start = now.replace(hour=0, minute=0) - timedelta(days=prior)
        payload = _diffuse_payload(start, (int(params["forecast_days"]) + prior) * 96, ghi)
        validate(payload)
        return payload

    monkeypatch.setattr(fetcher, "_fetch_validated", fetch)
    before = await coord._async_update_data()
    now += timedelta(minutes=30)
    after = await coord._async_update_data()
    assert len(requests) == 2
    # Triangle base 8h, height 300 W/m². Horizontal plane at 25 °C:
    # 1200 Wh/m² × 400 Wp / 1000 W/m² × 0.95 = 456 Wh AC.
    assert before["energy_today_kwh_ac"] == 0.456
    assert after["energy_today_kwh_ac"] == 0.456
    assert before["status"] == after["status"] == "fresh"


async def test_weather_cache_follows_location_and_reuses_matching_image(monkeypatch):
    _clock(monkeypatch)
    store = _PayloadStore()
    first = _coord(store)
    rich = _diffuse_payload(NOW + timedelta(minutes=15), 8, lambda _stamp: 500.0)
    first._fetcher = _FakeFetcher([rich])
    await first._async_try_fetch(NOW - timedelta(minutes=10))

    same_site = _coord(store)
    same_site._fetcher = _FakeFetcher([FetchError("provider unavailable")])
    await same_site.async_prime_from_store()
    assert (await same_site._async_update_data())["status"] == "cached"

    new_site = _coord(store)
    new_site._site = replace(first._site, longitude=first._site.longitude + 35.0)
    new_site._fetcher = _FakeFetcher([FetchError("provider unavailable")])
    await new_site.async_prime_from_store()
    assert await _status_after_fetch(new_site) == "unavailable"

    shorter = _diffuse_payload(NOW + timedelta(minutes=15), 4, lambda _stamp: 0.0)
    new_site._last_attempt_at = None
    new_site._fetcher = _FakeFetcher([shorter])
    data = await new_site._async_update_data()
    assert data["power_now_w_ac"] == 0.0
    assert data["status"] == "fresh"
    assert store.last_payload["payload"] == shorter


async def _status_after_fetch(coord):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    try:
        return (await coord._async_update_data())["status"]
    except UpdateFailed:
        return "unavailable"


async def test_fallback_for_aged_weather_disables_serve_ready_bias(monkeypatch):
    _clock(monkeypatch)
    store = _PayloadStore()
    store.set_last_payload(
        _diffuse_payload(NOW + timedelta(minutes=15), 8, lambda _stamp: 500.0),
        (NOW - timedelta(hours=24)).isoformat(),
    )
    coord = _coord(store)
    coord._site = _reference_site(0.0, 0.0)
    coord._fetcher = _FakeFetcher([FetchError("provider unavailable")])
    coord._learner_config = LearnerConfig(slow_enabled=False, fast_enabled=False, day_ahead_enabled=True)
    coord._bias_state = BiasState(cells={
        BiasState.cell_key(cloud, part): BiasCell(theta=1.3, covariance=1.0, n=99)
        for cloud in ("clear", "mixed", "overcast", "fog")
        for part in ("morning", "midday", "afternoon")
    })
    monkeypatch.setattr(coord, "_trusted_inverter_eta", lambda: 0.90)
    cached = await coord._async_update_data()
    assert cached["status"] == "cached"
    assert cached["power_now_w"] == pytest.approx(260.0)
    assert cached["power_now_w_ac"] == pytest.approx(234.0)

    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW + timedelta(seconds=1))
    data = await coord._async_update_data()
    assert data["status"] == "physics_fallback"
    assert data["power_now_w"] == pytest.approx(200.0)
    assert data["power_now_w_ac"] == pytest.approx(190.0)
    assert data["correction_source"] == "none"


def test_dc_diagnostics_clear_retained_forecast_on_update_failure():
    source = SimpleNamespace(
        entry=SimpleNamespace(entry_id="test"), last_update_success=True,
        data={"power_now_w": 320.0, "energy_today_kwh": 1.2},
    )
    power = PowerNowDcSensor(source)
    energy = EnergyProductionDcSensor(source, "energy_today_dc", "energy_today_kwh")
    assert (power.native_value, energy.native_value) == (320.0, 1.2)
    source.last_update_success = False
    assert power.available and energy.available
    assert (power.native_value, energy.native_value) == (None, None)
    source.last_update_success = True
    assert (power.native_value, energy.native_value) == (320.0, 1.2)


def test_ensemble_uses_ghi_independently_of_temperature_and_dni():
    coord = _coord()
    payload = _om_payload(n_quarters=4, start_iso="2026-07-05T12:15")
    weather = payload["minutely_15"]
    weather["shortwave_radiation"][:] = [100.0, 300.0, 500.0, 700.0]
    weather["temperature_2m"][-2:] = [None, None]
    weather["direct_normal_irradiance"][0] = None
    assert coord._det_ghi_by_hour(parse_weather(payload)) == {NOW.isoformat(): 400.0}
    weather["shortwave_radiation"][-1] = None
    assert coord._det_ghi_by_hour(parse_weather(payload)) == {}
