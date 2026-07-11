"""Energy dashboard hook: expose the hourly forecast as solar production.

Home Assistant's Energy dashboard discovers per-integration solar forecasts
through ``async_get_solar_forecast(hass, config_entry_id)`` returning
``{"wh_hours": {iso_hour: wh, ...}}`` (SPEC §8). We hand back the engine's
served-AC hourly Wh roll-up (``hourly_wh_ac``) for the requested entry: AC is
the energy actually produced into the home (the operator-facing standard, Phase
2), so it is what the Energy dashboard's expected-production overlay wants —
without any consumer-side coupling.
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
    # Served-AC hourly curve (Phase 2): the energy produced into the home. Already
    # keyed by ISO-8601 UTC hour start (coordinator). Empty/absent (no forecast
    # yet, or a v0.1 cached result) => no overlay rather than a stale curve.
    hourly = data.get("hourly_wh_ac")
    if not hourly:
        return None
    return {"wh_hours": dict(hourly)}
