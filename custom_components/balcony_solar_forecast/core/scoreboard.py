"""Skill scoreboard — the kill-gate the whole v0.4 plan hinges on (SPEC §9/§10).

Owner: scoreboard. Pure, HA-free (stdlib only). This module implements the
FROZEN public contract (signatures + docstrings) the coordinator, the sensors,
the binary_sensor, the diagnostics and the pure tests depend on: per-day error
scoring, rolling-window aggregation and the kill-gate verdict. It performs ONLY
the error maths — the coordinator (glue) owns all the leak-free IO.

Division of labour (critical — do NOT recompute the engine forecast here)
-------------------------------------------------------------------------
The COORDINATOR (glue) does all the IO with the NO-LEAKAGE guarantees:
  * the ENGINE number is the forecast AS ISSUED for yesterday, read from the
    issued ring's snapshot logged during yesterday — NEVER recomputed with
    today's learned state;
  * each COMPARISON number is that comparison entity's own value AS IT STOOD
    during yesterday, read from its recorder history for yesterday — NEVER
    today's live value;
  * the MEASURED number is the sum of the per-module actuals in the actuals ring
    for yesterday;
  * the weather class is yesterday's DOMINANT class (the coordinator already
    classifies clear/mixed/overcast/fog — reuse).
The coordinator assembles those into a :class:`DayScore` and hands the ring to
THIS pure module, which owns only the ERROR MATH: per-day absolute errors,
rolling-window aggregation into daily-kWh MAE (engine + per comparison), engine
hourly MAE, engine_vs_best_baseline_pct, the per-weather-stratum breakdown, and
the kill-gate verdict. Keeping the maths pure lets it be golden-tested with bare
pytest (SPEC §4) and keeps the fairness contract auditable in one place.

Frozen public contract (implementers depend on these EXACT signatures):

    # --- per-day scoring (pure; builds one ring entry from raw numbers) ---
    score_day(
        *, iso_date, weather_class, measured_kwh, engine_kwh,
        comparison_kwh, engine_hourly_mae=None,
    ) -> DayScore
    hourly_mae(issued_corrected_hourly, measured_hourly) -> float | None

    # --- rolling-window aggregation over a DayScore ring ---
    trim_window(state, *, window_days) -> ScoreboardState
    engine_daily_kwh_mae(state, *, window_days) -> float | None
    comparison_daily_kwh_mae(state, *, window_days) -> dict[str, float]
    engine_hourly_mae(state, *, window_days) -> float | None
    engine_vs_best_baseline_pct(state, *, window_days) -> float | None
    stratified_breakdown(state, *, window_days) -> dict[str, dict]

    # --- the kill-gate verdict ---
    kill_gate_passed(state, *, window_days, gate_margin) -> bool | None
    scoreboard_summary(state, *, window_days, gate_margin) -> dict

All tunables come from const. Every path is validate-and-clamp: an empty window
or a comparison with no scored days yields ``None`` / an absent entry rather than
a fabricated zero (SPEC §9: a partial window can never assert the kill-gate).
"""

from __future__ import annotations

import math

from ..const import (
    CLOUD_CLASSES,
    SCOREBOARD_MAX_STALENESS_DAYS,
    SCOREBOARD_MIN_PAIRED_DAYS,
    SCOREBOARD_MIN_WINDOW_DAYS,
)
from .types import DayScore, ScoreboardState


