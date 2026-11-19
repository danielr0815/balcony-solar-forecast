"""Closed-day learners require complete model and measurement evidence."""

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.balcony_solar_forecast import _actuals
from custom_components.balcony_solar_forecast.const import (
    DATA_KEY_CORRECTED_HOURLY_WH,
    DATA_KEY_RAW_HOURLY_WH,
    DROPOUT_REASON_IMPLAUSIBLE_CHANNEL,
)
from custom_components.balcony_solar_forecast.core.types import (
    IssuedSnapshot,
    WeatherSeries,
    WeatherSlot,
)
from custom_components.balcony_solar_forecast.store import ForecastStore, _empty_state
from tests.helpers.coordinator import _make_coordinator


def _patch_day_statistics(monkeypatch, read):
    from homeassistant.components import recorder
    from homeassistant.components.recorder import statistics

    class Recorder:
        async def async_add_executor_job(self, target):
            return target()

    monkeypatch.setattr(recorder, "get_instance", lambda _: Recorder())
    monkeypatch.setattr(statistics, "statistics_during_period", read)


def _five_minute_rows(start, watts):
    return [
        {"start": (start + timedelta(minutes=index * 5)).timestamp(),
         "min": watts - 5, "mean": watts, "max": watts + 5}
        for index in range(12)
    ]


@pytest.mark.parametrize("coverage", [1, 12, 13])
async def test_recorder_partial_or_duplicate_coverage_does_not_create_hour(monkeypatch, coverage):
    coord = _make_coordinator()
    at = datetime(2026, 7, 1, 12, tzinfo=UTC)
    key = at.isoformat()
    complete = _five_minute_rows(at, 120)
    second_channel = complete[:coverage] + ([complete[0]] if coverage == 13 else [])

    def read(_hass, _start, _end, _entities, period, _units, _fields):
        if period == "hour":
            # Real Recorder LTS reports AVG of the available means even if
            # only five minutes were recorded, so all scenarios look alike.
            return {name: [{"start": at.timestamp(), "mean": 120}] for name in ("sensor.m1", "sensor.m2")}
        return {"sensor.m1": complete, "sensor.m2": second_channel}

    _patch_day_statistics(monkeypatch, read)
    monkeypatch.setattr(_actuals, "_daylight_hour_keys_in_local_day", lambda *_: {key})
    daily, hourly = await coord._async_read_actuals(at.date())
    if coverage == 12:
        assert daily == {"M1": 120, "M2": 120}
        assert hourly == {"M1": {key: 120}, "M2": {key: 120}}
    else:
        assert daily == hourly == {}


@pytest.mark.parametrize("fault", ["missing_minimum", "unordered", "single_peak", "sustained"])
@pytest.mark.parametrize("edge", [0, 23])
async def test_recorder_checks_all_bucket_extrema_including_day_edges(monkeypatch, fault, edge):
    coord = _make_coordinator()
    midnight = datetime(2026, 7, 1, tzinfo=UTC)
    starts = [midnight + timedelta(hours=hour) for hour in (0, 11, 12, 23)]
    watts = [100, 120, 125, 105]
    day_keys = [start.isoformat() for start in starts]
    hours = {start.hour: _five_minute_rows(start, power) for start, power in zip(starts, watts, strict=True)}
    first_channel = [dict(row) for rows in hours.values() for row in rows]
    affected = hours[edge]
    if fault == "missing_minimum":
        affected[-1].pop("min")
    elif fault == "unordered":
        affected[-1]["min"] = 500
    elif fault == "single_peak":
        affected[-1]["max"] = 600  # M2 has 430 Wp, so its upper limit is 537.5 W.
    else:
        for row in affected:
            row.update(min=595, mean=600, max=605)

    def read(_hass, _start, _end, _entities, period, _units, fields):
        if period == "hour":
            hourly = [{"start": start.timestamp(), "mean": power}
                      for start, power in zip(starts, watts, strict=True)]
            if fault == "sustained":
                hourly[0 if edge == 0 else -1]["mean"] = 600.0
            return {"sensor.m1": hourly, "sensor.m2": hourly}
        # The mean-only adapter cannot see invalid extrema.
        return {name: [{key: value for key, value in row.items() if key == "start" or key in fields} for row in rows]
                for name, rows in (("sensor.m1", first_channel),
                                   ("sensor.m2", [row for hour in hours.values() for row in hour]))}

    _patch_day_statistics(monkeypatch, read)
    monkeypatch.setattr(_actuals, "_daylight_hour_keys_in_local_day", lambda *_: set(day_keys))
    daily, hourly = await coord._async_read_actuals(midnight.date())
    if fault == "sustained":
        assert daily == hourly == {}
        assert coord._last_actuals_dropout["reason"] == DROPOUT_REASON_IMPLAUSIBLE_CHANNEL
    else:
        expected = dict(zip(day_keys, watts, strict=True))
        assert hourly["M1"] == expected
        expected.pop((midnight + timedelta(hours=edge)).isoformat())
        assert hourly["M2"] == expected
        assert daily["M2"] == sum(expected.values())


