"""Tests for the options-flow learner kill switches (SPEC §5).

The options step must expose the three per-layer learner toggles (fast learner,
shademap learner, day-ahead bias — all default ON), pre-fill them from the
existing entry options, survive the exact HTTP-flow serialization, and persist
the submitted booleans. Boolean selectors sidestep the HA-2026 ``step >= 1e-3``
NumberSelector rule entirely, but we still assert the schema serializes.

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


def _schema(*, include_learner_switches: bool, **overrides):
    base = {
        "name": "Test",
        "latitude": 48.5479,
        "longitude": 12.1873,
        "fetch_interval": 1800,
        "recompute_interval": 900,
        "site": copy.deepcopy(DEFAULT_SITE),
    }
    base.update(overrides)
    return _user_schema(
        **base,
        include_name=False,
        include_learner_switches=include_learner_switches,
    )


def _field_names(schema) -> list[str]:
    fields = voluptuous_serialize.convert(
        schema, custom_serializer=cv.custom_serializer
    )
    return [f.get("name") for f in fields]


# --------------------------------------------------------------------------
# Schema shape: switches only appear when asked; serialize like the endpoint.
# --------------------------------------------------------------------------


def test_switches_absent_from_user_step() -> None:
    names = _field_names(_schema(include_learner_switches=False))
    for key in _SWITCH_KEYS:
        assert key not in names


def test_switches_present_in_options_step_and_serialize() -> None:
    names = _field_names(_schema(include_learner_switches=True))
    for key in _SWITCH_KEYS:
        assert key in names


def test_switch_defaults_are_on() -> None:
    """The rendered defaults must be True (both learners ON out of the box)."""
    fields = voluptuous_serialize.convert(
        _schema(include_learner_switches=True),
        custom_serializer=cv.custom_serializer,
    )
    by_name = {f.get("name"): f for f in fields}
    for key in _SWITCH_KEYS:
        assert by_name[key].get("default") is True


def test_switch_defaults_reflect_overrides() -> None:
    fields = voluptuous_serialize.convert(
        _schema(
            include_learner_switches=True,
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
# Options flow persists the submitted switches (and fills missing with ON).
# --------------------------------------------------------------------------


class _FakeEntry:
    def __init__(self, data, options=None):
        self.data = data
        self.options = options or {}


async def test_options_flow_persists_switches(monkeypatch) -> None:
    entry = _FakeEntry(
        data={
            CONF_LATITUDE: 48.5,
            CONF_LONGITUDE: 12.1,
            CONF_FETCH_INTERVAL: FETCH_INTERVAL_SECONDS,
            CONF_RECOMPUTE_INTERVAL: RECOMPUTE_INTERVAL_SECONDS,
            CONF_SITE: copy.deepcopy(DEFAULT_SITE),
        },
    )
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

    site = copy.deepcopy(DEFAULT_SITE)
    user_input = {
        CONF_LATITUDE: 48.5,
        CONF_LONGITUDE: 12.1,
        CONF_FETCH_INTERVAL: FETCH_INTERVAL_SECONDS,
        CONF_RECOMPUTE_INTERVAL: RECOMPUTE_INTERVAL_SECONDS,
        CONF_SITE: site,
        CONF_FAST_LEARNER_ENABLED: False,
        CONF_SLOW_LEARNER_ENABLED: True,
        # day-ahead omitted -> defaults ON.
    }
    await flow.async_step_init(user_input)

    data = captured["data"]
    assert data[CONF_FAST_LEARNER_ENABLED] is False
    assert data[CONF_SLOW_LEARNER_ENABLED] is True
    assert data[CONF_DAY_AHEAD_BIAS_ENABLED] is True


async def test_options_flow_renders_form_with_switches(monkeypatch) -> None:
    entry = _FakeEntry(
        data={
            CONF_LATITUDE: 48.5,
            CONF_LONGITUDE: 12.1,
            CONF_FETCH_INTERVAL: FETCH_INTERVAL_SECONDS,
            CONF_RECOMPUTE_INTERVAL: RECOMPUTE_INTERVAL_SECONDS,
            CONF_SITE: copy.deepcopy(DEFAULT_SITE),
        },
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

    def _fake_show_form(*, step_id, data_schema, errors):
        captured["step_id"] = step_id
        captured["schema"] = data_schema
        return {"type": "form"}

    monkeypatch.setattr(flow, "async_show_form", _fake_show_form)

    await flow.async_step_init(None)
    assert captured["step_id"] == "init"
    names = _field_names(captured["schema"])
    for key in _SWITCH_KEYS:
        assert key in names
    # The disabled fast learner from options must ride along as the default.
    fields = voluptuous_serialize.convert(
        captured["schema"], custom_serializer=cv.custom_serializer
    )
    by_name = {f.get("name"): f for f in fields}
    assert by_name[CONF_FAST_LEARNER_ENABLED].get("default") is False
