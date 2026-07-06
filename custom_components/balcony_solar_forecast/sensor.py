"""Sensor platform for the Balcony Solar Forecast integration.

Consumer-facing outputs (SPEC §8):

  * ``energy_production_today`` / ``_tomorrow`` / ``_d2`` — daily kWh with the
    full 15-min curve as ``watts`` / ``wh_period`` dict attributes (excluded
    from the recorder via :mod:`recorder`). Deliberately compatible with the
    rany2 open-meteo-solar-forecast entity pattern so battery_manager only
    re-points its three forecast entity pickers, no code change.
  * ``power_production_now`` — instantaneous site AC power (W, measurement).
  * diagnostic ``last_fetch_age_min`` and ``source_status`` — the degradation
    ladder made visible (SPEC §7).

Also registers the ``get_forecast`` service-with-response (SPEC §8) once, on
the first entry, returning the 15-min and hourly curves after the pattern of
``weather.get_forecasts``.

Coordinator contract read here (glue owns ``coordinator.py``): ``self.data``
is the flat dict documented on ``BalconySolarCoordinator._build_data`` —
``status``, ``degraded``, ``weather_age_seconds``, ``power_now_w``,
``energy_{today,tomorrow,d2}_kwh``, ``watts`` / ``wh_period`` (site-total
15-min curves keyed by ISO-UTC slot start), ``hourly_wh``, ``daily_kwh``,
``slot_starts``, ``plane_watts`` and ``computed_at``. Unavailable is signalled
by the coordinator raising ``UpdateFailed`` (so ``last_update_success`` is
False), never by a stale value (SPEC §7). This file imports HA; the core stays
HA-free.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import (
    HomeAssistant,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_WATTS,
    ATTR_WH_PERIOD,
    DATA_KEY_DRIFT_MAE,
    DATA_KEY_INTRADAY_SCALAR,
    DATA_KEY_LEARNER_STATUS,
    DOMAIN,
    INTEGRATION_NAME,
    INTEGRATION_VERSION,
    LEARNER_LAYER_DAY_AHEAD,
    LEARNER_LAYER_FAST,
    LEARNER_LAYER_SLOW,
    LEARNER_STATUS_ACTIVE,
    LEARNER_STATUS_DISABLED_BY_DRIFT,
    LEARNER_STATUS_FROZEN,
    LEARNER_STATUS_OFF,
    LEARNER_STATUS_VALUES,
    SENSOR_DRIFT_MAE_CORRECTED,
    SENSOR_ENERGY_D2,
    SENSOR_ENERGY_TODAY,
    SENSOR_ENERGY_TOMORROW,
    SENSOR_INTRADAY_SCALAR,
    SENSOR_POWER_NOW,
    SERVICE_GET_FORECAST,
    STATUS_CACHED,
    STATUS_FRESH,
    STATUS_PHYSICS_FALLBACK,
    STATUS_UNAVAILABLE,
)

# Diagnostic sensor keys (owned here; not part of the consumer contract).
SENSOR_LAST_FETCH_AGE = "last_fetch_age_min"
SENSOR_SOURCE_STATUS = "source_status"

# Per-layer learner status values + layer names (SPEC §5) live in const now
# (shared with the coordinator, which writes exactly these strings). Re-exported
# here so the display code and its tests keep importing them from this module.
__all__ = [
    "LEARNER_STATUS_ACTIVE",
    "LEARNER_STATUS_OFF",
    "LEARNER_STATUS_DISABLED_BY_DRIFT",
    "LEARNER_STATUS_FROZEN",
    "LEARNER_STATUS_VALUES",
    "LEARNER_LAYER_FAST",
    "LEARNER_LAYER_SLOW",
    "LEARNER_LAYER_DAY_AHEAD",
]

# Diagnostic sensor keys for the per-layer learner status enums.
SENSOR_LEARNER_STATUS_FAST = "learner_status_fast"
SENSOR_LEARNER_STATUS_SLOW = "learner_status_slow"
SENSOR_LEARNER_STATUS_DAY_AHEAD = "learner_status_day_ahead"

# Data-dict keys from the coordinator (see module docstring / _build_data).
_KEY_STATUS = "status"
_KEY_WEATHER_AGE_S = "weather_age_seconds"
_KEY_POWER_NOW_W = "power_now_w"
_KEY_WATTS = "watts"
_KEY_WH_PERIOD = "wh_period"
_KEY_HOURLY_WH = "hourly_wh"
_KEY_SLOT_STARTS = "slot_starts"
_KEY_PLANE_WATTS = "plane_watts"
_KEY_COMPUTED_AT = "computed_at"
# The three daily-energy keys, indexed by day offset (today=0).
_ENERGY_KEYS = ("energy_today_kwh", "energy_tomorrow_kwh", "energy_d2_kwh")

# Optional service field: restrict the returned curve to one config entry.
SERVICE_GET_FORECAST_SCHEMA = vol.Schema({vol.Optional("entry_id"): str})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Balcony Solar Forecast sensors and the get_forecast service."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        EnergyProductionSensor(coordinator, SENSOR_ENERGY_TODAY, day_offset=0),
        EnergyProductionSensor(coordinator, SENSOR_ENERGY_TOMORROW, day_offset=1),
        EnergyProductionSensor(coordinator, SENSOR_ENERGY_D2, day_offset=2),
        PowerNowSensor(coordinator),
        LastFetchAgeSensor(coordinator),
        SourceStatusSensor(coordinator),
        # --- learning-layer diagnostics (v0.2.0 + v0.3.0, SPEC §5) ---
        IntradayScalarSensor(coordinator),
        DriftMaeCorrectedSensor(coordinator),
        LearnerStatusSensor(
            coordinator, SENSOR_LEARNER_STATUS_FAST, LEARNER_LAYER_FAST
        ),
        LearnerStatusSensor(
            coordinator, SENSOR_LEARNER_STATUS_SLOW, LEARNER_LAYER_SLOW
        ),
        LearnerStatusSensor(
            coordinator,
            SENSOR_LEARNER_STATUS_DAY_AHEAD,
            LEARNER_LAYER_DAY_AHEAD,
        ),
    ]
    async_add_entities(entities)

    # Register the response service exactly once for the whole integration.
    if not hass.services.has_service(DOMAIN, SERVICE_GET_FORECAST):

        async def _get_forecast(call) -> ServiceResponse:
            return _build_forecast_response(hass, call.data.get("entry_id"))

        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_FORECAST,
            _get_forecast,
            schema=SERVICE_GET_FORECAST_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )


def _build_forecast_response(
    hass: HomeAssistant, entry_id: str | None
) -> ServiceResponse:
    """Assemble the get_forecast response for one or all entries.

    Returns ``{entries: {entry_id: {planes, slot_starts, total_15min,
    total_hourly, issued_at}}}``. ``planes`` maps plane name -> list of 15-min
    watts aligned to ``slot_starts``. All read from the coordinator's flat
    ``self.data`` dict; a coordinator without a current forecast yields empty
    curves rather than a stale one (SPEC §7).
    """
    entries: dict[str, Any] = {}
    store = hass.data.get(DOMAIN, {})
    for eid, coordinator in store.items():
        if entry_id is not None and eid != entry_id:
            continue
        data = coordinator.data or {}
        if not data:
            entries[eid] = _empty_forecast_entry()
            continue
        entries[eid] = {
            "slot_starts": list(data.get(_KEY_SLOT_STARTS, [])),
            "planes": {
                name: list(watts)
                for name, watts in (data.get(_KEY_PLANE_WATTS) or {}).items()
            },
            "total_15min": [
                w for _, w in _iter_curve(data)
            ],
            "total_hourly": dict(data.get(_KEY_HOURLY_WH) or {}),
            "issued_at": data.get(_KEY_COMPUTED_AT),
        }
    return {"entries": entries}


def _empty_forecast_entry() -> dict[str, Any]:
    return {
        "planes": {},
        "slot_starts": [],
        "total_15min": [],
        "total_hourly": {},
        "issued_at": None,
    }


def _iter_curve(data: dict[str, Any]):
    """Yield ``(iso_start, watts)`` pairs of the site-total 15-min curve.

    Ordered by ``slot_starts`` when present (the ``watts`` dict is keyed by
    the same ISO strings); falls back to the dict's own order otherwise.
    """
    watts = data.get(_KEY_WATTS) or {}
    starts = data.get(_KEY_SLOT_STARTS)
    if starts:
        for iso in starts:
            if iso in watts:
                yield iso, watts[iso]
    else:
        yield from watts.items()


class BalconyForecastEntity(CoordinatorEntity):
    """Common device grouping + honest availability for all our entities.

    Availability follows the coordinator: an entity is available while the
    last update succeeded (the coordinator raises ``UpdateFailed`` for the
    unavailable rung of the degradation ladder — SPEC §7). Diagnostic
    entities override this to stay available so the operator can always read
    *why* the forecast is degraded.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: Any, key: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.entry.entry_id
        self._key = key
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=INTEGRATION_NAME,
            manufacturer="Balcony Solar Forecast",
            model="Multi-plane PV forecast",
            sw_version=INTEGRATION_VERSION,
        )