async def test_inverted_ac_uses_signed_extrema_without_a_dc_channel_cap(monkeypatch):
    at = datetime(2026, 7, 1, 12, tzinfo=UTC)
    following = at + timedelta(hours=1)
    invalid = _five_minute_rows(following, -2000)
    invalid[0]["min"] = -1800

    def read(_hass, _start, _end, _entities, period, _units, _fields):
        if period == "hour":
            return {"sensor.export": [{"start": hour.timestamp(), "mean": -2000}
                                      for hour in (at, following)]}
        return {"sensor.export": _five_minute_rows(at, -2000) + invalid}

    _patch_day_statistics(monkeypatch, read)
    actuals = await _actuals.async_read_ac_actuals(None, "sensor.export", at.date(), UTC, invert=True)
    assert actuals == {at.isoformat(): 2000.0}


@pytest.mark.parametrize("explicit_gap", [False, True])
async def test_partial_issued_model_hour_is_neither_trainable_nor_scorable(explicit_gap, monkeypatch):
    c = _make_coordinator()
    monkeypatch.setattr(c, "_day_part_for_hourkey", lambda _: "midday")
    at = datetime(2026, 7, 1, 12, tzinfo=UTC)
    now = at.replace(hour=0)
    iso = at.date().isoformat()
    before = at - timedelta(hours=1)
    slots = [
        WeatherSlot(before + timedelta(minutes=15 * index), 600, 400, 200, 20)
        for index in range(5)
    ]
    if explicit_gap:
        slots += [WeatherSlot(at + timedelta(minutes=minute), None, 400, 200, 20)
                  for minute in (15, 30, 45)]
    result = await c._compute(WeatherSeries(tuple(slots)), now)
    c._last_result = result
    h1, h2 = before.isoformat(), at.isoformat()
    raw = {h1: 100.0, h2: 25.0}
    corrected = {h1: 100.0, h2: 30.0}
    c.data = {DATA_KEY_RAW_HOURLY_WH: raw, DATA_KEY_CORRECTED_HOURLY_WH: corrected,
              "issued_at": now.isoformat(), "status": "fresh"}
    c._store.actuals[iso] = {"M1": 100, "M2": 100}
    c._store.hourly_actuals[iso] = {"M1": {h1: 50, h2: 50}, "M2": {h1: 50, h2: 50}}
    await c._snapshot_issued(at.date())
    await c._train_and_guard(at.date())
    await c._score_scoreboard_day(at.date())

    issued = c._store.get_issued(iso)
    assert h2 in issued.get("invalid_weather_hours", ())
    assert not c._bias_state.cells
    assert sum(map(len, c._quantile_state.bins.values())) == 1
    assert c._drift_state.daily_mae[iso]["corrected"] == 0
    assert iso not in c._scoreboard_state.days


