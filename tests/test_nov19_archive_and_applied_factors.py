"""Regressions for verified legacy days and the live correction diagnostics."""
import json
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("homeassistant")

from homeassistant.util import dt as dt_util

from custom_components.balcony_solar_forecast import _services
from custom_components.balcony_solar_forecast.binary_sensor import LearnerActiveSensor
from custom_components.balcony_solar_forecast.const import DOMAIN
from custom_components.balcony_solar_forecast.core.shademap import update_bin
from custom_components.balcony_solar_forecast.core.types import (
    BiasCell,
    BiasState,
    DriftState,
    IssuedSnapshot,
    ShademapState,
)
from custom_components.balcony_solar_forecast.sensor import (
    IntradayScalarSensor,
    LearnerStatusSensor,
)
from tests.helpers.weather import NOW, _coord, _om_payload


def _legacy_day(monkeypatch, zone, iso, hour_count, *, proof=True):
    tz = ZoneInfo(zone)
    monkeypatch.setattr(dt_util, "as_local", lambda stamp: stamp.astimezone(tz))
    monkeypatch.setattr(dt_util, "DEFAULT_TIME_ZONE", tz)
    day = date.fromisoformat(iso)
    start = datetime.combine(day, datetime.min.time(), tz).astimezone(UTC)
    first_hour = (start + timedelta(minutes=59)).replace(minute=0)
    hours = [(first_hour + timedelta(hours=i)).isoformat() for i in range(hour_count)]
    curve = dict.fromkeys(hours, 0.0)
    curve[hours[9]] = 400.0
    coord = _coord()
    coord.hass.config.time_zone = zone
    coord._store.record_issued(iso, IssuedSnapshot(
        issued_at=start.isoformat(), status="fresh", corrected_hourly_wh=curve,
        complete_weather_hours=tuple(hours) if proof else None,
        # A historical stored sum alone cannot establish a complete local day.
        corrected_daily_kwh={iso: 0.4},
    ).to_dict())
    hass = SimpleNamespace(data={DOMAIN: {coord.entry.entry_id: coord}})
    call = SimpleNamespace(data={"date": iso, "entry_id": coord.entry.entry_id})
    return _services._handle_get_issued_forecast(hass, call)["result"]


@pytest.mark.parametrize(("zone", "day", "hours"), [
    ("UTC", "2026-07-05", 24),
    ("Europe/Berlin", "2026-03-29", 23),
    ("Europe/Berlin", "2026-10-25", 25),
])
def test_legacy_archive_recovers_a_proven_day(monkeypatch, zone, day, hours):
    result = _legacy_day(monkeypatch, zone, day, hours)
    assert len(result["hourly_wh"]) == hours
    assert result["daily_coverage"] == "complete"
    assert result["daily_wh"] == 400.0


