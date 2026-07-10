"""Tests for the v0.4 options-flow additions (SPEC §6, §9, §10).

The (slim) options step exposes the quantile kill switch (Boolean selector,
default ON) and the editable comparison-sensors list (structured ObjectSelector,
``multiple``); both pre-fill from the existing entry options, serialize through
the exact HTTP-flow path, and persist. The comparison list is now a STRUCTURED
selector — each row is a required ``name`` plus a required ``daily_entity`` — yet
it is still NORMALISED on save through ``ComparisonConfig.list_from_options`` so
malformed / half-filled rows are dropped. Ships EMPTY (D-P9): a stock install
persists no comparisons. Structural fields no longer live here (they moved to
the reconfigure flow), so a submit carries only the tunables.

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
    CONF_COMPARISON_DAILY_ENTITY,
    CONF_COMPARISON_NAME,
    CONF_COMPARISON_SENSORS,
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


def _user_structural_schema():
    return _user_schema(
        name="Test",
        latitude=48.5479,
        longitude=12.1873,
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
# Schema shape: the new fields appear only in the options step and serialize.
# --------------------------------------------------------------------------


def test_v04_fields_absent_from_user_step():
    names = _field_names(_user_structural_schema())
    assert CONF_QUANTILES_ENABLED not in names
    assert CONF_COMPARISON_SENSORS not in names


def test_v04_fields_present_in_options_step():
    names = _field_names(_options_schema())
    assert CONF_QUANTILES_ENABLED in names
    assert CONF_COMPARISON_SENSORS in names


def test_quantiles_switch_default_on_and_override():
    by_name = {f.get("name"): f for f in _fields(_options_schema())}
    assert by_name[CONF_QUANTILES_ENABLED].get("default") is True
    by_name2 = {
        f.get("name"): f
        for f in _fields(_options_schema(quantiles_enabled=False))
    }
    assert by_name2[CONF_QUANTILES_ENABLED].get("default") is False


def test_comparison_list_is_structured_object_multiple_selector():
    """The comparison list is a STRUCTURED object selector that still
    serializes through the exact flow-endpoint path."""
    fields = _fields(_options_schema())
    by_name = {f.get("name"): f for f in fields}
    sel = by_name[CONF_COMPARISON_SENSORS]["selector"]
    assert "object" in sel
    obj = sel["object"]
    assert obj.get("multiple") is True
    # New structured form: name header + a {name, daily_entity} row spec.
    assert obj.get("label_field") == CONF_COMPARISON_NAME
    assert set(obj.get("fields", {})) == {
        CONF_COMPARISON_NAME,
        CONF_COMPARISON_DAILY_ENTITY,
    }
    # The daily_entity row is an entity selector filtered to sensors.
    entity_sel = obj["fields"][CONF_COMPARISON_DAILY_ENTITY]["selector"]
    assert "entity" in entity_sel
    # Ships empty by default.
    assert by_name[CONF_COMPARISON_SENSORS].get("default") == []


def test_comparison_list_default_reflects_override():
    rows = [{"name": "A", "daily_entity": "sensor.a"}]
    fields = _fields(_options_schema(comparison_sensors=rows))
    by_name = {f.get("name"): f for f in fields}
    assert by_name[CONF_COMPARISON_SENSORS].get("default") == rows


# --------------------------------------------------------------------------
# _current_values precedence for the new fields.
# --------------------------------------------------------------------------


def test_current_values_v04_defaults():
    vals = _current_values(None, existing={})
    assert vals["quantiles_enabled"] is True
    assert vals["comparison_sensors"] == []


def test_current_values_reads_existing_comparison_and_quantiles():
    existing = {
        CONF_QUANTILES_ENABLED: False,
        CONF_COMPARISON_SENSORS: [{"name": "A", "daily_entity": "sensor.a"}],
    }
    vals = _current_values(None, existing=existing)
    assert vals["quantiles_enabled"] is False
    assert vals["comparison_sensors"] == [{"name": "A", "daily_entity": "sensor.a"}]


def test_current_values_submitted_comparison_beats_existing():
    existing = {CONF_COMPARISON_SENSORS: [{"name": "old", "daily_entity": "sensor.o"}]}
    submitted = {CONF_COMPARISON_SENSORS: [{"name": "new", "daily_entity": "sensor.n"}]}
    vals = _current_values(submitted, existing=existing)
    assert vals["comparison_sensors"] == [{"name": "new", "daily_entity": "sensor.n"}]


def test_current_values_non_list_comparison_tolerated():
    vals = _current_values(None, existing={CONF_COMPARISON_SENSORS: "oops"})
    assert vals["comparison_sensors"] == []


# --------------------------------------------------------------------------
# Options flow persists + normalises the new fields.
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


async def test_options_flow_persists_and_normalises_v04(monkeypatch):
    entry = _FakeEntry(data=_structural_data())
    flow, captured = _options_flow(monkeypatch, entry)

    # Slim submit: tunables only, no structural fields.
    user_input = {
        CONF_QUANTILES_ENABLED: False,
        CONF_COMPARISON_SENSORS: [
            {
                "name": "8-Entry Baseline",
                "daily_entity": "sensor.pv_prognose_heute_alle_module",
            },
            {"name": "", "daily_entity": "sensor.x"},  # dropped on normalise
            {"name": "half"},  # dropped (no entity)
        ],
    }
    await flow.async_step_init(user_input)

    data = captured["data"]
    assert data[CONF_QUANTILES_ENABLED] is False
    # Only the clean row survives; each is a plain {name, daily_entity} dict.
    assert data[CONF_COMPARISON_SENSORS] == [
        {
            "name": "8-Entry Baseline",
            "daily_entity": "sensor.pv_prognose_heute_alle_module",
        }
    ]
    # No structural key was written by the options save.
    for key in (CONF_LATITUDE, CONF_LONGITUDE, CONF_SITE):
        assert key not in data


async def test_options_flow_defaults_quantiles_on_when_omitted(monkeypatch):
    entry = _FakeEntry(data=_structural_data())
    flow, captured = _options_flow(monkeypatch, entry)

    await flow.async_step_init({})  # quantiles + comparisons omitted
    data = captured["data"]
    assert data[CONF_QUANTILES_ENABLED] is True
    assert data[CONF_COMPARISON_SENSORS] == []
