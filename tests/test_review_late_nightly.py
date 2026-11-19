"""Recorder catch-up, calendar rollback and local-midnight measurement regressions."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.balcony_solar_forecast.core.types import (
    BiasCell,
    BiasState,
    IssuedSnapshot,
    PlaneHourlyModeled,
    ShademapBin,
    ShademapState,
)
from custom_components.balcony_solar_forecast.store import ForecastStore, _empty_state
from tests.helpers.coordinator import _make_coordinator
from tests.test_review_nightly_evidence import _five_minute_rows, _patch_day_statistics


def _issued(day, raw, slow, corrected):
    return IssuedSnapshot(
        issued_at=datetime.combine(day, datetime.min.time(), UTC).isoformat(),
        status="fresh", raw_hourly_wh=raw, slow_only_hourly_wh=slow,
        corrected_hourly_wh=corrected, complete_weather_hours=tuple(raw),
    ).to_dict()


def _hourly(day):
    start = datetime.combine(day, datetime.min.time(), UTC)
    return {(start + timedelta(hours=index)).isoformat(): float(100 + index) for index in range(24)}


async def test_twelfth_recorder_bucket_completes_score_without_refolding_learners(monkeypatch):
    c = _make_coordinator()
    store = ForecastStore.__new__(ForecastStore)
    store._data = _empty_state()
    monkeypatch.setattr(store, "_schedule_save", lambda: None)
    c._store = store
    c.data = None
    c._site = replace(c._site, ac_actual_entity="sensor.total_ac")
    day = date(2026, 7, 1)
    iso = day.isoformat()
    actuals = _hourly(day)
    modeled = {hour: watts * 2.4 for hour, watts in actuals.items()}
    store.record_issued(iso, _issued(day, modeled, modeled, modeled))
    target = datetime(2026, 7, 1, 12, 55, tzinfo=UTC)
    buckets = [row for key, watts in actuals.items() for row in _five_minute_rows(datetime.fromisoformat(key), watts)]
    recorder_reads = []

    def read(_hass, _start, _end, _entities, _period, _units, _fields):
        recorder_reads.append(iso)
        channel_two = [row for row in buckets if row["start"] != target.timestamp()] if len(recorder_reads) == 1 else buckets
        return {"sensor.m1": buckets, "sensor.m2": channel_two}

    async def ac_actuals(_):
        return {hour: watts * 2 * .96 for hour, watts in actuals.items()}

    _patch_day_statistics(monkeypatch, read)
    monkeypatch.setattr(c, "_catchup_days", lambda _: [day])
    monkeypatch.setattr(c, "_async_read_ac_actuals", ac_actuals)
    now = datetime(2026, 7, 2, 1, 30, tzinfo=UTC)
    await c._async_nightly_job(now)
    assert store.is_day_trained(iso)
    assert c._inverter_cal_state.n > 0
    assert iso not in c._scoreboard_state.days
    assert "raw_daily_abs" not in c._drift_state.daily_mae[iso]
    trained = (c._bias_state, c._shademap_state, c._quantile_state, c._inverter_cal_state)

    await c._async_nightly_job(now)
    assert recorder_reads == [iso, iso]
    assert store.get_hourly_actuals(iso)["M2"] == actuals
    assert c._scoreboard_state.days[iso].measured_kwh == pytest.approx(sum(actuals.values()) * 2 / 1000)
    assert "raw_daily_abs" in c._drift_state.daily_mae[iso]
    assert (c._bias_state, c._shademap_state, c._quantile_state, c._inverter_cal_state) == trained
    await c._async_nightly_job(now)
    assert recorder_reads == [iso, iso]


@pytest.mark.parametrize("losing_layer", ["day_ahead", "slow"])
@pytest.mark.parametrize("has_pre_streak_snapshot", [True, False])
async def test_delayed_seventh_loss_uses_pre_streak_snapshot_of_guilty_layer(
    monkeypatch, losing_layer, has_pre_streak_snapshot,
):
    c = _make_coordinator()
    processed = []
    monkeypatch.setattr(c, "_raise_repair_issue", lambda *_: None)

    def train_bias(*_):
        c._bias_state = BiasState(cells={
            "clear|midday": BiasCell(theta=1.0 + len(processed) / 100, n=len(processed)),
        })

    def train_shade(*_):
        c._shademap_state = ShademapState(channels={
            "M1": {"10:5:0": ShademapBin(tau=.50 + len(processed) / 100, n=len(processed))},
        })

    monkeypatch.setattr(c, "_train_day_ahead", train_bias)
    monkeypatch.setattr(c, "_train_shademap", train_shade)
    for target in (1, 2, 3, 4, 5, 6, 8, 9, 7):
        day = date(2026, 7, target)
        iso = day.isoformat()
        run = datetime(2026, 7, target + 1 if target != 7 else 10, 1, 30, tzinfo=UTC)
        monkeypatch.setattr(dt_util, "utcnow", lambda run=run: run)
        keys = _hourly(day)
        noon = f"{iso}T12:00:00+00:00"
        raw = dict.fromkeys(keys, 0.0) | {noon: 200.0}
        slow = raw | {noon: 275.0} if target <= 7 and losing_layer == "slow" else raw
        corrected = raw | {noon: 275.0} if target <= 7 else raw
        c._store.record_issued(iso, _issued(day, raw, slow, corrected))
        measured_half = dict.fromkeys(keys, 0.0) | {noon: 100.0}
        c._store.record_verified_actuals(iso, {"M1": 100, "M2": 100}, {"M1": measured_half, "M2": measured_half})
        if target == 7:
            assert not c._drift_state.day_ahead_disabled and not c._drift_state.slow_disabled
            if not has_pre_streak_snapshot:
                del c._store.snapshots[0]
        processed.append(iso)
        await c._train_and_guard(day)

    if losing_layer == "day_ahead":
        assert c._drift_state.day_ahead_disabled
        assert c._shademap_state.channels["M1"]["10:5:0"].n == 9
        if has_pre_streak_snapshot:
            assert c._bias_state == BiasState()
        else:
            assert c._bias_state.cells["clear|midday"].n == 9
    else:
        assert c._drift_state.slow_disabled
        assert c._bias_state.cells["clear|midday"].theta == pytest.approx(1.09)
        if has_pre_streak_snapshot:
            assert c._shademap_state == ShademapState()
        else:
            assert c._shademap_state.channels["M1"]["10:5:0"].n == 9


@pytest.mark.parametrize("zone,leading_minutes", [("Asia/Kolkata", 30), ("Asia/Kathmandu", 45)])
@pytest.mark.parametrize("in_day_watts", [0, 240])
async def test_recorder_integrates_exact_local_edges_but_retains_complete_utc_hour(
    monkeypatch, zone, leading_minutes, in_day_watts,
):
    tz = ZoneInfo(zone)
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", tz)
    c = _make_coordinator()
    day = date(2026, 7, 5)
    start = datetime.combine(day, datetime.min.time(), tz).astimezone(UTC)
    end = start + timedelta(days=1)
    utc_hour_after_end = end.replace(minute=0) + timedelta(hours=1)
    records = []
    stamp = start
    while stamp < utc_hour_after_end:
        if stamp.hour == start.hour and stamp < start.replace(minute=0) + timedelta(hours=1):
            watts = in_day_watts
        elif stamp >= end:
            watts = 120
        else:
            watts = 0
        records.append({"start": stamp.timestamp(), "min": watts, "mean": watts, "max": watts})
        stamp += timedelta(minutes=5)

    def read(_hass, first, last, _entities, _period, _units, _fields):
        visible = [row for row in records if first.timestamp() <= row["start"] < last.timestamp()]
        return {"sensor.m1": visible, "sensor.m2": visible}

    _patch_day_statistics(monkeypatch, read)
    daily, hourly = await c._async_read_actuals(day)
    expected_day = in_day_watts * leading_minutes / 60
    assert daily == {"M1": expected_day, "M2": expected_day}
    trailing_utc_key = end.replace(minute=0).isoformat()
    assert hourly["M1"][trailing_utc_key] == 120 * leading_minutes / 60
    assert start.replace(minute=0).isoformat() not in hourly["M1"]


@pytest.mark.parametrize("partial_metering", [False, True])
@pytest.mark.parametrize("proved_local_total", [False, True])
async def test_local_day_scores_require_the_matching_slot_energy_basis(
    monkeypatch, partial_metering, proved_local_total,
):
    tz = ZoneInfo("Asia/Kathmandu")
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", tz)
    c = _make_coordinator()
    if partial_metering:
        p1, p2 = c._site.planes
        c._site = replace(c._site, planes=(p1, replace(p2, actual_entity=None)))
    channel_names = ["M1"] if partial_metering else ["M1", "M2"]
    n_channels = len(channel_names)
    day = date(2026, 7, 5)
    iso = day.isoformat()
    start = datetime.combine(day, datetime.min.time(), tz).astimezone(UTC)
    first_full_utc = start.replace(minute=0) + timedelta(hours=1)
    keys = [(first_full_utc + timedelta(hours=i)).isoformat() for i in range(24)]
    noon, tail = f"{iso}T08:00:00+00:00", keys[-1]
    # Each channel contributes 40 Wh in the leading local-day fragment,
    # 100 Wh midday, then a 60 Wh signal entirely after local midnight.
    actual_hourly = dict.fromkeys(keys, 0.0) | {noon: 100.0, tail: 60.0}
    model_hourly = dict.fromkeys(keys, 0.0) | {noon: 120.0, tail: 60.0}
    all_modeled = {hour: 2 * wh for hour, wh in model_hourly.items()}
    issued = IssuedSnapshot(
        issued_at=start.isoformat(), status="fresh", complete_weather_hours=tuple(keys),
        raw_hourly_wh=all_modeled, corrected_hourly_wh=all_modeled,
        raw_daily_kwh={iso: .3}, corrected_daily_kwh={iso: .3},
        per_plane={
            p.name: PlaneHourlyModeled(raw_wh=model_hourly, corrected_wh=model_hourly)
            for p in c._site.planes
        },
    ).to_dict()
    if proved_local_total:
        issued["weather_day_complete"] = True
        issued["per_plane_daily_wh"] = {
            name: {"raw": 150, "corrected": 150} for name in ("M1", "M2")
        }
    c._store.record_issued(iso, issued)
    actuals = dict.fromkeys(channel_names, 140.0)
    c._store.record_verified_actuals(
        iso, actuals, dict.fromkeys(channel_names, actual_hourly), local_day_edges_complete=True,
    )
    await c._score_scoreboard_day(day)
    c._update_drift(iso, issued, actuals)
    drift = c._drift_state.daily_mae[iso]
    assert drift["corrected"] == 10 * n_channels  # UTC-hour MAE remains independent.
    if proved_local_total:
        score = c._scoreboard_state.days[iso]
        assert score.engine_kwh == pytest.approx(.150 * n_channels)
        assert score.measured_kwh == pytest.approx(.140 * n_channels)
        assert score.engine_hourly_mae == 10 * n_channels
        assert drift["corrected_daily_bias"] == 10 * n_channels
    else:
        assert iso not in c._scoreboard_state.days
        assert "corrected_daily_bias" not in drift
