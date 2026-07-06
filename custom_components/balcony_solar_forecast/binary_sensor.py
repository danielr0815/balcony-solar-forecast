"""Binary sensor platform for the Balcony Solar Forecast integration.

A single ``degraded`` problem sensor makes the degradation ladder (SPEC §7)
visible: it is *on* whenever the forecast is not fresh (cached last-good
payload, pure-physics fallback, or unavailable), with the current status and
the payload age exposed as attributes. It intentionally stays available even
when the forecast itself is unavailable, so the operator can always read
*why* the system is degraded.

Two ``*_learner_active`` diagnostics (v0.2.0 + v0.3.0, SPEC §5) show at a
glance whether each learner layer is currently shaping the served curve: they
are *on* only while the layer status is ``active`` (kill switch on, not
drift-disabled, not collapse-frozen). They too stay available during a
degraded forecast, and expose the fine-grained status string as an attribute.
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

from .const import (
    BINARY_SENSOR_DEGRADED,
    BINARY_SENSOR_FAST_LEARNER,
    BINARY_SENSOR_KILL_GATE_PASSED,
    BINARY_SENSOR_SLOW_LEARNER,
    DATA_KEY_KILL_GATE_PASSED,
    DATA_KEY_LEARNER_STATUS,
    DATA_KEY_SCOREBOARD,
    DOMAIN,
    STATUS_FRESH,
    STATUS_UNAVAILABLE,
)
from .sensor import (
    LEARNER_LAYER_FAST,
    LEARNER_LAYER_SLOW,
    LEARNER_STATUS_ACTIVE,
    BalconyForecastEntity,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Balcony Solar Forecast binary sensors."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            DegradedSensor(coordinator),
            LearnerActiveSensor(
                coordinator, BINARY_SENSOR_FAST_LEARNER, LEARNER_LAYER_FAST
            ),
            LearnerActiveSensor(
                coordinator, BINARY_SENSOR_SLOW_LEARNER, LEARNER_LAYER_SLOW
            ),
            KillGatePassedSensor(coordinator),
        ]
    )


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


class LearnerActiveSensor(BalconyForecastEntity, BinarySensorEntity):
    """'On' while a learner layer is actively shaping the served curve.

    On == the layer's status is ``active``; off for every other status (kill
    switch off, drift-auto-disabled, or collapse-frozen — SPEC §5). Reports the
    fine-grained status as an attribute for the operator. Always available so
    "the learner is off / disabled" survives the forecast going unavailable.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:brain"

    def __init__(self, coordinator: Any, key: str, layer: str) -> None:
        super().__init__(coordinator, key)
        self._layer = layer

    def _status(self) -> str | None:
        data = self.coordinator.data or {}
        status_map = data.get(DATA_KEY_LEARNER_STATUS)
        if not isinstance(status_map, dict):
            return None
        value = status_map.get(self._layer)
        return value if isinstance(value, str) else None

    @property
    def available(self) -> bool:
        # Diagnostic: stays available even when the forecast is unavailable.
        return True

    @property
    def is_on(self) -> bool | None:
        status = self._status()
        if status is None:
            return None
        return status == LEARNER_STATUS_ACTIVE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"status": self._status()}


class KillGatePassedSensor(BalconyForecastEntity, BinarySensorEntity):
    """'On' when the engine passes the kill-gate over a FULL window (SPEC §9/§10).

    The gate the whole v0.4 plan hinges on: on == the engine is at least
    ``SCOREBOARD_GATE_MARGIN`` better than the best baseline on daily-kWh MAE
    across a full rolling window. Reads the coordinator's
    ``DATA_KEY_KILL_GATE_PASSED`` (bool | None). ``None`` — the honest
    "insufficient data" state while the window is not yet full — is surfaced as
    an unknown (``is_on`` None), never a premature pass/fail. The headline
    scoreboard numbers ride along as attributes so the operator can read the
    verdict's basis at a glance. Diagnostic + always available (the verdict must
    survive the forecast going unavailable).
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:gate"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, BINARY_SENSOR_KILL_GATE_PASSED)

    @property
    def available(self) -> bool:
        # Diagnostic: the kill-gate verdict must remain readable even when the
        # forecast itself is unavailable (SPEC §7 -- never silent).
        return True

    def _gate(self) -> bool | None:
        data = self.coordinator.data or {}
        value = data.get(DATA_KEY_KILL_GATE_PASSED)
        return value if isinstance(value, bool) else None

    @property
    def is_on(self) -> bool | None:
        # bool -> pass/fail; None (window not full, or scoreboard absent) ->
        # unknown, so the UI shows honest "insufficient data".
        return self._gate()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        sb = data.get(DATA_KEY_SCOREBOARD)
        sb = sb if isinstance(sb, dict) else {}
        return {
            "window_days": sb.get("window_days"),
            "scored_days": sb.get("scored_days"),
            "engine_daily_kwh_mae": sb.get("engine_daily_kwh_mae"),
            "engine_vs_best_baseline_pct": sb.get(
                "engine_vs_best_baseline_pct"
            ),
        }
