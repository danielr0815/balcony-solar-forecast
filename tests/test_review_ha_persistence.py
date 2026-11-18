"""Protect foreign dashboards, persisted observations and archive provenance."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_install_dashboard import (
    ServiceValidationError,
    _FakeCoordinator,
    _FakeDash,
    _FakeHass,
    _FakeLovelace,
    _patch_registry,
    _registry_for_all_keys,
)
from tests.test_install_dashboard import (
    _Call as DashboardCall,
)
from tests.test_install_dashboard import (
    svc as dashboard_svc,
)
from tests.test_services_learning import (
    _Call,
    _IssuedCoordinator,
    svc,
)
from tests.test_services_learning import (
    _FakeHass as IssuedHass,
)
from tests.test_services_wiring import _WiredHass


@pytest.mark.parametrize("foreign", [
    {"views": [{"type": "sections", "sections": [{"cards": [{"type": "markdown"}]}]}]},
    {"views": [{"badges": [{"type": "entity", "entity": "sensor.temperature"}]}]},
    {"strategy": {"type": "custom:home"}, "views": []},
    {"views": [{"strategy": {"type": "custom:room"}}]},
    {"views": [{"type": "custom:mosaic", "tiles": ["sensor.temperature"]}]},
    {"views": "invalid"},
])
async def test_dashboard_install_preserves_foreign_layouts(foreign, monkeypatch):
    dash = _FakeDash(existing=foreign)
    hass = _FakeHass(
        {"e1": _FakeCoordinator()}, lovelace=_FakeLovelace({"balcony-solar": dash}),
    )
    _patch_registry(monkeypatch, _registry_for_all_keys())
    with pytest.raises(ServiceValidationError, match="refusing to overwrite"):
        await dashboard_svc._handle_install_dashboard(hass, DashboardCall({"dashboard": "balcony-solar"}))
    assert dash.saved is None


async def test_dashboard_install_accepts_known_empty_sections(monkeypatch):
    dash = _FakeDash(existing={"title": "Solar", "views": [
        {"type": "sections", "title": "Overview", "sections": [], "badges": []},
    ]})
    hass = _FakeHass(
        {"e1": _FakeCoordinator()}, lovelace=_FakeLovelace({"balcony-solar": dash}),
    )
    _patch_registry(monkeypatch, _registry_for_all_keys())
    await dashboard_svc._handle_install_dashboard(hass, DashboardCall({"dashboard": "balcony-solar"}))
    assert dash.saved is not None


def test_dashboard_service_registers_through_ha_admin_helper(monkeypatch):
    captured = []
    hass = _WiredHass({})
    from homeassistant.helpers.service import async_register_admin_service

    def record(hass, domain, service, handler, **kwargs):
        captured.append(service)
        return async_register_admin_service(hass, domain, service, handler, **kwargs)

    monkeypatch.setattr(svc, "async_register_admin_service", record, raising=False)
    svc.async_register_services(hass)
    svc.async_register_services(hass)
    assert captured == ["install_dashboard"]


@pytest.mark.parametrize("version", [{}, {"version": 1}])
def test_legacy_archive_supplies_only_known_dc_point_curve(version):
    iso = "2026-06-21"
    stamp = iso + "T12:00:00+00:00"
    coord = _IssuedCoordinator({iso: {**version, "status": "fresh", "hourly_wh": {
        stamp: 123,
        "2026-06-22T12:00:00+00:00": 555,
    }}})
    hass = IssuedHass({"e1": coord})
    result = svc._handle_get_issued_forecast(hass, _Call({"date": iso, "include_planes": True}))["result"]
    assert result["hourly_wh"] == {stamp: 123.0}
    assert result["curve_source"] == "legacy"
    assert result["raw_hourly_wh"] == {}
    assert result["applied_factor_by_hour"] == {}
    assert result["per_plane"] == {}


def test_issued_planes_are_opt_in_day_sliced_and_have_no_fabricated_curves():
    iso = "2026-06-21"
    stamp = iso + "T12:00:00+00:00"
    other_day = "2026-06-22T12:00:00+00:00"
    coord = _IssuedCoordinator({iso: {
        "version": 2,
        "per_plane": {
            "east": {"raw_wh": {stamp: 14.1234, other_day: 20}, "corrected_wh": {stamp: 13}},
            "north": {"slow_wh": {stamp: 7}, "beam_wh": {stamp: 16}},
            "no-reading-this-day": {"slow_wh": {other_day: 10}},
        },
    }})
    hass = IssuedHass({"e1": coord})
    assert "per_plane" not in svc._handle_get_issued_forecast(hass, _Call({"date": iso}))["result"]
    result = svc._handle_get_issued_forecast(hass, _Call({"date": iso, "include_planes": True}))["result"]
    assert result.get("per_plane") == {
        "east": {"raw_wh": {stamp: 14.123}, "corrected_wh": {stamp: 13.0}},
        "north": {"slow_wh": {stamp: 7.0}},
    }
    assert svc.GET_ISSUED_FORECAST_SCHEMA({"date": iso})["include_planes"] is False
    assert svc.GET_ISSUED_FORECAST_SCHEMA({"date": iso, "include_planes": True})["include_planes"] is True


@pytest.mark.parametrize(("key", "section", "expected"), [
    ("learning_health", {"eta_oob_last_median": 10**309, "eta_oob_streak": 4},
     {"eta_oob_last_median": None, "eta_oob_streak": 4}),
    ("curve_audit", {"last_revision": {"old_kwh": 10**309, "new_kwh": 2}},
     {"last_revision": {"at": None, "date": None, "day_offset": None, "old_kwh": None,
                        "new_kwh": 2.0, "payload_refreshed": False}}),
    ("comparison_ring", {"2026-06-21": {"valid": 4, "damaged": 10**309}},
     {"2026-06-21": {"valid": 4.0}}),
    ("hourly_actuals_log", {"2026-06-21": {"A": {"valid": 4, "damaged": 10**309}}},
     {"2026-06-21": {"A": {"valid": 4.0}}}),
])
def test_invalid_store_number_keeps_valid_neighbors(key, section, expected):
    from balcony_solar_forecast.store import validate_state

    saved = {"2026-06-21": {"hourly_wh": {"2026-06-21T12:00:00+00:00": 12}}}
    result = validate_state({"schema_version": 3, "forecast_issued_log": saved, key: section})
    assert result["forecast_issued_log"] == saved
    assert {k: result[key][k] for k in expected} == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_power_history_selection_across_site_midnight():
    harness = Path(__file__).parent / "harness" / "review_history_midnight.mjs"
    proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [shutil.which("node"), str(harness)], capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_archived_exact_ac_needs_no_current_eta():
    iso = "2026-06-21"
    stamp = iso + "T12:00:00+00:00"
    coord = _IssuedCoordinator({iso: {
        "version": 2,
        "corrected_hourly_wh": {stamp: 123},
        "corrected_ac_hourly_wh": {stamp: 98},
    }})
    result = svc._handle_get_issued_forecast(IssuedHass({"e1": coord}), _Call({"date": iso}))["result"]
    assert result["hourly_wh_ac"] == {stamp: 98.0}
    assert result["ac_source"] == "snapshot"
    assert result["eta"] is None
    assert result["eta_source"] == "unavailable"
