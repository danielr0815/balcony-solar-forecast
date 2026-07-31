"""End-to-end tests for ``diagnostics.async_get_config_entry_diagnostics``.

The download is the operator-facing forensics surface (SPEC §14.6): it must
dump the entry, the degradation state, a compact forecast summary and the
learner/scoreboard/quantile/ensemble blocks — while provably redacting the
operator's coordinates BOTH at the entry top level and inside the nested
``site`` object (a bug report must never pin the install's location). The
optional coordinator accessors (store stats, learner/quantile/ensemble state)
must never crash the dump: absent or raising accessors degrade to
``{"available": False}`` / ``{"error": ...}`` blocks.

The companion tests/test_diagnostics_learning.py covers ``_learner_summary``
in isolation; here the whole entry point is driven against fakes.

Needs Home Assistant (``async_redact_data``); skipped on the plain-core path.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.diagnostics import REDACTED  # noqa: E402

from custom_components.balcony_solar_forecast import (  # noqa: E402
    diagnostics as diag_mod,
)
from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_SITE,
    DATA_KEY_CORRECTION_SOURCE,
    DATA_KEY_DRIFT_MAE,
    DATA_KEY_INTRADAY_SCALAR,
    DATA_KEY_LEARNER_STATUS,
    DATA_KEY_SCOREBOARD,
    DOMAIN,
)

_ENTRY_ID = "entry1"


class _FakeEntry:
    entry_id = _ENTRY_ID
    title = "Balkon"

    def __init__(self, data, options) -> None:
        self.data = data
        self.options = options


class _FakeHass:
    def __init__(self, coordinator=None) -> None:
        self.data = {DOMAIN: {_ENTRY_ID: coordinator}} if coordinator else {}


def _site() -> dict:
    return {
        CONF_LATITUDE: 51.1,
        CONF_LONGITUDE: 10.4,
        "planes": [{"name": "M1", "azimuth_deg": 115.0}],
    }


def _coordinator_data() -> dict:
    return {
        "status": "fresh",
        "degraded": False,
        "weather_age_seconds": 900.0,
        "last_error": None,
        "computed_at": "2026-07-30T10:00:00+00:00",
        "slot_starts": ["2026-07-30T10:00:00+00:00", "2026-07-30T10:15:00+00:00"],
        "plane_watts": {"M1": [100.0, 120.0]},
        "daily_kwh": {"2026-07-30": 1.5},
        "daily_kwh_ac": {"2026-07-30": 1.35},
        "hourly_wh": {"2026-07-30T10:00": 100.0},
        DATA_KEY_LEARNER_STATUS: {"fast": "active"},
        DATA_KEY_INTRADAY_SCALAR: 1.05,
        DATA_KEY_DRIFT_MAE: {"raw": 200.0, "corrected": 150.0},
        DATA_KEY_CORRECTION_SOURCE: "intraday",
        DATA_KEY_SCOREBOARD: {
            "engine_daily_kwh_mae": 0.31,
            "window_days": 14,
            "scored_days": 9,
            "strata": {"clear": {"days": 4}},
        },
    }


class _FullCoordinator:
    """Coordinator double exposing every optional diagnostics accessor."""

    last_update_success = True

    def __init__(self, data) -> None:
        self.data = data

    def learner_state_summary(self):
        return {"bias_cells": 5, "shademap_channels": 2}

    def store_stats(self):
        return {"issued_snapshots": 12, "learning_health": {"streak": 0}}

    def quantile_state_summary(self):
        return {"bins": {"clear|midday": 7}}

    def ensemble_state_summary(self):
        return {"enabled": False}


async def test_diagnostics_redacts_coordinates_top_level_and_nested():
    """SPEC §14.6: entry lat/lon AND the site-nested copies are redacted in
    BOTH data and options; non-coordinate fields stay visible."""
    entry = _FakeEntry(
        data={CONF_LATITUDE: 51.1, CONF_LONGITUDE: 10.4, CONF_SITE: _site()},
        options={CONF_SITE: _site()},
    )
    hass = _FakeHass(_FullCoordinator(_coordinator_data()))

    out = await diag_mod.async_get_config_entry_diagnostics(hass, entry)

    assert out["entry"]["title"] == "Balkon"
    for bucket in ("data", "options"):
        site = out["entry"][bucket][CONF_SITE]
        assert site[CONF_LATITUDE] == REDACTED
        assert site[CONF_LONGITUDE] == REDACTED
        # Geometry the operator wants visible is NOT redacted.
        assert site["planes"] == [{"name": "M1", "azimuth_deg": 115.0}]
    assert out["entry"]["data"][CONF_LATITUDE] == REDACTED
    assert out["entry"]["data"][CONF_LONGITUDE] == REDACTED
    # No raw coordinate survives anywhere in the dump.
    assert "51.1" not in repr(out)
    assert "10.4" not in repr(out)


async def test_diagnostics_without_coordinator_reports_unavailable():
    entry = _FakeEntry(data={CONF_SITE: _site()}, options={})
    out = await diag_mod.async_get_config_entry_diagnostics(_FakeHass(), entry)
    assert out["state"] == {"available": False, "reason": "coordinator_missing"}
    # Nothing coordinator-derived is fabricated.
    assert "forecast" not in out
    assert "learners" not in out


async def test_diagnostics_state_forecast_and_scoreboard_blocks():
    out = await diag_mod.async_get_config_entry_diagnostics(
        _FakeHass(_FullCoordinator(_coordinator_data())),
        _FakeEntry(data={}, options={}),
    )
    state = out["state"]
    assert state["last_update_success"] is True
    assert state["source_status"] == "fresh"
    assert state["weather_age_seconds"] == 900.0
    # The age is also served in minutes for the bug-report reader.
    assert state["last_fetch_age_min"] == pytest.approx(15.0)

    forecast = out["forecast"]
    assert forecast["slot_count"] == 2
    assert forecast["first_slot"] == "2026-07-30T10:00:00+00:00"
    assert forecast["last_slot"] == "2026-07-30T10:15:00+00:00"
    assert forecast["plane_names"] == ["M1"]
    # DC and AC roll-ups stay distinct (the v0.20.7 split).
    assert forecast["daily_kwh_dc"] == {"2026-07-30": 1.5}
    assert forecast["daily_kwh_ac"] == {"2026-07-30": 1.35}
    assert forecast["hourly_count"] == 1

    learners = out["learners"]
    assert learners["status"] == {"fast": "active"}
    assert learners["intraday_scalar"] == pytest.approx(1.05)
    assert learners["state"] == {"bias_cells": 5, "shademap_channels": 2}

    scoreboard = out["scoreboard"]
    assert scoreboard["engine_daily_kwh_mae"] == pytest.approx(0.31)
    assert scoreboard["strata"] == {"clear": {"days": 4}}

    assert out["store"] == {"issued_snapshots": 12, "learning_health": {"streak": 0}}
    assert out["quantiles"] == {"bins": {"clear|midday": 7}}
    assert out["ensemble"] == {"enabled": False}


async def test_diagnostics_optional_accessors_degrade_safely():
    """Absent / raising / non-dict accessors must never crash the dump."""

    class _NoAccessors:
        data = {}
        last_update_success = False

    class _RaisingAccessors:
        data = {"weather_age_seconds": None}
        last_update_success = False

        def learner_state_summary(self):
            raise RuntimeError("boom")

        def store_stats(self):
            raise RuntimeError("boom")

        def quantile_state_summary(self):
            raise RuntimeError("boom")

        def ensemble_state_summary(self):
            return "not-a-dict"

    entry = _FakeEntry(data={}, options={})

    out = await diag_mod.async_get_config_entry_diagnostics(
        _FakeHass(_NoAccessors()), entry
    )
    assert out["learners"]["state"] == {"available": False}
    assert out["store"] == {"available": False}
    assert out["quantiles"] == {"available": False}
    assert out["ensemble"] == {"available": False}
    # Empty data: no forecast block content, no scoreboard, None age stays None.
    assert out["forecast"] is None
    assert out["scoreboard"] == {"available": False}
    assert out["state"]["last_fetch_age_min"] is None

    out = await diag_mod.async_get_config_entry_diagnostics(
        _FakeHass(_RaisingAccessors()), entry
    )
    assert "error" in out["learners"]["state"]
    assert "error" in out["store"]
    assert "error" in out["quantiles"]
    assert out["ensemble"] == {"available": False}
