"""Skill scoreboard — nightly engine error accounting (SPEC §15.2/§15.1).

Owner: scoreboard. Pure, HA-free (stdlib only). This module implements the
FROZEN public contract (signatures + docstrings) the coordinator, the sensors,
the diagnostics and the pure tests depend on: per-day error scoring and
rolling-window aggregation. It performs ONLY the error maths — the coordinator
(glue) owns all the leak-free IO.

Division of labour (critical — do NOT recompute the engine forecast here)
-------------------------------------------------------------------------
The COORDINATOR (glue) does all the IO with the NO-LEAKAGE guarantees:
  * the ENGINE number is the forecast AS ISSUED for yesterday, read from the
    issued ring's snapshot logged during yesterday — NEVER recomputed with
    today's learned state;
  * the MEASURED number is the sum of the per-module actuals in the actuals ring
    for yesterday;
  * the weather class is yesterday's DOMINANT class (the coordinator already
    classifies clear/mixed/overcast/fog — reuse).
The coordinator assembles those into a :class:`DayScore` and hands the ring to
THIS pure module, which owns only the ERROR MATH: per-day absolute errors,
rolling-window aggregation into engine daily-kWh MAE, engine hourly MAE, and
the per-weather-stratum breakdown. Keeping the maths pure lets it be
golden-tested with bare pytest (SPEC §2) and keeps the fairness contract
auditable in one place.

Frozen public contract (implementers depend on these EXACT signatures):

    # --- per-day scoring (pure; builds one ring entry from raw numbers) ---
    score_day(
        *, iso_date, weather_class, measured_kwh, engine_kwh,
        engine_hourly_mae=None,
    ) -> DayScore | None
    hourly_mae(issued_corrected_hourly, measured_hourly) -> float | None

    # --- rolling-window aggregation over a DayScore ring ---
    trim_window(state, *, window_days) -> ScoreboardState
    engine_daily_kwh_mae(state, *, window_days) -> float | None
    engine_hourly_mae(state, *, window_days) -> float | None
    stratified_breakdown(state, *, window_days) -> dict[str, dict]

    # --- aggregate view ---
    scoreboard_summary(state, *, window_days) -> dict

All tunables come from const. Every path is validate-and-clamp: an empty window
yields ``None`` rather than a fabricated zero.
"""

from __future__ import annotations

import math

from ..const import (
    CLOUD_CLASSES,
    SCOREBOARD_STRATUM_MIN_N,
)
from .types import DayScore, ScoreboardState


def _finite_nonneg(value: float) -> float:
    """Coerce ``value`` to a finite, non-negative float (validate-and-clamp).

    A NaN / inf / negative input degrades to 0.0 rather than propagating an
    exception or a nonsensical negative error up into the aggregates (SPEC §9
    clamp ethos, applied to the scoreboard maths). Used ONLY for already-scored
    per-day error values on the aggregation path, never to coerce a raw
    measured input (see :func:`_finite_or_none`).
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f) or f < 0.0:
        return 0.0
    return f


def _finite_or_none(value: object) -> float | None:
    """Coerce to a finite, non-negative float, or None on garbage.

    Unlike :func:`_finite_nonneg` this does NOT fabricate a 0.0 for a NaN / inf /
    negative / non-numeric input — the caller drops the value instead (SPEC
    §15.2).
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0.0:
        return None
    return f


def _mean(values: list[float]) -> float | None:
    """Arithmetic mean, or None for an empty list (never a fabricated zero)."""
    if not values:
        return None
    return sum(values) / len(values)


def _window_days_list(state: ScoreboardState, window_days: int) -> list[DayScore]:
    """The newest ``window_days`` scored days, oldest-first.

    ISO-date lexicographic order == chronological, so sorting the keys and
    slicing the tail yields the newest window. A non-positive ``window_days``
    keeps the single newest day (mirrors :func:`trim_window`).
    """
    if not state.days:
        return []
    keep = window_days if window_days > 0 else 1
    ordered = sorted(state.days)
    kept_keys = ordered[-keep:]
    return [state.days[k] for k in kept_keys]

__all__ = [
    "score_day",
    "hourly_mae",
    "trim_window",
    "engine_daily_kwh_mae",
    "engine_hourly_mae",
    "stratified_breakdown",
    "scoreboard_summary",
]


# ---------------------------------------------------------------------------
# Per-day scoring (pure; the coordinator supplies the leak-free raw numbers)
# ---------------------------------------------------------------------------


