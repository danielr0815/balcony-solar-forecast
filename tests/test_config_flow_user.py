"""Tests for the initial ``async_step_user`` submit path.

The onboarding front door was previously untested. The load-bearing invariant
is the lat/lon-into-site merge (config_flow.py: "the coordinator reads the
site-embedded coordinates only ... otherwise they are stored but silently
ignored and every off-reference user forecasts for the shipped Landshut
default") — a refactor dropping that merge would produce plausible-looking but
geographically wrong forecasts for every new user while all tests stay green.

Also covered: the blank-name error, the SiteValidationError-to-error-code
mapping, and the duplicate-name unique-id abort. Uses the same
``Flow.__new__`` + monkeypatched ``async_create_entry``/``async_show_form``
pattern as the options-flow tests (no HA flow manager needed).
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from balcony_solar_forecast.config_flow import (  # noqa: E402
    BalconySolarForecastConfigFlow,
)
from balcony_solar_forecast.const import (  # noqa: E402
    CONF_FETCH_INTERVAL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SITE,
    DEFAULT_SITE,
    FETCH_INTERVAL_SECONDS,
    RECOMPUTE_INTERVAL_SECONDS,
)
from homeassistant.data_entry_flow import AbortFlow  # noqa: E402

LAT = 50.9375   # deliberately NOT the shipped reference-site coordinates
LON = 6.9603


def _user_input(**overrides) -> dict:
    base = {
        CONF_NAME: "My Balcony",
        CONF_LATITUDE: LAT,
        CONF_LONGITUDE: LON,
        CONF_FETCH_INTERVAL: FETCH_INTERVAL_SECONDS,
        CONF_RECOMPUTE_INTERVAL: RECOMPUTE_INTERVAL_SECONDS,
        CONF_SITE: copy.deepcopy(DEFAULT_SITE),
    }
    base.update(overrides)
    return base


def _flow(monkeypatch, *, abort_duplicate: bool = False):
    """A user-step flow with the HA plumbing faked out.

    Returns (flow, captured): ``captured['entry']`` holds the
    ``async_create_entry`` kwargs, ``captured['form']`` the
    ``async_show_form`` kwargs, ``captured['unique_id']`` the requested
    unique id.
    """
    flow = BalconySolarForecastConfigFlow.__new__(BalconySolarForecastConfigFlow)
    flow.hass = SimpleNamespace(
        config=SimpleNamespace(latitude=48.0, longitude=11.0)
    )
    captured: dict = {}

    async def _set_unique_id(uid):
        captured["unique_id"] = uid

    def _abort_if_configured():
        if abort_duplicate:
            raise AbortFlow("already_configured")

    def _create_entry(*, title, data):
        captured["entry"] = {"title": title, "data": data}
        return {"type": "create_entry", "title": title, "data": data}

    def _show_form(*, step_id, data_schema, errors=None):
        captured["form"] = {"step_id": step_id, "errors": dict(errors or {})}
        return {"type": "form", "step_id": step_id, "errors": errors}

    monkeypatch.setattr(flow, "async_set_unique_id", _set_unique_id)
    monkeypatch.setattr(
        flow, "_abort_if_unique_id_configured", _abort_if_configured
    )
    monkeypatch.setattr(flow, "async_create_entry", _create_entry)
    monkeypatch.setattr(flow, "async_show_form", _show_form)
    return flow, captured


# ---------------------------------------------------------------------------
# Happy path: the lat/lon-into-site merge invariant
# ---------------------------------------------------------------------------


async def test_user_step_merges_coordinates_into_site(monkeypatch):
    flow, captured = _flow(monkeypatch)

    result = await flow.async_step_user(_user_input())

    assert result["type"] == "create_entry"
    data = captured["entry"]["data"]
    # Top-level coordinates stored...
    assert data[CONF_LATITUDE] == pytest.approx(LAT)
    assert data[CONF_LONGITUDE] == pytest.approx(LON)
    # ...AND merged into the site dict — the ONLY coordinates the coordinator
    # reads. Dropping this merge would silently forecast for the shipped
    # reference site for every new user.
    assert data[CONF_SITE][CONF_LATITUDE] == pytest.approx(LAT)
    assert data[CONF_SITE][CONF_LONGITUDE] == pytest.approx(LON)
    # The site round-tripped through validate_site (planes present, canonical).
    assert data[CONF_SITE]["planes"], "site must round-trip with its planes"
    assert captured["entry"]["title"] == "My Balcony"
    assert data[CONF_FETCH_INTERVAL] == FETCH_INTERVAL_SECONDS
    assert data[CONF_RECOMPUTE_INTERVAL] == RECOMPUTE_INTERVAL_SECONDS


async def test_user_step_unique_id_is_casefolded_name(monkeypatch):
    flow, captured = _flow(monkeypatch)
    await flow.async_step_user(_user_input(**{CONF_NAME: "My BALCONY"}))
    assert captured["unique_id"] == "my balcony"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_user_step_blank_name_rerenders_with_error(monkeypatch):
    flow, captured = _flow(monkeypatch)

    result = await flow.async_step_user(_user_input(**{CONF_NAME: "   "}))

    assert result["type"] == "form"
    assert captured["form"]["errors"] == {CONF_NAME: "name_required"}
    assert "entry" not in captured


async def test_user_step_invalid_site_maps_error_code(monkeypatch):
    flow, captured = _flow(monkeypatch)
    bad_site = copy.deepcopy(DEFAULT_SITE)
    bad_site["planes"] = []  # -> SiteValidationError("no_planes")

    result = await flow.async_step_user(_user_input(**{CONF_SITE: bad_site}))

    assert result["type"] == "form"
    assert captured["form"]["errors"] == {CONF_SITE: "no_planes"}
    assert "entry" not in captured


async def test_user_step_duplicate_name_aborts(monkeypatch):
    flow, captured = _flow(monkeypatch, abort_duplicate=True)
    with pytest.raises(AbortFlow):
        await flow.async_step_user(_user_input())
    assert "entry" not in captured