def _finite_nonneg(value: float) -> float:
    """Coerce ``value`` to a finite, non-negative float (validate-and-clamp).

    A NaN / inf / negative input degrades to 0.0 rather than propagating an
    exception or a nonsensical negative error up into the aggregates (SPEC §5
    clamp ethos, applied to the scoreboard maths). Used ONLY for already-scored
    per-day error values on the aggregation path, never to coerce a raw
    comparison/measured input (see :func:`_finite_or_none`).
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
    negative / non-numeric input — the caller drops the value instead (a missing
    comparison is ABSENT, never a fabricated zero-kWh forecast; SPEC §9).
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
    "comparison_daily_kwh_mae",
    "engine_hourly_mae",
    "engine_vs_best_baseline_pct",
    "stratified_breakdown",
    "kill_gate_passed",
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
    comparison_kwh: dict[str, float],
    engine_hourly_mae: float | None = None,
) -> DayScore:
    """Build one :class:`DayScore` from yesterday's leak-free raw numbers.

    Computes ``engine_daily_abs_err = |engine_kwh - measured_kwh|`` and, for each
    named comparison in ``comparison_kwh`` (only those with a usable recorded
    value — a missing comparison is simply absent, NOT zero), its
    ``|cmp_kwh - measured_kwh|``. ``weather_class`` must be one of
    const.CLOUD_CLASSES (yesterday's dominant class); ``engine_hourly_mae`` is
    the pre-computed hourly MAE for the day (see :func:`hourly_mae`) or None. The
    returned DayScore round-trips through the store's scoreboard ring.

    Pure and total: negative / non-finite inputs are clamped to a sane
    non-negative error by the DayScore dataclass on the persistence round-trip;
    this function performs the leak-free arithmetic only.
    """
    # measured / engine are dropped (day unscored) if non-finite, never zeroed.
    measured = _finite_or_none(measured_kwh)
    engine = _finite_or_none(engine_kwh)
    if measured is None:
        measured = 0.0
    if engine is None:
        engine = 0.0
    engine_err = abs(engine - measured)

    cmp_kwh: dict[str, float] = {}
    cmp_err: dict[str, float] = {}
    if isinstance(comparison_kwh, dict):
        for name, value in comparison_kwh.items():
            # A missing / non-finite / negative comparison is ABSENT, never a
            # fabricated zero-kWh forecast (which would charge the baseline the
            # entire measured energy and unfairly inflate the engine's edge).
            v = _finite_or_none(value)
            if v is None:
                continue
            cmp_kwh[name] = v
            cmp_err[name] = abs(v - measured)

    hmae = None if engine_hourly_mae is None else _finite_nonneg(engine_hourly_mae)
    return DayScore(
        iso_date=str(iso_date),
        weather_class=str(weather_class),
        measured_kwh=measured,
        engine_kwh=engine,
        engine_daily_abs_err=engine_err,
        comparison_kwh=cmp_kwh,
        comparison_daily_abs_err=cmp_err,
        engine_hourly_mae=hmae,
    )


def hourly_mae(
    issued_corrected_hourly: dict[str, float],
    measured_hourly: dict[str, float],
) -> float | None:
    """Mean absolute per-hour Wh error for one day (engine hourly MAE, SPEC §10).

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
    # DAYLIGHT restriction (SPEC §10 Taglicht-Stunden-MAE): restrict the union to
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
    ``engine_daily_kwh_mae`` sensor value (SPEC §10) and the numerator of the
    kill-gate comparison.
    """
    return _engine_daily_kwh_mae_for_days(_window_days_list(state, window_days))


def comparison_daily_kwh_mae(
    state: ScoreboardState,
    *,
    window_days: int,
) -> dict[str, float]:
    """Per-comparison daily-kWh MAE over the window (SPEC §10).

    Returns ``{comparison_name: mae}`` averaging each comparison's
    ``comparison_daily_abs_err`` over ONLY the days that comparison was actually
    scored (a comparison added mid-window is averaged over its own scored days,
    not penalised for the days before it existed). A comparison with zero scored
    days in the window is ABSENT from the result (not zero). These back the
    per-comparison MAE sensors.
    """
    return _comparison_daily_kwh_mae_for_days(
        _window_days_list(state, window_days)
    )


def engine_hourly_mae(
    state: ScoreboardState,
    *,
    window_days: int,
) -> float | None:
    """Engine hourly MAE over the window (mean of per-day hourly MAE).

    Averages ``DayScore.engine_hourly_mae`` across the days in the window that
    HAVE an hourly MAE (days where hourly actuals were unavailable are skipped,
    not counted as zero). Returns None when no day in the window has an hourly
    MAE. Backs the ``engine_hourly_mae`` sensor (SPEC §10 second metric).
    """
    days = _window_days_list(state, window_days)
    vals = [
        _finite_nonneg(d.engine_hourly_mae)
        for d in days
        if d.engine_hourly_mae is not None
    ]
    return _mean(vals)


def engine_vs_best_baseline_pct(
    state: ScoreboardState,
    *,
    window_days: int,
    min_paired_days: int = 1,
) -> float | None:
    """Percent the engine beats the BEST baseline on daily-kWh MAE (SPEC §10).

    MATCHED-PAIR (fairness, SPEC §9): for each comparison, the engine MAE and the
    comparison MAE are both computed over ONLY the days on which that comparison
    was scored (the intersection), and the best baseline is the comparison whose
    PAIRED engine-vs-comparison delta is largest — never a comparison judged on a
    different day subset than the engine. A comparison must have at least
    ``min_paired_days`` scored days to be eligible. Returns ``(b - e) / b * 100``
    for the best eligible comparison — POSITIVE when the engine is better —
    or None when there is no engine day, no eligible comparison, or the best
    baseline's paired MAE is 0 (undefined ratio). Backs the
    ``engine_vs_best_baseline`` sensor / dashboard gauge and the kill-gate.
    """
    days = _window_days_list(state, window_days)
    result = _vs_best_baseline_pct_for_days(days, min_paired_days=min_paired_days)
    return None if result is None else result[0]


def _engine_daily_kwh_mae_for_days(days: list[DayScore]) -> float | None:
    """Engine daily-kWh MAE over an already-selected list of days."""
    return _mean([_finite_nonneg(d.engine_daily_abs_err) for d in days])


def _comparison_daily_kwh_mae_for_days(days: list[DayScore]) -> dict[str, float]:
    """Per-comparison daily-kWh MAE over an already-selected list of days."""
    acc: dict[str, list[float]] = {}
    for d in days:
        errs = d.comparison_daily_abs_err
        if not isinstance(errs, dict):
            continue
        for name, err in errs.items():
            acc.setdefault(name, []).append(_finite_nonneg(err))
    out: dict[str, float] = {}
    for name, errs in acc.items():
        m = _mean(errs)
        if m is not None:
            out[name] = m
    return out


def _vs_best_baseline_pct_for_days(
    days: list[DayScore], *, min_paired_days: int = 1
) -> tuple[float, str, int] | None:
    """Matched-pair ``(pct, best_name, paired_days)`` for a day list, or None.

    For each comparison, restrict BOTH the engine MAE and the comparison MAE to
    the days on which that comparison was scored (the intersection — a
    matched-pair evaluation), require at least ``min_paired_days`` such days, and
    compute ``pct = (cmp_mae - engine_mae) / cmp_mae * 100`` (positive == engine
    better). The "best" baseline is the eligible comparison with the LARGEST pct
    (the hardest one for the engine to beat is the smallest pct; the gate must
    clear the best = largest-pct... — see below). Returns the winning comparison
    plus its paired-day count. None when there is no engine day, no eligible
    comparison, or the best comparison's paired MAE is 0 (undefined ratio).

    Gate semantics: the engine must beat the STRONGEST baseline, i.e. the one on
    which the engine's paired advantage is SMALLEST. So the reported pct is the
    MINIMUM paired pct across eligible comparisons (the engine's worst matched
    result), matching "engine >= margin better than the best baseline".
    """
    if not days:
        return None
    # Collect the set of comparison names that appear anywhere in the window.
    names: set[str] = set()
    for d in days:
        errs = d.comparison_daily_abs_err
        if isinstance(errs, dict):
            names.update(errs)
    if not names:
        return None

    worst_pct: float | None = None
    worst_name = ""
    worst_paired = 0
    for name in names:
        paired = [d for d in days if name in (d.comparison_daily_abs_err or {})]
        if len(paired) < max(1, min_paired_days):
            continue
        engine_mae = _engine_daily_kwh_mae_for_days(paired)
        cmp_mae = _mean(
            [_finite_nonneg(d.comparison_daily_abs_err[name]) for d in paired]
        )
        if engine_mae is None or cmp_mae is None or cmp_mae <= 0.0:
            continue
        pct = (cmp_mae - engine_mae) / cmp_mae * 100.0
        # The engine must beat the STRONGEST baseline => the smallest paired pct.
        if worst_pct is None or pct < worst_pct:
            worst_pct = pct
            worst_name = name
            worst_paired = len(paired)
    if worst_pct is None:
        return None
    return worst_pct, worst_name, worst_paired


def stratified_breakdown(
    state: ScoreboardState,
    *,
    window_days: int,
) -> dict[str, dict]:
    """Per-weather-stratum error breakdown over the window (SPEC §9/§10).

    Returns ``{weather_class: {...}}`` for each class in const.CLOUD_CLASSES that
    has at least one scored day in the window, each inner dict carrying at least:
      * ``"n"``: scored days in this stratum;
      * ``"engine_daily_kwh_mae"``: engine daily-kWh MAE within the stratum;
      * ``"comparison_daily_kwh_mae"``: ``{name: mae}`` within the stratum;
      * ``"engine_vs_best_baseline_pct"``: within-stratum percent (or None).
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
            "comparison_daily_kwh_mae": _comparison_daily_kwh_mae_for_days(
                stratum
            ),
            "engine_vs_best_baseline_pct": _stratum_pct(stratum),
        }
    return out


