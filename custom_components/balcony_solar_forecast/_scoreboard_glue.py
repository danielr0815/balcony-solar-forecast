"""Nightly skill-scoreboard scorer — SPEC §15.2.

Owner: scoreboard (glue). The LEAK-FREE IO around the pure ``core/scoreboard.py``
math: score each closed local day's engine forecast AS ISSUED (from the issued
ring) against the measured site energy (from the actuals ring), then persist a
DayScore into the rolling window.

Every function takes the coordinator as ``coord`` and reads exactly the same
attributes the methods did (``coord._store`` / ``coord._site`` /
``coord._scoreboard_state`` / …); the persistence and the shared nightly helpers
(``_store_hourly_actuals`` / ``_site_measured_hourly``) stay on the coordinator
and are reached back through ``coord``. The coordinator exposes each of these as
a 1-2 line delegate (the tests build it via ``__new__`` and call the delegates
directly).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from math import isclose
from typing import Any

from homeassistant.util import dt as dt_util

from ._actuals import _daylight_hour_keys_in_local_day, _next_utc_hour_boundary
from ._glue_util import _filter_hourly_to_local_day
from ._nightly import (
    actuals_match_metering,
    expected_production_hours,
    has_daylight_weather_gap,
    has_fractional_utc_day_boundary,
    metered_local_day_wh,
    metered_modeled_hourly,
)
from .const import CLOUD_CLASS_CLEAR, SLOT_MINUTES
from .core import IssuedSnapshot, ScoreboardState
from .core import scoreboard as scoreboard_mod
from .core.measurement_quality import dc_power

_LOGGER = logging.getLogger(__name__)

# Scoreboard leakage guard: a day is only scored when its issued snapshot was
# logged BEFORE this local-hour cutoff of the scored day. A snapshot issued
# later (e.g. a mid-day startup catch-up recomputed from a fresh Open-Meteo
# fetch that has assimilated the scored day's observed weather) is a
# hindcast/nowcast, not a day-ahead forecast, and must not flatter the engine.
_SCOREBOARD_ISSUE_CUTOFF_HOUR = 6


async def score_scoreboard_day(coord, day: date) -> None:
    """Score one closed local ``day`` into the rolling scoreboard window.

    NO-LEAKAGE (SPEC §15.2):
      * the ENGINE number is the forecast AS ISSUED for ``day`` — read from
        the issued ring's snapshot logged during that day (the CORRECTED
        served curve, sliced to the local day), NEVER recomputed with today's
        learned state;
      * the MEASURED number is the sum of the per-module actuals in the
        actuals ring for ``day``.
    Idempotent + date-keyed: a day already present in the ring is re-scored
    (engine + measured are deterministic from the rings, so a re-run is a
    no-op).
    """
    iso = day.isoformat()
    if not coord._scoreboard_enabled and iso not in coord._scoreboard_state.days:
        return
    issued = coord._store.get_issued(iso)
    actuals = coord._store.get_actuals(iso)
    # Need both the issued snapshot and the measured actuals to score a day;
    # a day missing either is retried by a later catch-up (like training).
    if not issued or not actuals or not actuals_match_metering(coord, iso):
        return
    if (iso in coord._scoreboard_state.days and coord._store.has_verified_actuals(iso)
            and coord._store.actuals_bindings(iso) is None):
        return  # Preserve a historical score whose original entity binding is unknown.

    snap = IssuedSnapshot.from_dict(issued)
    # LEAKAGE GUARD (SPEC §15.2): only score a day whose snapshot was issued
    # before the early-morning cutoff of that local day. A snapshot issued
    # later (a mid-day startup catch-up recomputed from a fresh weather fetch
    # that has assimilated the scored day's observed weather) is a
    # hindcast/nowcast, not a day-ahead forecast; leave the day UNSCORED so
    # it never flatters the engine.
    if coord._issued_after_cutoff(snap, day):
        _LOGGER.debug(
            "Skipping scoreboard for %s: snapshot issued after the "
            "day-ahead cutoff (issued_at=%s)", iso, snap.issued_at,
        )
        if remember_confirmed_late_issue(coord, snap, day):
            discard_scoreboard_day(coord, iso)
        return
    if not coord._scoreboard_enabled:
        return

    original_curve = issued.get("corrected_hourly_wh") or issued.get("raw_hourly_wh")
    if not isinstance(original_curve, dict) or any(
        not isinstance(stamp, str) or isinstance(value, bool)
        or not isinstance(value, (int, float)) or dc_power(value) is None
        for stamp, value in original_curve.items()
    ):
        # The loader may sanitize individual entries for display. Scoring the
        # surviving subtotal as a whole-day forecast would fabricate a miss;
        # falling back to RAW would silently change the evaluated model.
        return
    # Snow / total dropout is not a forecast label. Keep it out of the skill
    # window for the same reason the nightly learner quarantine does.
    if coord._is_collapse_day(iso, issued, actuals):
        return

    fractional_day = has_fractional_utc_day_boundary(day)
    if not fractional_day and has_daylight_weather_gap(coord, snap, day):
        return

    # Engine AS ISSUED: the CORRECTED served hourly curve, sliced to the day.
    corrected_hourly = _filter_hourly_to_local_day(
        snap.corrected_hourly_wh or snap.raw_hourly_wh, iso
    )
    corrected_hourly = metered_modeled_hourly(
        coord, snap, corrected_hourly, layer="corrected"
    )
    corrected_hourly = {
        hour: wh for hour, wh in (corrected_hourly or {}).items()
        if snap.weather_hour_usable(hour)
    }
    if not corrected_hourly:
        # Missing/corrupt curves are unknown, whereas an explicit zero-valued
        # hour is valid energy. An empty sum must never become a zero forecast.
        return

    hourly_actuals = coord._store_hourly_actuals(iso)
    measured_hourly = coord._site_measured_hourly(iso, hourly_actuals)
    if snap.complete_weather_hours is not None:
        expected = expected_production_hours(coord, day, corrected_hourly, hourly_actuals or {})
        if not expected.issubset((measured_hourly or {}).keys() & corrected_hourly.keys()):
            return
    elif hourly_actuals:
        if any(wh > 0.0 and not snap.weather_hour_usable(hour)
               for hour, wh in (measured_hourly or {}).items()):
            return
        # The primary scoreboard number is DAILY kWh MAE, not an error over an
        # arbitrary covered subset. A current store with recorder gaps must
        # therefore leave the day unscored. The daily-only fallback below is
        # reserved for a genuine legacy store with no hourly ring at all.
        if not measured_hourly:
            return
        start = dt_util.start_of_local_day(
            datetime(day.year, day.month, day.day)
        )
        end = dt_util.start_of_local_day(
            datetime(day.year, day.month, day.day) + timedelta(days=1)
        )
        expected = (
            _daylight_hour_keys_in_local_day(coord._site, start, end)
            & set(corrected_hourly)
        )
        if expected and not expected.issubset(measured_hourly):
            return
    if fractional_day:
        if not coord._store.has_verified_local_day_edges(iso):
            return
        engine_wh = metered_local_day_wh(coord, issued, snap, iso, layer="corrected")
        if engine_wh is None:
            # UTC hour pairs may span local midnight. A day score needs the
            # separately archived local-day energy on the DC metering basis.
            return
    else:
        engine_wh = sum(corrected_hourly.values())
    engine_kwh = engine_wh / 1000.0
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
    if measured_hourly:
        if snap.complete_weather_hours is not None:
            paired = measured_hourly.keys() & corrected_hourly.keys()
            measured_hourly = {hour: measured_hourly[hour] for hour in paired}
            corrected_hourly = {hour: corrected_hourly[hour] for hour in paired}
        engine_hourly_mae = scoreboard_mod.hourly_mae(
            corrected_hourly, measured_hourly
        )

    day_score = scoreboard_mod.score_day(
        iso_date=iso,
        weather_class=weather_class,
        measured_kwh=measured_kwh,
        engine_kwh=engine_kwh,
        engine_hourly_mae=engine_hourly_mae,
    )
    if day_score is None:
        # Non-finite / negative measured or engine number: the day stays
        # UNSCORED — no ring entry, no persist (a fabricated 0.0-kWh day
        # would poison the window, SPEC §15.2).
        _LOGGER.warning(
            "Scoreboard: %s unscorable (measured=%r, engine=%r); day skipped",
            iso, measured_kwh, engine_kwh,
        )
        return
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


def discard_scoreboard_day(coord, iso: str) -> None:
    if iso in coord._scoreboard_state.days:
        days = dict(coord._scoreboard_state.days)
        del days[iso]
        coord._scoreboard_state = ScoreboardState(days=days, version=coord._scoreboard_state.version)
        coord._persist_scoreboard_state()


def _effective_issue_time(snap: IssuedSnapshot) -> datetime | None:
    # The archive write time can legitimately be much later than publication.
    # But an issued timestamp must never antedate the computation it refers to.
    stamps = [
        dt_util.as_utc(parsed)
        for value in (snap.issued_at, snap.computed_at)
        if (parsed := dt_util.parse_datetime(value or "")) is not None
    ]
    return max(stamps, default=None)


def _confirmed_late_issue(coord, snap: IssuedSnapshot, day: date, issued_at: datetime) -> bool:
    iso = day.isoformat()
    if coord._store.confirmed_late_issued(iso) == issued_at.isoformat():
        return True
    start = dt_util.start_of_local_day(datetime(day.year, day.month, day.day))
    if issued_at <= dt_util.as_utc(start):
        return False
    if issued_at > dt_util.as_utc(start + timedelta(hours=_SCOREBOARD_ISSUE_CUTOFF_HOUR)):
        return True
    for stamp in (
        snap.first_local_edge_production,
        coord._store.first_actuals_edge_production(iso),
    ):
        target = dt_util.parse_datetime(stamp or "")
        if target is not None and dt_util.as_local(target).date() == day and dt_util.as_utc(target) < issued_at:
            return True
    # An early hour can have real production even when the model missed it.
    # One positive metered channel is enough to establish that the hour counts.
    actuals = coord._store_hourly_actuals(iso) or {}
    for curve in (snap.raw_hourly_wh, snap.corrected_hourly_wh, *actuals.values()):
        for key, wh in curve.items():
            if not dc_power(wh):
                continue
            target = dt_util.parse_datetime(key)
            if target is not None and dt_util.as_local(target).date() == day and dt_util.as_utc(target) < issued_at:
                return True
    return False


def remember_confirmed_late_issue(coord, snap: IssuedSnapshot, day: date) -> bool:
    """Retain proven lateness even if another source replaces the labels."""
    issued_at = _effective_issue_time(snap)
    if issued_at is None or not _confirmed_late_issue(coord, snap, day, issued_at):
        return False
    coord._store.record_late_issued(day.isoformat(), issued_at.isoformat())
    return True


def _proved_zero_local_edges(snap: IssuedSnapshot, start: datetime, day: date) -> bool:
    """A complete nonnegative day can prove both edges zero by its interior."""
    if snap.weather_day_complete is not True or snap.complete_weather_hours is None:
        return False
    local_end = dt_util.start_of_local_day(datetime.combine(day + timedelta(days=1), datetime.min.time()))
    end = dt_util.as_utc(local_end)
    hour = _next_utc_hour_boundary(start)
    step = timedelta(hours=1)
    inside = set()
    while hour + step <= end:
        inside.add(hour.isoformat())
        hour += step
    if not all(snap.weather_hour_usable(key) for key in inside):
        return False
    iso = day.isoformat()
    return all(
        iso in daily and inside.issubset(curve) and isclose(
            daily[iso] * 1000, sum(curve[key] for key in sorted(inside)), rel_tol=0.0, abs_tol=1e-9,
        )
        for daily, curve in ((snap.raw_daily_kwh, snap.raw_hourly_wh),
                             (snap.corrected_daily_kwh, snap.corrected_hourly_wh))
    )


def _legacy_leading_edge_known(snap: IssuedSnapshot, start: datetime, edge_end: datetime) -> bool:
    """The previous writer archived the first positive quarter in this edge."""
    if (
        snap.version != 2 or snap.leading_edge_forecast_known is not None
        or snap.weather_day_complete is not True or snap.complete_weather_hours is None
    ):
        return False
    first = dt_util.parse_datetime(snap.first_local_edge_production or "")
    if first is None or first.tzinfo is None:
        return False
    first = dt_util.as_utc(first)
    # Complete slot coverage makes the writer's scan proof of zeros before
    # this time. Its time marker belongs only to the leading fragment.
    return start <= first < edge_end and not (first - start) % timedelta(minutes=SLOT_MINUTES)


def issued_after_cutoff(coord, snap: IssuedSnapshot, day: date) -> bool:
    """Reject confirmed late issues and older ones with an unresolved early edge."""
    issued_at = _effective_issue_time(snap)
    if issued_at is None:
        return False
    if _confirmed_late_issue(coord, snap, day, issued_at):
        return True
    if snap.leading_edge_forecast_known is True:
        return False
    start = dt_util.as_utc(dt_util.start_of_local_day(datetime.combine(day, datetime.min.time())))
    edge_end = _next_utc_hour_boundary(start)
    if start >= min(edge_end, issued_at):
        return False
    # Diffuse irradiance can yield energy even below the twilight threshold.
    return not (
        _legacy_leading_edge_known(snap, start, edge_end)
        or _proved_zero_local_edges(snap, start, day)
    )


def dominant_weather_class(coord, snap: IssuedSnapshot, iso: str) -> str:
    """Yesterday's DOMINANT cloud class from the issued snapshot (SPEC §15.2).

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


def scoreboard_summary(coord) -> dict[str, Any]:
    """The current scoreboard aggregate view for ``self.data`` / platforms."""
    return scoreboard_mod.scoreboard_summary(
        coord._scoreboard_state,
        window_days=coord._scoreboard_window_days,
    )
