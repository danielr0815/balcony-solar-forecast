"""Reproduce incomplete historical hours and days through the public archive."""
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("homeassistant")

from homeassistant.util import dt as dt_util

from custom_components.balcony_solar_forecast import _services
from custom_components.balcony_solar_forecast.const import DOMAIN
from custom_components.balcony_solar_forecast.core.types import (
    InverterGroup,
    IssuedSnapshot,
    PlaneConfig,
    PlaneHourlyModeled,
    SiteConfig,
)
from tests.helpers.weather import _coord, _FakeFetcher, _om_payload

DAY = "2026-07-05"
NOON = f"{DAY}T12:00:00+00:00"
NEXT = f"{DAY}T13:00:00+00:00"
NIGHT = f"{DAY}T02:00:00+00:00"


@pytest.fixture
def clock(monkeypatch):
    now = datetime(2026, 7, 5, 1, 30, tzinfo=UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    monkeypatch.setattr(dt_util, "as_local", lambda stamp: stamp.astimezone(UTC))
    return now


def _archive_response(coord):
    hass = SimpleNamespace(data={DOMAIN: {coord.entry.entry_id: coord}})
    call = SimpleNamespace(data={"date": DAY, "entry_id": coord.entry.entry_id, "include_planes": True})
    return _services._handle_get_issued_forecast(hass, call)["result"]


async def _archive_generation(now, *, missing_noon: bool, dark=False):
    coord = _coord()
    coord._site = SiteConfig(
        latitude=0.0, longitude=0.0,
        planes=(PlaneConfig("panel", 180.0, 0.0, 400.0, efficiency=1.0, ross_coeff=0.0),),
        groups=(InverterGroup("mic", ("panel",), 1000.0, 0.95),),
    )
    payload = _om_payload(n_quarters=96, start_iso=f"{DAY}T00:15")
    light = [500.0 if not dark and 48 <= i < 56 else 0.0 for i in range(96)]
    payload["minutely_15"].update(
        shortwave_radiation=light, diffuse_radiation=light.copy(),
        direct_normal_irradiance=[0.0] * 96, temperature_2m=[25.0] * 96,
    )
    if missing_noon:
        payload["minutely_15"]["direct_normal_irradiance"][48] = None
    coord._fetcher = _FakeFetcher([payload])
    coord.data = await coord._async_update_data()
    coord.last_update_success = True
    await coord._snapshot_issued(now.date())
    return coord


async def test_archived_module_curves_omit_the_same_gap_as_site_curve(clock):
    coord = await _archive_generation(clock, missing_noon=True)
    result = _archive_response(coord)
    for key in ("hourly_wh", "raw_hourly_wh", "hourly_wh_ac"):
        assert NOON not in result[key]
        assert result[key][NIGHT] == 0
    assert result["hourly_wh"][NEXT] == pytest.approx(200)
    assert result["hourly_wh_ac"][NEXT] == pytest.approx(190)
    assert NOON not in result["cloud_class_by_hour"]
    assert NEXT in result["cloud_class_by_hour"]
    assert result["daily_coverage"] == "incomplete"
    assert result["daily_wh"] is None
    for curve in ("raw_wh", "slow_wh", "corrected_wh"):
        values = result["per_plane"]["panel"][curve]
        assert values[NEXT] == pytest.approx(200)
        assert NOON not in values, f"{curve} exposes {values.get(NOON)} Wh from three quarters"


async def test_partial_day_archive_does_not_persist_a_complete_day_total(clock):
    coord = await _archive_generation(clock, missing_noon=True)
    archived = coord._store.get_issued(DAY)
    assert archived["raw_daily_kwh"].get(DAY) is None
    assert archived["corrected_daily_kwh"].get(DAY) is None
    assert archived["weather_day_complete"] is False
    assert "per_plane_daily_wh" not in archived
    for curve in ("raw_wh", "slow_wh", "corrected_wh"):
        assert NOON not in archived["per_plane"]["panel"][curve]


def test_archive_reader_filters_explicit_legacy_weather_gap(clock):
    coord = _coord()
    partial = {NOON: 150.0, NEXT: 200.0}
    coord._store.record_issued(DAY, IssuedSnapshot(
        issued_at=clock.isoformat(), status="fresh",
        corrected_hourly_wh=partial,
        raw_hourly_wh=partial,
        corrected_ac_hourly_wh={NOON: 142.5, NEXT: 190.0},
        per_plane={"panel": PlaneHourlyModeled(raw_wh=partial, slow_wh=partial, corrected_wh=partial)},
        invalid_weather_hours=(NOON,),
    ).to_dict())
    result = _archive_response(coord)
    assert result["hourly_wh"] == {NEXT: 200.0}
    assert result["raw_hourly_wh"] == {NEXT: 200.0}
    assert result["hourly_wh_ac"] == {NEXT: 190.0}
    assert set(result["per_plane"]["panel"]["corrected_wh"]) == {NEXT}
    assert result["daily_coverage"] == "incomplete"
    assert result["daily_wh"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("missing_noon", [False, True], ids=["complete", "partial"])
async def test_weekly_card_uses_archive_coverage_not_hour_sum(clock, missing_noon):
    coord = await _archive_generation(clock, missing_noon=missing_noon)
    result = _archive_response(coord)
    script = Path(__file__).parent / "harness" / "nov19_week_archive_probe.mjs"
    work = Path(_services.__file__).resolve().parents[2]
    expected = "null" if missing_noon else "400"
    run = subprocess.run(  # noqa: S603 -- fixed script, no shell
        [shutil.which("node"), str(script), str(work), DAY, expected],
        input=json.dumps(result), capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_legacy_archive_without_day_evidence_preserves_hourly_curve(clock):
    coord = _coord()
    coord._store.record_issued(DAY, IssuedSnapshot(
        issued_at=clock.isoformat(), status="fresh",
        corrected_hourly_wh={NEXT: 200.0},
        corrected_daily_kwh={DAY: 0.2},
    ).to_dict())
    result = _archive_response(coord)
    assert result["hourly_wh"] == {NEXT: 200.0}
    assert result["daily_coverage"] == "unknown"
    assert result["daily_wh"] is None


async def test_complete_dark_archive_has_verified_zero_day_and_hour(clock):
    coord = await _archive_generation(clock, missing_noon=False, dark=True)
    result = _archive_response(coord)
    assert result["daily_coverage"] == "complete"
    assert result["daily_wh"] == 0.0
    assert result["hourly_wh"][NIGHT] == 0.0
    assert result["hourly_wh_ac"][NIGHT] == 0.0


@pytest.mark.parametrize(("zone", "expected"), [("Asia/Kolkata", 100.0), ("Asia/Kathmandu", 50.0)])
async def test_archive_preserves_utc_hours_and_local_quarter_day(monkeypatch, zone, expected):
    tz = ZoneInfo(zone)
    coord = _coord()
    coord.hass.config.time_zone = zone
    coord._site = SiteConfig(
        latitude=0.0, longitude=0.0,
        planes=(PlaneConfig("panel", 180.0, 0.0, 400.0, efficiency=1.0, ross_coeff=0.0),),
        groups=(),
    )
    start = datetime(2026, 7, 4, 18, 0, tzinfo=UTC)
    now = datetime(2026, 7, 5, 1, 30, tzinfo=tz).astimezone(UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    monkeypatch.setattr(dt_util, "as_local", lambda stamp: stamp.astimezone(tz))
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", tz)
    payload = _om_payload(n_quarters=100, start_iso=(start + timedelta(minutes=15)).isoformat())
    light = [500.0 if i >= 96 else 0.0 for i in range(100)]
    payload["minutely_15"].update(
        shortwave_radiation=light, diffuse_radiation=light.copy(),
        direct_normal_irradiance=[0.0] * 100, temperature_2m=[25.0] * 100,
    )
    coord._fetcher = _FakeFetcher([payload])
    coord.data = await coord._async_update_data()
    coord.last_update_success = True
    await coord._snapshot_issued(now.astimezone(tz).date())
    result = _archive_response(coord)
    crossing = f"{DAY}T18:00:00+00:00"
    assert result["hourly_wh"][crossing] == pytest.approx(200.0)
    assert result["per_plane"]["panel"]["corrected_wh"][crossing] == pytest.approx(200.0)
    assert result["daily_coverage"] == "complete"
    assert result["daily_wh"] == pytest.approx(expected)
    loaded = IssuedSnapshot.from_dict(coord._store.get_issued(DAY))
    assert loaded.raw_daily_kwh[DAY] == pytest.approx(expected / 1000.0)
    assert loaded.per_plane_daily_wh == {"panel": dict.fromkeys(("raw", "slow", "corrected"), expected)}
