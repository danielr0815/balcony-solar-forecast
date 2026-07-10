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

The ``get_forecast`` service-with-response (SPEC §8) is registered from
``async_setup`` (see ``_services.py``); this module owns only its response
builder (:func:`_build_forecast_response`), after the pattern of
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

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, ServiceResponse, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_SP_AXIS_AZ_MAX,
    ATTR_SP_AXIS_AZ_MIN,
    ATTR_SP_AZIMUTH,
    ATTR_SP_HORIZON_AZIMUTH,
    ATTR_SP_SHADE_HORIZON,
    ATTR_SP_STATIC_HORIZON,
    ATTR_SP_SUN_ELEVATION,
    ATTR_SP_TIME,
    ATTR_SP_TRANSMITTANCE,
    ATTR_WATTS,
    ATTR_WH_PERIOD,
    ATTR_WH_PERIOD_P10,
    ATTR_WH_PERIOD_P50,
    ATTR_WH_PERIOD_P90,
    CONF_COMPARISON_SENSORS,
    DATA_KEY_DRIFT_MAE,
    DATA_KEY_INTRADAY_SCALAR,
    DATA_KEY_LEARNER_STATUS,
    DATA_KEY_QUANTILE_CURVES,
    DATA_KEY_SCOREBOARD,
    DOMAIN,
    FORECAST_RESP_KEY_P10,
    FORECAST_RESP_KEY_P50,
    FORECAST_RESP_KEY_P90,
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
    SENSOR_COMPARISON_DAILY_KWH_MAE_PREFIX,
    SENSOR_DRIFT_MAE_CORRECTED,
    SENSOR_ENERGY_D2,
    SENSOR_ENERGY_TODAY,
    SENSOR_ENERGY_TODAY_P10,
    SENSOR_ENERGY_TODAY_P90,
    SENSOR_ENERGY_TOMORROW,
    SENSOR_FORECAST_DAILY_KWH_MAE,
    SENSOR_FORECAST_HOURLY_MAE,
    SENSOR_FORECAST_VS_BEST_BASELINE_PCT,
    SENSOR_INTRADAY_SCALAR,
    SENSOR_MEASURED_DC_TOTAL,
    SENSOR_POWER_NOW,
    SENSOR_SHADE_PROFILE,
    STATUS_CACHED,
    STATUS_FRESH,
    STATUS_PHYSICS_FALLBACK,
    STATUS_UNAVAILABLE,
)
from .core.types import ComparisonConfig

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
_LOGGER = logging.getLogger(__name__)

_KEY_SLOT_STARTS = "slot_starts"
_KEY_PLANE_WATTS = "plane_watts"
_KEY_COMPUTED_AT = "computed_at"
# The three daily-energy keys, indexed by day offset (today=0).
_ENERGY_KEYS = ("energy_today_kwh", "energy_tomorrow_kwh", "energy_d2_kwh")

