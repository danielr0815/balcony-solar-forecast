"""Display-name edits preserve existing module and physical-model identities."""
from dataclasses import replace
from datetime import UTC, datetime

from balcony_solar_forecast.core.bootstrap_build import site_signature
from balcony_solar_forecast.core.config_changes import site_changes
from balcony_solar_forecast.core.config_fingerprint import site_fingerprint
from balcony_solar_forecast.core.engine import compute_forecast
from balcony_solar_forecast.core.types import (
    PlaneConfig,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)


def test_legacy_serialization_and_label_edit_do_not_change_forecasts_or_keys():
    plane = PlaneConfig('M1', 180, 30, 400, actual_entity='sensor.p')
    before = SiteConfig(48, 12, (plane,), ())
    after = replace(before, planes=(replace(plane, display_name='South balcony'),))
    legacy = plane.to_dict()
    assert 'display_name' not in legacy
    assert PlaneConfig.from_dict(legacy).to_dict() == legacy
    assert PlaneConfig.from_dict({**legacy, 'display_name': ' South balcony '}).label == 'South balcony'
    assert after.planes[0].name == 'M1' and after.planes[0].shade_channel == 'M1'
    assert site_fingerprint(before) == site_fingerprint(after)
    assert site_signature(before) == site_signature(after)
    assert site_changes(before, after) == {'model_changed': False, 'modules': {'M1':['display_name']}, 'site_fields':[]}
    start = datetime(2026,6,21,12,tzinfo=UTC)
    weather = WeatherSeries((WeatherSlot(start, 600, 650, 100, 20),))
    assert compute_forecast(before,weather,start) == compute_forecast(after,weather,start)
