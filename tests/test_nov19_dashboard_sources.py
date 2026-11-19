"""Verify entry scoping and authorization at the dashboard WebSocket boundary."""
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.helpers import entity_registry

from custom_components.balcony_solar_forecast import _services
from custom_components.balcony_solar_forecast.const import DOMAIN
from custom_components.balcony_solar_forecast.core.types import PlaneConfig


@pytest.fixture
def command(monkeypatch):
    def coordinator(source, name):
        return SimpleNamespace(_site=SimpleNamespace(planes=(
            PlaneConfig(name, 180.0, 0.0, 400.0, actual_entity=source),
        )))

    def row(entry, key, *, platform=DOMAIN, disabled=None):
        return SimpleNamespace(
            platform=platform, config_entry_id=entry, unique_id=f"{entry}_{key}", disabled_by=disabled,
        )

    rows = {
        "sensor.a_total": row("first", "measured_dc_power_total"),
        "sensor.b_total": row("second", "measured_dc_power_total"),
        "sensor.b_forecast": row("second", "energy_production_today"),
        "sensor.b_disabled": row("second", "measured_dc_power_total", disabled="user"),
        "sensor.other": row("second", "measured_dc_power_total", platform="template"),
    }
    monkeypatch.setattr(entity_registry, "async_get", lambda _hass: SimpleNamespace(async_get=rows.get))
    hass = SimpleNamespace(
        data={DOMAIN: {
            "first": coordinator("sensor.first_module", "M1"),
            "second": coordinator("sensor.second_module", "M2"),
        }},
        services=SimpleNamespace(has_service=lambda *_: False, async_register=lambda *a, **kw: None),
    )
    _services.async_register_services(hass)
    def call(entity, entry, *, allowed=True, source_allowed=True):
        registered = hass.data.get("websocket_api", {}).get(f"{DOMAIN}/measurement_sources")
        assert registered is not None, "the card needs a read API while the measured total is unavailable"
        handler, schema = registered
        errors = []
        replies = []
        checked = []

        def check_entity(entity_id, policy):
            checked.append((entity_id, policy))
            return allowed if entity_id == entity else source_allowed

        user = SimpleNamespace(is_admin=False, permissions=SimpleNamespace(check_entity=check_entity))
        connection = SimpleNamespace(
            user=user, send_error=lambda *args: errors.append(args),
            send_result=lambda *args: replies.append(args),
        )
        msg = schema({
            "id": 7, "type": f"{DOMAIN}/measurement_sources", "entity_id": entity, "entry_id": entry,
        })
        handler(hass, connection, msg)
        assert checked[0] == (entity, POLICY_READ)
        assert all(policy == POLICY_READ for _, policy in checked)
        return errors, replies

    return call


def test_measurement_sources_allows_normal_entity_read_permission(command):
    errors, replies = command("sensor.b_total", "second")
    assert not errors
    assert replies == [(7, {
        "sources": ["sensor.second_module"], "source_names": ["M2"], "source_limits_w": [500.0],
    })]


@pytest.mark.parametrize("entity", [
    "sensor.a_total", "sensor.b_forecast", "sensor.b_disabled", "sensor.other", "sensor.missing",
])
def test_measurement_sources_requires_matching_enabled_total_in_entry(command, entity):
    errors, replies = command(entity, "second")
    assert errors and errors[0][:2] == (7, "not_found")
    assert not replies


def test_measurement_sources_requires_entity_read_permission(command):
    errors, replies = command("sensor.b_total", "second", allowed=False)
    assert errors and errors[0][:2] == (7, "unauthorized")
    assert not replies


def test_measurement_sources_redacts_individually_denied_module(command):
    errors, replies = command("sensor.b_total", "second", source_allowed=False)
    assert not errors
    assert replies == [(7, {"sources": [], "source_names": [], "source_limits_w": []})]
