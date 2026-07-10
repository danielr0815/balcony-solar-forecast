"""Tests for the (slim) options-flow learner kill switches (SPEC §5).

The options step exposes the three per-layer learner toggles (fast learner,
shademap learner, day-ahead bias — all default ON), pre-fills them from the
existing entry options, survives the exact HTTP-flow serialization, and
persists the submitted booleans. Structural setup (location, intervals, site)
no longer lives here — it moved to the reconfigure flow — so the options schema
must NOT carry it, an options submit must NOT include it, and any structural key
an OLD entry still carries in options must ride through an options save
untouched (the migration-safety rule). Boolean selectors sidestep the HA-2026
``step >= 1e-3`` NumberSelector rule entirely, but we still assert the schema
serializes.

Needs Home Assistant + voluptuous; skipped on the plain-core path.
"""

from __future__ import annotations

import copy

import pytest

ha = pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

import voluptuous_serialize  # noqa: E402  (ships with homeassistant)
from balcony_solar_forecast.config_flow import (  # noqa: E402
    BalconySolarForecastOptionsFlow,
    _current_values,
    _options_schema,
    _user_schema,
)
from balcony_solar_forecast.const import (  # noqa: E402
    CONF_DAY_AHEAD_BIAS_ENABLED,
    CONF_FAST_LEARNER_ENABLED,
    CONF_FETCH_INTERVAL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SITE,
    CONF_SLOW_LEARNER_ENABLED,
    DEFAULT_SITE,
    FETCH_INTERVAL_SECONDS,
    RECOMPUTE_INTERVAL_SECONDS,
)
from homeassistant.helpers import config_validation as cv  # noqa: E402

_SWITCH_KEYS = (
    CONF_FAST_LEARNER_ENABLED,
    CONF_SLOW_LEARNER_ENABLED,
    CONF_DAY_AHEAD_BIAS_ENABLED,
)

# The structural keys that must NOT appear in the slim options schema/submit.
_STRUCTURAL_KEYS = (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_FETCH_INTERVAL,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SITE,
)


def _user_structural_schema():
    """The user/reconfigure schema — structural fields only, no switches."""
    return _user_schema(
        name="Test",
        latitude=48.5479,
        longitude=12.1873,
        fetch_interval=1800,
        recompute_interval=900,
        site=copy.deepcopy(DEFAULT_SITE),
        include_name=False,
    )


def _field_names(schema) -> list[str]:
    fields = voluptuous_serialize.convert(
        schema, custom_serializer=cv.custom_serializer
    )
    return [f.get("name") for f in fields]


# --------------------------------------------------------------------------
# Schema shape: switches live in the options schema, never the user schema.
# --------------------------------------------------------------------------


def test_switches_absent_from_user_schema() -> None:
    names = _field_names(_user_structural_schema())
    for key in _SWITCH_KEYS:
        assert key not in names
    # The user/reconfigure schema carries ONLY the structural fields.
    for key in _STRUCTURAL_KEYS:
        assert key in names


def test_switches_present_in_options_schema_and_serialize() -> None:
    names = _field_names(_options_schema())
    for key in _SWITCH_KEYS:
        assert key in names
    # ...and no structural field leaked into the options schema.
    for key in _STRUCTURAL_KEYS:
        assert key not in names


def test_switch_defaults_are_on() -> None:
    """The rendered defaults must be True (both learners ON out of the box)."""
    fields = voluptuous_serialize.convert(
        _options_schema(),
        custom_serializer=cv.custom_serializer,
    )
    by_name = {f.get("name"): f for f in fields}
    for key in _SWITCH_KEYS:
        assert by_name[key].get("default") is True


def test_switch_defaults_reflect_overrides() -> None:
    fields = voluptuous_serialize.convert(
        _options_schema(
            fast_learner_enabled=False,
            slow_learner_enabled=True,
            day_ahead_bias_enabled=False,
        ),
        custom_serializer=cv.custom_serializer,
    )
    by_name = {f.get("name"): f for f in fields}
    assert by_name[CONF_FAST_LEARNER_ENABLED].get("default") is False
    assert by_name[CONF_SLOW_LEARNER_ENABLED].get("default") is True
    assert by_name[CONF_DAY_AHEAD_BIAS_ENABLED].get("default") is False


# --------------------------------------------------------------------------
# _current_values: switch precedence (submitted > existing > shipped default).
# --------------------------------------------------------------------------


def test_current_values_defaults_when_unset() -> None:
    vals = _current_values(None, existing={})
    assert vals["fast_learner_enabled"] is True
    assert vals["slow_learner_enabled"] is True
    assert vals["day_ahead_bias_enabled"] is True


def test_current_values_reads_existing_options() -> None:
    existing = {
        CONF_FAST_LEARNER_ENABLED: False,
        CONF_SLOW_LEARNER_ENABLED: False,
        CONF_DAY_AHEAD_BIAS_ENABLED: True,
    }
    vals = _current_values(None, existing=existing)
    assert vals["fast_learner_enabled"] is False
    assert vals["slow_learner_enabled"] is False
    assert vals["day_ahead_bias_enabled"] is True


def test_current_values_submitted_beats_existing() -> None:
    existing = {CONF_FAST_LEARNER_ENABLED: True}
    submitted = {CONF_FAST_LEARNER_ENABLED: False}
    vals = _current_values(submitted, existing=existing)
    assert vals["fast_learner_enabled"] is False


