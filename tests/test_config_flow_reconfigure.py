"""Tests for the reconfigure flow (structural setup edits ``entry.data``).

Reconfigure is the HA quality-scale entry point for editing STRUCTURAL setup
(location, fetch/recompute cadences, the full site object) back into
``entry.data`` — never ``entry.options``, where it would permanently shadow
``entry.data`` through the ``{**data, **options}`` merge. Two invariants are
load-bearing and untested elsewhere:

  * the lat/lon-into-site merge (the coordinator reads ONLY the site-embedded
    coordinates — see config_flow.py), shared verbatim with the user step; and
  * the atomic strip of stale structural keys from ``entry.options`` in the SAME
    ``async_update_reload_and_abort`` call, so a legacy entry cannot keep
    shadowing the just-reconfigured data.

Uses the same ``Flow.__new__`` + monkeypatched-fake pattern as
tests/test_config_flow_user.py (no HA flow manager needed).
"""

from __future__ import annotations

import copy

import pytest

pytest.importorskip("homeassistant")

from balcony_solar_forecast.config_flow import (  # noqa: E402
    _STRUCTURAL_OPTION_KEYS,
    BalconySolarForecastConfigFlow,
)
from balcony_solar_forecast.const import (  # noqa: E402
    CONF_AC_ACTUAL_ENTITY,
    CONF_AC_ACTUAL_INVERT,
    CONF_FETCH_INTERVAL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SITE,
    DEFAULT_SITE,
    FETCH_INTERVAL_SECONDS,
    RECOMPUTE_INTERVAL_SECONDS,
)
from balcony_solar_forecast.core.types import SiteConfig  # noqa: E402

# Submitted coordinates: DIFFERENT from both the entry's stored lat/lon AND the
# site dict's own embedded lat/lon, so the merge is unambiguously observable.
SUBMIT_LAT = 52.5200
SUBMIT_LON = 13.4050


class _FakeEntry:
    def __init__(self, data, options=None):
        self.data = data
        self.options = options or {}


def _entry(options=None) -> _FakeEntry:
    return _FakeEntry(
        data={
            "name": "My Balcony",
            CONF_LATITUDE: 48.0,
            CONF_LONGITUDE: 11.0,
            CONF_FETCH_INTERVAL: FETCH_INTERVAL_SECONDS,
            CONF_RECOMPUTE_INTERVAL: RECOMPUTE_INTERVAL_SECONDS,
            # DEFAULT_SITE carries the shipped reference coordinates, distinct
            # from the submitted ones — the merge must overwrite them.
            CONF_SITE: copy.deepcopy(DEFAULT_SITE),
        },
        options=options,
    )


def _flow(monkeypatch, entry):
    """A reconfigure flow with the HA plumbing faked out.

    ``captured['kwargs']`` holds the ``async_update_reload_and_abort`` kwargs,
    ``captured['entry']`` the entry passed to it, ``captured['form']`` the
    ``async_show_form`` kwargs.
    """
    flow = BalconySolarForecastConfigFlow.__new__(BalconySolarForecastConfigFlow)
    captured: dict = {}

    monkeypatch.setattr(flow, "_get_reconfigure_entry", lambda: entry)

    def _update_reload_and_abort(entry_arg, **kwargs):
        captured["entry"] = entry_arg
        captured["kwargs"] = kwargs
        return {"type": "abort", "reason": "reconfigure_successful"}

    def _show_form(*, step_id, data_schema, errors=None):
        captured["form"] = {"step_id": step_id, "errors": dict(errors or {})}
        return {"type": "form", "step_id": step_id, "errors": errors}

    monkeypatch.setattr(
        flow, "async_update_reload_and_abort", _update_reload_and_abort
    )
    monkeypatch.setattr(flow, "async_show_form", _show_form)
    return flow, captured


