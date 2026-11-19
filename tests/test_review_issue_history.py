"""Proof of late issuance survives meter changes and preserves genuine old scores."""

from dataclasses import replace
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.balcony_solar_forecast.core import solpos
from custom_components.balcony_solar_forecast.core.types import (
    IssuedSnapshot,
    PlaneHourlyModeled,
)
from custom_components.balcony_solar_forecast.store import validate_state
from tests.helpers.weather import FETCH_INTERVAL, _FakeFetcher, _om_payload
from tests.test_review_nightly_evidence import _patch_day_statistics
from tests.test_review_score_evidence import _polar_day, _recorded_coordinator, _row


def _issued_after_midnight(monkeypatch):
    c, day, start = _recorded_coordinator(monkeypatch, ZoneInfo("UTC"), latitude=62, longitude=0)
    curve = {(start + timedelta(hours=h)).isoformat(): 200.0 if h == 12 else 0.0 for h in range(24)}
    snapshot = IssuedSnapshot(
        issued_at=(start + timedelta(hours=1, minutes=30)).isoformat(),
        status="fresh", raw_hourly_wh=curve, corrected_hourly_wh=curve,
        complete_weather_hours=tuple(curve),
        per_plane={p.name: PlaneHourlyModeled() for p in c._site.planes},
    )
    c._store.record_issued(day.isoformat(), snapshot.to_dict())
    rows = [_row(start + timedelta(minutes=i * 5), 100 if i // 12 == 12 else 0) for i in range(288)]
    early = [_row(start + timedelta(minutes=i * 5), 2) if i < 12 else row for i, row in enumerate(rows)]
    return c, day, start, snapshot, rows, early


@pytest.mark.parametrize("already_withdrawn", [True, False])
async def test_confirmed_late_issue_survives_meter_replacement_and_store_reload(monkeypatch, already_withdrawn):
    c, day, start, snapshot, rows, early = _issued_after_midnight(monkeypatch)
    now = start + timedelta(days=1, hours=2)
    iso = day.isoformat()
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.m2": early[:11] + early[12:]})
    await c._async_nightly_job(now)
    assert c._store.get_scoreboard_state().days[iso].measured_kwh == .2
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.m2": early})
    if already_withdrawn:
        await c._async_nightly_job(now)
        assert iso not in c._store.get_scoreboard_state().days
    else:
        daily, hourly = await c._async_read_actuals(day)
        c._store.record_verified_actuals(iso, daily, hourly, local_day_edges_complete=True)
    c._store._data = validate_state(c._store._data)
    a, b = c._site.planes
    c._site = replace(c._site, planes=(a, replace(b, actual_entity="sensor.replacement_m2")))
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.replacement_m2": rows[12:]})
    await c._async_nightly_job(now)
    assert c._issued_after_cutoff(snapshot, day)
    assert iso not in c._store.get_scoreboard_state().days
    assert iso not in c._store.get_drift_state().daily_mae


async def test_historical_score_without_entity_provenance_is_retained(monkeypatch):
    c, day, start, _, rows, early = _issued_after_midnight(monkeypatch)
    now = start + timedelta(days=1, hours=2)
    iso = day.isoformat()
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows[:-1], "sensor.m2": rows[:-1]})
    await c._async_nightly_job(now)
    score = c._store.get_scoreboard_state().days[iso]
    drift = c._store.get_drift_state().daily_mae[iso]
    # A prior version recorded verified hours with no Entity attribution.
    legacy = dict(c._store._data)
    legacy.pop("verified_actuals_context")
    c._store._data = validate_state(legacy)
    a, b = c._site.planes
    c._site = replace(c._site, planes=(a, replace(b, actual_entity="sensor.replacement_m2")))
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.replacement_m2": early})
    await c._async_nightly_job(now)
    assert c._store.get_scoreboard_state().days.get(iso) == score
    assert c._store.get_drift_state().daily_mae.get(iso) == drift


