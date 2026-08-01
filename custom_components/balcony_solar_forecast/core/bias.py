"""FAST learner: intraday clear-sky-index scalar + day-ahead RLS bias.

Owner: bias. Pure, HA-free (stdlib only). Implements SPEC §9.4 "Schneller
Lerner". Two independent mechanisms live here:

  1. INTRADAY SCALAR (transient, NEVER persisted): an exponentially decayed
     (tau ~ INTRADAY_TAU_MINUTES) ratio of measured/forecast site energy over
     a trailing 2-4 h window, computed in CLEAR-SKY-INDEX space (both sides
     normalised by the Haurwitz clear-sky curve so geometry/season cancel),
     applied to the next ~6 h with linear decay toward 1.0, clamped
     [INTRADAY_SCALAR_MIN, INTRADAY_SCALAR_MAX]. After an HA restart it MUST
     re-init to 1.0 — its state is never loaded from disk.

  2. DAY-AHEAD RLS BIAS (persisted as BiasState): one single-parameter
     recursive-least-squares scalar per (cloud class x day part) cell, trained
     nightly from the issued-vs-actuals rings, clamped
     [DAY_AHEAD_BIAS_MIN, DAY_AHEAD_BIAS_MAX].

All tunables come from const. Everything is clamped, gated and disable-able;
a corrupt/absent state degrades to the neutral 1.0 factor, never an
exception (SPEC §9.5).

Frozen public contract (7 implementers depend on these exact signatures):

    # --- intraday scalar (transient) ---
    compute_intraday_scalar(samples, *, now) -> float
    apply_intraday_scalar(hourly_wh, scalar, *, now) -> dict[str, float]

    # --- day-ahead RLS bias (persisted) ---
    classify_cloud(*, cloud_low, cloud_mid, cloud_high, visibility_m, month,
                   ghi=None, elevation_deg=None) -> str
    day_part_for_hour(local_hour) -> str
    train_day_ahead_bias(state, samples) -> BiasState
    apply_day_ahead_bias(state, *, cloud_class, day_part, wh) -> float

See the docstrings for the argument shapes. Implementers must NOT change these
signatures without updating this contract module.

--- IMPLEMENTATION NOTES (bias owner) ------------------------------------

Intraday k_c-space ratio, why ratio-of-sums (not a mean of per-sample
ratios): each sample carries the site energy already normalised by the
Haurwitz clear-sky reference (``measured_kc`` / ``modeled_kc``). We form the
scalar as

    s = sum_i(w_i * measured_kc_i) / sum_i(w_i * modeled_kc_i),
    w_i = exp(-age_i / tau) * modeled_wh_i.

This ratio-of-sums is invariant to the plane mix (SPEC §9.4: "Geometrie/Saison
cancel"): scaling every plane's contribution by the same measured/modeled
proportion leaves ``s`` unchanged regardless of how the total splits across
planes, and low-elevation slots (tiny denominator, noisy per-sample ratio)
cannot dominate because they contribute little to either sum. A plain mean of
``measured_kc_i / modeled_kc_i`` would over-weight those noisy dawn/dusk slots
and is deliberately avoided. The weight is the time decay times the slot's
modeled energy (SPEC §9.4): high-production slots dominate and dawn/dusk slots
fall out on their own — the live analysis 2026-08 showed the unweighted scalar
flipping 0.39<->1.0 in the evening while behaving correctly at midday. Samples
whose measured AND modeled energy both sit under INTRADAY_NEUTRAL_FLOOR_WH are
skipped outright (a ratio of two noise floors is meaningless); one-sided
samples (modeled high, measured low) keep passing, that is the real
over-forecast signal this layer exists for.

Day-ahead RLS is the textbook single-regressor recursive least squares with
exponential forgetting, specialised to one scalar parameter theta modelling
``measured_wh ~= theta * modeled_wh``:

    K   = P*x / (lambda + x*P*x)          (scalar gain)
    e   = y - theta*x                     (a-priori residual)
    theta_new = theta + K*e
    P_new     = (P - K*x*P) / lambda      (covariance update)

with x = modeled_wh, y = measured_wh, lambda = RLS_FORGETTING_FACTOR,
P0 = RLS_INIT_COVARIANCE. Degenerate steps (x ~ 0, non-finite inputs) are
skipped so a dark/garbage day never corrupts a cell. theta is clamped into
the day-ahead band on every write; the covariance is floored positive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..const import (
    CLOUD_CLASS_CLEAR,
    CLOUD_CLASS_FOG,
    CLOUD_CLASS_MIXED,
    CLOUD_CLASS_OVERCAST,
    CLOUD_CLEAR_MAX_PCT,
    CLOUD_KC_CLEAR_MIN,
    CLOUD_KC_MIN_ELEVATION_DEG,
    CLOUD_KC_OVERCAST_MAX,
    CLOUD_OVERCAST_MIN_PCT,
    DAY_AHEAD_BIAS_MAX,
    DAY_AHEAD_BIAS_MIN,
    DAY_AHEAD_BIAS_NEUTRAL,
    DAY_PART_AFTERNOON,
    DAY_PART_AFTERNOON_START_HOUR,
    DAY_PART_BLEND_HALFWIDTH_MIN,
    DAY_PART_MIDDAY,
    DAY_PART_MORNING,
    DAY_PART_MORNING_END_HOUR,
    DAY_PART_SOLAR_BLEND_HALFWIDTH_H,
    FOG_CLOUD_LOW_PCT,
    FOG_MONTHS,
    FOG_VISIBILITY_M,
    INTRADAY_APPLY_HORIZON_MINUTES,
    INTRADAY_MIN_MODELED_WH,
    INTRADAY_MIN_TRAILING_MINUTES,
    INTRADAY_NEUTRAL,
    INTRADAY_NEUTRAL_FLOOR_WH,
    INTRADAY_SCALAR_MAX,
    INTRADAY_SCALAR_MIN,
    INTRADAY_TAU_MINUTES,
    INTRADAY_TRAILING_WINDOW_MINUTES,
    MIDDAY_SOLAR_HALFWIDTH_H,
    RLS_FORGETTING_FACTOR,
    RLS_INIT_COVARIANCE,
    RLS_MIN_DAY_SECTION_MODELED_WH,
)
from .clearsky import clear_sky_index
from .types import BiasCell, BiasState

__all__ = [
    "IntradaySample",
    "DayAheadSample",
    "compute_intraday_scalar",
    "apply_intraday_scalar",
    "intraday_factor_at",
    "classify_cloud",
    "day_part_for_hour",
    "day_part_for_solar",
    "day_ahead_factor",
    "day_ahead_factor_solar",
    "train_day_ahead_bias",
    "apply_day_ahead_bias",
    "reseed_day_ahead_bias",
]


# ---------------------------------------------------------------------------
# Small numeric helpers (kept local; the core stays stdlib-only).
# ---------------------------------------------------------------------------


def _is_finite(x: float) -> bool:
    """True iff ``x`` is a finite real number."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _clamp(v: float, lo: float, hi: float) -> float:
    """Clamp ``v`` into [lo, hi]; non-finite -> ``lo`` (never propagate NaN)."""
    if not _is_finite(v):
        return lo
    f = float(v)
    return lo if f < lo else hi if f > hi else f