class EnergyProductionSensor(BalconyForecastEntity, SensorEntity):
    """Daily forecast energy (kWh) with the full 15-min curve as attributes.

    No ``state_class``: a *forecast* must never feed long-term statistics
    (matches the rany2 integration and battery_manager's forecast sensor).
    The bulky ``watts`` / ``wh_period`` dicts are kept out of the recorder via
    :mod:`recorder` ``exclude_attributes`` (and ``_unrecorded_attributes``).

    State is the coordinator's day roll-up (``energy_{today,tomorrow,d2}_kwh``,
    already bucketed to local calendar days). The attribute curve is this
    sensor's own day sliced out of the site-total ``watts`` / ``wh_period``
    dicts by local date, so each daily sensor carries just its day.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power"
    _unrecorded_attributes = frozenset({ATTR_WATTS, ATTR_WH_PERIOD})

    def __init__(self, coordinator: Any, key: str, day_offset: int) -> None:
        super().__init__(coordinator, key)
        self._day_offset = day_offset
        self._energy_key = _ENERGY_KEYS[day_offset]

    def _target_date(self):
        """Local calendar date this sensor sums (today + offset)."""
        return dt_util.now().date() + timedelta(days=self._day_offset)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        value = data.get(self._energy_key)
        return None if value is None else round(float(value), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        watts_all = data.get(_KEY_WATTS) or {}
        wh_all = data.get(_KEY_WH_PERIOD) or {}
        target = self._target_date()
        watts: dict[str, float] = {}
        wh_period: dict[str, float] = {}
        for iso in data.get(_KEY_SLOT_STARTS, []):
            local = _local_date_of(iso)
            if local != target:
                continue
            if iso in watts_all:
                watts[iso] = watts_all[iso]
            if iso in wh_all:
                wh_period[iso] = wh_all[iso]
        return {ATTR_WATTS: watts, ATTR_WH_PERIOD: wh_period}


class PowerNowSensor(BalconyForecastEntity, SensorEntity):
    """Instantaneous forecast AC power for the slot covering *now* (W)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, SENSOR_POWER_NOW)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        value = data.get(_KEY_POWER_NOW_W)
        return None if value is None else round(float(value), 1)


