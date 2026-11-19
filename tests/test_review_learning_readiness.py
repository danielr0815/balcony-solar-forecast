"""A learned candidate is not an applied, trusted inverter efficiency."""
from types import SimpleNamespace

import pytest

from custom_components.balcony_solar_forecast.sensor import PowerNowSensor


@pytest.mark.parametrize('n', [0, 1, 19, 20])
def test_eta_source_describes_applied_value(n):
    sensor = PowerNowSensor.__new__(PowerNowSensor)
    learned = {'n': n, 'eta': .9, 'effective': .9 if n >= 20 else None}
    sensor.coordinator = SimpleNamespace(data={"status": "fresh"}, inverter_efficiency_learned=lambda: learned)
    attributes = sensor.extra_state_attributes
    assert attributes['inverter_efficiency_source'] == ('learned' if n >= 20 else 'config')
    if n:
        assert attributes['inverter_efficiency_learned']['n'] == n


def test_eta_source_stays_configured_during_physics_fallback():
    sensor = PowerNowSensor.__new__(PowerNowSensor)
    sensor.coordinator = SimpleNamespace(
        data={"status": "physics_fallback"},
        inverter_efficiency_learned=lambda: {"n": 20, "eta": .9, "effective": .9},
    )
    attributes = sensor.extra_state_attributes
    assert attributes["inverter_efficiency_source"] == "config"
    assert attributes["inverter_efficiency_learned"]["effective"] is None
