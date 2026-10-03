"""Full production bootstrap days cannot train from a surviving weather subset."""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from balcony_solar_forecast.core.bootstrap_build import HourlyWeather, accumulate_days
from balcony_solar_forecast.core.daylight import daylight_hour_keys
from balcony_solar_forecast.core.types import PlaneConfig, SiteConfig


@pytest.mark.parametrize(('day', 'hours'), [('2026-03-29', 23), ('2026-10-25', 25)])
def test_dst_enumerates_utc_hours_and_strict_weather_gaps_reject(day, hours):
    tz = ZoneInfo('Europe/Berlin')
    start = datetime.fromisoformat(day).replace(tzinfo=tz)
    end = (start+timedelta(days=1)).astimezone(UTC)
    first = start.astimezone(UTC)
    starts = [first+timedelta(hours=i) for i in range(hours)]
    site = SiteConfig(0, 0, (PlaneConfig('P', 180, 30, 400, actual_entity='sensor.p'),), ())
    expected = daylight_hour_keys(0, 0, start, end)
    assert expected and len(starts) == hours and starts[-1]+timedelta(hours=1) == end
    weather = [HourlyWeather(t, 600 if t.isoformat() in expected else 0,
                              700 if t.isoformat() in expected else 0,
                              100 if t.isoformat() in expected else 0, 20) for t in starts]
    actual = {'P': {t.isoformat(): 150+i if t.isoformat() in expected else 0 for i, t in enumerate(starts)}}
    accepted = accumulate_days(site, weather, actual, svf_by_plane={}, tz=tz, require_complete_day=True)
    assert accepted.days_used == 1
    duplicate = accumulate_days(site, weather + [weather[0]], actual,
                                svf_by_plane={}, tz=tz, require_complete_day=True)
    assert duplicate.days_used == 0 and not duplicate.bias
    missing = next(stamp for stamp in expected)
    partial = [wx for wx in weather if wx.start.isoformat() != missing]
    rejected = accumulate_days(site, partial, actual, svf_by_plane={}, tz=tz, require_complete_day=True)
    assert rejected.days_used == 0 and rejected.days_skipped == 1
    assert not rejected.bias and not rejected.shade and not rejected.quantile_state.bins


def test_invalid_coverage_windows_are_rejected():
    start = datetime(2026, 9, 30, tzinfo=UTC)
    for end in (start, start+timedelta(days=2), start.replace(tzinfo=None)):
        with pytest.raises(ValueError):
            daylight_hour_keys(0, 0, start, end)