class _DiagnosticSensor(BalconyForecastEntity, SensorEntity):
    """Base for always-available diagnostic sensors (SPEC §7 visibility)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        # Diagnostics stay available even when the forecast is unavailable, so
        # the operator can always see the age and the reason. We only require
        # the coordinator entity to be wired (not its last update to succeed).
        return True


class LastFetchAgeSensor(_DiagnosticSensor):
    """Minutes since the last good Open-Meteo payload (weather-image age)."""

    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, SENSOR_LAST_FETCH_AGE)

    @property
    def native_value(self) -> float | None:
        # Prefer the coordinator's LIVE age so this always-available diagnostic
        # keeps climbing during an outage instead of freezing at the last
        # computed value (SPEC §7). Fall back to the frozen snapshot only if the
        # live property is unavailable.
        age_s = getattr(self.coordinator, "weather_age_seconds_live", None)
        if age_s is None:
            data = self.coordinator.data or {}
            age_s = data.get(_KEY_WEATHER_AGE_S)
        return None if age_s is None else round(float(age_s) / 60.0, 1)


class SourceStatusSensor(_DiagnosticSensor):
    """Current rung of the degradation ladder (fresh/cached/physics/unavail)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_icon = "mdi:stairs"
    _attr_options = [
        STATUS_FRESH,
        STATUS_CACHED,
        STATUS_PHYSICS_FALLBACK,
        STATUS_UNAVAILABLE,
    ]

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, SENSOR_SOURCE_STATUS)

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        if not self.coordinator.last_update_success:
            # No fresh/cached/physics curve was issued this cycle.
            return STATUS_UNAVAILABLE
        return data.get(_KEY_STATUS)