@pytest.mark.parametrize(
    "case", ["uncertain", "dark", "at_midnight", "proved_zero", "complete_edge",
             "legacy_complete", "legacy_partial", "legacy_unknown", "outside_edge", "off_grid"],
)
async def test_issued_leading_edge_requires_evidence_before_first_score(monkeypatch, case):
    c, day, start, snapshot, rows = _polar_day(monkeypatch, issue_minutes=0 if case == "at_midnight" else 15)
    iso = day.isoformat()
    legacy = snapshot.to_dict()
    legacy.pop("leading_edge_forecast_known", None)
    if case == "dark":
        c._site = replace(c._site, latitude=22.5, longitude=88.4)
    elif case == "proved_zero":
        # Exact total equals the 23 in-day UTC hours on both DC curves. The
        # remaining greatest possible energy in the leading fragment is zero.
        legacy["raw_daily_kwh"] = {iso: 23 * 220 / 1000}
        legacy["corrected_daily_kwh"] = {iso: 23 * 220 / 1000}
    elif case in {"complete_edge", "legacy_complete", "legacy_partial", "legacy_unknown", "outside_edge", "off_grid"}:
        legacy["first_local_edge_production"] = (start + timedelta(minutes=15)).isoformat()
        if case == "complete_edge":
            legacy["leading_edge_forecast_known"] = True
        elif case == "legacy_partial":
            legacy["weather_day_complete"] = False
        elif case == "legacy_unknown":
            legacy.pop("weather_day_complete")
        elif case == "outside_edge":
            legacy["first_local_edge_production"] = (start + timedelta(hours=23, minutes=45)).isoformat()
        elif case == "off_grid":
            legacy["first_local_edge_production"] = (start + timedelta(minutes=16)).isoformat()
    c._store.record_issued(iso, legacy)
    actuals = [_row(start + timedelta(minutes=i * 5), 0) if i < 6 else row for i, row in enumerate(rows)]
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": actuals, "sensor.m2": actuals})
    await c._async_nightly_job(start + timedelta(days=1, hours=2))
    blocked = case not in {"at_midnight", "proved_zero", "complete_edge", "legacy_complete"}
    assert (iso not in c._store.get_scoreboard_state().days) is blocked
    assert c._issued_after_cutoff(IssuedSnapshot.from_dict(legacy), day) is blocked
    assert c._store.confirmed_late_issued(iso) is None


@pytest.mark.parametrize("first_quarter_zero", [False, True])
async def test_diffuse_twilight_first_score_uses_legacy_coverage_and_time_proof(monkeypatch, first_quarter_zero):
    c, day, start = _recorded_coordinator(monkeypatch, ZoneInfo("Asia/Kolkata"), latitude=60, longitude=82.5)
    iso = day.isoformat()
    c._fetch_interval = FETCH_INTERVAL
    c._store._last_payload_save_at = None
    payload = _om_payload(n_quarters=100, start_iso=(start - timedelta(minutes=15)).isoformat())
    weather = payload["minutely_15"]
    following = start + timedelta(minutes=15)
    for name, value in [
        ("shortwave_radiation", 2.0), ("diffuse_radiation", 2.0), ("direct_normal_irradiance", 0.0),
    ]:
        weather[name][:6] = [value] * 6
        if first_quarter_zero:
            for index, end in enumerate(weather["time"]):
                if end <= following.strftime("%Y-%m-%dT%H:%M"):
                    weather[name][index] = 0.0
    c._fetcher = _FakeFetcher([payload])
    issue = start + timedelta(minutes=10 if first_quarter_zero else 15)
    monkeypatch.setattr(dt_util, "utcnow", lambda: issue)
    c.data = await c._async_update_data()
    c.last_update_success = True
    # The core has actual output in the leading quarter even though the
    # midpoint sun is below -6 degrees and all irradiation is diffuse.
    assert solpos.sun_position(start + timedelta(minutes=7, seconds=30), 60, 82.5)[1] == pytest.approx(-6.5, abs=0.02)
    assert c.data["wh_period"][start.isoformat()] == (0.0 if first_quarter_zero else pytest.approx(.29, abs=.01))
    assert c.data["wh_period"][following.isoformat()] == pytest.approx(.29, abs=.01)
    await c._snapshot_issued(day)
    issued = c._store.get_issued(iso)
    assert issued["weather_day_complete"] is True
    assert issued["first_local_edge_production"] == (following if first_quarter_zero else start).isoformat()
    assert c._issued_after_cutoff(IssuedSnapshot.from_dict(issued), day) is not first_quarter_zero

    legacy = dict(issued)
    legacy.pop("leading_edge_forecast_known")
    if not first_quarter_zero:
        # The oldest archive format did not record the first positive slot.
        legacy.pop("first_local_edge_production")
    c._store.record_issued(iso, legacy)
    rows = [_row(start + timedelta(minutes=5 * i), 0 if i < 6 else 120 + (i // 12) % 5)
            for i in range(294)]
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.m2": rows})
    c.data = None
    await c._async_nightly_job(start + timedelta(days=1, hours=2))
    assert c._store.has_verified_actuals(iso)
    score = c._store.get_scoreboard_state().days.get(iso)
    assert (score is not None) is first_quarter_zero
    assert c._issued_after_cutoff(IssuedSnapshot.from_dict(legacy), day) is not first_quarter_zero
    if score is not None:
        assert score.engine_kwh == pytest.approx(5.231, abs=.001)
        assert score.measured_kwh == pytest.approx(5.732)
    assert c._store.confirmed_late_issued(iso) is None