def _stratum_pct(stratum: list[DayScore]) -> float | None:
    """Within-stratum matched-pair pct (informational; no min-paired gate)."""
    result = _vs_best_baseline_pct_for_days(stratum, min_paired_days=1)
    return None if result is None else result[0]


# ---------------------------------------------------------------------------
# The kill-gate verdict
# ---------------------------------------------------------------------------


def kill_gate_passed(
    state: ScoreboardState,
    *,
    window_days: int,
    gate_margin: float,
    today: str | None = None,
) -> bool | None:
    """Kill-gate verdict: is the engine >= ``gate_margin`` better over a FULL window?

    The gate PASSES when the window holds at least ``window_days`` scored days
    (and at least SCOREBOARD_MIN_WINDOW_DAYS overall — a partial window can never
    assert the gate, SPEC §9 "over a full window"), the ring is not STALE (its
    newest scored day is within SCOREBOARD_MAX_STALENESS_DAYS of ``today`` when
    ``today`` is supplied), AND the matched-pair
    ``engine_vs_best_baseline_pct >= gate_margin * 100`` for at least one
    comparison paired over >= SCOREBOARD_MIN_PAIRED_DAYS days.

    Returns:
      * ``True``  — full, fresh window and the margin is met (engine wins);
      * ``False`` — full, fresh window but the margin is NOT met (engine loses);
      * ``None``  — UNDETERMINED: not enough scored days yet, the window is
        stale, or there is NO eligible comparison to beat (stock install, a
        typo'd / renamed / purged comparison entity). A missing baseline is
        "no baseline data", NOT an engine loss — so the binary_sensor / dashboard
        shows "insufficient data" honestly rather than a false FAIL.
    """
    scored = len(state.days)
    required = max(window_days, SCOREBOARD_MIN_WINDOW_DAYS)
    if scored < required:
        # A partial window can never assert (or deny) the gate: undetermined.
        return None
    if _window_is_stale(state, today=today):
        # Scoring has stopped; the frozen ring must not publish a live verdict.
        return None
    pct = engine_vs_best_baseline_pct(
        state, window_days=window_days, min_paired_days=SCOREBOARD_MIN_PAIRED_DAYS
    )
    if pct is None:
        # Full window but NO comparison to beat (or a degenerate 0 baseline):
        # this is "no baseline data", not an engine loss => UNDETERMINED.
        return None
    return pct >= gate_margin * 100.0


