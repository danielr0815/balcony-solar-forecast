"""Shared pure helpers for the coordinator's concern-group modules.

Owner: glue (shared utilities). Small, HA-light building blocks lifted verbatim
out of ``coordinator.py`` so the extracted concern modules (``_actuals``,
``_nightly``, ``_scoreboard_glue``) and the coordinator itself share ONE copy:

  * ISO / hour-key formatting (``_iso`` / ``_hour_key`` / ``_round3``);
  * the 15-min slot walk (``_power_at`` / ``_slot_index_at`` / ``_power_now`` /
    ``_raw_power_now``);
  * the LOCAL calendar-day roll-ups (``_local_daily_kwh`` /
    ``_daily_kwh_from_hourly`` / ``_filter_hourly_to_local_day``);
  * the frozen-dataclass copy ``_replace_drift``;
  * the live-actual state guard ``_usable_power`` (SPEC §5 label gates) with its
    ``_UNUSABLE_STATES`` constant.

Pure functions only — no coordinator state, no I/O. ``coordinator.py`` re-imports
every name here so the historical ``coordinator._usable_power`` /
``coordinator._filter_hourly_to_local_day`` / … import paths (used by the tests)
keep resolving.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homeassistant.core import State
from homeassistant.util import dt as dt_util

from .const import LABEL_FROZEN_STALE_SECONDS
from .core import DriftState, ForecastResult

# Live-actual state guards: states we never treat as a measurement.
_UNUSABLE_STATES = ("unknown", "unavailable", "none", "")


def _replace_drift(state: DriftState, **changes) -> DriftState:
    """Return a copy of a DriftState with fields replaced (frozen dataclass)."""
    from dataclasses import replace

    return replace(state, **changes)


def _usable_power(state: State | None, now: datetime) -> float | None:
    """Numeric live power from a state, or None if unusable / frozen.

    Guards (SPEC §5 label gates applied live): missing state, unknown /
    unavailable / empty state, non-numeric value, or a stale reading whose
    ``last_updated`` is older than LABEL_FROZEN_STALE_SECONDS (a frozen sensor
    holding an old value — treated as missing). A fresh zero is a legitimate
    night/shade reading and IS usable.
    """
    if state is None:
        return None
    raw = (state.state or "").strip().lower()
    if raw in _UNUSABLE_STATES:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    last_updated = getattr(state, "last_updated", None)
    if last_updated is not None:
        age = (dt_util.as_utc(now) - dt_util.as_utc(last_updated)).total_seconds()
        if age > LABEL_FROZEN_STALE_SECONDS:
            # Frozen: the sensor stopped reporting (value held). Skip it.
            return None
    return value


def _iso(dt: datetime) -> str:
    return dt_util.as_utc(dt).isoformat()


def _hour_key(dt: datetime) -> str:
    """ISO-UTC hour-start key for an aligned slot start."""

    return (
        dt_util.as_utc(dt)
        .replace(minute=0, second=0, microsecond=0)
        .astimezone(UTC)
        .isoformat()
    )


def _round3(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _power_at(slot_starts, watts, now: datetime) -> float:
    """Instantaneous power at the 15-min slot containing ``now`` (shared walk)."""
    now_utc = dt_util.as_utc(now)
    slot = timedelta(minutes=15)
    for start, w in zip(slot_starts, watts, strict=False):
        start_utc = dt_util.as_utc(start)
        if start_utc <= now_utc < start_utc + slot:
            return w
        if start_utc > now_utc:
            break
    return 0.0


def _slot_index_at(slot_starts, now: datetime) -> int | None:
    """Index of the 15-min slot containing ``now``, or None if out of range."""
    now_utc = dt_util.as_utc(now)
    slot = timedelta(minutes=15)
    for i, start in enumerate(slot_starts):
        start_utc = dt_util.as_utc(start)
        if start_utc <= now_utc < start_utc + slot:
            return i
        if start_utc > now_utc:
            break
    return None


def _power_now(result: ForecastResult, now: datetime) -> float:
    """Instantaneous site power at the 15-min slot containing ``now``."""
    return _power_at(result.slot_starts, result.total_watts, now)


def _raw_power_now(result: ForecastResult, now: datetime) -> float:
    """Instantaneous RAW site power at the slot containing ``now``."""
    series = result.raw_total_watts or result.total_watts
    return _power_at(result.slot_starts, series, now)


def _local_daily_kwh(result: ForecastResult) -> dict[str, float]:
    """Roll the 15-min curve up to LOCAL calendar-day kWh."""
    daily: dict[str, float] = {}
    for start, watts in zip(result.slot_starts, result.total_watts, strict=False):
        local_day = dt_util.as_local(dt_util.as_utc(start)).date().isoformat()
        daily[local_day] = daily.get(local_day, 0.0) + watts * 0.25 / 1000.0
    return {k: round(v, 3) for k, v in daily.items()}


def _filter_hourly_to_local_day(
    hourly_wh: dict[str, float], iso_day: str
) -> dict[str, float]:
    """Keep only hour keys whose LOCAL calendar date equals ``iso_day``.

    The issued curves span the full FORECAST_DAYS window; every trainer/guard
    compares them against ONE day of measured actuals, so they must only ever
    see that day's hours (a 22:00 UTC hour belongs to the NEXT local day in
    CET/CEST — bucket by local date, exactly like _daily_kwh_from_hourly).
    """
    out: dict[str, float] = {}
    for hkey, wh in hourly_wh.items():
        dt = dt_util.parse_datetime(hkey)
        if dt is None:
            continue
        if dt_util.as_local(dt_util.as_utc(dt)).date().isoformat() == iso_day:
            out[hkey] = float(wh)
    return out


def _daily_kwh_from_hourly(hourly_wh: dict[str, float]) -> dict[str, float]:
    """Roll an ISO-UTC-hour Wh curve up to LOCAL calendar-day kWh."""
    daily: dict[str, float] = {}
    for hkey, wh in hourly_wh.items():
        dt = dt_util.parse_datetime(hkey)
        if dt is None:
            continue
        local_day = dt_util.as_local(dt_util.as_utc(dt)).date().isoformat()
        daily[local_day] = daily.get(local_day, 0.0) + float(wh) / 1000.0
    return {k: round(v, 3) for k, v in daily.items()}
