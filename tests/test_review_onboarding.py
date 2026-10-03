"""New installs must not inherit somebody else's physical measurement sources."""
from types import SimpleNamespace

import pytest
from balcony_solar_forecast._site_validation import SiteValidationError, validate_site
from balcony_solar_forecast.config_flow import _current_values


def test_new_install_uses_neutral_unmetered_single_panel():
    values = _current_values(None, hass_config=SimpleNamespace(latitude=35, longitude=139))
    site = validate_site(values['site'])
    assert len(site.planes) == 1
    assert (site.latitude, site.longitude) == (35, 139)
    assert not site.planes[0].actual_entity
    assert not site.planes[0].horizon


def test_duplicate_dc_source_cannot_count_energy_twice():
    raw = {'latitude': 35, 'longitude': 139, 'planes': [
        {'name': name, 'azimuth_deg': 180, 'tilt_deg': 30, 'wp': 400,
         'actual_entity': 'sensor.one'} for name in ('A', 'B')], 'groups': []}
    with pytest.raises(SiteValidationError, match='duplicate_actual_entity'):
        validate_site(raw)
