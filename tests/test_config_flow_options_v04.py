"""Tests for the v0.4 options-flow additions (SPEC §11).

The (slim) options step exposes the quantile kill switch (Boolean selector,
default ON); it pre-fills from the existing entry options, serializes through
the exact HTTP-flow path, and persists. Structural fields no longer live here
(they moved to the reconfigure flow), so a submit carries only the tunables.
The removed comparison-sensors list must stay gone: the schema carries no such
field, and a legacy ``comparison_sensors`` key left in an old entry's options is
dropped on the next save.

Needs Home Assistant + voluptuous; skipped on the plain-core path.
"""

from __future__ import annotations

import copy

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

import voluptuous_serialize  # noqa: E402
from balcony_solar_forecast.config_flow import (  # noqa: E402
    BalconySolarForecastOptionsFlow,
    _current_values,
    _options_schema,
    _user_schema,
)
from balcony_solar_forecast.const import (  # noqa: E402
    CONF_FETCH_INTERVAL,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_QUANTILES_ENABLED,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SITE,
    DEFAULT_SITE,
    FETCH_INTERVAL_SECONDS,
    RECOMPUTE_INTERVAL_SECONDS,
)
from homeassistant.helpers import config_validation as cv  # noqa: E402

# The options key the removed external-comparison machinery used to persist.
# Kept as a literal on purpose: the constant is gone from const.py (no reader
# remains), only the cleanup-on-save path still names the key.
_LEGACY_COMPARISON_KEY = "comparison_sensors"


def _user_structural_schema():
    return _user_schema(
        name="Test",
        latitude=51.1,
        longitude=10.4,
        fetch_interval=1800,
        recompute_interval=900,
        site=copy.deepcopy(DEFAULT_SITE),
        include_name=False,
    )


def _fields(schema):
    return voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)


def _field_names(schema):
    return [f.get("name") for f in _fields(schema)]


# --------------------------------------------------------------------------
# Schema shape: the quantile switch appears only in the options step.
# --------------------------------------------------------------------------


def test_v04_fields_absent_from_user_step():
    names = _field_names(_user_structural_schema())
    assert CONF_QUANTILES_ENABLED not in names


def test_v04_fields_present_in_options_step():
    names = _field_names(_options_schema())
    assert CONF_QUANTILES_ENABLED in names


def test_options_schema_has_no_comparison_sensors_field():
    # The editable comparison-sensors list was removed with the external
    # comparisons: neither the options schema nor the structural user schema
    # may carry the field anymore.
    assert _LEGACY_COMPARISON_KEY not in _field_names(_options_schema())
    assert _LEGACY_COMPARISON_KEY not in _field_names(_user_structural_schema())


def test_quantiles_switch_default_on_and_override():
    by_name = {f.get("name"): f for f in _fields(_options_schema())}
    assert by_name[CONF_QUANTILES_ENABLED].get("default") is True
    by_name2 = {
        f.get("name"): f
        for f in _fields(_options_schema(quantiles_enabled=False))
    }
    assert by_name2[CONF_QUANTILES_ENABLED].get("default") is False


# --------------------------------------------------------------------------
# _current_values precedence for the quantile switch.
# --------------------------------------------------------------------------


def test_current_values_v04_defaults():
    vals = _current_values(None, existing={})
    assert vals["quantiles_enabled"] is True
    # No comparison default is resolved anymore (the tunable is gone).
    assert "comparison_sensors" not in vals


def test_current_values_reads_existing_quantiles():
    vals = _current_values(None, existing={CONF_QUANTILES_ENABLED: False})
    assert vals["quantiles_enabled"] is False


# --------------------------------------------------------------------------
# Options flow persists the tunables (+ legacy comparison cleanup).
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
    monkeypatch.setattr(
        BalconySolarForecastOptionsFlow,
        "config_entry",
        property(lambda self: entry),
        raising=False,
    )
    captured: dict = {}

    def _fake_create_entry(*, title, data):
        captured["data"] = data
        return {"type": "create_entry", "data": data}

    monkeypatch.setattr(flow, "async_create_entry", _fake_create_entry)
    return flow, captured


async def test_options_flow_persists_v04(monkeypatch):
    entry = _FakeEntry(data=_structural_data())
    flow, captured = _options_flow(monkeypatch, entry)

    # Slim submit: tunables only, no structural fields.
    await flow.async_step_init({CONF_QUANTILES_ENABLED: False})

    data = captured["data"]
    assert data[CONF_QUANTILES_ENABLED] is False
    # No structural key was written by the options save.
    for key in (CONF_LATITUDE, CONF_LONGITUDE, CONF_SITE):
        assert key not in data


async def test_options_flow_defaults_quantiles_on_when_omitted(monkeypatch):
    entry = _FakeEntry(data=_structural_data())
    flow, captured = _options_flow(monkeypatch, entry)

    await flow.async_step_init({})  # quantiles omitted
    data = captured["data"]
    assert data[CONF_QUANTILES_ENABLED] is True


async def test_options_flow_drops_legacy_comparison_sensors_key(monkeypatch):
    """An old entry may still carry a ``comparison_sensors`` list in options
    (written before the external comparisons were removed). The save spreads
    the existing options first, so without an explicit cleanup the dead list
    would ride along forever — it must be gone after the next save."""
    entry = _FakeEntry(
        data=_structural_data(),
        options={
            _LEGACY_COMPARISON_KEY: [
                {"name": "8-Entry Baseline", "daily_entity": "sensor.pv"}
            ],
        },
    )
    flow, captured = _options_flow(monkeypatch, entry)

    await flow.async_step_init({})
    data = captured["data"]
    assert _LEGACY_COMPARISON_KEY not in data