# ---------------------------------------------------------------------------
# Sample shapes (frozen dataclasses; the coordinator builds them each tick /
# nightly). Kept minimal and plain so they round-trip cheaply and the pure
# tests can construct them without HA.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntradaySample:
    """One trailing-window observation for the intraday scalar.

    ``at`` is the tz-aware UTC slot time; ``measured_kc`` / ``modeled_kc`` are
    the site energy already normalised into clear-sky-index space (measured or
    modeled site Wh divided by the Haurwitz clear-sky reference energy for that
    slot); ``modeled_wh`` is the raw modeled site Wh (used for the
    >INTRADAY_MIN_MODELED_WH gate). Provided by the coordinator each tick from
    live actual reads + the raw physics curve.
    """

    at: datetime
    measured_kc: float
    modeled_kc: float
    modeled_wh: float


@dataclass(frozen=True, slots=True)
class DayAheadSample:
    """One nightly training observation for the day-ahead RLS.

    ``cloud_class`` (a const CLOUD_CLASS_* string) and ``day_part`` (a const
    DAY_PART_* string) select the cell; ``measured_wh`` / ``modeled_wh`` are the
    day-part-aggregated site energy for one issued day, derived from the issued
    (raw) snapshot and the actuals ring.
    """

    cloud_class: str
    day_part: str
    measured_wh: float
    modeled_wh: float


# ---------------------------------------------------------------------------
# Intraday scalar (transient — never persisted)
# ---------------------------------------------------------------------------