# Sub-keys inside the coordinator's DATA_KEY_SCOREBOARD summary dict (the shape
# core.scoreboard.scoreboard_summary emits; see that module's docstring). Read
# defensively — the scoreboard is optional/disable-able and the coordinator may
# not have populated it yet (validate-and-clamp: a missing field -> None).
_SB_ENGINE_DAILY_MAE = "engine_daily_kwh_mae"
_SB_ENGINE_HOURLY_MAE = "engine_hourly_mae"
_SB_COMPARISON_DAILY_MAE = "comparison_daily_kwh_mae"
_SB_ENGINE_VS_BEST_PCT = "engine_vs_best_baseline_pct"
# Sub-keys inside the DATA_KEY_QUANTILE_CURVES dict (15-min band Wh curves keyed
# by ISO-UTC slot start), matching the get_forecast response block names.
_Q_P10 = FORECAST_RESP_KEY_P10
_Q_P50 = FORECAST_RESP_KEY_P50
_Q_P90 = FORECAST_RESP_KEY_P90


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Balcony Solar Forecast sensors."""
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
        # --- v0.4 skill scoreboard (SPEC §9/§10) ---
        EngineDailyKwhMaeSensor(coordinator),
        EngineHourlyMaeSensor(coordinator),
        EngineVsBestBaselinePctSensor(coordinator),
        # --- v0.4 quantile bands (SPEC §6/§10): today's P10/P90 ---
        EnergyBandSensor(coordinator, SENSOR_ENERGY_TODAY_P10, _Q_P10),
        EnergyBandSensor(coordinator, SENSOR_ENERGY_TODAY_P90, _Q_P90),
        # --- Shade-profile diagram data (SPEC §15) ---
        ShadeProfileSensor(coordinator),
    ]

    # Measured site-total DC power (ground truth): the live sum of the planes'
    # actual_entity sensors. Added ONLY when at least one plane has an
    # actual_entity — with none configured there is nothing to sum, so the
    # sensor is omitted rather than published permanently unavailable.
    measured_ids = _measured_source_ids(coordinator)
    if measured_ids:
        entities.append(MeasuredDcTotalSensor(coordinator, measured_ids))

    # One MAE sensor per configured comparison forecast (SPEC §9/§10). The list
    # is read from the merged entry config (data + options); it ships EMPTY, so
    # a stock install adds zero comparison sensors. A rename produces a new
    # sensor (slug-keyed unique_id) rather than silently rewriting history.
    comparisons = _configured_comparisons(coordinator)
    for cmp in comparisons:
        entities.append(ComparisonDailyKwhMaeSensor(coordinator, cmp))

    # Prune ghost per-comparison sensors left behind by a rename/removal via the
    # options flow: any registry entry whose unique_id is a comparison-MAE slug
    # not in the CURRENT configured set would otherwise linger permanently
    # "unavailable" (a restored entity) on every rename. Remove those stale ids.
    _prune_stale_comparison_sensors(hass, entry, comparisons)

    async_add_entities(entities)
    # NOTE: the get_forecast service is registered in async_setup (see
    # _services.async_register_services, quality-scale action-setup); only its
    # response builder (_build_forecast_response below) lives in this module.


def _prune_stale_comparison_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    comparisons: tuple[ComparisonConfig, ...],
) -> None:
    """Remove registry entries for comparison sensors no longer configured.

    A comparison MAE sensor's unique_id is
    ``{entry_id}_{PREFIX}_{slug}``; after a rename/removal via the options flow
    the old slug's registry entry lingers "unavailable" forever unless pruned.
    We keep only the unique_ids of the CURRENTLY configured comparisons and drop
    the rest. Best-effort: never raises (a registry hiccup must not block setup).
    """
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        prefix = f"{entry.entry_id}_{SENSOR_COMPARISON_DAILY_KWH_MAE_PREFIX}_"
        keep = {
            f"{entry.entry_id}_{SENSOR_COMPARISON_DAILY_KWH_MAE_PREFIX}_{c.slug}"
            for c in comparisons
        }
        for reg_entry in er.async_entries_for_config_entry(
            registry, entry.entry_id
        ):
            uid = reg_entry.unique_id
            if uid.startswith(prefix) and uid not in keep:
                registry.async_remove(reg_entry.entity_id)
    except Exception:  # pragma: no cover - registry cleanup is best-effort
        _LOGGER.debug("Comparison-sensor prune skipped", exc_info=True)


def _configured_comparisons(coordinator: Any) -> tuple[ComparisonConfig, ...]:
    """Resolve the configured comparison forecasts for a coordinator's entry.

    Reads CONF_COMPARISON_SENSORS from the merged ``{**entry.data,
    **entry.options}`` (options win) and parses it leniently via
    ``ComparisonConfig.list_from_options`` (malformed / half-filled rows are
    dropped). Ships EMPTY (D-P9), so a stock install returns no comparisons.
    Never raises: an entry without options or a missing key yields ().
    """
    entry = getattr(coordinator, "entry", None)
    if entry is None:
        return ()
    merged = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
    return ComparisonConfig.list_from_options(merged.get(CONF_COMPARISON_SENSORS))


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
        entry_resp = {
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
        # v0.4 quantile bands (SPEC §6/§8): plane-agnostic TOTAL p10/p50/p90
        # 15-min + hourly Wh curves alongside the served (corrected) curve. Only
        # present when the engine issued bands this cycle; absent otherwise so a
        # quantiles-off / cold-start install simply omits the blocks rather than
        # fabricating a spread.
        bands = _band_blocks(data)
        if bands:
            entry_resp.update(bands)
        entries[eid] = entry_resp
    return {"entries": entries}


def _band_blocks(data: dict[str, Any]) -> dict[str, Any]:
    """Assemble the p10/p50/p90 15-min + hourly forecast-response blocks.

    Reads the coordinator's ``DATA_KEY_QUANTILE_CURVES`` (15-min band Wh curves
    keyed by ISO-UTC slot start) and rolls each up to hourly Wh. Returns a dict
    ``{p10: {"wh_period": {...}, "hourly": {...}}, p50: ..., p90: ...}`` — or an
    empty dict when no bands were issued (quantiles off / cold start), so the
    caller omits the blocks entirely. Pure; never raises on a malformed curve.
    """
    curves = data.get(DATA_KEY_QUANTILE_CURVES)
    if not isinstance(curves, dict) or not curves:
        return {}
    out: dict[str, Any] = {}
    for key in (_Q_P10, _Q_P50, _Q_P90):
        curve = curves.get(key)
        if not isinstance(curve, dict) or not curve:
            continue
        out[key] = {
            ATTR_WH_PERIOD: dict(curve),
            "hourly": _hourly_from_slots(curve),
        }
    return out


def _hourly_from_slots(slot_wh: dict[str, float]) -> dict[str, float]:
    """Roll a 15-min ``{iso_slot: Wh}`` curve up to ``{iso_hour: Wh}``.

    Buckets each slot's Wh into its containing UTC hour (truncating the slot
    start to the hour). Malformed keys/values are skipped so a diagnostic curve
    can never crash the response.
    """
    hourly: dict[str, float] = {}
    for iso, wh in slot_wh.items():
        parsed = dt_util.parse_datetime(iso) if isinstance(iso, str) else None
        if parsed is None or not isinstance(wh, (int, float)):
            continue
        hour_key = parsed.replace(minute=0, second=0, microsecond=0).isoformat()
        hourly[hour_key] = round(hourly.get(hour_key, 0.0) + float(wh), 2)
    return hourly


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
    # The bulky curve dicts (served + the three quantile bands) are all excluded
    # from the recorder (SPEC §8) via _unrecorded_attributes + recorder.py.
    _unrecorded_attributes = frozenset(
        {
            ATTR_WATTS,
            ATTR_WH_PERIOD,
            ATTR_WH_PERIOD_P10,
            ATTR_WH_PERIOD_P50,
            ATTR_WH_PERIOD_P90,
        }
    )

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
        # v0.4 band curves (SPEC §6/§8): 15-min P10/P90 Wh, sliced to this day
        # like the served curve. Empty when quantiles are off / cold-started, so
        # the attrs are simply empty dicts (no fabricated spread).
        curves = data.get(DATA_KEY_QUANTILE_CURVES)
        p10_all = curves.get(_Q_P10) if isinstance(curves, dict) else None
        p90_all = curves.get(_Q_P90) if isinstance(curves, dict) else None
        p10_all = p10_all if isinstance(p10_all, dict) else {}
        p90_all = p90_all if isinstance(p90_all, dict) else {}
        target = self._target_date()
        watts: dict[str, float] = {}
        wh_period: dict[str, float] = {}
        wh_p10: dict[str, float] = {}
        wh_p90: dict[str, float] = {}
        for iso in data.get(_KEY_SLOT_STARTS, []):
            local = _local_date_of(iso)
            if local != target:
                continue
            if iso in watts_all:
                watts[iso] = watts_all[iso]
            if iso in wh_all:
                wh_period[iso] = wh_all[iso]
            if iso in p10_all:
                wh_p10[iso] = p10_all[iso]
            if iso in p90_all:
                wh_p90[iso] = p90_all[iso]
        return {
            ATTR_WATTS: watts,
            ATTR_WH_PERIOD: wh_period,
            ATTR_WH_PERIOD_P10: wh_p10,
            ATTR_WH_PERIOD_P90: wh_p90,
        }


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


class MeasuredDcTotalSensor(BalconyForecastEntity, SensorEntity):
    """Measured site-total DC power: the live sum of the per-module measured
    DC-power sensors (SPEC §14.3 ground truth) — the time-accurate partner of
    the forecast power sensor.

    Source entities are the configured planes' ``actual_entity`` ids (planes
    without one skipped, de-duplicated, plane order preserved); the sensor is
    not created at all when that list is empty (nothing to sum). State is the
    sum of the sources' current numeric states; a source that is unknown /
    unavailable / non-numeric is skipped, so a partial DTU dropout reads as the
    reduced live total rather than going blank.

    Two design points worth calling out:
      * ``available`` is DECOUPLED from the coordinator — True while AT LEAST ONE
        source reports a numeric value, unavailable only when every source is
        dead. Measured production is ground truth and must keep being recorded
        even while the FORECAST is degraded/unavailable; the base class ties
        availability to ``coordinator.last_update_success``, so this override is
        essential.
      * ``MEASUREMENT`` + ``POWER`` + ``W`` => Home Assistant keeps long-term
        statistics, and there are no bulky curve attributes, so this sensor is
        deliberately NOT excluded from the recorder: its history IS the point.

    It never reads ``coordinator.data``: it subscribes to the source entities
    directly via ``async_track_state_change_event`` (auto-unsubscribed on removal
    through ``async_on_remove`` — HA best practice, no manual teardown) and
    recomputes on every change plus once on add, so it is fully independent of
    the forecast recompute cadence.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: Any, source_ids: list[str]) -> None:
        super().__init__(coordinator, SENSOR_MEASURED_DC_TOTAL)
        self._source_ids = list(source_ids)
        self._value: float | None = None
        self._reporting = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Track the source sensors directly (ground truth is independent of the
        # forecast coordinator's tick). async_on_remove auto-unsubscribes when
        # the entity is removed — no manual unsub bookkeeping needed.
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._source_ids, self._handle_source_event
            )
        )
        # Seed the cached state before the first write so it is correct on add.
        self._recompute()

    @callback
    def _handle_source_event(self, event: Any) -> None:
        """Recompute + publish on any source-sensor state change."""
        self._recompute()
        self.async_write_ha_state()

    def _recompute(self) -> None:
        """Cache the summed value + reporting count from the current states."""
        total = 0.0
        reporting = 0
        for entity_id in self._source_ids:
            value = _numeric_state(self.hass.states.get(entity_id))
            if value is None:
                continue
            total += value
            reporting += 1
        self._reporting = reporting
        self._value = round(total, 1) if reporting else None

    @property
    def available(self) -> bool:
        # Ground truth: available while >=1 source reports, decoupled from the
        # coordinator's last_update_success (panels produce even when the
        # forecast is unavailable).
        return self._reporting > 0

    @property
    def native_value(self) -> float | None:
        return self._value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "channels_total": len(self._source_ids),
            "channels_reporting": self._reporting,
            "sources": list(self._source_ids),
        }


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
# v0.4 skill-scoreboard diagnostics (SPEC §9/§10 — the kill-gate metrics)
# ---------------------------------------------------------------------------