def _newest_scored_date(state: ScoreboardState) -> str | None:
    """The lexicographically-greatest (== newest) scored ISO date, or None."""
    if not state.days:
        return None
    return max(state.days)


def _window_is_stale(state: ScoreboardState, *, today: str | None) -> bool:
    """True when the newest scored day is > SCOREBOARD_MAX_STALENESS_DAYS old.

    ``today`` is the current LOCAL ISO date (the coordinator supplies it). When
    ``today`` is None (pure tests that don't care about recency) staleness is
    never asserted.
    """
    if today is None:
        return False
    newest = _newest_scored_date(state)
    if newest is None:
        return True
    try:
        from datetime import date as _date

        gap = (_date.fromisoformat(today) - _date.fromisoformat(newest)).days
    except (ValueError, TypeError):
        return False
    return gap > SCOREBOARD_MAX_STALENESS_DAYS


def scoreboard_summary(
    state: ScoreboardState,
    *,
    window_days: int,
    gate_margin: float,
    today: str | None = None,
) -> dict:
    """One aggregate dict for the coordinator payload (DATA_KEY_SCOREBOARD).

    Bundles the whole scoreboard view so the coordinator writes it once and the
    sensors / binary_sensor / diagnostics / dashboard read fields off it:
      * ``"engine_daily_kwh_mae"``: float | None;
      * ``"engine_hourly_mae"``: float | None;
      * ``"comparison_daily_kwh_mae"``: {name: float};
      * ``"engine_vs_best_baseline_pct"``: float | None (matched-pair);
      * ``"kill_gate_passed"``: bool | None (None == undetermined);
      * ``"window_days"``: the configured window;
      * ``"scored_days"``: number of scored days currently in the ring;
      * ``"baselines_scored"``: number of comparisons with >= 1 scored day;
      * ``"newest_scored_date"``: newest scored ISO date (staleness visibility);
      * ``"strata"``: :func:`stratified_breakdown` output.
    ``today`` (current local ISO date) enables the staleness suspension of the
    verdict; omit it in pure tests that don't care about recency. Pure assembly
    over the other functions in this module; never raises.
    """
    return {
        "engine_daily_kwh_mae": engine_daily_kwh_mae(
            state, window_days=window_days
        ),
        "engine_hourly_mae": engine_hourly_mae(state, window_days=window_days),
        "comparison_daily_kwh_mae": comparison_daily_kwh_mae(
            state, window_days=window_days
        ),
        "engine_vs_best_baseline_pct": engine_vs_best_baseline_pct(
            state, window_days=window_days,
            min_paired_days=SCOREBOARD_MIN_PAIRED_DAYS,
        ),
        "kill_gate_passed": kill_gate_passed(
            state, window_days=window_days, gate_margin=gate_margin, today=today
        ),
        "window_days": window_days,
        "scored_days": len(state.days),
        "baselines_scored": len(
            comparison_daily_kwh_mae(state, window_days=window_days)
        ),
        "newest_scored_date": _newest_scored_date(state),
        "strata": stratified_breakdown(state, window_days=window_days),
    }
