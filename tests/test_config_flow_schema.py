"""Regression tests for the config-flow FORM schema (needs homeassistant).

The user-step schema must not only build — it must survive the exact
serialization the ``/api/config/config_entries/flow`` endpoint performs
(``voluptuous_serialize.convert`` with HA's custom serializer). HA's
``NumberSelector`` rejects numeric ``step`` values below ``1e-3`` at
CONSTRUCTION time; a violating selector therefore breaks the very first
form render as a bare HTTP 400 with no log line (observed live on
2026-07-06 with ``step=1e-6`` on the coordinate fields).

Skipped when homeassistant is not installed (plain-core test envs).
"""

from __future__ import annotations

import copy

import pytest

ha = pytest.importorskip("homeassistant")

import voluptuous_serialize  # noqa: E402  (ships with homeassistant)
from homeassistant.helpers import config_validation as cv  # noqa: E402

from balcony_solar_forecast.config_flow import _user_schema  # noqa: E402
from balcony_solar_forecast.const import DEFAULT_SITE  # noqa: E402


def _build_schema(include_name: bool = True):
    return _user_schema(
        name="Test",
        latitude=48.5479,
        longitude=12.1873,
        fetch_interval=1800,
        recompute_interval=900,
        site=copy.deepcopy(DEFAULT_SITE),
        include_name=include_name,
    )


@pytest.mark.parametrize("include_name", [True, False])
def test_user_schema_builds(include_name: bool) -> None:
    """Selector configs are validated at construction — this alone catches
    invalid selector parameters like step < 1e-3."""
    schema = _build_schema(include_name)
    assert schema is not None


@pytest.mark.parametrize("include_name", [True, False])
def test_user_schema_serializes_like_the_flow_endpoint(include_name: bool) -> None:
    """The exact conversion the HTTP flow view applies to render the form."""
    schema = _build_schema(include_name)
    fields = voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)
    names = [f.get("name") for f in fields]
    assert "latitude" in names
    assert "site" in names
    # the shipped default site must ride along as the field default
    site_field = next(f for f in fields if f.get("name") == "site")
    assert site_field.get("default"), "DEFAULT_SITE must prefill the object selector"
