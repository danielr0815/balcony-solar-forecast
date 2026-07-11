"""Select platform: pick the module for the shade-profile diagram (SPEC §15).

A single ``shade_profile_module`` select whose options are the configured
plane/channel names. Choosing one re-points the ``shade_profile`` sensor (and
the ApexCharts card that reads it) at that module's sun-path + learned-shade
curve. The selection is persisted across restarts via RestoreEntity and pushed
into the coordinator, which recomputes the diagram entities on change.

Config entity + always available: the diagram is pure geometry and must be
selectable even while the live forecast is unavailable.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, SELECT_SHADE_PROFILE_MODULE
from .sensor import BalconyForecastEntity

# Coordinator-centralised I/O — entity updates are local, no throttling needed.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the shade-profile module select."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ShadeProfileModuleSelect(coordinator)])


class ShadeProfileModuleSelect(BalconyForecastEntity, SelectEntity, RestoreEntity):
    """Pick which module/plane the shade-profile diagram renders."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, SELECT_SHADE_PROFILE_MODULE)

    @property
    def available(self) -> bool:
        # Config entity: selectable regardless of forecast availability.
        return True

    @property
    def options(self) -> list[str]:
        return self.coordinator.shade_profile_plane_names()

    @property
    def current_option(self) -> str | None:
        module = self.coordinator.shade_profile_module
        return module or None

    async def async_select_option(self, option: str) -> None:
        self.coordinator.set_shade_profile_module(option)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        # Restore a still-valid selection; a plane renamed/removed since falls
        # back to the coordinator default (the front-facing plane — the azimuth
        # the most planes share, see coordinator.shade_profile_module ->
        # shadeprofile.default_module) rather than a dead option.
        if last is not None and last.state in self.options:
            self.coordinator.set_shade_profile_module(last.state)
