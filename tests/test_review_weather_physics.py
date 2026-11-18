"""The forecast preserves weather gaps and removes Intraday before group clips."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.balcony_solar_forecast._weather_cache import WeatherCache
from custom_components.balcony_solar_forecast.const import (
    OPEN_METEO_HOURLY,
    OPEN_METEO_MINUTELY_15,
)
from custom_components.balcony_solar_forecast.core.engine import compute_forecast
from custom_components.balcony_solar_forecast.core.types import (
    BiasCell,
    BiasState,
    InverterGroup,
    PlaneConfig,
    SiteConfig,
)
from custom_components.balcony_solar_forecast.fetcher import parse_weather
from tests.helpers.coordinator import _make_coordinator

NOW = datetime(2026, 6, 20, 12, tzinfo=UTC)


def _payload(start=NOW, ghi=800, dni=750, dhi=100):
    return {
        "minutely_15": {
            "time": [
                (start + timedelta(minutes=15 * i)).isoformat()
                for i in (1, 2)
            ],
            "shortwave_radiation": [ghi, ghi],
            "direct_normal_irradiance": [dni, dni],
            "diffuse_radiation": [dhi, dhi],
            "temperature_2m": [25, 25],
        },
        "hourly": {
            "time": [start.replace(minute=0).isoformat()],
            **{field: [0] for field in OPEN_METEO_HOURLY},
        },
    }


def _plane(name, wp):
    return PlaneConfig(name=name, azimuth_deg=180, tilt_deg=0, wp=wp)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", OPEN_METEO_MINUTELY_15[:3])
async def test_missing_radiation_retains_richer_weather_and_skips_physics(field):
    prior = _payload()
    partial = _payload()
    partial["minutely_15"][field][0] = None
    cache = WeatherCache()
    prior_at = NOW - timedelta(minutes=30)
    cache.restore({"payload": prior, "fetched_at": prior_at.isoformat()})
    writes = []

    async def fetch():
        return partial

    await cache.async_refresh(NOW, fetch=fetch, persist=lambda *args: writes.append(args))
    assert cache.payload is prior
    assert cache.fetched_at == prior_at
    assert writes == []

    site = SiteConfig(latitude=49, longitude=8, planes=(_plane("PV", 400),), groups=())
    complete = compute_forecast(site, cache.weather(), NOW)
    assert complete.ac_watts[0] > 0
    result = compute_forecast(site, parse_weather(partial), NOW)
    assert result.ac_watts == (0.0, complete.ac_watts[1])
    assert result.plane_results[0].beam_ref_watts[0] == 0
    assert result.invalid_weather_slots == {NOW}


@pytest.mark.asyncio
async def test_numeric_zero_is_valid_at_night():
    midnight = NOW.replace(hour=0)
    darkness = _payload(start=midnight, ghi=0, dni=0, dhi=0)
    cache = WeatherCache()
    writes = []

    async def fetch():
        return darkness

    await cache.async_refresh(midnight, fetch=fetch, persist=lambda *args: writes.append(args))
    assert cache.payload is darkness
    assert len(writes) == 1
    site = SiteConfig(latitude=49, longitude=8, planes=(_plane("PV", 400),), groups=())
    result = compute_forecast(site, cache.weather(), midnight)
    assert result.total_watts == (0.0, 0.0)
    assert not getattr(result, "invalid_weather_slots", ())


@pytest.mark.parametrize("scalar", [0.6, 1.2])
def test_headlines_match_engine_with_intraday_disabled(scalar, monkeypatch):
    weather = parse_weather(_payload())
    site = SiteConfig(
        latitude=49, longitude=8,
        planes=(_plane("A", 400), _plane("B", 30), _plane("Ungrouped", 60)),
        groups=(
            InverterGroup(name="A", plane_names=("A",), ac_limit_w=100, inverter_efficiency=0.96),
            InverterGroup(name="B", plane_names=("B",), ac_limit_w=100, inverter_efficiency=0.87),
        ),
    )
    coord = _make_coordinator()
    coord._site = site
    coord._bias_state = BiasState(cells={"unused": BiasCell()})
    monkeypatch.setattr(
        "custom_components.balcony_solar_forecast.coordinator.bias_mod.day_ahead_factor_solar",
        lambda *args, **kwargs: 1.2,
    )
    reference = compute_forecast(site, weather, NOW, hooks=coord._build_learner_hooks(weather, NOW))
    coord._intraday_scalar = scalar
    served = compute_forecast(site, weather, NOW, hooks=coord._build_learner_hooks(weather, NOW))
    assert served.total_watts != reference.total_watts
    assert served.ac_watts != reference.ac_watts
    date = NOW.date().isoformat()
    assert coord._dayahead_today_kwh(served, NOW) == round(reference.daily_kwh[date], 3)
    assert coord._dayahead_today_kwh_ac(served, NOW) == round(reference.ac_daily_kwh[date], 3)
    assert served.dayahead_watts == reference.total_watts
    assert served.ac_dayahead_watts == reference.ac_watts