def score_day(
    *,
    iso_date: str,
    weather_class: str,
    measured_kwh: float,
    engine_kwh: float,
    engine_hourly_mae: float | None = None,
) -> DayScore | None:
    """Build one :class:`DayScore` from yesterday's leak-free raw numbers.

    Computes ``engine_daily_abs_err = |engine_kwh - measured_kwh|``.
    ``weather_class`` must be one of const.CLOUD_CLASSES (yesterday's dominant
    class); ``engine_hourly_mae`` is the pre-computed hourly MAE for the day
    (see :func:`hourly_mae`) or None. The returned DayScore round-trips through
    the store's scoreboard ring.

    Pure and total. A non-finite or NEGATIVE ``measured_kwh`` / ``engine_kwh``
    leaves the day UNSCORED (returns ``None``, mirroring
    :func:`_finite_or_none`): the earlier 0.0 clamp fabricated a day that never
    happened — a NaN measured sum scored as ``|engine - 0|``, the engine's
    worst possible day (SPEC §15.2).
    """
    # measured / engine are dropped (day unscored) if non-finite or negative,
    # never zeroed.
    measured = _finite_or_none(measured_kwh)
    engine = _finite_or_none(engine_kwh)
    if measured is None or engine is None:
        return None
    engine_err = abs(engine - measured)

    hmae = None if engine_hourly_mae is None else _finite_nonneg(engine_hourly_mae)
    return DayScore(
        iso_date=str(iso_date),
        weather_class=str(weather_class),
        measured_kwh=measured,
        engine_kwh=engine,
        engine_daily_abs_err=engine_err,
        engine_hourly_mae=hmae,
    )


def hourly_mae(
    issued_corrected_hourly: dict[str, float],
    measured_hourly: dict[str, float],
) -> float | None:
    """Mean absolute per-hour Wh error for one day (engine hourly MAE, SPEC §15.1).

    ``issued_corrected_hourly`` is the engine's AS-ISSUED corrected hourly curve
    for the day (keyed by ISO-8601 UTC hour, already sliced to the local day);
    ``measured_hourly`` is the measured site energy per the same hours. The MAE
    is the mean of ``|issued - measured|`` over the UNION of daylight hours
    present in either dict (a modeled hour with no measurement, or vice-versa,
    contributes its full magnitude — an honest error, not a skipped one). Returns
    None when there are no comparable hours at all (hourly actuals unavailable),
    so the caller can leave ``DayScore.engine_hourly_mae`` None rather than
    fabricate a zero.
    """
    if not isinstance(issued_corrected_hourly, dict):
        issued_corrected_hourly = {}
    if not isinstance(measured_hourly, dict):
        measured_hourly = {}
    # DAYLIGHT restriction (SPEC §15.1 Taglicht-Stunden-MAE): restrict the union to
    # hours where EITHER side is materially non-zero. Night / twilight rows
    # (issued ~0 dark slots, measured 0-W LTS mean rows around the clock) would
    # otherwise contribute |0-0|=0 and dilute the denominator by the night/day
    # ratio (~2x in summer), understating the reported per-daylight-hour MAE.
    keys = {
        k
        for k in (set(issued_corrected_hourly) | set(measured_hourly))
        if _finite_nonneg(issued_corrected_hourly.get(k, 0.0)) > 0.0
        or _finite_nonneg(measured_hourly.get(k, 0.0)) > 0.0
    }
    if not keys:
        return None
    errors: list[float] = []
    for k in keys:
        issued = _finite_nonneg(issued_corrected_hourly.get(k, 0.0))
        measured = _finite_nonneg(measured_hourly.get(k, 0.0))
        errors.append(abs(issued - measured))
    return _mean(errors)


# ---------------------------------------------------------------------------
# Rolling-window aggregation over the DayScore ring
# ---------------------------------------------------------------------------


def trim_window(
    state: ScoreboardState,
    *,
    window_days: int,
) -> ScoreboardState:
    """Return a copy of ``state`` keeping only the newest ``window_days`` days.

    ISO-date lexicographic order == chronological, so the newest ``window_days``
    keys are kept and older ones dropped. Used by the store/coordinator after
    appending a new day so the ring never grows past the configured window. Never
    raises; a non-positive ``window_days`` keeps a single day (the newest).
    """
    if not state.days:
        return ScoreboardState(days={}, version=state.version)
    keep = window_days if window_days > 0 else 1
    ordered = sorted(state.days)
    kept_keys = ordered[-keep:]
    return ScoreboardState(
        days={k: state.days[k] for k in kept_keys},
        version=state.version,
    )