def compute_intraday_scalar(
    samples: list[IntradaySample],
    *,
    now: datetime,
) -> float:
    """Energy- and recency-weighted measured/modeled ratio in k_c space (SPEC §9.4).

    Over the trailing INTRADAY_TRAILING_WINDOW_MINUTES up to ``now``, weight
    each sample by exp(-age_minutes / INTRADAY_TAU_MINUTES) x ``modeled_wh``
    and take the weighted ratio of measured k_c to modeled k_c, using only
    samples with ``modeled_wh`` > INTRADAY_MIN_MODELED_WH whose measured AND
    modeled slot energy are not both below INTRADAY_NEUTRAL_FLOOR_WH. Requires
    at least INTRADAY_MIN_TRAILING_MINUTES of coverage; returns
    INTRADAY_NEUTRAL (1.0) when there is too little data. The result is
    clamped to [INTRADAY_SCALAR_MIN, INTRADAY_SCALAR_MAX].

    This value is TRANSIENT: the coordinator holds it in memory and re-inits to
    1.0 after any restart; it is never written to the store.

    Robustness: samples in the future or older than the trailing window are
    dropped; a clock jump that leaves every sample out-of-window collapses to
    neutral rather than acting on stale data. The ratio is a ratio-of-sums of
    the per-slot site k_c, each sample weighted by the exp(-age/tau) time
    decay TIMES its modeled slot energy — high-production slots dominate and
    noisy low-energy dawn/dusk slots fall out on their own (SPEC §9.4; the
    equal-time-weighted scalar flipped 0.39<->1.0 on summer evenings).
    Samples where BOTH sides sit under INTRADAY_NEUTRAL_FLOOR_WH are skipped
    (the measured Wh is recovered exactly as measured_kc x modeled_wh /
    modeled_kc — both sides share the same clear-sky reference); a one-sided
    sample (modeled high, measured low) is a genuine over-forecast and keeps
    its full weight. The plane mix cancels because measured and modeled are
    both site sums normalised by the same clear-sky reference.
    Divide-by-near-zero is guarded by the modeled-Wh gate plus a floor on the
    weighted modeled-k_c denominator.
    """
    if not samples:
        return INTRADAY_NEUTRAL

    weighted_measured = 0.0
    weighted_modeled = 0.0
    span_min = 0.0   # oldest .. newest in-window age spread (coverage proxy)
    oldest_age = None
    newest_age = None
    used = 0

    for s in samples:
        # Defensive: skip malformed / non-finite records entirely.
        at = getattr(s, "at", None)
        if not isinstance(at, datetime):
            continue
        measured_kc = getattr(s, "measured_kc", None)
        modeled_kc = getattr(s, "modeled_kc", None)
        modeled_wh = getattr(s, "modeled_wh", None)
        if not (_is_finite(measured_kc) and _is_finite(modeled_kc)
                and _is_finite(modeled_wh)):
            continue

        # Age in minutes; drop future samples and anything past the window.
        age_min = (now - at).total_seconds() / 60.0
        if age_min < 0.0 or age_min > INTRADAY_TRAILING_WINDOW_MINUTES:
            continue
        # Energy gate: only slots with meaningful modeled energy contribute
        # (avoids dawn/dusk / deep-shade divide-by-near-zero noise).
        if float(modeled_wh) <= INTRADAY_MIN_MODELED_WH:
            continue
        # Clear-sky index must be usable on both sides. Negative/zero modeled
        # k_c cannot anchor a ratio.
        m_kc = float(measured_kc)
        f_kc = float(modeled_kc)
        if f_kc <= 0.0 or m_kc < 0.0:
            continue
        # Neutral floor (SPEC §9.4): when BOTH sides are dim the ratio of two
        # noise floors is meaningless — skip the sample. The measured Wh is
        # recovered exactly (both k_c values share the slot's clear-sky
        # reference). One-sided samples (modeled high, measured low = a REAL
        # over-forecast) keep passing; that is the signal this layer is for.
        e_wh = float(modeled_wh)
        if (e_wh < INTRADAY_NEUTRAL_FLOOR_WH
                and m_kc * e_wh / f_kc < INTRADAY_NEUTRAL_FLOOR_WH):
            continue

        # Weight: recency decay TIMES the modeled slot energy (SPEC §9.4) —
        # high-production slots dominate, dim dawn/dusk slots fall out.
        w = math.exp(-age_min / INTRADAY_TAU_MINUTES) * e_wh
        weighted_measured += w * m_kc
        weighted_modeled += w * f_kc
        used += 1
        if oldest_age is None or age_min > oldest_age:
            oldest_age = age_min
        if newest_age is None or age_min < newest_age:
            newest_age = age_min

    if used == 0 or oldest_age is None or newest_age is None:
        return INTRADAY_NEUTRAL

    # Coverage gate: the in-window samples must span at least the minimum
    # trailing duration, otherwise we do not have enough recent history to
    # trust the correction (SPEC §9.4: "letzten 2-4 h").
    span_min = oldest_age - newest_age
    if span_min < INTRADAY_MIN_TRAILING_MINUTES:
        return INTRADAY_NEUTRAL

    # Guard the denominator: with the modeled-Wh gate and f_kc>0 filter the sum
    # is already strictly positive, but stay defensive against float underflow.
    if weighted_modeled <= 0.0 or not _is_finite(weighted_modeled):
        return INTRADAY_NEUTRAL

    scalar = weighted_measured / weighted_modeled
    return _clamp(scalar, INTRADAY_SCALAR_MIN, INTRADAY_SCALAR_MAX)


