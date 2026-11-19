"""Issued forecasts and Recorder labels cover the same DC modules."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.balcony_solar_forecast import _nightly
from custom_components.balcony_solar_forecast.core.types import PlaneHourlyModeled
from tests.test_review_nightly_evidence import _patch_day_statistics
from tests.test_review_score_evidence import _polar_day


@pytest.mark.asyncio
async def test_removed_module_before_first_score_uses_issued_module_energy(monkeypatch):
    coord, day, start, _, rows = _polar_day(monkeypatch)
    iso = day.isoformat()
    now = start + timedelta(days=1, hours=2)
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows, "sensor.m2": rows[:1] + rows[6:]})
    await coord._async_nightly_job(now)
    assert iso not in coord._store.get_scoreboard_state().days

    coord._site = replace(coord._site, planes=coord._site.planes[:1])
    _patch_day_statistics(monkeypatch, lambda *_: {"sensor.m1": rows[:288]})
    await coord._async_nightly_job(now)
    score = coord._store.get_scoreboard_state().days[iso]
    assert score.measured_kwh == pytest.approx(2.484)
    assert score.engine_kwh == pytest.approx(2.64)
    assert score.engine_hourly_mae == pytest.approx(6.5)
    assert coord._drift_state.daily_mae[iso]["corrected_daily_bias"] == pytest.approx(156)


@pytest.mark.parametrize("keep_unmetered", [False, True])
@pytest.mark.parametrize("evidence", ["present", "missing", "legacy_missing"])
def test_archive_module_values_are_required_for_a_metered_subset(monkeypatch, keep_unmetered, evidence):
    coord, day, _, snapshot, _ = _polar_day(monkeypatch)
    a, b = coord._site.planes
    coord._site = replace(coord._site, planes=(a, replace(b, actual_entity=None)) if keep_unmetered else (a,))
    if evidence != "present":
        snapshot = replace(
            snapshot,
            per_plane={a.name: PlaneHourlyModeled(), b.name: snapshot.per_plane[b.name]},
            per_plane_daily_wh={b.name: snapshot.per_plane_daily_wh[b.name]},
        )
    if evidence == "legacy_missing":
        snapshot = replace(snapshot, complete_weather_hours=None, weather_day_complete=None)

    hours = _nightly.metered_modeled_hourly(coord, snapshot, snapshot.corrected_hourly_wh, layer="corrected")
    daily = _nightly.metered_local_day_wh(coord, snapshot.to_dict(), snapshot, day.isoformat(), layer="corrected")
    if evidence != "present":
        assert hours is None
        assert daily is None
    else:
        assert hours == dict.fromkeys(snapshot.corrected_hourly_wh, 110.0)
        assert daily == 2640


def test_issued_day_without_any_module_metadata_cannot_prove_current_meter_basis(monkeypatch):
    coord, day, _, snapshot, _ = _polar_day(monkeypatch)
    coord._site = replace(coord._site, planes=coord._site.planes[:1])
    snapshot = replace(snapshot, per_plane={}, per_plane_daily_wh=None)

    assert _nightly.metered_modeled_hourly(coord, snapshot, snapshot.corrected_hourly_wh, layer="corrected") is None
    assert _nightly.metered_local_day_wh(coord, snapshot.to_dict(), snapshot, day.isoformat(), layer="corrected") is None


def test_legacy_hourly_site_curve_without_module_metadata_remains_available(monkeypatch):
    coord, day, _, snapshot, _ = _polar_day(monkeypatch)
    snapshot = replace(snapshot, per_plane={}, per_plane_daily_wh=None, weather_day_complete=None)

    hours = _nightly.metered_modeled_hourly(coord, snapshot, snapshot.corrected_hourly_wh, layer="corrected")
    assert hours == dict.fromkeys(snapshot.corrected_hourly_wh, 220.0)
    assert _nightly.metered_local_day_wh(coord, snapshot.to_dict(), snapshot, day.isoformat(), layer="corrected") is None


def test_subset_hour_with_missing_positive_reference_stays_unknown(monkeypatch):
    coord, _, _, snapshot, _ = _polar_day(monkeypatch)
    a, b = coord._site.planes
    coord._site = replace(coord._site, planes=(a, replace(b, actual_entity=None)))
    known, missing, night = tuple(snapshot.corrected_hourly_wh)[:3]
    site = {known: 220.0, missing: 220.0, night: 0.0}
    snapshot = replace(snapshot, per_plane={
        a.name: PlaneHourlyModeled(corrected_wh={known: 110.0}),
        b.name: snapshot.per_plane[b.name],
    })

    assert _nightly.metered_modeled_hourly(coord, snapshot, site, layer="corrected") == {known: 110.0, night: 0.0}