@pytest.mark.parametrize("order", [(1, 2, 3, 4, 5, 6, 7, 8), (1, 2, 3, 4, 5, 7, 8, 6)])
@pytest.mark.parametrize("good_sixth_day", [True, False])
def test_drift_joins_only_consecutive_dated_losses(monkeypatch, order, good_sixth_day):
    c = _make_coordinator()
    disabled = []
    monkeypatch.setattr(c, "_restore_layer_snapshot", lambda layer, _: disabled.append(layer))
    monkeypatch.setattr(c, "_raise_repair_issue", lambda _: None)
    for offset in order:
        day = date(2026, 7, offset)
        iso = day.isoformat()
        hour = f"{iso}T12:00:00+00:00"
        actuals = {"M1": 100, "M2": 100}
        c._store.hourly_actuals[iso] = {channel: {hour: wh} for channel, wh in actuals.items()}
        good = offset == 6 and good_sixth_day
        snap = IssuedSnapshot(
            issued_at=f"{iso}T00:00:00+00:00", status="fresh",
            raw_hourly_wh={hour: 200}, slow_only_hourly_wh={hour: 200},
            corrected_hourly_wh={hour: 200 if good else 260},
        )
        c._update_drift(iso, snap.to_dict(), actuals)
        if offset == 8 and order[-1] == 6:
            assert not disabled
    assert c._drift_state.day_ahead_disabled is (not good_sixth_day)
    assert c._drift_state.day_ahead_loss_streak == (2 if good_sixth_day else 0)
    assert disabled == ([] if good_sixth_day else ["day_ahead"])


async def test_fully_evidenced_zero_is_a_real_model_error():
    c = _make_coordinator()
    day = date(2026, 7, 1)
    iso = day.isoformat()
    target = f"{iso}T12:00:00+00:00"
    curve = {f"{iso}T{i:02d}:00:00+00:00": 0.0 for i in range(24)}
    issued = IssuedSnapshot(issued_at=f"{iso}T00:00:00+00:00", status="fresh",
                            raw_hourly_wh=curve, corrected_hourly_wh=curve).to_dict()
    issued["complete_weather_hours"] = list(curve)
    actuals = {"M1": 50, "M2": 50}
    c._store.record_issued(iso, issued)
    c._store.record_actuals(iso, actuals)
    c._store.record_hourly_actuals(iso, {
        channel: {**curve, target: value} for channel, value in actuals.items()
    })
    await c._score_scoreboard_day(day)
    c._update_drift(iso, issued, actuals)
    assert c._scoreboard_state.days[iso].engine_hourly_mae == 100.0
    assert c._drift_state.daily_mae[iso]["corrected"] == 100.0


async def test_legacy_actuals_stay_readable_without_authorizing_new_training(monkeypatch):
    c = _make_coordinator()
    day = date(2026, 7, 1)
    iso = day.isoformat()
    store = ForecastStore.__new__(ForecastStore)
    store._data = _empty_state()
    monkeypatch.setattr(store, "_schedule_save", lambda: None)
    c._store = store
    hour = f"{iso}T12:00:00+00:00"
    legacy = {"M1": 120, "M2": 120}
    store.record_actuals(iso, legacy)
    store.record_hourly_actuals(iso, {name: {hour: watts} for name, watts in legacy.items()})
    issued = IssuedSnapshot(issued_at=f"{iso}T00:00:00+00:00", status="fresh").to_dict()
    issued["complete_weather_hours"] = [hour]
    store.record_issued(iso, issued)
    calls = []

    async def unavailable(_):
        return {}, {}

    async def invoked(_):
        calls.append("consumer")

    monkeypatch.setattr(c, "_snapshot_issued", unavailable)
    monkeypatch.setattr(c, "_read_actuals_safe", unavailable)
    monkeypatch.setattr(c, "_catchup_days", lambda _: [day])
    monkeypatch.setattr(c, "_record_actuals_outcome", lambda *_, **__: None)
    for name in ("_train_and_guard", "_score_scoreboard_day", "_train_inverter_cal"):
        monkeypatch.setattr(c, name, invoked)

    await c._async_nightly_job(datetime(2026, 7, 2, 12, tzinfo=UTC))
    assert store.get_actuals(iso) == legacy
    assert store.get_hourly_actuals(iso) == {name: {hour: value} for name, value in legacy.items()}
    assert calls == []