def _intraday_factor_at(age_min: float, scalar: float) -> float:
    """Per-hour applied factor: full ``scalar`` at now, linear to 1.0 by horizon.

    ``age_min`` is minutes forward from ``now`` (>=0). The correction ramps
    ``scalar -> 1.0`` linearly over INTRADAY_APPLY_HORIZON_MINUTES; beyond the
    horizon (or for past hours) the factor is exactly 1.0. The result is
    clamped into the intraday band as a final safety net.
    """
    if age_min <= 0.0:
        frac = 1.0
    elif age_min >= INTRADAY_APPLY_HORIZON_MINUTES:
        frac = 0.0
    else:
        frac = 1.0 - (age_min / INTRADAY_APPLY_HORIZON_MINUTES)
    factor = 1.0 + (scalar - 1.0) * frac
    return _clamp(factor, INTRADAY_SCALAR_MIN, INTRADAY_SCALAR_MAX)


# Public alias: the coordinator composes this per-slot decay factor into
# LearnerHooks.slot_factor (additive to the frozen contract, not a change).
intraday_factor_at = _intraday_factor_at


def apply_intraday_scalar(
    hourly_wh: dict[str, float],
    scalar: float,
    *,
    now: datetime,
) -> dict[str, float]:
    """Apply the intraday scalar to the forward hourly curve with linear decay.

    The scalar is applied full-strength at ``now`` and ramps linearly back to
    1.0 over INTRADAY_APPLY_HORIZON_MINUTES; hours beyond the horizon are
    unchanged. ``hourly_wh`` is keyed by ISO-8601 UTC hour start. Returns a new
    dict (input untouched). The applied per-hour factor stays within
    [INTRADAY_SCALAR_MIN, INTRADAY_SCALAR_MAX].

    A neutral / non-finite scalar, or an unparseable key, leaves that entry
    unchanged. Fully-past hours (the whole hour ended at/before ``now``) are
    never scaled. The hour currently in progress (start <= now < start+1h) and
    all future hours are scaled; the forward age used for the decay ramp is
    measured from ``now`` to the hour's START, clamped at 0 so the in-progress
    hour gets the full scalar.
    """
    out: dict[str, float] = {}
    if not isinstance(hourly_wh, dict):
        return out

    apply_scaling = _is_finite(scalar) and float(scalar) != INTRADAY_NEUTRAL

    for key, wh in hourly_wh.items():
        # Carry non-finite / unparseable energies through untouched (float()
        # them where possible so the returned dict is clean).
        val = float(wh) if _is_finite(wh) else wh

        if not apply_scaling:
            out[key] = val
            continue

        hour_start = _parse_iso_hour(key)
        if hour_start is None or not _is_finite(wh):
            out[key] = val
            continue

        # Fully-past hours (ended at/before now) are left untouched: only the
        # in-progress hour and future hours are corrected.
        if hour_start + timedelta(hours=1) <= now:
            out[key] = val
            continue

        # Forward age from now to the hour's start. The in-progress hour
        # (start <= now) maps to age <= 0 -> full scalar; future hours ramp.
        age_min = (hour_start - now).total_seconds() / 60.0
        factor = _intraday_factor_at(age_min, float(scalar))
        out[key] = float(val) * factor

    return out


def _parse_iso_hour(key: str) -> datetime | None:
    """Parse an ISO-8601 UTC hour-start key into a tz-aware datetime, or None.

    Tolerant of a trailing ``Z`` (mapped to +00:00). Naive results are treated
    as UTC. Any parse failure returns None so the caller can pass the entry
    through unscaled instead of raising.
    """
    if not isinstance(key, str):
        return None
    text = key.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Day-ahead RLS bias (persisted as BiasState)
