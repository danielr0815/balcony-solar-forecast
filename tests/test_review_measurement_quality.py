"""Corrupt DC labels must never become accepted energy or training data."""
from datetime import UTC, date, datetime

import pytest

from custom_components.balcony_solar_forecast._actuals import _actuals_from_stats


@pytest.mark.parametrize("bad", [-100.0, float("nan"), float("inf"), True, "invalid"])
def test_invalid_dc_statistic_quarantines_whole_day(bad):
    rows = [{"start": datetime(2026, 9, 30, h, tzinfo=UTC), "mean": h + 1.0}
            for h in range(24)]
    rows[12]["mean"] = bad
    dropout = {}
    result = _actuals_from_stats(
        {"sensor.p": rows}, {"P": "sensor.p"}, expected_daylight_hours=0,
        day=date(2026, 9, 30), wp_by_module={"P": 400}, dropout_out=dropout,
    )
    assert result == ({}, {})
    assert dropout["reason"] == "implausible_channel"


def test_half_hour_timezone_does_not_shift_recorder_solar_midpoints(monkeypatch):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from custom_components.balcony_solar_forecast._actuals import (
        _daylight_hour_keys_in_local_day,
    )
    from custom_components.balcony_solar_forecast.core import solpos
    from custom_components.balcony_solar_forecast.core.types import SiteConfig

    calls = []
    def position(at, latitude, longitude):
        calls.append(at)
        return 0, 10 if at.hour == 10 else -10
    monkeypatch.setattr(solpos, 'sun_position', position)
    start = datetime(2026, 9, 30, tzinfo=ZoneInfo('Asia/Kolkata'))
    keys = _daylight_hour_keys_in_local_day(SiteConfig(0, 0, (), ()), start, start+timedelta(days=1))
    assert keys == {'2026-09-30T10:00:00+00:00'}
    assert all(at.minute == 30 for at in calls)
