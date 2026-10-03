"""A correction of post-clip DC cannot resurrect clipped energy on AC."""
from datetime import UTC, datetime

import pytest
from balcony_solar_forecast.core.engine import LearnerHooks, compute_forecast
from balcony_solar_forecast.core.types import (
    InverterGroup,
    PlaneConfig,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)


@pytest.mark.parametrize('factor', [.5, 1, 1.5])
def test_ac_is_bounded_by_the_corrected_dc_source(factor):
    now = datetime(2026, 6, 21, 10, tzinfo=UTC)
    site = SiteConfig(0, 0, (PlaneConfig('P', 180, 30, 4000),),
                      (InverterGroup('G', ('P',), 90, .9),))
    weather = WeatherSeries((WeatherSlot(now, 900, 900, 100, 20),))
    result = compute_forecast(site, weather, now=now,
                              hooks=LearnerHooks(slot_factor=lambda start: factor))
    assert result.total_watts[0] == pytest.approx(min(100 * factor, 100))
    assert result.ac_watts[0] == pytest.approx(.9 * result.total_watts[0])