def engine_daily_kwh_mae(
    state: ScoreboardState,
    *,
    window_days: int,
) -> float | None:
    """Engine daily-kWh MAE over the newest ``window_days`` scored days.

    Mean of ``DayScore.engine_daily_abs_err`` across the window. Returns None
    when the window has no scored days (never a fabricated zero). This is the
    ``engine_daily_kwh_mae`` sensor value (SPEC §15.1).
    """
    return _engine_daily_kwh_mae_for_days(_window_days_list(state, window_days))


def engine_hourly_mae(
    state: ScoreboardState,
    *,
    window_days: int,
) -> float | None:
    """Engine hourly MAE over the window (mean of per-day hourly MAE).

    Averages ``DayScore.engine_hourly_mae`` across the days in the window that
    HAVE an hourly MAE (days where hourly actuals were unavailable are skipped,
    not counted as zero). Returns None when no day in the window has an hourly
    MAE. Backs the ``engine_hourly_mae`` sensor (SPEC §15.1 second metric).
    """
    days = _window_days_list(state, window_days)
    vals = [
        _finite_nonneg(d.engine_hourly_mae)
        for d in days
        if d.engine_hourly_mae is not None
    ]
    return _mean(vals)


def _engine_daily_kwh_mae_for_days(days: list[DayScore]) -> float | None:
    """Engine daily-kWh MAE over an already-selected list of days."""
    return _mean([_finite_nonneg(d.engine_daily_abs_err) for d in days])


def stratified_breakdown(
    state: ScoreboardState,
    *,
    window_days: int,
) -> dict[str, dict]:
    """Per-weather-stratum error breakdown over the window (SPEC §15.2/§15.1).

    Returns ``{weather_class: {...}}`` for each class in const.CLOUD_CLASSES that
    has at least one scored day in the window, each inner dict carrying:
      * ``"n"``: scored days in this stratum;
      * ``"engine_daily_kwh_mae"``: engine daily-kWh MAE within the stratum;
      * ``"engine_hourly_mae"``: engine hourly MAE within the stratum;
      * ``"low_n"``: True when the stratum is below SCOREBOARD_STRATUM_MIN_N
        (C1/SPEC-5: on a tiny sample the figures are meaningless — a single
        day produced absurd values such as a -480 % row on n=2 — so the
        dashboard/diagnostics can hide the row rather than render it).
    A class with no scored days is ABSENT (not a zero-filled row). Backs the
    diagnostics stratum breakdown; the coordinator surfaces it under
    DATA_KEY_SCOREBOARD for the dashboard markdown.
    """
    days = _window_days_list(state, window_days)
    by_class: dict[str, list[DayScore]] = {}
    for d in days:
        by_class.setdefault(d.weather_class, []).append(d)
    out: dict[str, dict] = {}
    # Iterate the const class order for a stable, canonical breakdown; a class
    # with no scored days in the window is absent (not a zero-filled row).
    for cls in CLOUD_CLASSES:
        stratum = by_class.get(cls)
        if not stratum:
            continue
        out[cls] = {
            "n": len(stratum),
            "engine_daily_kwh_mae": _engine_daily_kwh_mae_for_days(stratum),
            "engine_hourly_mae": _mean(
                [
                    _finite_nonneg(d.engine_hourly_mae)
                    for d in stratum
                    if d.engine_hourly_mae is not None
                ]
            ),
            "low_n": len(stratum) < SCOREBOARD_STRATUM_MIN_N,
        }
    return out


def _newest_scored_date(state: ScoreboardState) -> str | None:
    """The lexicographically-greatest (== newest) scored ISO date, or None."""
    if not state.days:
        return None
    return max(state.days)


def scoreboard_summary(
    state: ScoreboardState,
    *,
    window_days: int,
) -> dict:
    """One aggregate dict for the coordinator payload (DATA_KEY_SCOREBOARD).

    Bundles the whole scoreboard view so the coordinator writes it once and the
    sensors / diagnostics / dashboard read fields off it:
      * ``"engine_daily_kwh_mae"``: float | None;
      * ``"engine_hourly_mae"``: float | None;
      * ``"window_days"``: the configured window;
      * ``"scored_days"``: number of scored days currently in the ring;
      * ``"newest_scored_date"``: newest scored ISO date (staleness visibility);
      * ``"strata"``: :func:`stratified_breakdown` output.
    Pure assembly over the other functions in this module; never raises.
    """
    return {
        "engine_daily_kwh_mae": engine_daily_kwh_mae(
            state, window_days=window_days
        ),
        "engine_hourly_mae": engine_hourly_mae(state, window_days=window_days),
        "window_days": window_days,
        "scored_days": len(state.days),
        "newest_scored_date": _newest_scored_date(state),
        "strata": stratified_breakdown(state, window_days=window_days),
    }