# ---------------------------------------------------------------------------
# Learning-layer diagnostics (v0.2.0 + v0.3.0, SPEC §5, §9)
# ---------------------------------------------------------------------------


class IntradayScalarSensor(_DiagnosticSensor):
    """The FAST learner's currently applied intraday clear-sky-index scalar.

    Unitless multiplier in [INTRADAY_SCALAR_MIN, MAX]; 1.0 == no correction.
    Transient (re-inits to 1.0 on restart, never persisted — SPEC §5), so it is
    read straight from the coordinator's live ``self.data`` snapshot. Stays
    available even during a degraded forecast so the operator can see the
    learner is neutralised.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:tune-variant"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, SENSOR_INTRADAY_SCALAR)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        value = data.get(DATA_KEY_INTRADAY_SCALAR)
        return None if value is None else round(float(value), 3)


class DriftMaeCorrectedSensor(_DiagnosticSensor):
    """Rolling 7-day daylight MAE of the CORRECTED (served) curve (SPEC §5).

    The drift monitor's headline number: mean absolute error of the learner-
    corrected forecast against measured production over the trailing
    DRIFT_WINDOW_DAYS. Wh unit. The paired raw-physics and baseline MAE ride
    along as attributes so the operator can see at a glance whether the
    learners are winning (corrected < raw) or losing (which auto-disables the
    layer after DRIFT_LOSS_STREAK_DAYS).
    """

    # No device_class: an MAE is not an energy quantity, and ENERGY +
    # MEASUREMENT is an invalid combination HA rejects at entity-add time
    # (energy needs total/total_increasing). Keep the Wh unit + measurement
    # state class like the other error-metric sensors (sensor:427).
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-bell-curve-cumulative"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, SENSOR_DRIFT_MAE_CORRECTED)

    def _mae(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        mae = data.get(DATA_KEY_DRIFT_MAE)
        return mae if isinstance(mae, dict) else {}

    @property
    def native_value(self) -> float | None:
        value = self._mae().get("corrected")
        return None if value is None else round(float(value), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        mae = self._mae()

        def _r(key: str) -> float | None:
            v = mae.get(key)
            return None if v is None else round(float(v), 1)

        return {
            "raw_mae": _r("raw"),
            "corrected_mae": _r("corrected"),
            "baseline_mae": _r("baseline"),
        }


class LearnerStatusSensor(_DiagnosticSensor):
    """Per-layer learner status ENUM (active / off / disabled_by_drift / frozen).

    One instance per learner layer (fast intraday, slow shademap, day-ahead
    bias). Reads ``self.data[DATA_KEY_LEARNER_STATUS][<layer>]`` — a string the
    coordinator sets from the resolved kill switch + drift-disable flag +
    collapse freeze (SPEC §5). Unknown/missing values report ``None`` (unknown)
    rather than inventing a status. Always available so the operator can always
    see why a layer is or is not correcting.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_icon = "mdi:brain"
    _attr_options = list(LEARNER_STATUS_VALUES)

    def __init__(self, coordinator: Any, key: str, layer: str) -> None:
        super().__init__(coordinator, key)
        self._layer = layer

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        status_map = data.get(DATA_KEY_LEARNER_STATUS)
        if not isinstance(status_map, dict):
            return None
        value = status_map.get(self._layer)
        # Only advertise values we declared as ENUM options; anything else is
        # treated as unknown so HA does not log an invalid-enum-state warning.
        if value in LEARNER_STATUS_VALUES:
            return value
        return None


# ---------------------------------------------------------------------------
# Small pure helpers (no HA dependency beyond dt_util for local-tz handling).
# ---------------------------------------------------------------------------


def _local_date_of(iso: str):
    """Local calendar date of an ISO-8601 UTC slot-start string, or None."""
    parsed = dt_util.parse_datetime(iso)
    if parsed is None:
        return None
    return dt_util.as_local(parsed).date()
