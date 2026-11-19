"""Nightly score evidence has an exact local day, issuance time and meter basis."""

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
from tests.helpers.weather import _coord, _FakeFetcher, _om_payload
from tests.test_review_nightly_evidence import _patch_day_statistics


def _recorded_coordinator(monkeypatch, tz, *, latitude, longitude):
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", tz)
    c = _make_coordinator()
    c._site = replace(c._site, latitude=latitude, longitude=longitude)
    c.hass.config.time_zone = tz.key
    c.data = None
    store = ForecastStore.__new__(ForecastStore)
    store._data = _empty_state()
    monkeypatch.setattr(store, "_schedule_save", lambda: None)
    c._store = store
    day = date(2026, 6, 21)
    monkeypatch.setattr(c, "_catchup_days", lambda _: [day])
    start = datetime.combine(day, datetime.min.time(), tz).astimezone(UTC)
    return c, day, start


def _row(start, watts):
    return {"start": start.timestamp(), "min": watts, "mean": watts, "max": watts}


def _polar_day(monkeypatch, *, issue_minutes=0, include_last_hour=True):
    c, day, start = _recorded_coordinator(monkeypatch, ZoneInfo("Asia/Kolkata"), latitude=78.2, longitude=15.6)
    base = start.replace(minute=0) + timedelta(hours=1)
    curve = {(base + timedelta(hours=i)).isoformat(): 220.0 for i in range(24 if include_last_hour else 23)}
    planes = {
        p.name: PlaneHourlyModeled(raw_wh=dict.fromkeys(curve, 110), corrected_wh=dict.fromkeys(curve, 110))
        for p in c._site.planes
    }
    snapshot = IssuedSnapshot(
        issued_at=(start + timedelta(minutes=issue_minutes)).isoformat(),
        status="fresh", raw_hourly_wh=curve, corrected_hourly_wh=curve,
        raw_daily_kwh={day.isoformat(): 5.28}, corrected_daily_kwh={day.isoformat(): 5.28},
        complete_weather_hours=tuple(curve), weather_day_complete=True, per_plane=planes,
        leading_edge_forecast_known=True,
        per_plane_daily_wh={p.name: {"raw": 2640, "corrected": 2640} for p in c._site.planes},
    )
    c._store.record_issued(day.isoformat(), snapshot.to_dict())
    rows = [_row(start + timedelta(minutes=i * 5), 100 + (i // 12) % 8) for i in range(294)]
    return c, day, start, snapshot, rows


@pytest.mark.parametrize("already_reloaded", [False, True])
async def test_late_first_production_withdraws_persisted_score_and_drift(monkeypatch, already_reloaded):
    c, day, start = _recorded_coordinator(monkeypatch, ZoneInfo("UTC"), latitude=62, longitude=0)
    iso = day.isoformat()
    curve = {(start + timedelta(hours=h)).isoformat(): 200.0 if h == 12 else 0.0 for h in range(24)}
    snapshot = IssuedSnapshot(
        issued_at=(start + timedelta(hours=1, minutes=30)).isoformat(),
        status="fresh", raw_hourly_wh=curve, corrected_hourly_wh=curve, complete_weather_hours=tuple(curve),
        per_plane={p.name: PlaneHourlyModeled() for p in c._site.planes},
    )
    c._store.record_issued(iso, snapshot.to_dict())
    rows = [_row(start + timedelta(minutes=i * 5), 2 if i < 12 else 100 if i // 12 == 12 else 0)
            for i in range(288)]
    partial = rows[:11] + rows[12:]
    now = start + timedelta(days=1, hours=2)
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": partial, "sensor.m2": partial})
    await c._async_nightly_job(now)
    assert c._scoreboard_state.days[iso].measured_kwh == .2
    assert iso in c._drift_state.daily_mae
    assert iso in c._drift_state.loss_evidence
    trained = (c._bias_state, c._shademap_state, c._quantile_state)
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.m2": rows})
    if already_reloaded:
        daily, hourly = await c._async_read_actuals(day)
        c._store.record_verified_actuals(iso, daily, hourly, local_day_edges_complete=True)
    await c._async_nightly_job(now)
    assert sum(c._store.get_actuals(iso).values()) == 204
    assert c._issued_after_cutoff(snapshot, day)
    assert iso not in c._store.get_scoreboard_state().days
    drift = c._store.get_drift_state()
    assert iso not in drift.daily_mae and iso not in drift.loss_evidence
    assert c._store.is_day_trained(iso)
    assert (c._bias_state, c._shademap_state, c._quantile_state) == trained


@pytest.mark.parametrize("include_last_weather_hour", [False, True])
async def test_local_day_score_does_not_need_following_day_buckets(monkeypatch, include_last_weather_hour):
    c, day, start, _, rows = _polar_day(monkeypatch, include_last_hour=include_last_weather_hour)
    iso = day.isoformat()
    local_only = rows[:288]
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": local_only, "sensor.m2": local_only})
    await c._async_nightly_job(start + timedelta(days=1, hours=2))
    score = c._scoreboard_state.days.get(iso)
    assert score is not None
    assert score.engine_kwh == 5.28
    assert score.measured_kwh == pytest.approx(sum(row["mean"] for row in local_only) * 2 / 12000)
    assert score.engine_hourly_mae == pytest.approx(
        sum(abs(220 - 2 * sum(row["mean"] for row in rows[6 + i*12:18 + i*12]) / 12) for i in range(23)) / 23
    )
    assert c._drift_state.daily_mae[iso]["corrected_daily_bias"] == pytest.approx(5280 - score.measured_kwh * 1000)


@pytest.mark.parametrize("first_positive_minute", [0, 20])
async def test_measured_leading_local_fragment_sets_cutoff(monkeypatch, first_positive_minute):
    c, day, start, snapshot, rows = _polar_day(monkeypatch, issue_minutes=15)
    for i in range(first_positive_minute // 5):
        rows[i] = _row(start + timedelta(minutes=i * 5), 0)
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.m2": rows})
    await c._async_nightly_job(start + timedelta(days=1, hours=2))
    eligible = first_positive_minute > 15
    assert c._issued_after_cutoff(snapshot, day) is not eligible
    assert (day.isoformat() in c._scoreboard_state.days) is eligible


async def test_archived_modeled_leading_quarter_sets_cutoff_before_recorder_labels(monkeypatch):
    tz = ZoneInfo("Asia/Kolkata")
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", tz)
    day = date(2026, 6, 21)
    start = datetime.combine(day, datetime.min.time(), tz).astimezone(UTC)
    c = _coord()
    c.hass.config.time_zone = tz.key
    c._site = replace(c._site, latitude=78.2, longitude=15.6)
    c._fetcher = _FakeFetcher([_om_payload(n_quarters=100, start_iso=(start - timedelta(minutes=15)).isoformat())])
    monkeypatch.setattr(dt_util, "utcnow", lambda: start + timedelta(minutes=15))
    c.data = await c._async_update_data()
    c.last_update_success = True
    assert c.data["wh_period"][start.isoformat()] > 0
    await c._snapshot_issued(day)
    issued = IssuedSnapshot.from_dict(c._store.get_issued(day.isoformat()))
    assert c._issued_after_cutoff(issued, day)


@pytest.mark.parametrize("detach", [True, False])
async def test_binding_change_preserves_completed_score_on_original_basis(monkeypatch, detach):
    c, day, start, _, rows = _polar_day(monkeypatch)
    iso = day.isoformat()
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.m2": rows})
    now = start + timedelta(days=1, hours=2)
    await c._async_nightly_job(now)
    old_score = c._scoreboard_state.days[iso]
    old_actuals = dict(c._store.get_actuals(iso))
    a, b = c._site.planes
    c._site = replace(c._site, planes=(a, replace(b, actual_entity=None if detach else "sensor.new_m2")))
    replacement = [_row(start + timedelta(minutes=i * 5), 180 + (i // 12) % 8) for i in range(294)]
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.m2": rows, "sensor.new_m2": replacement})
    await c._async_nightly_job(now)
    assert c._scoreboard_state.days[iso] == old_score
    assert c._store.get_actuals(iso) == old_actuals


async def test_unscored_date_can_replace_the_source_for_same_module(monkeypatch):
    c, day, start, _, rows = _polar_day(monkeypatch)
    iso = day.isoformat()
    missing_leading = rows[:1] + rows[6:]
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.m2": missing_leading})
    now = start + timedelta(days=1, hours=2)
    await c._async_nightly_job(now)
    assert c._store.has_verified_actuals(iso)
    assert iso not in c._scoreboard_state.days
    a, b = c._site.planes
    c._site = replace(c._site, planes=(a, replace(b, actual_entity="sensor.new_m2")))
    replacement = [_row(start + timedelta(minutes=i * 5), 180 + (i // 12) % 8) for i in range(288)]
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows[:288], "sensor.new_m2": replacement})
    await c._async_nightly_job(now)
    score = c._scoreboard_state.days.get(iso)
    assert score is not None
    assert score.measured_kwh == pytest.approx(sum(row["mean"] for row in rows[:288] + replacement) / 12000)
