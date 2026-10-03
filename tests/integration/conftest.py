"""Real Home Assistant fixture, isolated from the portable unit-test shims.

Run with --confcutdir=tests/integration -p no:homeassistant. Only the external
weather HTTP boundary is replaced; setup, registries, entry tasks and disk stores
are the installed HA implementations. HTTP listens on loopback with a random port.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component


def weather_payload() -> dict:
    """Fresh deterministic three-day weather, including full parsing context."""
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "minutely_15": {
            "time": [(start + timedelta(minutes=15 * i)).isoformat() for i in range(288)],
            "shortwave_radiation": [400.0] * 288,
            "direct_normal_irradiance": [500.0] * 288,
            "diffuse_radiation": [100.0] * 288,
            "temperature_2m": [20.0] * 288,
        },
        "hourly": {
            "time": [(start + timedelta(hours=i)).isoformat() for i in range(72)],
            "cloud_cover_low": [10.0] * 72,
            "cloud_cover_mid": [10.0] * 72,
            "cloud_cover_high": [10.0] * 72,
            "visibility": [20000.0] * 72,
            "snowfall": [0.0] * 72,
            "snow_depth": [0.0] * 72,
        },
    }


@pytest.fixture
async def real_hass(tmp_path, monkeypatch, unused_tcp_port):
    from custom_components.balcony_solar_forecast.fetcher import OpenMeteoFetcher

    async def weather_http(self, aiohttp, params, **kwargs):
        return weather_payload()

    monkeypatch.setattr(OpenMeteoFetcher, "_request_once", weather_http)
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    hass.config.skip_pip = True
    hass.config.skip_pip_packages = []
    config = {
        "homeassistant": {
            "name": "Lifecycle test", "latitude": 52.0, "longitude": 13.0,
            "elevation": 40, "time_zone": "Europe/Berlin", "unit_system": "metric",
            "country": "DE",
        },
        "http": {"server_host": "127.0.0.1", "server_port": unused_tcp_port},
        "lovelace": {"mode": "storage"},
    }
    try:
        hass.config_entries = ConfigEntries(hass, config)
        assert await bootstrap.async_load_base_functionality(hass) is not False
        await bootstrap.async_process_ha_core_config(hass, config["homeassistant"])
        for domain in ("homeassistant", "http", "lovelace"):
            assert await async_setup_component(hass, domain, config)
        await hass.async_start()
        hass.states.async_set("sensor.module_dc", "120", {
            "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement",
        })
        yield hass
    finally:
        await hass.async_stop(force=True)


@pytest.fixture
async def recorder_hass(real_hass, tmp_path):
    """Real SQLite and statistics import queue; no mocked recorder read path."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.helpers.recorder import async_initialize_recorder

    async_initialize_recorder(real_hass)
    config = {'recorder': {'db_url': f'sqlite:///{tmp_path / "statistics.db"}', 'commit_interval': 0}}
    assert await async_setup_component(real_hass, 'recorder', config)
    await real_hass.async_block_till_done()
    await get_instance(real_hass).async_block_till_done()
    return real_hass