@pytest.mark.parametrize(("zone", "hours", "proof"), [
    ("UTC", 23, True),
    ("UTC", 24, False),
    ("Asia/Kolkata", 24, True),
    ("Asia/Kathmandu", 24, True),
])
def test_legacy_archive_requires_proof_for_exact_local_day(monkeypatch, zone, hours, proof):
    result = _legacy_day(monkeypatch, zone, "2026-07-05", hours, proof=proof)
    assert result["daily_coverage"] == "unknown"
    assert result["daily_wh"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_weekly_card_renders_recovered_legacy_day(monkeypatch):
    day = "2026-07-05"
    response = _legacy_day(monkeypatch, "UTC", day, 24)
    tests_dir = Path(__file__).resolve().parent
    harness = tests_dir / "harness" / "nov19_week_archive_probe.mjs"
    run = subprocess.run(  # noqa: S603 -- fixed script, no shell
        [shutil.which("node"), str(harness), str(tests_dir.parent), day, "400"],
        input=json.dumps(response), capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def _warm(monkeypatch, age):
    coord = _coord()
    coord._weather.restore({
        "payload": _om_payload(start_iso="2026-07-05T12:15"),
        "fetched_at": (NOW - timedelta(hours=age)).isoformat(),
    })
    coord._last_attempt_at = NOW
    coord._bias_state = BiasState(cells={"clear|midday": BiasCell(theta=1.3, covariance=1.0, n=99)})
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    return coord


@pytest.mark.parametrize("gate", ["enabled", "off", "drift", "legacy_drift", "collapse", "fallback"])
async def test_bias_diagnostics_use_serving_gate(monkeypatch, gate):
    coord = _warm(monkeypatch, 30 if gate == "fallback" else 1)
    if gate == "off":
        coord._learner_config = replace(coord._learner_config, day_ahead_enabled=False)
    elif gate == "drift":
        coord._drift_state = DriftState(day_ahead_disabled=True)
    elif gate == "legacy_drift":
        coord._drift_state = DriftState(fast_disabled=True)
    elif gate == "collapse":
        coord._drift_state = DriftState(collapse_frozen_date=dt_util.as_local(NOW).date().isoformat())
    result = await coord._async_update_data()
    cell = result["bias_cells"]["clear|midday"]
    assert cell["theta"] == 1.3
    assert cell["n"] == 99
    assert cell["applied"] == (1.3 if gate == "enabled" else 1.0)


async def test_shade_profiles_use_static_transmission_during_fallback_and_recover(monkeypatch):
    coord = _warm(monkeypatch, 1)
    plane = coord._site.planes[0]
    day = NOW.date()
    coord._shade_profile_module = plane.name
    coord._shade_profile_date = day
    static = coord.build_shade_profile()
    i = static["sun_elevation"].index(max(static["sun_elevation"]))
    for _ in range(200):
        coord._shademap_state = update_bin(
            coord._shademap_state, channel=plane.name, sun_az=static["azimuth"][i],
            sun_el=static["sun_elevation"][i], doy=static["doy"], measured_t=0.0,
        )

    coord.data = await coord._async_update_data()
    learned = coord.build_shade_profile()
    assert learned["transmittance"][i] < static["transmittance"][i]
    assert coord.data["status"] == "cached"
    coord._last_fetched_at = NOW - timedelta(hours=30)
    coord.data = await coord._async_update_data()
    assert coord.data["status"] == "physics_fallback"
    assert coord.build_shade_profile()["transmittance"] == static["transmittance"]
    assert coord.build_shade_profile_for(plane.name, day)["transmittance"] == static["transmittance"]

    coord._last_fetched_at = NOW
    coord._last_fetch_ok = True
    coord.data = await coord._async_update_data()
    assert coord.data["status"] == "fresh"
    assert coord.build_shade_profile()["transmittance"] == learned["transmittance"]
    stored_shademap = coord._shademap_state
    coord._shademap_state = ShademapState()
    # New learner state must not relabel the generation already shown.
    assert coord.build_shade_profile()["transmittance"] == learned["transmittance"]
    coord._shademap_state = stored_shademap
    coord.last_update_success = False
    assert coord.build_shade_profile()["transmittance"] == static["transmittance"]
    assert coord.get_shademap_state().channels


def test_failed_refresh_hides_applied_factors_but_keeps_learned_cells():
    coord = _coord()
    cell = {"theta": 1.3, "n": 99, "applied": 1.3}
    coord.data = {
        "intraday_scalar": 1.3, "learner_status": {"day_ahead": "active"},
        "bias_cells": {"clear|midday": cell},
    }
    coord.last_update_success = False
    status = LearnerStatusSensor(coord, "learner_status_day_ahead", "day_ahead")
    active = LearnerActiveSensor(coord, "learner_active_day_ahead", "day_ahead")
    assert status.native_value is None
    assert active.is_on is None
    assert IntradayScalarSensor(coord).native_value is None
    assert status.extra_state_attributes["bias_cells"]["clear|midday"] == {**cell, "applied": None}
    assert cell["applied"] == 1.3


async def test_bias_diagnostics_keep_the_state_bound_before_executor_yield(monkeypatch):
    coord = _warm(monkeypatch, 1)
    previous_executor = coord.hass.async_add_executor_job

    async def learn_during_compute(job, *args):
        coord._bias_state = BiasState(cells={
            "clear|midday": BiasCell(theta=0.8, covariance=1.0, n=100),
        })
        return await previous_executor(job, *args)

    coord.hass.async_add_executor_job = learn_during_compute
    data = await coord._async_update_data()
    cell = data["bias_cells"]["clear|midday"]
    assert (cell["theta"], cell["applied"], cell["n"]) == (1.3, 1.3, 99)
    assert coord._bias_state.cells["clear|midday"].theta == 0.8