# ---------------------------------------------------------------------------


def classify_cloud(
    *,
    cloud_low: float,
    cloud_mid: float,
    cloud_high: float,
    visibility_m: float | None,
    month: int,
    ghi: float | None = None,
    elevation_deg: float | None = None,
) -> str:
    """Cloud class in {clear, mixed, overcast, fog} (SPEC §8).

    Fog test FIRST (it overrides everything): fog when ``visibility_m`` <
    FOG_VISIBILITY_M OR (``cloud_low`` > FOG_CLOUD_LOW_PCT AND ``month`` in
    FOG_MONTHS). ``visibility_m`` is sentinel-free: ``None`` (no reading,
    provider hole) means UNKNOWN and never fires the fog rule — only a real
    measured visibility does (a measured 0 m IS dense fog).

    Otherwise split by the CLEAR-SKY INDEX ``k_c = ghi / haurwitz_ghi(elevation)``
    when both ``ghi`` and ``elevation_deg`` are supplied and the sun is at least
    CLOUD_KC_MIN_ELEVATION_DEG above the horizon (below that the Haurwitz
    reference is too coarse): k_c >= CLOUD_KC_CLEAR_MIN => clear,
    k_c <= CLOUD_KC_OVERCAST_MAX => overcast, else mixed. k_c reflects the
    irradiance that actually reaches the horizontal, so the shared taxonomy that
    the RLS bias, the quantile bins and the scoreboard strata all key on stays
    consistent — the previous random-overlap layer cover counted mid/high decks
    at full weight and routed genuinely sunny hours into the overcast cell (A5).

    FALLBACK (twilight / low sun / missing GHI): the random-overlap TOTAL layer
    cover ``total = 100*(1 - (1-low/100)*(1-mid/100)*(1-high/100))`` (each layer
    clamped to [0, 100]): < CLOUD_CLEAR_MAX_PCT => clear,
    > CLOUD_OVERCAST_MIN_PCT => overcast, else mixed. Total (not the mean) so a
    single opaque low deck (100/0/0) still overcasts rather than misfiling mixed.

    Returns one of the const CLOUD_CLASS_* strings. Non-finite inputs degrade
    gracefully: an unusable visibility does not fire the fog rule, an unusable
    GHI / elevation falls back to the layer cover, and unusable cover components
    are treated as 0.
    """
    vis = float(visibility_m) if _is_finite(visibility_m) else float("inf")
    low = float(cloud_low) if _is_finite(cloud_low) else 0.0
    mid = float(cloud_mid) if _is_finite(cloud_mid) else 0.0
    high = float(cloud_high) if _is_finite(cloud_high) else 0.0
    try:
        m = int(month)
    except (TypeError, ValueError):
        m = 0

    # Fog first — it overrides both the k_c and the cover-based split.
    if vis < FOG_VISIBILITY_M or (low > FOG_CLOUD_LOW_PCT and m in FOG_MONTHS):
        return CLOUD_CLASS_FOG

    # Clear-sky-index split when GHI and a usable sun elevation are available.
    if (
        ghi is not None
        and elevation_deg is not None
        and _is_finite(ghi)
        and _is_finite(elevation_deg)
        and float(elevation_deg) >= CLOUD_KC_MIN_ELEVATION_DEG
    ):
        kc = clear_sky_index(float(ghi), float(elevation_deg))
        if kc >= CLOUD_KC_CLEAR_MIN:
            return CLOUD_CLASS_CLEAR
        if kc <= CLOUD_KC_OVERCAST_MAX:
            return CLOUD_CLASS_OVERCAST
        return CLOUD_CLASS_MIXED

    # Fallback: random-overlap total cover (layers occlude cumulatively; a single
    # full deck must classify overcast). Clamp each layer to [0, 100] first.
    lc = _clamp(low, 0.0, 100.0)
    mc = _clamp(mid, 0.0, 100.0)
    hc = _clamp(high, 0.0, 100.0)
    total = 100.0 * (1.0 - (1.0 - lc / 100.0) * (1.0 - mc / 100.0) * (1.0 - hc / 100.0))
    if total < CLOUD_CLEAR_MAX_PCT:
        return CLOUD_CLASS_CLEAR
    if total > CLOUD_OVERCAST_MIN_PCT:
        return CLOUD_CLASS_OVERCAST
    return CLOUD_CLASS_MIXED


