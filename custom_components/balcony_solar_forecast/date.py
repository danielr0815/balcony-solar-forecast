"""Date platform: pick the date for the shade-profile diagram (SPEC §5).

A single ``shade_profile_date`` date entity choosing which local calendar day
the ``shade_profile`` sensor visualises (the sun path + learned shade differ by
season: sun geometry, the shademap half-year split, and the seasonal foliage
ramp all key on the date). It always **defaults to today** — the selection is
NOT persisted across restarts, so every restart/reload opens on the current day
(an ad-hoc pick holds only within the session). Setting a date pushes it into
the coordinator, which recomputes the diagram entities.

Config entity + always available: the diagram is pure geometry and must be
adjustable even while the live forecast is unavailable.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATE_SHADE_PROFILE_DATE, DOMAIN
from .sensor import BalconyForecastEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the shade-profile date picker."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ShadeProfileDate(coordinator)])


class ShadeProfileDate(BalconyForecastEntity, DateEntity):
    """Pick which local date the shade-profile diagram renders (defaults today)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:calendar"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, DATE_SHADE_PROFILE_DATE)

    @property
    def available(self) -> bool:
        # Config entity: adjustable regardless of forecast availability.
        return True

    @property
    def native_value(self) -> date | None:
        # Defaults to today (coordinator.shade_profile_date) until set this
        # session; deliberately not restored, so a restart re-opens on today.
        return self.coordinator.shade_profile_date

    async def async_set_value(self, value: date) -> None:
        self.coordinator.set_shade_profile_date(value)
        self.async_write_ha_state()
