"""Production flow-manager, platform/entity and store lifecycle contracts."""
from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from tests.integration.helpers import DOMAIN, start_advanced, user_data


async def test_real_flow_entities_services_reload_unload(real_hass):
    """Prove HA-managed reload preserves entities, stored learning and other sites."""
    hass = real_hass
    flow = await start_advanced(hass)
    assert flow["type"] == FlowResultType.FORM
    invalid = user_data()
    invalid["name"] = " "
    invalid_result = await hass.config_entries.flow.async_configure(flow["flow_id"], invalid)
    assert invalid_result["type"] == FlowResultType.FORM
    assert invalid_result["errors"]["name"] == "name_required"
    created = await hass.config_entries.flow.async_configure(flow["flow_id"], user_data())
    assert created["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = created["result"]
    assert entry.state is ConfigEntryState.LOADED
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert {e.domain for e in entities} == {"sensor", "binary_sensor", "select", "date"}
    assert entities

    entity_ids = {e.unique_id.removeprefix(f"{entry.entry_id}_"): e.entity_id for e in entities}
    today = hass.states.get(entity_ids["energy_production_today"])
    assert today is not None and today.state not in {"unknown", "unavailable"}
    assert today.attributes["unit_of_measurement"] == "kWh"
    assert today.attributes["wh_period_ac"]
    response = await hass.services.async_call(
        DOMAIN, "get_forecast", {"entry_id": entry.entry_id},
        blocking=True, return_response=True,
    )
    forecast = response["entries"][entry.entry_id]
    assert len(forecast["slot_starts"]) == len(forecast["total_15min"]) > 0
    assert set(forecast["planes"]) == {"M1"}

    from custom_components.balcony_solar_forecast.energy import async_get_solar_forecast

    energy = await async_get_solar_forecast(hass, entry.entry_id)
    assert energy and energy["wh_hours"]
    assert sum(energy["wh_hours"].values()) > 0
    await hass.services.async_call("date", "set_value", {
        "entity_id": entity_ids["shade_profile_date"], "date": "2026-09-01",
    }, blocking=True)
    assert hass.states.get(entity_ids["shade_profile_date"]).state == "2026-09-01"
    assert hass.states.get(entity_ids["shade_profile"]).attributes["date"] == "2026-09-01"

    old = hass.data[DOMAIN][entry.entry_id]
    options = await hass.config_entries.options.async_init(entry.entry_id)
    assert options["type"] == FlowResultType.FORM
    saved = await hass.config_entries.options.async_configure(
        options["flow_id"], {"quantiles_enabled": False}
    )
    assert saved["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    current = hass.data[DOMAIN][entry.entry_id]
    assert current is not old
    assert entry.options["quantiles_enabled"] is False
    assert {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)} == set(entity_ids.values())
    assert hass.states.get(entity_ids["energy_production_today"]).state not in {"unknown", "unavailable"}

    # Store a valid non-neutral learner state through the real disk store, then
    # restart the entry. This catches delayed-write/unload and restore mistakes.
    from custom_components.balcony_solar_forecast.core.types import BiasCell, BiasState

    learned = BiasState(cells={"clear|midday": BiasCell(theta=1.1, covariance=3.0, n=9)})
    current._store.set_bias_state(learned)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert await async_get_solar_forecast(hass, entry.entry_id) is None
    assert hass.services.has_service(DOMAIN, "get_forecast")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    restored = hass.data[DOMAIN][entry.entry_id]
    assert restored is not current
    assert restored._store.get_bias_state().to_dict() == learned.to_dict()
    assert hass.states.get(entity_ids["energy_production_today"]).state not in {"unknown", "unavailable"}

    # A second real entry owns different registry entities and survives the
    # first entry's unload. Service resolution must enforce explicit targeting.
    second = await start_advanced(hass)
    second_result = await hass.config_entries.flow.async_configure(second["flow_id"], user_data("Other balcony"))
    await hass.async_block_till_done()
    other = second_result["result"]
    assert other.state is ConfigEntryState.LOADED
    other_entities = er.async_entries_for_config_entry(registry, other.entry_id)
    assert {e.entity_id for e in other_entities}.isdisjoint(entity_ids.values())

    with pytest.raises(ServiceValidationError, match="Multiple sites"):
        await hass.services.async_call(DOMAIN, "get_shade_profile", {}, blocking=True, return_response=True)
    response = await hass.services.async_call(DOMAIN, "get_forecast", {}, blocking=True, return_response=True)
    assert set(response["entries"]) == {entry.entry_id, other.entry_id}
    assert await hass.config_entries.async_unload(entry.entry_id)
    response = await hass.services.async_call(DOMAIN, "get_forecast", {}, blocking=True, return_response=True)
    assert set(response["entries"]) == {other.entry_id}
    assert other.state is ConfigEntryState.LOADED