def day_part_for_hour(local_hour: int) -> str:
    """Map a local clock hour (0..23) to a day part (SPEC §8).

    [dawn, DAY_PART_MORNING_END_HOUR) => morning; [that,
    DAY_PART_AFTERNOON_START_HOUR) => midday; [that, dusk) => afternoon.
    Returns a const DAY_PART_* string. Out-of-range / non-integer hours are
    coerced by clock arithmetic (mod 24) so a stray value never raises.
    """
    try:
        h = int(local_hour) % 24
    except (TypeError, ValueError):
        h = 0
    if h < DAY_PART_MORNING_END_HOUR:
        return DAY_PART_MORNING
    if h < DAY_PART_AFTERNOON_START_HOUR:
        return DAY_PART_MIDDAY
    return DAY_PART_AFTERNOON


def day_part_for_solar(hours_from_noon: float) -> str:
    """Map APPARENT SOLAR time to a day part (v0.19, SPEC §8).

    ``hours_from_noon`` is the signed solar time relative to solar noon
    (``solpos.hours_from_solar_noon``): negative = morning, 0 = noon,
    positive = afternoon. Midday brackets solar noon symmetrically by
    ``MIDDAY_SOLAR_HALFWIDTH_H``; outside it is morning (before) / afternoon
    (after). Unlike :func:`day_part_for_hour` this is anchored to the SUN, so
    the boundary does not drift with DST or the season. Non-finite input
    coerces to midday (neutral-ish) so a stray value never raises.
    """
    try:
        h = float(hours_from_noon)
    except (TypeError, ValueError):
        return DAY_PART_MIDDAY
    if not _is_finite(h):
        return DAY_PART_MIDDAY
    if h <= -MIDDAY_SOLAR_HALFWIDTH_H:
        return DAY_PART_MORNING
    if h >= MIDDAY_SOLAR_HALFWIDTH_H:
        return DAY_PART_AFTERNOON
    return DAY_PART_MIDDAY


def day_ahead_factor_solar(
    state: BiasState,
    *,
    cloud_class: str,
    hours_from_noon: float,
) -> float:
    """Applied day-ahead bias for one slot, binned by SOLAR time — CONTINUOUS.

    The solar-time analogue of :func:`day_ahead_factor`: the learned per-part
    cells are the anchors, and within ``DAY_PART_SOLAR_BLEND_HALFWIDTH_H`` (in
    SOLAR hours) either side of an internal boundary (at
    ``-/+MIDDAY_SOLAR_HALFWIDTH_H`` from solar noon) the two adjacent parts'
    factors are linearly blended by the slot's solar time, so the served
    correction ramps continuously instead of stepping. Because the coordinate
    is the sun's own hour angle (``solpos.hours_from_solar_noon``), the
    boundary tracks solar noon rather than a fixed wall-clock hour — no DST or
    seasonal drift, and the morning/afternoon split stays symmetric about noon.

    Both blended cells use the SAME ``cloud_class``; a convex combination of two
    in-band factors stays in the clamp band. Never raises.
    """
    try:
        h = float(hours_from_noon)
    except (TypeError, ValueError):
        h = 0.0
    if not _is_finite(h):
        h = 0.0

    half = DAY_PART_SOLAR_BLEND_HALFWIDTH_H
    if half > 0.0:
        for boundary, lo_part, hi_part in (
            (-MIDDAY_SOLAR_HALFWIDTH_H, DAY_PART_MORNING, DAY_PART_MIDDAY),
            (MIDDAY_SOLAR_HALFWIDTH_H, DAY_PART_MIDDAY, DAY_PART_AFTERNOON),
        ):
            if boundary - half <= h < boundary + half:
                weight = (h - (boundary - half)) / (2.0 * half)
                lo = state.get_bias(cloud_class, lo_part)
                hi = state.get_bias(cloud_class, hi_part)
                return (1.0 - weight) * lo + weight * hi

    return state.get_bias(cloud_class, day_part_for_solar(h))


