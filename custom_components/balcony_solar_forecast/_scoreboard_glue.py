"""Nightly skill-scoreboard scorer (the kill-gate) — SPEC §9/§10.

Owner: scoreboard (glue). The LEAK-FREE IO around the pure ``core/scoreboard.py``
math: score each closed local day's engine forecast AS ISSUED (from the issued
ring), the measured site energy (from the actuals ring) and each configured
comparison entity's value AS IT STOOD during the day (from the recorder history
at the engine's matched day-ahead horizon), then persist a DayScore into the
rolling window.

Every function takes the coordinator as ``coord`` and reads exactly the same
attributes the methods did (``coord._store`` / ``coord._site`` /
``coord._comparisons`` / ``coord._scoreboard_state`` / …); the persistence and
the shared nightly helpers (``_store_hourly_actuals`` / ``_site_measured_hourly``)
stay on the coordinator and are reached back through ``coord``. The coordinator
exposes each of these as a 1-2 line delegate (the tests build it via ``__new__``
and call the delegates directly).
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from ._glue_util import _UNUSABLE_STATES, _filter_hourly_to_local_day
from ._nightly import _NIGHTLY_HOUR, _NIGHTLY_MINUTE
from .const import (
    CLOUD_CLASS_CLEAR,
    SCOREBOARD_COMPARISON_UNIT_KWH,
)
from .core import ComparisonConfig, IssuedSnapshot, ScoreboardState
from .core import scoreboard as scoreboard_mod

_LOGGER = logging.getLogger(__name__)

# Comparison read horizon: the engine snapshot is issued at the nightly hour, so
# each comparison is read at the SAME day-ahead horizon (the first usable state
# at/after the issue time), never the settled end-of-day value — a live-updating
# "today" forecast sensor that blends actuals intraday must not be judged on a
# value that has converged toward the measured truth (biases the gate).
_COMPARISON_ISSUE_HOUR = _NIGHTLY_HOUR
_COMPARISON_ISSUE_MINUTE = _NIGHTLY_MINUTE
# How long after the issue time to look for the first usable comparison state
# (a comparison that does not refresh right at 01:30 is still captured).
_COMPARISON_ISSUE_WINDOW_HOURS = 8
# A comparison daily-kWh value this many times the site's physical daily ceiling
# (installed Wp x 24 h, expressed in kWh) is discarded as a unit artifact /
# garbage rather than scored (e.g. a Wh-reporting sensor read as kWh).
_COMPARISON_MAX_PHYSICAL_FACTOR = 1.0
# Scoreboard leakage guard: a day is only scored when its issued snapshot was
# logged BEFORE this local-hour cutoff of the scored day. A snapshot issued
# later (e.g. a mid-day startup catch-up recomputed from a fresh Open-Meteo
# fetch that has assimilated the scored day's observed weather) is a
# hindcast/nowcast, not a day-ahead forecast, and must not flatter the engine.
_SCOREBOARD_ISSUE_CUTOFF_HOUR = 6


async def score_scoreboard_day(coord, day: date) -> None:
    """Score one closed local ``day`` into the rolling scoreboard window.

    NO-LEAKAGE (SPEC §9, the whole point of the gate):
      * the ENGINE number is the forecast AS ISSUED for ``day`` — read from
        the issued ring's snapshot logged during that day (the CORRECTED
        served curve, sliced to the local day), NEVER recomputed with today's
        learned state;
      * each COMPARISON number is that comparison entity's own value AS IT
        STOOD during ``day`` — read from its recorder history for that local
        calendar day (the settled end-of-day forecast the consumer saw),
        NEVER today's live state;
      * the MEASURED number is the sum of the per-module actuals in the
        actuals ring for ``day``.
    Idempotent + date-keyed: a day already present in the ring is re-scored
    (engine + measured are deterministic from the rings, so a re-run is a
    no-op unless a late comparison read now succeeds). A missing comparison
    is SKIPPED for the day (that source is unscored), never the whole day.
    """
    if not coord._scoreboard_enabled:
        return
    iso = day.isoformat()
    issued = coord._store.get_issued(iso)
    actuals = coord._store.get_actuals(iso)
    # Need both the issued snapshot and the measured actuals to score a day;
    # a day missing either is retried by a later catch-up (like training).
    if not issued or not actuals:
        return

    snap = IssuedSnapshot.from_dict(issued)
    # LEAKAGE GUARD (SPEC §9): only score a day whose snapshot was issued
    # before the early-morning cutoff of that local day. A snapshot issued
    # later (a mid-day startup catch-up recomputed from a fresh weather fetch
    # that has assimilated the scored day's observed weather) is a
    # hindcast/nowcast, not a day-ahead forecast; leave the day UNSCORED so
    # it never flatters the engine on the kill-gate.
    if coord._issued_after_cutoff(snap, day):
        _LOGGER.debug(
            "Skipping scoreboard for %s: snapshot issued after the "
            "day-ahead cutoff (issued_at=%s)", iso, snap.issued_at,
        )
        return

    # Engine AS ISSUED: the CORRECTED served hourly curve, sliced to the day.
    corrected_hourly = _filter_hourly_to_local_day(
        snap.corrected_hourly_wh or snap.raw_hourly_wh, iso
    )
    engine_kwh = sum(corrected_hourly.values()) / 1000.0
    # Measured site energy for the day = sum of the per-module actuals.
    measured_kwh = (
        sum(
            float(v)
            for v in actuals.values()
            if isinstance(v, (int, float))
        )
        / 1000.0
    )
    weather_class = coord._dominant_weather_class(snap, iso)

    # Engine hourly MAE: issued corrected hourly (Wh) vs measured hourly (Wh).
    engine_hourly_mae = None
    hourly_actuals = coord._store_hourly_actuals(iso)
    measured_hourly = coord._site_measured_hourly(iso, hourly_actuals)
    if measured_hourly:
        engine_hourly_mae = scoreboard_mod.hourly_mae(
            corrected_hourly, measured_hourly
        )

    # Comparisons AS THEY STOOD during the day (recorder history), cached in
    # the comparison ring so a re-run does not re-hit the recorder. A
    # TRANSIENT recorder failure here (DB locked by a nightly purge/backup)
    # must NOT lose the whole day for the engine: engine + measured need no
    # recorder, so we score them anyway and best-effort-merge the comparisons
    # (a later re-run fills them in).
    try:
        comparison_kwh = await coord._comparison_kwh_for_day(day)
    except Exception:  # pragma: no cover - recorder is best-effort
        _LOGGER.warning(
            "Comparison read failed for %s; scoring engine/measured only",
            iso, exc_info=True,
        )
        comparison_kwh = {}

    day_score = scoreboard_mod.score_day(
        iso_date=iso,
        weather_class=weather_class,
        measured_kwh=measured_kwh,
        engine_kwh=engine_kwh,
        comparison_kwh=comparison_kwh,
        engine_hourly_mae=engine_hourly_mae,
    )
    # eMMC-wear guard (minor): a deterministic re-score with an identical
    # DayScore (restart-heavy day, same rings) is a no-op — skip the write.
    if coord._scoreboard_state.days.get(iso) == day_score:
        return
    days = dict(coord._scoreboard_state.days)
    days[iso] = day_score
    state = ScoreboardState(days=days, version=coord._scoreboard_state.version)
    # Trim to the configured window so the ring never grows unbounded.
    state = scoreboard_mod.trim_window(
        state, window_days=coord._scoreboard_window_days
    )
    coord._scoreboard_state = state
    coord._persist_scoreboard_state()


def issued_after_cutoff(coord, snap: IssuedSnapshot, day: date) -> bool:
    """True when the snapshot was issued after the day-ahead cutoff of ``day``.

    The cutoff is ``_SCOREBOARD_ISSUE_CUTOFF_HOUR`` local time of ``day``.
    A snapshot with an unparseable / empty ``issued_at`` is treated as valid
    (not after cutoff) — a legacy/v0.1 snapshot pre-dates the catch-up
    recompute path this guard defends against.
    """
    issued_at = dt_util.parse_datetime(snap.issued_at or "")
    if issued_at is None:
        return False
    cutoff = dt_util.start_of_local_day(
        datetime(day.year, day.month, day.day)
    ) + timedelta(hours=_SCOREBOARD_ISSUE_CUTOFF_HOUR)
    return dt_util.as_utc(issued_at) > dt_util.as_utc(cutoff)


def dominant_weather_class(coord, snap: IssuedSnapshot, iso: str) -> str:
    """Yesterday's DOMINANT cloud class from the issued snapshot (SPEC §9).

    The issued snapshot stores the forecast cloud class per ISO-UTC hour
    (``cloud_class_by_hour``); the dominant class is the one carrying the most
    forecast ENERGY over the local day (weighted by the issued Wh per hour),
    so nocturnal / near-zero-Wh hours cannot outvote the handful of daylight
    hours where the PV error actually lives — a radiation-fog morning is filed
    under 'fog', not 'clear'. Ties broken by the const CLOUD_CLASSES order.
    Falls back to CLEAR when the snapshot carries no per-hour classes (v0.1
    issued / empty), so a day is never left unstratified.
    """
    weight_by_hour = snap.corrected_hourly_wh or snap.raw_hourly_wh or {}
    weights: dict[str, float] = {}
    for hkey, cc in snap.cloud_class_by_hour.items():
        dt = dt_util.parse_datetime(hkey)
        if dt is None:
            continue
        if dt_util.as_local(dt_util.as_utc(dt)).date().isoformat() != iso:
            continue
        # Weight by issued Wh (daylight energy); a near-zero-Wh night hour
        # contributes essentially nothing. Fall back to an equal +1 vote when
        # no Wh curve exists so an all-zero-weight day still stratifies.
        w = weight_by_hour.get(hkey, 0.0)
        weights[cc] = weights.get(cc, 0.0) + (float(w) if w > 0.0 else 0.0)
    if not any(v > 0.0 for v in weights.values()):
        # No daylight-energy signal: fall back to an unweighted hour count so
        # a Wh-less (legacy) snapshot still gets a class.
        weights = {}
        for hkey, cc in snap.cloud_class_by_hour.items():
            dt = dt_util.parse_datetime(hkey)
            if dt is None:
                continue
            if dt_util.as_local(dt_util.as_utc(dt)).date().isoformat() != iso:
                continue
            weights[cc] = weights.get(cc, 0.0) + 1.0
    if not weights:
        return CLOUD_CLASS_CLEAR
    best_n = max(weights.values())
    from .const import CLOUD_CLASSES

    for cls in CLOUD_CLASSES:
        if abs(weights.get(cls, 0.0) - best_n) < 1e-9:
            return cls
    # A class outside the canonical tuple (defensive): return any max.
    return max(weights, key=lambda c: weights[c])


async def comparison_kwh_for_day(coord, day: date) -> dict[str, float]:
    """Per-comparison daily-kWh AS IT STOOD during ``day`` (no leakage).

    Uses the cached comparison ring when present (idempotent re-runs, and a
    successful earlier read is authoritative); otherwise reads each
    configured comparison entity's recorder history for the day's LOCAL
    calendar and caches the result. A comparison with no usable recorded
    state that day is ABSENT from the returned map (that source is unscored
    for the day, SPEC §9), never a fabricated zero.
    """
    if not coord._comparisons:
        return {}
    iso = day.isoformat()
    cached = None
    try:
        cached = coord._store.get_comparison(iso)
    except Exception:  # pragma: no cover - defensive
        cached = None
    # Read only the comparisons not already cached (a renamed/added
    # comparison mid-window is filled on its first close).
    cached = dict(cached) if isinstance(cached, dict) else {}
    missing = [c for c in coord._comparisons if c.name not in cached]
    if missing:
        read = await coord._async_read_comparison_history(day, missing)
        if read:
            cached.update(read)
            try:
                coord._store.record_comparison(iso, cached)
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug(
                    "Could not cache comparison ring for %s", iso, exc_info=True
                )
    return cached


async def async_read_comparison_history(
    coord, day: date, comparisons: list[ComparisonConfig]
) -> dict[str, float]:
    """Read each comparison's daily-kWh AT THE ENGINE'S HORIZON for ``day``.

    FAIRNESS (SPEC §9, matched horizon): the engine is scored on its ~01:30
    issued snapshot, so each comparison is read at the SAME day-ahead horizon
    — the FIRST usable (numeric, finite, non-unavailable) recorded state
    at/after the local issue time (01:30), NOT the settled end-of-day value.
    A live-updating "today" forecast sensor that blends actuals intraday would
    otherwise converge toward the measured truth by 23:00, giving it an error
    the 01:30 engine snapshot can never match.

    Robustness guards (each -> comparison OMITTED for the day, never zeroed):
      * FRESHNESS: the accepted state must have ``last_updated`` inside the
        scored local day (drops a pure start-of-day carry-in from a
        comparison that produced no in-day update — a frozen / hung sensor
        holding its last numeric value);
      * NON-FINITE: a 'nan' / 'inf' state is rejected (never clamped to 0);
      * UNIT: the entity's live unit is read; a Wh unit is normalised to kWh;
      * PHYSICAL SANITY: a value orders of magnitude above the site's daily
        ceiling (installed Wp x 24 h) is discarded as a unit artifact.
    Runs the recorder read in the recorder executor (SPEC).
    """
    if not comparisons:
        return {}
    day_start = dt_util.start_of_local_day(
        datetime(day.year, day.month, day.day)
    )
    day_end = dt_util.start_of_local_day(
        datetime(day.year, day.month, day.day) + timedelta(days=1)
    )
    issue_at = day_start + timedelta(
        hours=_COMPARISON_ISSUE_HOUR, minutes=_COMPARISON_ISSUE_MINUTE
    )
    read_end = min(
        issue_at + timedelta(hours=_COMPARISON_ISSUE_WINDOW_HOURS), day_end
    )
    entity_ids = [c.daily_entity for c in comparisons]

    from homeassistant.components.recorder import get_instance

    def _read() -> dict[str, tuple[str, bool] | None]:
        """Return {entity: (raw_state, in_day)} for the first usable state."""
        from homeassistant.components.recorder.history import (
            state_changes_during_period,
        )

        out: dict[str, tuple[str, bool] | None] = {}
        for entity_id in entity_ids:
            history = state_changes_during_period(
                coord.hass,
                issue_at,
                read_end,
                entity_id,
                include_start_time_state=True,
                no_attributes=True,
            )
            states = history.get(entity_id) or []
            chosen: tuple[str, bool] | None = None
            for st in states:
                raw = getattr(st, "state", None)
                if raw is None:
                    continue
                if str(raw).strip().lower() in _UNUSABLE_STATES:
                    continue
                last_updated = getattr(st, "last_updated", None)
                in_day = bool(
                    last_updated is not None
                    and dt_util.as_utc(last_updated) >= day_start
                )
                chosen = (raw, in_day)
                break  # FIRST usable state at/after the issue time
            out[entity_id] = chosen
        return out

    raw_by_entity = await get_instance(coord.hass).async_add_executor_job(_read)
    result: dict[str, float] = {}
    for cfg in comparisons:
        picked = raw_by_entity.get(cfg.daily_entity)
        if picked is None:
            continue  # no usable state at the horizon -> comparison skipped
        raw, in_day = picked
        if not in_day:
            # Only a start-of-day carry-in (no in-day update): a frozen /
            # hung sensor holding a stale value. Skip (unscored), not zero.
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value < 0.0:
            continue  # 'nan'/'inf'/negative -> unscored, never a fake zero
        value = coord._normalise_comparison_kwh(cfg.daily_entity, value)
        if value is None:
            continue  # unusable unit / physically impossible -> skipped
        result[cfg.name] = value
    return result


def normalise_comparison_kwh(
    coord, entity_id: str, value: float
) -> float | None:
    """Normalise a raw comparison state to daily kWh, or None if unusable.

    Reads the entity's live ``unit_of_measurement``: 'Wh' is scaled to kWh;
    'kWh' (or an absent/unknown unit — assumed kWh per the documented
    contract) passes through; any other energy-incompatible unit ('W', '%',
    etc.) is rejected with a warning. A value above the site's physical daily
    ceiling (installed Wp x 24 h) is discarded as a unit artifact.
    """
    unit = None
    try:
        state = coord.hass.states.get(entity_id)
    except AttributeError:  # a hass stub without a state machine (tests)
        state = None
    if state is not None:
        unit = state.attributes.get("unit_of_measurement")
    u = str(unit).strip().lower() if unit is not None else ""
    if u in ("wh", "watt-hour", "watt-hours"):
        value = value / 1000.0
    elif u in ("kwh", "", "none"):
        pass  # kWh (or unit-less -> assumed kWh per the documented contract)
    elif not SCOREBOARD_COMPARISON_UNIT_KWH:  # pragma: no cover - config-only
        value = value / 1000.0
    else:
        _LOGGER.warning(
            "Comparison %s reports unit %r, not an energy unit; skipping",
            entity_id, unit,
        )
        return None
    ceiling = coord._site_daily_kwh_ceiling()
    if ceiling is not None and value > ceiling * _COMPARISON_MAX_PHYSICAL_FACTOR:
        _LOGGER.warning(
            "Comparison %s daily value %.1f kWh exceeds the site ceiling "
            "%.1f kWh; discarding as a unit artifact",
            entity_id, value, ceiling,
        )
        return None
    return value


def site_daily_kwh_ceiling(coord) -> float | None:
    """The site's physical daily-energy ceiling: installed Wp x 24 h (kWh)."""
    total_wp = sum(p.wp for p in coord._site.planes)
    if total_wp <= 0.0:
        return None
    return total_wp * 24.0 / 1000.0


def scoreboard_summary(coord) -> dict[str, Any]:
    """The current scoreboard aggregate view for ``self.data`` / platforms."""
    today = dt_util.as_local(dt_util.utcnow()).date().isoformat()
    return scoreboard_mod.scoreboard_summary(
        coord._scoreboard_state,
        window_days=coord._scoreboard_window_days,
        gate_margin=coord._scoreboard_gate_margin,
        today=today,
    )
