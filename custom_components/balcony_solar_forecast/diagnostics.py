"""Diagnostics for the Balcony Solar Forecast integration.

Dumps the config entry, the current degradation state (source status +
payload age + issued time) and a compact forecast summary for bug reports.
Coordinates are redacted (both the top-level entry lat/lon and the per-site
copy) -- everything else is site geometry the operator wants visible.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_SITE,
    DOMAIN,
)

# Redact anything that pins the operator's location.
TO_REDACT = {CONF_LATITUDE, CONF_LONGITUDE, "latitude", "longitude"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (coordinates redacted)."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    entry_dump = {
        "title": entry.title,
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
    }
    # The site object nests its own latitude/longitude keys; redact those too.
    for bucket in ("data", "options"):
        site = entry_dump[bucket].get(CONF_SITE)
        if isinstance(site, dict):
            entry_dump[bucket][CONF_SITE] = async_redact_data(site, TO_REDACT)

    diagnostics: dict[str, Any] = {"entry": entry_dump}

    if coordinator is None:
        diagnostics["state"] = {"available": False, "reason": "coordinator_missing"}
        return diagnostics

    data: dict[str, Any] = coordinator.data or {}
    age_s = data.get("weather_age_seconds")
    diagnostics["state"] = {
        "last_update_success": bool(
            getattr(coordinator, "last_update_success", False)
        ),
        "source_status": data.get("status"),
        "degraded": data.get("degraded"),
        "weather_age_seconds": age_s,
        "last_fetch_age_min": (
            None if age_s is None else round(float(age_s) / 60.0, 1)
        ),
        "last_error": data.get("last_error"),
        "computed_at": data.get("computed_at"),
    }
    diagnostics["forecast"] = _forecast_summary(data)
    diagnostics["store"] = _store_stats(coordinator)
    return diagnostics


def _forecast_summary(data: dict[str, Any]) -> dict[str, Any] | None:
    """Compact, coordinate-free summary of the last forecast."""
    if not data:
        return None
    starts = data.get("slot_starts") or []
    return {
        "slot_count": len(starts),
        "first_slot": starts[0] if starts else None,
        "last_slot": starts[-1] if starts else None,
        "plane_names": list((data.get("plane_watts") or {}).keys()),
        "daily_kwh": dict(data.get("daily_kwh") or {}),
        "hourly_count": len(data.get("hourly_wh") or {}),
    }


def _store_stats(coordinator: Any) -> dict[str, Any]:
    """Best-effort store statistics; tolerant of a coordinator without them.

    The glue's coordinator may expose ``store_stats()`` returning counts of
    the issued-forecast and daily-actuals logs. We never crash diagnostics if
    that method is absent (contract still stabilising).
    """
    getter = getattr(coordinator, "store_stats", None)
    if callable(getter):
        try:
            return dict(getter())
        except Exception as err:  # noqa: BLE001 -- diagnostics must not raise
            return {"error": repr(err)}
    return {"available": False}
