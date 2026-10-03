"""A learned candidate is not an applied, trusted inverter efficiency."""
from types import SimpleNamespace

import pytest

from custom_components.balcony_solar_forecast.sensor import PowerNowSensor


@pytest.mark.parametrize('n', [0, 1, 19, 20])
def test_eta_source_describes_applied_value(n):
    sensor = PowerNowSensor.__new__(PowerNowSensor)
    learned = {'n': n, 'eta': .9, 'effective': .9 if n >= 20 else None}
    sensor.coordinator = SimpleNamespace(inverter_efficiency_learned=lambda: learned)
    attributes = sensor.extra_state_attributes
    assert attributes['inverter_efficiency_source'] == ('learned' if n >= 20 else 'config')
    if n:
        assert attributes['inverter_efficiency_learned']['n'] == n
