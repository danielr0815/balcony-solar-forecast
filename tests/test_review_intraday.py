"""A clipped meter reading is a censored observation, not a weather deficit."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.balcony_solar_forecast.core.types import (
    ForecastResult,
    InverterCalState,
    InverterGroup,
    PlaneConfig,
    PlaneResult,
    SiteConfig,
)
from tests.helpers.coordinator import _make_coordinator


def _saturated_site(measured, eta=0.9):
    start = datetime(2026, 6, 21, 12, tzinfo=UTC)
    coord = _make_coordinator()
    coord.hass.states.set("sensor.m1", measured, last_updated=start)
    coord._site = SiteConfig(latitude=50, longitude=10,
        planes=(PlaneConfig(name="M1", azimuth_deg=180, tilt_deg=30, wp=1000,
                            actual_entity="sensor.m1"),),
        groups=(InverterGroup(name="WR", plane_names=("M1",),
                              ac_limit_w=600 * eta, inverter_efficiency=eta),))
    starts = (start, start + timedelta(minutes=15))
    result = ForecastResult(slot_starts=starts, total_watts=(600, 600), hourly_wh={},
        plane_results=(PlaneResult(name="M1", watts=(600, 600), raw_watts=(600, 600),
                                   slow_watts=(600, 600)),))
    coord._day_factor = dict.fromkeys(starts, 1.5)
    coord._inverter_cal_state = InverterCalState(eta=0.9, n=100)
    return coord, result, start


@pytest.mark.parametrize("eta", [0.9, 1.0])
def test_live_saturation_does_not_train_a_weather_loss(eta):
    coord, result, start = _saturated_site(600, eta)
    assert coord._build_intraday_sample(result, start) is None


@pytest.mark.parametrize("theta", [0.8, 1.5])
def test_rearm_saturation_does_not_restore_false_weather_loss(theta):
    coord, result, start = _saturated_site(600)
    coord._day_factor = dict.fromkeys(result.slot_starts, theta)
    rows = [(start + timedelta(minutes=minute), 600.0) for minute in (0, 5, 10)]
    assert coord._rearm_samples_from_rows(result, rows, start + timedelta(minutes=15)) == []


def test_real_unsaturated_deficit_keeps_preclip_reference():
    coord, result, start = _saturated_site(400)
    sample = coord._build_intraday_sample(result, start)
    assert sample is not None
    # Scaling by 400/900 recovers 400 W before clipping. 400/600 would not.
    assert sample.measured_kc / sample.modeled_kc == pytest.approx(400 / 900)


def test_custom_group_efficiency_does_not_discard_an_unsaturated_deficit():
    coord, result, start = _saturated_site(500, eta=0.8)
    sample = coord._build_intraday_sample(result, start)
    assert sample is not None
    assert sample.measured_kc / sample.modeled_kc == pytest.approx(500 / 900)