def day_ahead_factor(
    state: BiasState,
    *,
    cloud_class: str,
    local_hour: int,
    local_minute: int = 0,
) -> float:
    """Applied day-ahead bias for one slot — CONTINUOUS across day-part edges.

    The bias is LEARNED per (cloud class x day part), but a hard per-part step
    has no physical basis: the forecast shape comes from weather x physics x
    shading, which is smooth, so the correction on top must be smooth too. The
    learned cells are the anchors; within DAY_PART_BLEND_HALFWIDTH_MIN either
    side of an INTERNAL boundary (morning/midday at DAY_PART_MORNING_END_HOUR,
    midday/afternoon at DAY_PART_AFTERNOON_START_HOUR) the two adjacent parts'
    factors are LINEARLY BLENDED by the slot's local time of day, so the served
    factor ramps continuously instead of stepping. Outside the transition zones
    it is exactly the pure day-part factor (:func:`day_part_for_hour` +
    ``state.get_bias``), so away from boundaries nothing changes.

    Both blended cells use the SAME ``cloud_class`` (the slot's own class), so a
    starved cell simply pulls the blend toward neutral near the boundary — a
    convex combination of two in-band factors stays in the clamp band. Only the
    two internal boundaries are smoothed (dawn/dusk carry ~no production, so the
    first/last part edges are not blended). Never raises.
    """
    try:
        h = int(local_hour) % 24
    except (TypeError, ValueError):
        h = 0
    try:
        m = int(local_minute)
    except (TypeError, ValueError):
        m = 0
    m = 0 if m < 0 else (59 if m > 59 else m)
    tod = h + m / 60.0  # continuous local time-of-day, hours

    half = DAY_PART_BLEND_HALFWIDTH_MIN / 60.0
    if half > 0.0:
        for boundary, lo_part, hi_part in (
            (DAY_PART_MORNING_END_HOUR, DAY_PART_MORNING, DAY_PART_MIDDAY),
            (DAY_PART_AFTERNOON_START_HOUR, DAY_PART_MIDDAY, DAY_PART_AFTERNOON),
        ):
            if boundary - half <= tod < boundary + half:
                # weight ramps 0 -> 1 across [boundary-half, boundary+half];
                # 0.5 exactly at the boundary.
                weight = (tod - (boundary - half)) / (2.0 * half)
                lo = state.get_bias(cloud_class, lo_part)
                hi = state.get_bias(cloud_class, hi_part)
                return (1.0 - weight) * lo + weight * hi

    return state.get_bias(cloud_class, day_part_for_hour(h))


def _rls_step(cell: BiasCell, modeled_wh: float, measured_wh: float) -> BiasCell:
    """One single-parameter RLS update of a bias cell (theta ~ measured/modeled).

    Model: ``measured = theta * modeled`` with regressor x = modeled_wh,
    target y = measured_wh, forgetting factor lambda = RLS_FORGETTING_FACTOR.
    Standard scalar RLS:

        K = P*x / (lambda + x*P*x)
        theta_new = theta + K * (y - theta*x)
        P_new     = (P - K*x*P) / lambda

    Degenerate steps (non-finite inputs, x ~ 0, or a collapsing denominator)
    are SKIPPED: the cell is returned unchanged and ``n`` is NOT incremented,
    so a dark or garbage day cannot corrupt or age-out a cell.
    """
    if not (_is_finite(modeled_wh) and _is_finite(measured_wh)):
        return cell
    x = float(modeled_wh)
    y = float(measured_wh)
    # Both must be usable non-negative energies; a ~0 regressor carries no bias
    # information (0 = theta*0 for any theta) so we skip it. The floor is the
    # DAY-SECTION aggregate gate (the regressor sums several daylight hours),
    # not the 15-min-slot INTRADAY_MIN_MODELED_WH.
    if x <= RLS_MIN_DAY_SECTION_MODELED_WH or y < 0.0:
        return cell

    lam = RLS_FORGETTING_FACTOR
    p = cell.covariance if _is_finite(cell.covariance) and cell.covariance > 0.0 else RLS_INIT_COVARIANCE
    theta = cell.theta if _is_finite(cell.theta) else DAY_AHEAD_BIAS_NEUTRAL

    denom = lam + x * p * x
    if denom <= 0.0 or not _is_finite(denom):
        return cell
    gain = (p * x) / denom
    residual = y - theta * x
    theta_new = theta + gain * residual
    p_new = (p - gain * x * p) / lam

    if not (_is_finite(theta_new) and _is_finite(p_new)):
        return cell
    # Floor the covariance strictly positive so the estimator never freezes.
    p_new = p_new if p_new > 0.0 else RLS_INIT_COVARIANCE
    theta_clamped = _clamp(theta_new, DAY_AHEAD_BIAS_MIN, DAY_AHEAD_BIAS_MAX)
    return BiasCell(theta=theta_clamped, covariance=p_new, n=cell.n + 1)