# --------------------------------------------------------------------------
# Options flow persists the submitted switches (and fills missing with ON),
# writes NO structural keys, and preserves stale structural keys in options.
# --------------------------------------------------------------------------


class _FakeEntry:
    def __init__(self, data, options=None):
        self.data = data
        self.options = options or {}


def _structural_data() -> dict:
    return {
        CONF_LATITUDE: 48.5,
        CONF_LONGITUDE: 12.1,
        CONF_FETCH_INTERVAL: FETCH_INTERVAL_SECONDS,
        CONF_RECOMPUTE_INTERVAL: RECOMPUTE_INTERVAL_SECONDS,
        CONF_SITE: copy.deepcopy(DEFAULT_SITE),
    }


def _options_flow(monkeypatch, entry):
    flow = BalconySolarForecastOptionsFlow.__new__(BalconySolarForecastOptionsFlow)
    # Patch the read-only config_entry property on the class for this instance.
    monkeypatch.setattr(
        BalconySolarForecastOptionsFlow,
        "config_entry",
        property(lambda self: entry),
        raising=False,
    )
    captured: dict = {}

    def _fake_create_entry(*, title, data):
        captured["title"] = title
        captured["data"] = data
        return {"type": "create_entry", "data": data}

    monkeypatch.setattr(flow, "async_create_entry", _fake_create_entry)
    return flow, captured


async def test_options_flow_persists_switches(monkeypatch) -> None:
    entry = _FakeEntry(data=_structural_data())
    flow, captured = _options_flow(monkeypatch, entry)

    # Slim options submit: switches only, no structural fields.
    user_input = {
        CONF_FAST_LEARNER_ENABLED: False,
        CONF_SLOW_LEARNER_ENABLED: True,
        # day-ahead omitted -> defaults ON.
    }
    await flow.async_step_init(user_input)

    data = captured["data"]
    assert data[CONF_FAST_LEARNER_ENABLED] is False
    assert data[CONF_SLOW_LEARNER_ENABLED] is True
    assert data[CONF_DAY_AHEAD_BIAS_ENABLED] is True
    # An options save never writes structural keys (they belong to entry.data).
    for key in _STRUCTURAL_KEYS:
        assert key not in data


async def test_options_save_preserves_stale_structural_keys_in_options(
    monkeypatch,
) -> None:
    """Migration-safety: an old entry that edited its site via the LEGACY
    options flow still carries structural keys in ``entry.options``. An options
    save must ride them through UNTOUCHED — dropping them would silently revert
    the live site to the stale ``entry.data`` version until the next reconfigure
    cleans them up.
    """
    legacy_site = copy.deepcopy(DEFAULT_SITE)
    legacy_site["planes"][0]["name"] = "LEGACY_M1"  # a distinguishable marker
    entry = _FakeEntry(
        data=_structural_data(),
        options={
            CONF_LATITUDE: 51.0,
            CONF_LONGITUDE: 7.0,
            CONF_FETCH_INTERVAL: 1200,
            CONF_RECOMPUTE_INTERVAL: 600,
            CONF_SITE: legacy_site,
            CONF_FAST_LEARNER_ENABLED: True,
        },
    )
    flow, captured = _options_flow(monkeypatch, entry)

    await flow.async_step_init({CONF_FAST_LEARNER_ENABLED: False})

    data = captured["data"]
    # The submitted switch is (re)written on top...
    assert data[CONF_FAST_LEARNER_ENABLED] is False
    # ...while every stale structural key survives verbatim.
    assert data[CONF_LATITUDE] == 51.0
    assert data[CONF_LONGITUDE] == 7.0
    assert data[CONF_FETCH_INTERVAL] == 1200
    assert data[CONF_RECOMPUTE_INTERVAL] == 600
    assert data[CONF_SITE]["planes"][0]["name"] == "LEGACY_M1"


async def test_options_flow_renders_form_with_switches(monkeypatch) -> None:
    entry = _FakeEntry(
        data=_structural_data(),
        options={CONF_FAST_LEARNER_ENABLED: False},
    )
    flow = BalconySolarForecastOptionsFlow.__new__(BalconySolarForecastOptionsFlow)
    monkeypatch.setattr(
        BalconySolarForecastOptionsFlow,
        "config_entry",
        property(lambda self: entry),
        raising=False,
    )

    captured: dict = {}

    def _fake_show_form(*, step_id, data_schema, errors=None):
        captured["step_id"] = step_id
        captured["schema"] = data_schema
        return {"type": "form"}

    monkeypatch.setattr(flow, "async_show_form", _fake_show_form)

    await flow.async_step_init(None)
    assert captured["step_id"] == "init"
    names = _field_names(captured["schema"])
    for key in _SWITCH_KEYS:
        assert key in names
    # No structural fields in the slim options form.
    for key in _STRUCTURAL_KEYS:
        assert key not in names
    # The disabled fast learner from options must ride along as the default.
    fields = voluptuous_serialize.convert(
        captured["schema"], custom_serializer=cv.custom_serializer
    )
    by_name = {f.get("name"): f for f in fields}
    assert by_name[CONF_FAST_LEARNER_ENABLED].get("default") is False
