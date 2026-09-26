"""A later recorded day must not hide an earlier catch-up gap."""

from datetime import UTC, date, datetime

from tests.helpers.coordinator import _FakeStore, _make_coordinator


async def test_nightly_retries_older_gap_after_newer_actuals_were_recorded(monkeypatch):
    store = _FakeStore()
    store.record_actuals("2026-03-11", {"M1": 1.0})
    coord = _make_coordinator(store)
    reads, trained = [], []

    async def snapshot(_):
        pass

    async def read(day):
        reads.append(day)
        return {"M1": 1.0}, {}

    async def train(day):
        trained.append(day)

    monkeypatch.setattr(coord, "_snapshot_issued", snapshot)
    monkeypatch.setattr(coord, "_read_actuals_safe", read)
    monkeypatch.setattr(coord, "_train_and_guard", train)
    monkeypatch.setattr(coord, "_score_scoreboard_day", snapshot)
    monkeypatch.setattr(coord, "_train_inverter_cal", snapshot)
    await coord._async_nightly_job(datetime(2026, 3, 12, 12, tzinfo=UTC))

    assert date(2026, 3, 10) in reads
    assert date(2026, 3, 10) in trained
    assert date(2026, 3, 11) not in reads
    assert trained == sorted(trained)


async def test_inverter_calibration_folds_each_closed_day_only_once():
    from custom_components.balcony_solar_forecast.core.types import (
        PlaneConfig,
        SiteConfig,
    )

    coord = _make_coordinator()
    coord._site = SiteConfig(latitude=50, longitude=10,
        planes=(PlaneConfig(name="M1", azimuth_deg=180, tilt_deg=30, wp=1000,
                            actual_entity="sensor.m1"),),
        groups=(), ac_actual_entity="sensor.ac")
    day = date(2026, 7, 5)
    hour = "2026-07-05T12:00:00+00:00"
    coord._store.record_hourly_actuals(day.isoformat(), {"M1": {hour: 400.0}})

    async def read(_):
        return {hour: 384.0}

    coord._async_read_ac_actuals = read
    await coord._train_inverter_cal(day)
    once = coord._inverter_cal_state
    assert once.n == 1
    await coord._train_inverter_cal(day)
    assert coord._inverter_cal_state == once


async def test_inverter_day_marker_survives_store_reload():
    from custom_components.balcony_solar_forecast.store import ForecastStore
    from tests.helpers.store import FakeStore

    backend = FakeStore()
    store = ForecastStore(None, "entry", store=backend)
    store.mark_inverter_day_trained("2026-07-05")
    await store.async_flush()
    reloaded = ForecastStore(None, "entry", store=FakeStore(backend.saved))
    await reloaded.async_load()
    assert reloaded.is_inverter_day_trained("2026-07-05")
    assert not reloaded.is_inverter_day_trained("2026-07-04")