class _ScoreboardSensor(_DiagnosticSensor):
    """Base for the always-available scoreboard metric sensors.

    Reads the coordinator's ``DATA_KEY_SCOREBOARD`` summary dict (the shape
    ``core.scoreboard.scoreboard_summary`` emits). All fields are read
    defensively: the scoreboard is optional and disable-able, and the
    coordinator may not have populated it yet, so a missing summary or field
    yields ``None`` (never a fabricated zero — SPEC §9).
    """

    def _summary(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        sb = data.get(DATA_KEY_SCOREBOARD)
        return sb if isinstance(sb, dict) else {}


class EngineDailyKwhMaeSensor(_ScoreboardSensor):
    """Engine daily-kWh MAE over the rolling window (SPEC §10 primary metric).

    The engine forecast AS ISSUED for each scored day, mean absolute daily-kWh
    error vs. the measured site energy. kWh unit; no device_class (an MAE is not
    a cumulative energy quantity). ``None`` until the window has a scored day.
    The window length + scored-day count ride along as attributes.
    """

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-line"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, SENSOR_FORECAST_DAILY_KWH_MAE)

    @property
    def native_value(self) -> float | None:
        value = self._summary().get(_SB_ENGINE_DAILY_MAE)
        return None if value is None else round(float(value), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sb = self._summary()
        return {
            "window_days": sb.get("window_days"),
            "scored_days": sb.get("scored_days"),
        }


class EngineHourlyMaeSensor(_ScoreboardSensor):
    """Engine hourly MAE over the window (SPEC §10 second metric).

    Mean per-daylight-hour Wh error of the issued corrected curve vs. measured.
    Wh unit; ``None`` until at least one day in the window has an hourly MAE.
    """

    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-bell-curve"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, SENSOR_FORECAST_HOURLY_MAE)

    @property
    def native_value(self) -> float | None:
        value = self._summary().get(_SB_ENGINE_HOURLY_MAE)
        return None if value is None else round(float(value), 1)


class EngineVsBestBaselinePctSensor(_ScoreboardSensor):
    """Percent the engine beats the BEST baseline on daily-kWh MAE (SPEC §10).

    Positive == engine better (smaller error) than the best configured
    comparison. ``None`` when there is no scored engine day, no comparison with a
    scored day, or an undefined ratio. Backs the dashboard gauge.
    """

    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:trophy-outline"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, SENSOR_FORECAST_VS_BEST_BASELINE_PCT)

    @property
    def native_value(self) -> float | None:
        value = self._summary().get(_SB_ENGINE_VS_BEST_PCT)
        return None if value is None else round(float(value), 1)


class ComparisonDailyKwhMaeSensor(_ScoreboardSensor):
    """Daily-kWh MAE of one configured external comparison forecast (SPEC §10).

    One instance per ``CONF_COMPARISON_SENSORS`` entry. The object_id is suffixed
    with the comparison's stable slug so a rename mints a new sensor rather than
    rewriting history; the friendly name carries the operator's label as an
    attribute (the entity ``name`` translation is generic). Reads its own MAE
    out of the scoreboard summary's ``comparison_daily_kwh_mae`` map by the
    comparison NAME (the key core.scoreboard uses). ``None`` until that
    comparison has a scored day in the window (a comparison added mid-window is
    absent, not zero).
    """

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-line-variant"

    def __init__(self, coordinator: Any, comparison: ComparisonConfig) -> None:
        super().__init__(
            coordinator,
            f"{SENSOR_COMPARISON_DAILY_KWH_MAE_PREFIX}_{comparison.slug}",
        )
        self._comparison = comparison
        # A per-comparison entity name so the dynamic sensors are distinguishable
        # in the UI (the shared translation_key would otherwise name them all
        # identically). has_entity_name stays True: this becomes the object name.
        self._attr_translation_key = None
        self._attr_name = f"Comparison daily kWh MAE {comparison.name}"
        # Pin the object_id to the documented dashboard id
        # `…_comparison_daily_kwh_mae_<slug>` via the SUPPORTED integration-
        # suggested path: a pre-set ``entity_id`` (HA 2026 stores it as the
        # suggested object id for new registry entries). The formerly used
        # ``_attr_suggested_object_id`` does not exist in HA — it was silently
        # ignored and the id fell back to slugifying the name, which diverges
        # from ComparisonConfig.slug for non-ASCII labels ("Süd"). The slug is
        # strictly ASCII (types.ComparisonConfig.slug), so this entity_id is
        # always valid.
        self.entity_id = (
            f"sensor.{DOMAIN}_"
            f"{SENSOR_COMPARISON_DAILY_KWH_MAE_PREFIX}_{comparison.slug}"
        )

    def _comparison_mae_map(self) -> dict[str, Any]:
        cmp_map = self._summary().get(_SB_COMPARISON_DAILY_MAE)
        return cmp_map if isinstance(cmp_map, dict) else {}

    @property
    def native_value(self) -> float | None:
        value = self._comparison_mae_map().get(self._comparison.name)
        return None if value is None else round(float(value), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "comparison_name": self._comparison.name,
            "daily_entity": self._comparison.daily_entity,
        }


# ---------------------------------------------------------------------------
# v0.4 quantile daily band sensors (SPEC §6/§10 — today's P10 / P90 energy)
# ---------------------------------------------------------------------------


class EnergyBandSensor(BalconyForecastEntity, SensorEntity):
    """Today's forecast energy at one quantile band (P10 or P90), kWh.

    Sums the 15-min band Wh curve (``DATA_KEY_QUANTILE_CURVES[band]``) over the
    slots that fall on the local *today*, mirroring the served energy sensor.
    A *forecast*, so no ``state_class`` (never feeds long-term statistics, like
    the served energy sensors). ``None`` when no band was issued (quantiles off
    / cold start) — the band honestly collapses rather than fabricating a
    spread. Follows the coordinator's availability (not a diagnostic).
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: Any, key: str, band: str) -> None:
        super().__init__(coordinator, key)
        self._band = band

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        curves = data.get(DATA_KEY_QUANTILE_CURVES)
        curve = curves.get(self._band) if isinstance(curves, dict) else None
        if not isinstance(curve, dict) or not curve:
            return None
        target = dt_util.now().date()
        total_wh = 0.0
        seen = False
        for iso, wh in curve.items():
            if _local_date_of(iso) != target or not isinstance(wh, (int, float)):
                continue
            total_wh += float(wh)
            seen = True
        return round(total_wh / 1000.0, 3) if seen else None


# ---------------------------------------------------------------------------
# Shade-profile diagram (sun path vs learned shade) — SPEC §15
# ---------------------------------------------------------------------------


class ShadeProfileSensor(BalconyForecastEntity, SensorEntity):
    """Sun-path + learned-shade diagram data for the selected module/date.

    State is the shaded fraction of daylight (%): the share of daylight samples
    whose *effective* beam transmittance (engine-exact static-horizon gate
    blended with the learned shademap) is below the shade threshold, for the
    module chosen by ``select.…_shade_profile_module`` on the date chosen by
    ``date.…_shade_profile_date``. The full curve arrays — the sun path
    (azimuth / elevation / transmittance) plus the static config horizon and the
    learned shade horizon on an azimuth grid — ride along as attributes for an
    ApexCharts card (docs/DASHBOARD.md), excluded from the recorder like the
    energy-curve dicts. Diagnostic + always available: the diagram is pure
    geometry and must render even while the live forecast is unavailable.

    The profile is rebuilt on every coordinator update (nightly shademap
    changes) and whenever the select/date entities push a new selection (they
    call ``coordinator.set_shade_profile_*`` -> ``async_update_listeners``).
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:sun-angle"
    # The bulky curve arrays are kept out of the recorder (mirrors the energy
    # sensors' curve dicts) via _unrecorded_attributes + recorder.py. The two
    # year-stable axis bounds ride along: constant site geometry, so their
    # recorder history is pure noise.
    _unrecorded_attributes = frozenset(
        {
            ATTR_SP_TIME,
            ATTR_SP_AZIMUTH,
            ATTR_SP_SUN_ELEVATION,
            ATTR_SP_TRANSMITTANCE,
            ATTR_SP_HORIZON_AZIMUTH,
            ATTR_SP_STATIC_HORIZON,
            ATTR_SP_SHADE_HORIZON,
            ATTR_SP_AXIS_AZ_MIN,
            ATTR_SP_AXIS_AZ_MAX,
        }
    )

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, SENSOR_SHADE_PROFILE)
        self._data: dict[str, Any] = {}

    @property
    def available(self) -> bool:
        # Diagnostic: the diagram is pure geometry, so it stays available even
        # when the live forecast is unavailable.
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._recompute()

    @callback
    def _handle_coordinator_update(self) -> None:
        # Recompute once per update (tick or selection change), then write state;
        # both properties read the cached result so there is a single build.
        self._recompute()
        super()._handle_coordinator_update()

    def _recompute(self) -> None:
        builder = getattr(self.coordinator, "build_shade_profile", None)
        if not callable(builder):
            self._data = {}
            return
        try:
            self._data = builder() or {}
        except Exception:  # pragma: no cover - the diagram must never crash HA
            _LOGGER.debug("Shade-profile build failed", exc_info=True)
            self._data = {}

    @property
    def native_value(self) -> float | None:
        if not self._data.get("sample_count"):
            return None
        frac = self._data.get("shaded_fraction")
        return None if frac is None else round(float(frac) * 100.0, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._data)


# ---------------------------------------------------------------------------
# Small pure helpers (no HA dependency beyond dt_util for local-tz handling).
# ---------------------------------------------------------------------------


def _local_date_of(iso: str):
    """Local calendar date of an ISO-8601 UTC slot-start string, or None."""
    parsed = dt_util.parse_datetime(iso)
    if parsed is None:
        return None
    return dt_util.as_local(parsed).date()


def _numeric_state(state: Any) -> float | None:
    """Current numeric value of a source state, or None if unusable.

    None for a missing state, an unknown/unavailable state, or a non-numeric
    value — the caller then skips that channel when summing.
    """
    if state is None:
        return None
    raw = getattr(state, "state", None)
    if raw is None or raw in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _measured_source_ids(coordinator: Any) -> list[str]:
    """Ordered, de-duplicated measured DC-power entity ids from the site planes.

    Each plane's ``actual_entity`` (the HA sensor of that module's measured DC
    power); planes without one are skipped and duplicates dropped while plane
    order is preserved. Empty when no plane has an ``actual_entity`` — the
    platform then omits the summing sensor entirely (nothing to sum).
    """
    site = getattr(coordinator, "_site", None)
    if site is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for plane in getattr(site, "planes", ()):
        entity_id = getattr(plane, "actual_entity", None)
        if isinstance(entity_id, str) and entity_id and entity_id not in seen:
            seen.add(entity_id)
            out.append(entity_id)
    return out
