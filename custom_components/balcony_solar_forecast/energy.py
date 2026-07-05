"""Energy dashboard hook: expose the hourly forecast as solar production.

Home Assistant's Energy dashboard discovers per-integration solar forecasts
through ``async_get_solar_forecast(hass, config_entry_id)`` returning
``{"wh_hours": {iso_hour: wh, ...}}`` (SPEC §8). We hand back the engine's
hourly Wh roll-up for the requested entry so the dashboard can draw the
expected-production overlay without any consumer-side coupling.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> dict[str, dict[str, float]] | None:
    """Return ``{"wh_hours": {iso_hour: wh}}`` for the Energy dashboard.

    ``None`` when the entry is unknown or holds no forecast yet -- the
    dashboard then simply shows no solar-forecast overlay rather than a stale
    curve (SPEC §7: never silent stale values).
    """
    coordinator = hass.data.get(DOMAIN, {}).get(config_entry_id)
    if coordinator is None:
        return None
    data: dict[str, Any] = coordinator.data or {}
    hourly = data.get("hourly_wh")
    if not hourly:
        return None
    # hourly_wh is already keyed by ISO-8601 UTC hour start (coordinator).
    return {"wh_hours": dict(hourly)}
