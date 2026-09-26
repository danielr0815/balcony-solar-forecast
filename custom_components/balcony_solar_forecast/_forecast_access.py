"""One availability boundary for public forecast consumers (SPEC §13)."""

from typing import Any, Protocol


class ForecastSource(Protocol):
    """Minimum read-only contract shared by entities, actions and Energy."""

    data: dict[str, Any] | None
    last_update_success: bool


def current_forecast(source: ForecastSource) -> dict[str, Any]:
    """HA retains old coordinator data after failure; it is not a live curve."""
    return (source.data or {}) if source.last_update_success else {}
