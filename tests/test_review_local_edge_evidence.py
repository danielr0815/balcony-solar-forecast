"""UTC-hour labels and local-day midnight fragments carry separate evidence."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.balcony_solar_forecast.core.types import (
    IssuedSnapshot,
    PlaneHourlyModeled,
)
from custom_components.balcony_solar_forecast.store import ForecastStore, _empty_state
from tests.helpers.coordinator import _make_coordinator
from tests.test_review_nightly_evidence import _five_minute_rows, _patch_day_statistics


@pytest.mark.parametrize("midnight_sun", [True, False])
async def test_local_day_needs_only_production_relevant_edge_evidence(monkeypatch, midnight_sun):
    tz = ZoneInfo("Asia/Kolkata")
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", tz)
    c = _make_coordinator()
    c.data = None
    if midnight_sun:
        c._site = replace(c._site, latitude=78.2, longitude=15.6)
    else:
        c._site = replace(c._site, latitude=22.5, longitude=88.4)
    day = date(2026, 6, 21)
    iso = day.isoformat()
    start = datetime.combine(day, datetime.min.time(), tz).astimezone(UTC)
    first_hour = start.replace(minute=0) + timedelta(hours=1)
    starts = [first_hour + timedelta(hours=index) for index in range(24)]
    rows = [row for index, hour in enumerate(starts) for row in _five_minute_rows(hour, 95 + index)]
    leading = _five_minute_rows(start.replace(minute=0), 120 if midnight_sun else 5)[6:]
    if not midnight_sun:
        leading = [{**row, "min": 0, "mean": 0, "max": 0} for row in leading]
    curve = {hour.isoformat(): 200 for hour in starts}
    store = ForecastStore.__new__(ForecastStore)
    store._data = _empty_state()
    monkeypatch.setattr(store, "_schedule_save", lambda: None)
    c._store = store
    snap = IssuedSnapshot(
        issued_at=start.isoformat(), status="fresh", raw_hourly_wh=curve,
        corrected_hourly_wh=curve, raw_daily_kwh={iso: 4.8}, corrected_daily_kwh={iso: 4.8},
        complete_weather_hours=tuple(curve),
        per_plane={p.name: PlaneHourlyModeled() for p in c._site.planes},
    ).to_dict()
    snap["weather_day_complete"] = True
    store.record_issued(iso, snap)
    monkeypatch.setattr(c, "_catchup_days", lambda _: [day])
    complete = False

    def read(_hass, _first, _last, _entities, _period, _units, _fields):
        return {"sensor.m1": leading + rows, "sensor.m2": (leading if complete else leading[:1]) + rows}

    _patch_day_statistics(monkeypatch, read)
    now = start + timedelta(days=1, hours=2)
    await c._async_nightly_job(now)
    assert "corrected" in c._drift_state.daily_mae[iso]
    assert store.is_day_trained(iso)
    if midnight_sun:
        assert iso not in c._scoreboard_state.days
        assert "corrected_daily_abs" not in c._drift_state.daily_mae[iso]
        partial_total = sum(store.get_actuals(iso).values())
        complete = True
        await c._async_nightly_job(now)
        assert sum(store.get_actuals(iso).values()) == partial_total + 50
        assert c._scoreboard_state.days[iso].measured_kwh == pytest.approx((partial_total + 50) / 1000)
        assert "corrected_daily_abs" in c._drift_state.daily_mae[iso]
    else:
        assert iso in c._scoreboard_state.days
        assert "corrected_daily_abs" in c._drift_state.daily_mae[iso]