def _submit(**overrides) -> dict:
    base = {
        CONF_LATITUDE: SUBMIT_LAT,
        CONF_LONGITUDE: SUBMIT_LON,
        CONF_FETCH_INTERVAL: 1200,
        CONF_RECOMPUTE_INTERVAL: 600,
        CONF_SITE: copy.deepcopy(DEFAULT_SITE),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path: merged coordinates into entry.data + atomic options strip.
# ---------------------------------------------------------------------------


async def test_reconfigure_merges_coordinates_and_strips_stale_options(monkeypatch):
    # Options carry BOTH stale structural keys (must be stripped) AND an
    # unrelated tunable (must survive).
    entry = _entry(
        options={
            CONF_LATITUDE: 40.0,  # stale structural -> stripped
            CONF_SITE: {"stale": True},  # stale structural -> stripped
            "fast_learner_enabled": False,  # unrelated tunable -> survives
        }
    )
    flow, captured = _flow(monkeypatch, entry)

    result = await flow.async_step_reconfigure(_submit())

    assert result["type"] == "abort"
    assert captured["entry"] is entry
    kwargs = captured["kwargs"]

    updates = kwargs["data_updates"]
    # Top-level coordinates carry the SUBMITTED values...
    assert updates[CONF_LATITUDE] == pytest.approx(SUBMIT_LAT)
    assert updates[CONF_LONGITUDE] == pytest.approx(SUBMIT_LON)
    # ...AND are merged into the site dict — the ONLY coordinates the
    # coordinator reads. This must be the submitted value, NOT the DEFAULT_SITE
    # reference coordinate the submitted site dict shipped with.
    assert updates[CONF_SITE][CONF_LATITUDE] == pytest.approx(SUBMIT_LAT)
    assert updates[CONF_SITE][CONF_LONGITUDE] == pytest.approx(SUBMIT_LON)
    assert updates[CONF_SITE][CONF_LATITUDE] != pytest.approx(
        DEFAULT_SITE[CONF_LATITUDE]
    )
    assert updates[CONF_SITE]["planes"], "site must round-trip with its planes"
    assert updates[CONF_FETCH_INTERVAL] == 1200
    assert updates[CONF_RECOMPUTE_INTERVAL] == 600

    stripped = kwargs["options"]
    # Every structural key is stripped from options in the same atomic call...
    for key in _STRUCTURAL_OPTION_KEYS:
        assert key not in stripped
    # ...while OTHER (tunable) option keys survive untouched.
    assert stripped["fast_learner_enabled"] is False


async def test_reconfigure_ac_meter_merges_into_site_and_round_trips(monkeypatch):
    """The AC-meter picker (Phase 4) merges INTO the site dict and round-trips
    through SiteConfig — exactly like lat/lon."""
    entry = _entry()
    flow, captured = _flow(monkeypatch, entry)

    await flow.async_step_reconfigure(
        _submit(
            **{
                CONF_AC_ACTUAL_ENTITY: "sensor.house_ac_meter",
                CONF_AC_ACTUAL_INVERT: True,
            }
        )
    )

    site_dict = captured["kwargs"]["data_updates"][CONF_SITE]
    assert site_dict[CONF_AC_ACTUAL_ENTITY] == "sensor.house_ac_meter"
    assert site_dict[CONF_AC_ACTUAL_INVERT] is True
    # The persisted site dict reloads into a SiteConfig carrying both.
    site = SiteConfig.from_dict(site_dict)
    assert site.ac_actual_entity == "sensor.house_ac_meter"
    assert site.ac_actual_invert is True


async def test_reconfigure_empty_ac_meter_stays_none(monkeypatch):
    """No AC entity + invert off -> absent from the site dict (None/False on
    reload), and any value the submitted site OBJECT carried is cleared (the
    visible fields are authoritative)."""
    entry = _entry()
    flow, captured = _flow(monkeypatch, entry)

    # The submitted site object even carries a stale meter; the empty form field
    # must win and clear it.
    stale_site = copy.deepcopy(DEFAULT_SITE)
    stale_site[CONF_AC_ACTUAL_ENTITY] = "sensor.stale"
    stale_site[CONF_AC_ACTUAL_INVERT] = True

    await flow.async_step_reconfigure(
        _submit(**{CONF_SITE: stale_site, CONF_AC_ACTUAL_ENTITY: "  "})
    )

    site_dict = captured["kwargs"]["data_updates"][CONF_SITE]
    assert CONF_AC_ACTUAL_ENTITY not in site_dict
    assert CONF_AC_ACTUAL_INVERT not in site_dict
    site = SiteConfig.from_dict(site_dict)
    assert site.ac_actual_entity is None
    assert site.ac_actual_invert is False


async def test_reconfigure_with_empty_options_still_strips_cleanly(monkeypatch):
    entry = _entry()  # no options
    flow, captured = _flow(monkeypatch, entry)

    await flow.async_step_reconfigure(_submit())

    assert captured["kwargs"]["options"] == {}


# ---------------------------------------------------------------------------
# Error path + first render.
# ---------------------------------------------------------------------------


async def test_reconfigure_invalid_site_maps_error_and_rerenders(monkeypatch):
    entry = _entry()
    flow, captured = _flow(monkeypatch, entry)
    bad_site = copy.deepcopy(DEFAULT_SITE)
    bad_site["planes"] = []  # -> SiteValidationError("no_planes")

    result = await flow.async_step_reconfigure(_submit(**{CONF_SITE: bad_site}))

    assert result["type"] == "form"
    assert captured["form"]["step_id"] == "reconfigure"
    assert captured["form"]["errors"] == {CONF_SITE: "no_planes"}
    assert "kwargs" not in captured  # no update happened


async def test_reconfigure_first_render_shows_form(monkeypatch):
    entry = _entry()
    flow, captured = _flow(monkeypatch, entry)

    result = await flow.async_step_reconfigure(None)

    assert result["type"] == "form"
    assert captured["form"]["step_id"] == "reconfigure"
    assert captured["form"]["errors"] == {}
    assert "kwargs" not in captured
