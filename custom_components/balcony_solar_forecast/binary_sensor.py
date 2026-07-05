"""Binary sensor platform for the Balcony Solar Forecast integration.

A single ``degraded`` problem sensor makes the degradation ladder (SPEC §7)
visible: it is *on* whenever the forecast is not fresh (cached last-good
payload, pure-physics fallback, or unavailable), with the current status and
the payload age exposed as attributes. It intentionally stays available even
when the forecast itself is unavailable, so the operator can always read
*why* the system is degraded.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BINARY_SENSOR_DEGRADED, DOMAIN, STATUS_FRESH, STATUS_UNAVAILABLE
from .sensor import BalconyForecastEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Balcony Solar Forecast binary sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DegradedSensor(coordinator)])


class DegradedSensor(BalconyForecastEntity, BinarySensorEntity):
    """'On' when the forecast is running on anything below a fresh pull."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-decagram-outline"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, BINARY_SENSOR_DEGRADED)

    @property
    def available(self) -> bool:
        # Always available: reporting "we are degraded" must survive the
        # forecast itself going unavailable (SPEC §7 -- never silent).
        return True

    @property
    def is_on(self) -> bool | None:
        # Unavailable is the deepest rung of the degradation ladder: the
        # coordinator raised UpdateFailed, so there is no fresh curve at all
        # -> report the problem as on.
        if not self.coordinator.last_update_success:
            return True
        data = self.coordinator.data
        if not data:
            return None
        status = data.get("status")
        if status is None:
            return None
        return status != STATUS_FRESH

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        # Live age (climbs during an outage) with a frozen-snapshot fallback,
        # so the always-available diagnostic never freezes (SPEC §7).
        age_s = getattr(self.coordinator, "weather_age_seconds_live", None)
        if age_s is None:
            age_s = data.get("weather_age_seconds")
        status = (
            data.get("status")
            if self.coordinator.last_update_success
            else STATUS_UNAVAILABLE
        )
        return {
            "source_status": status,
            "last_fetch_age_min": (
                None if age_s is None else round(float(age_s) / 60.0, 1)
            ),
        }
