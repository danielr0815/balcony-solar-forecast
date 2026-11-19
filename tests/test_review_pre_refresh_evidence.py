"""Check source-independent issuance evidence before replacing verified measurements."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.balcony_solar_forecast.core.types import PlaneHourlyModeled
from custom_components.balcony_solar_forecast.store import validate_state
from tests.test_review_issue_history import _issued_after_midnight
from tests.test_review_nightly_evidence import _patch_day_statistics
from tests.test_review_score_evidence import _polar_day, _row


@pytest.mark.parametrize("remove_plane", [False, True])
async def test_unbound_verified_hours_establish_lateness_before_the_first_score(monkeypatch, remove_plane):
    c, day, start, snapshot, rows, early = _issued_after_midnight(monkeypatch)
    iso = day.isoformat()
    single_model = {hour: watts / 2 for hour, watts in snapshot.raw_hourly_wh.items()}
    snapshot = replace(snapshot, per_plane={
        p.name: PlaneHourlyModeled(raw_wh=single_model, corrected_wh=single_model)
        for p in c._site.planes
    })
    c._store.record_issued(iso, snapshot.to_dict())
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows[:-1], "sensor.m2": early[:-1]})
    daily, hourly = await c._async_read_actuals(day)
    c._store.record_verified_actuals(iso, daily, hourly, local_day_edges_complete=True)
    c._store._data = validate_state(c._store._data)
    assert c._store.actuals_bindings(iso) is None
    assert hourly["M2"][start.isoformat()] == 2
    assert iso not in c._store.get_scoreboard_state().days
    a, b = c._site.planes
    c._site = replace(c._site, planes=(a,) if remove_plane else (a, replace(b, actual_entity="sensor.new_m2")))
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.new_m2": rows})
    now = start + timedelta(days=1, hours=2)
    await c._async_nightly_job(now)
    assert c._issued_after_cutoff(snapshot, day)
    assert iso not in c._store.get_scoreboard_state().days
    assert iso not in c._store.get_drift_state().daily_mae
    c._store._data = validate_state(c._store._data)
    await c._async_nightly_job(now)
    assert c._issued_after_cutoff(snapshot, day)
    assert iso not in c._store.get_scoreboard_state().days


async def test_unbound_edge_proof_survives_incomplete_refresh_then_replacement(monkeypatch):
    c, day, start, snapshot, rows = _polar_day(monkeypatch, issue_minutes=15)
    iso = day.isoformat()
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows[:-1], "sensor.m2": rows[:-1]})
    daily, hourly = await c._async_read_actuals(day)
    c._store.record_verified_actuals(
        iso, daily, hourly, local_day_edges_complete=True,
        first_edge_production=c._last_actuals_first_edge_production,
    )
    # Only the original measuring basis has recorded pre-issue edge production.
    a, b = c._site.planes
    c._site = replace(c._site, planes=(a, replace(b, actual_entity="sensor.new_m2")))
    during_recorder_read = []

    def incomplete(*_):
        during_recorder_read.append(c._store.confirmed_late_issued(iso))
        return {"sensor.m1": rows[:1], "sensor.new_m2": rows[:1]}

    _patch_day_statistics(monkeypatch, incomplete)
    now = start + timedelta(days=1, hours=2)
    await c._async_nightly_job(now)
    assert during_recorder_read == [snapshot.issued_at]
    c._store._data = validate_state(c._store._data)
    quiet_leading = [_row(start + timedelta(minutes=index * 5), 0) if index < 6 else row
                     for index, row in enumerate(rows)]
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": quiet_leading, "sensor.new_m2": quiet_leading})
    await c._async_nightly_job(now)
    assert c._issued_after_cutoff(snapshot, day)
    assert iso not in c._store.get_scoreboard_state().days