def train_day_ahead_bias(
    state: BiasState,
    samples: list[DayAheadSample],
) -> BiasState:
    """Nightly RLS update of the day-ahead bias cells (SPEC §9.5).

    For each sample, run one single-parameter RLS step (forgetting factor
    RLS_FORGETTING_FACTOR, init covariance RLS_INIT_COVARIANCE) on the cell
    keyed by (cloud_class, day_part), regressing measured_wh on modeled_wh to
    estimate the multiplicative bias theta. Returns a NEW BiasState (input
    untouched); every cell's theta is clamped to [DAY_AHEAD_BIAS_MIN, MAX] and
    ``n`` incremented on each accepted step. Idempotence over a given night is
    the coordinator's responsibility (date-keyed guard); this function is a
    pure state->state map.

    Samples with an unknown cloud class / day part, or with degenerate energies,
    are silently skipped (no cell created) so junk never seeds a cell.
    """
    # Start from a shallow copy of the existing cells (BiasCell is immutable).
    cells: dict[str, BiasCell] = dict(state.cells) if isinstance(state.cells, dict) else {}

    valid_classes = (
        CLOUD_CLASS_CLEAR,
        CLOUD_CLASS_MIXED,
        CLOUD_CLASS_OVERCAST,
        CLOUD_CLASS_FOG,
    )
    valid_parts = (DAY_PART_MORNING, DAY_PART_MIDDAY, DAY_PART_AFTERNOON)

    for sample in samples or ():
        cloud_class = getattr(sample, "cloud_class", None)
        day_part = getattr(sample, "day_part", None)
        if cloud_class not in valid_classes or day_part not in valid_parts:
            continue
        modeled_wh = getattr(sample, "modeled_wh", None)
        measured_wh = getattr(sample, "measured_wh", None)

        key = BiasState.cell_key(cloud_class, day_part)
        cell = cells.get(key, BiasCell())
        updated = _rls_step(cell, modeled_wh, measured_wh)
        # Only write back when the step was accepted (n advanced); this avoids
        # materialising an untrained neutral cell for a skipped junk sample.
        if updated is not cell:
            cells[key] = updated

    return BiasState(cells=cells, version=state.version)


def reseed_day_ahead_bias(state: BiasState, *, n_cap: int) -> BiasState:
    """Re-open every day-ahead bias cell for fast re-adaptation (A4/FOR-4).

    Called when the forecast-relevant site config changed: the learned theta of
    each cell still fits the OLD geometry and, at RLS steady state (n ~ 100,
    lambda RLS_FORGETTING_FACTOR), the gain has decayed so far that live days
    only nudge theta ~0.001/day — months to re-converge. The learning-rate lever
    is the RLS covariance P (the gain K = P*x/(lambda + x*P*x) depends on P, NOT
    on n), so this resets every cell's ``covariance`` to RLS_INIT_COVARIANCE (the
    same "large P0 => fast initial adaptation" seed a cold cell starts from) and
    caps its effective sample count ``n`` at ``n_cap`` — keeping the current
    theta as the starting point (strictly gentler than reset_day_ahead_bias,
    which clears theta to neutral). A cell already at or below the cap keeps its
    n; its covariance is re-opened regardless. Returns a NEW state; input
    untouched. Empty input round-trips unchanged.
    """
    from dataclasses import replace

    cap = max(0, int(n_cap))
    cells: dict[str, BiasCell] = {}
    src = state.cells if isinstance(state.cells, dict) else {}
    for key, cell in src.items():
        cells[key] = replace(
            cell,
            covariance=RLS_INIT_COVARIANCE,
            n=min(int(cell.n), cap),
        )
    return BiasState(cells=cells, version=state.version)


def apply_day_ahead_bias(
    state: BiasState,
    *,
    cloud_class: str,
    day_part: str,
    wh: float,
) -> float:
    """Scale one hour's Wh by the (cloud class x day part) day-ahead bias.

    Delegates to ``state.get_bias`` (neutral when the cell is missing or has
    fewer than RLS_MIN_SAMPLES trained days), multiplies ``wh`` by the clamped
    theta and returns the corrected Wh (>= 0).

    A non-finite input Wh degrades to 0.0 rather than propagating NaN.
    """
    if not _is_finite(wh):
        return 0.0
    bias = state.get_bias(cloud_class, day_part)
    if not _is_finite(bias):
        bias = DAY_AHEAD_BIAS_NEUTRAL
    result = float(wh) * float(bias)
    return result if result > 0.0 else 0.0
