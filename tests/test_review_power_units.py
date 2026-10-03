"""DC live values must use physical watts and transport freshness."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.balcony_solar_forecast._glue_util import _usable_power


def test_kw_source_matches_watt_source():
    now = datetime(2026, 6, 21, 10, tzinfo=UTC)
    state = SimpleNamespace(state='.25', attributes={'unit_of_measurement': 'kW'}, last_updated=now)
    assert _usable_power(state, now, max_w=500) == 250


def test_energy_source_is_not_dc_power():
    now = datetime(2026, 6, 21, 10, tzinfo=UTC)
    state = SimpleNamespace(state='25', attributes={'unit_of_measurement': 'kWh'}, last_updated=now)
    assert _usable_power(state, now) is None


def test_unchanged_recently_reported_power_is_not_a_transport_dropout():
    now = datetime(2026, 6, 21, 10, tzinfo=UTC)
    state = SimpleNamespace(state='100', attributes={'unit_of_measurement': 'W'},
                            last_updated=now-timedelta(hours=6), last_reported=now)
    assert _usable_power(state, now) == 100


def test_measurement_metadata_distinguishes_power_energy_and_unknown():
    from balcony_solar_forecast.core.measurement_quality import power_source_problem
    assert power_source_problem({'unit_of_measurement': 'kW', 'device_class': 'power',
                                 'state_class': 'measurement'}) is None
    assert power_source_problem({}) is None
    assert power_source_problem({'unit_of_measurement': 'kWh'}) == 'invalid_power_source'
    assert power_source_problem({'unit_of_measurement': 'W', 'state_class': 'total_increasing'}) == 'invalid_power_source'


def test_flow_rejects_energy_source_and_shared_dc_ac_role():
    from types import SimpleNamespace

    import pytest

    from custom_components.balcony_solar_forecast._site_validation import (
        SiteValidationError,
    )
    from custom_components.balcony_solar_forecast.config_flow import (
        _validate_measurement_sources,
    )
    from custom_components.balcony_solar_forecast.core.types import (
        PlaneConfig,
        SiteConfig,
    )
    site = SiteConfig(0, 0, (PlaneConfig('one', 180, 30, 400, actual_entity='sensor.port'),), ())
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: SimpleNamespace(attributes={'unit_of_measurement': 'kWh'})))
    with pytest.raises(SiteValidationError, match='invalid_power_source'):
        _validate_measurement_sources(hass, site, {})
    with pytest.raises(SiteValidationError, match='measurement_role_collision'):
        _validate_measurement_sources(hass, site, {'ac_actual_entity': 'sensor.port'})
