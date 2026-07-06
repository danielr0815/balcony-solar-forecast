"""Quantile bands P10/P50/P90 — historical-simulation uncertainty (SPEC §6/§10).

Owner: quantiles. Pure, HA-free (stdlib only). This module implements the
FROZEN public contract (signatures + docstrings) that the engine hook, the
store, the coordinator, the sensors, the diagnostics and the pure tests depend
on.

Design (SPEC §6, nonparametric historical simulation)
-----------------------------------------------------
Maintain a 90-day ring (QUANTILE_RING_DAYS) of hourly RELATIVE errors

    relerr = measured_wh / corrected_forecast_wh

keyed by (weather class x day part) — the SAME (cloud_class, day_part) taxonomy
as the day-ahead RLS bias (``QuantileState.bin_key``). At FORECAST time, for
each hour, look up the matching bin's empirical P10/P50/P90 multipliers and
apply them to that hour's corrected forecast Wh to produce the band curves.

COLD START (SPEC §6/§10, "no fake spread"): a bin with fewer than
QUANTILE_MIN_SAMPLES samples collapses its band to P50 (p10 == p50 == p90);
an empty bin is the neutral identity (1.0). A band is therefore NEVER wider than
the data supports.

TRAINING (nightly, reuses the existing rings): from issued(corrected) hourly vs
the measured hourly actuals, one relerr sample per daylight hour whose corrected
forecast Wh exceeds QUANTILE_MIN_FORECAST_WH, clamped to
[QUANTILE_REL_ERR_MIN, QUANTILE_REL_ERR_MAX], appended to its bin's ring.

All tunables come from const. Every path is validate-and-clamp: a corrupt/absent
state or an empty bin degrades to the neutral band, never an exception (SPEC §5).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime

from ..const import (
    QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN,
    QUANTILE_MIN_FORECAST_WH,
    QUANTILE_MIN_SAMPLES,
    QUANTILE_NEUTRAL_MULT,
    QUANTILE_P_HIGH,
    QUANTILE_P_LOW,
    QUANTILE_P_MID,
    QUANTILE_REL_ERR_MAX,
    QUANTILE_REL_ERR_MIN,
    QUANTILE_RING_DAYS,
)
from .types import QuantileBands, QuantileState

__all__ = [
    "QuantileSample",
    "empirical_percentile",
    "bands_for_bin",
    "quantile_bin_key",
    "train_quantiles",
    "apply_bands",
    "band_curve_from_corrected",
]


# Per-bin ring cap. The ring holds hourly samples and a single bin can receive a
# whole day-part's worth per day (~8 daylight hours in summer), so the cap is
# QUANTILE_RING_DAYS x QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN — sizing the FIFO to
# ~90 days of a frequently-hit bin rather than ~2 weeks (SPEC §6 "90-day ring").
# Caps by COUNT (samples are un-timestamped in this pure layer); a true
# date-windowed ring is a follow-up.
_BIN_RING_CAP = QUANTILE_RING_DAYS * QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN


def _finite(x: object) -> float | None:
    """Coerce to a finite float, or None on garbage (NaN/inf/non-numeric)."""
    try:
        f = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


# ---------------------------------------------------------------------------
# Sample shape (frozen; the coordinator builds these nightly from the issued
# corrected hourly curve + the measured hourly actuals ring).
# ---------------------------------------------------------------------------


class QuantileSample:
    """One nightly hourly relative-error observation for the quantile ring.

    ``cloud_class`` (const CLOUD_CLASS_*) + ``day_part`` (const DAY_PART_*) select
    the bin; ``measured_wh`` / ``corrected_wh`` are the measured vs. the
    issued-CORRECTED site energy for one daylight hour of one issued day. The
    trainer forms ``relerr = measured_wh / corrected_wh`` (clamped), and skips
    hours whose ``corrected_wh`` <= QUANTILE_MIN_FORECAST_WH. Realised as a plain
    object (attribute access only) so the pure tests can build it without HA.
    """

    __slots__ = ("cloud_class", "day_part", "measured_wh", "corrected_wh")

    def __init__(
        self,
        cloud_class: str,
        day_part: str,
        measured_wh: float,
        corrected_wh: float,
    ) -> None:
        self.cloud_class = cloud_class
        self.day_part = day_part
        self.measured_wh = measured_wh
        self.corrected_wh = corrected_wh


# ---------------------------------------------------------------------------
# Percentile math (pure, stdlib — NO numpy)
# ---------------------------------------------------------------------------


def empirical_percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Empirical ``pct``-th percentile of an ASCENDING-SORTED sequence.

    ``pct`` in [0, 100]. Uses linear interpolation between the two nearest ranks
    (the standard "linear" / type-7 method, matching numpy's default), stdlib
    only. An empty sequence returns QUANTILE_NEUTRAL_MULT (1.0) so a caller's
    cold-start path is neutral. A single-element sequence returns that element
    for any ``pct``.

    Contract for the band producer: call with ``pct`` in
    {QUANTILE_P_LOW, QUANTILE_P_MID, QUANTILE_P_HIGH}; the results are guaranteed
    monotonic (p10 <= p50 <= p90) because the input is sorted.
    """
    n = len(sorted_values)
    if n == 0:
        return QUANTILE_NEUTRAL_MULT
    if n == 1:
        return float(sorted_values[0])
    # Clamp pct into [0, 100] so a caller can't index out of range.
    p = pct
    if p < 0.0:
        p = 0.0
    elif p > 100.0:
        p = 100.0
    # Type-7: rank = (n - 1) * p/100, interpolate between floor and ceil.
    rank = (n - 1) * (p / 100.0)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(sorted_values[lo])
    frac = rank - lo
    return float(sorted_values[lo]) + frac * (
        float(sorted_values[hi]) - float(sorted_values[lo])
    )


def quantile_bin_key(cloud_class: str, day_part: str) -> str:
    """Canonical (weather class x day part) bin key.

    Delegates to ``QuantileState.bin_key`` so the trainer, the applier and the
    store share ONE taxonomy (identical to the day-ahead BiasState cell key —
    ``f"{cloud_class}|{day_part}"``). Kept as a module function so callers need
    not import the dataclass staticmethod.
    """
    return QuantileState.bin_key(cloud_class, day_part)


def bands_for_bin(
    state: QuantileState,
    *,
    cloud_class: str,
    day_part: str,
) -> QuantileBands:
    """Empirical P10/P50/P90 multipliers for one (class x part) bin (SPEC §6).

    Looks up the bin's relative-error ring in ``state``; sorts it; returns a
    QuantileBands with p10 / p50 / p90 = empirical_percentile at
    QUANTILE_P_LOW / QUANTILE_P_MID / QUANTILE_P_HIGH and ``n`` = ring length.

    COLD START (SPEC §6/§10, "no fake spread AND no fake shift"): a ring with
    fewer than QUANTILE_MIN_SAMPLES samples returns ``QuantileBands.neutral()``
    (1.0/1.0/1.0) — a single clamped outlier must not scale the served curve, so
    a thin bin gets the neutral identity band, NOT its unshrunk empirical median.
    An empty / missing bin is likewise neutral. Never raises (validate-and-clamp):
    a corrupt ring degrades to the neutral band.
    """
    if state is None or not isinstance(getattr(state, "bins", None), dict):
        return QuantileBands.neutral()

    key = QuantileState.bin_key(cloud_class, day_part)
    ring = state.bins.get(key)
    if not ring:
        return QuantileBands.neutral()

    # Defensive finite filter (the state loader already clamps, but a directly
    # constructed QuantileState may carry raw values).
    vals = [f for f in (_finite(v) for v in ring) if f is not None]
    n = len(vals)
    if n == 0:
        return QuantileBands.neutral()

    if n < QUANTILE_MIN_SAMPLES:
        # Cold start: no fabricated spread AND no fabricated SHIFT. A thin bin's
        # empirical median is dominated by a single clamped outlier (relerr up to
        # QUANTILE_REL_ERR_MAX), which would scale all three band curves off the
        # served corrected curve with no statistical backing — so we return the
        # neutral identity band (== the corrected curve, the engine's
        # missing-band pass-through), reserving any deviation for a bin with
        # >= QUANTILE_MIN_SAMPLES samples.
        return QuantileBands.neutral()

    ordered = sorted(vals)
    p50 = empirical_percentile(ordered, QUANTILE_P_MID)
    p10 = empirical_percentile(ordered, QUANTILE_P_LOW)
    p90 = empirical_percentile(ordered, QUANTILE_P_HIGH)
    # p10 <= p50 <= p90 holds by construction (sorted input + monotone pct), but
    # clamp defensively so any float slop can never invert the band.
    if p10 > p50:
        p10 = p50
    if p90 < p50:
        p90 = p50
    return QuantileBands(p10=p10, p50=p50, p90=p90, n=n)


# ---------------------------------------------------------------------------
# Training (nightly; pure state -> state)
# ---------------------------------------------------------------------------


def train_quantiles(
    state: QuantileState,
    samples: Iterable[QuantileSample],
) -> QuantileState:
    """Append hourly relative-error samples into their bins' rings (SPEC §6).

    For each sample with ``corrected_wh`` > QUANTILE_MIN_FORECAST_WH, form
    ``relerr = measured_wh / corrected_wh`` clamped to
    [QUANTILE_REL_ERR_MIN, QUANTILE_REL_ERR_MAX] and append it to the
    ``bin_key(cloud_class, day_part)`` ring. Each bin's ring is trimmed so the
    whole state holds at most QUANTILE_RING_DAYS worth of samples (a simple
    per-bin FIFO cap keeps the eMMC-friendly store small). Returns a NEW
    QuantileState (input untouched). Samples with an unknown class/part, a
    non-finite value or a below-threshold forecast are silently skipped so junk
    never enters the ring. Idempotence over a night is the coordinator's
    responsibility (date-keyed guard); this is a pure state->state map.
    """
    # Deep-copy the existing rings so the input state is untouched.
    if state is not None and isinstance(getattr(state, "bins", None), dict):
        bins: dict[str, list[float]] = {k: list(v) for k, v in state.bins.items()}
        version = state.version
    else:
        bins = {}
        version = 1

    for s in samples or ():
        cloud_class = getattr(s, "cloud_class", None)
        day_part = getattr(s, "day_part", None)
        if not isinstance(cloud_class, str) or not cloud_class:
            continue
        if not isinstance(day_part, str) or not day_part:
            continue
        corrected = _finite(getattr(s, "corrected_wh", None))
        measured = _finite(getattr(s, "measured_wh", None))
        if corrected is None or measured is None:
            continue
        if corrected <= QUANTILE_MIN_FORECAST_WH:
            continue
        relerr = measured / corrected
        if not math.isfinite(relerr):
            continue
        # Clamp into the sane band so a dawn/dusk near-zero-forecast hour that
        # slipped the threshold can't inject a runaway multiplier.
        if relerr < QUANTILE_REL_ERR_MIN:
            relerr = QUANTILE_REL_ERR_MIN
        elif relerr > QUANTILE_REL_ERR_MAX:
            relerr = QUANTILE_REL_ERR_MAX

        key = QuantileState.bin_key(cloud_class, day_part)
        ring = bins.get(key)
        if ring is None:
            ring = []
            bins[key] = ring
        ring.append(relerr)
        # Per-bin FIFO cap (drop oldest).
        if len(ring) > _BIN_RING_CAP:
            del ring[: len(ring) - _BIN_RING_CAP]

    return QuantileState(bins=bins, version=version)


# ---------------------------------------------------------------------------
# Application (forecast time)
# ---------------------------------------------------------------------------


def apply_bands(
    hourly_wh: dict[str, float],
    band_by_hour: dict[str, QuantileBands],
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Apply per-hour bands to a corrected hourly Wh curve (SPEC §6).

    ``hourly_wh`` is the corrected curve keyed by ISO-8601 UTC hour;
    ``band_by_hour`` maps the same hour keys to their QuantileBands. Returns
    ``(p10_hourly, p50_hourly, p90_hourly)`` where each hour's Wh is multiplied
    by the bin's respective percentile multiplier. An hour with no band entry
    (missing bin) passes through unchanged in all three curves (band == corrected
    == P50, no fabricated spread). Input untouched; all three outputs are fresh
    dicts with the same keys as ``hourly_wh``.
    """
    p10: dict[str, float] = {}
    p50: dict[str, float] = {}
    p90: dict[str, float] = {}
    if not isinstance(hourly_wh, dict):
        return p10, p50, p90
    bands = band_by_hour if isinstance(band_by_hour, dict) else {}
    for hkey, wh in hourly_wh.items():
        base = _finite(wh)
        if base is None:
            base = 0.0
        band = bands.get(hkey)
        if band is None:
            p10[hkey] = base
            p50[hkey] = base
            p90[hkey] = base
        else:
            p10[hkey] = base * band.p10
            p50[hkey] = base * band.p50
            p90[hkey] = base * band.p90
    return p10, p50, p90


def band_curve_from_corrected(
    slot_watts: Sequence[float],
    slot_starts: Sequence[datetime],
    band_by_slot: dict[datetime, QuantileBands],
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    """Per-slot P10/P50/P90 watts curves from the corrected 15-min curve.

    The engine hook path (SPEC §6/§10): for each slot, multiply the corrected
    ``slot_watts[i]`` by the QuantileBands looked up for ``slot_starts[i]`` in
    ``band_by_slot`` (keyed by the identical slot-start datetime the engine
    iterates). Returns ``(p10_watts, p50_watts, p90_watts)`` aligned to
    ``slot_starts``. A slot with no band entry passes the corrected watts through
    unchanged in all three curves (band == corrected). This keeps the band watts
    in the SAME instantaneous frame as ``ForecastResult.total_watts`` so the
    hourly Wh roll-ups derived from them stay mutually consistent.
    """
    bands = band_by_slot if isinstance(band_by_slot, dict) else {}
    p10: list[float] = []
    p50: list[float] = []
    p90: list[float] = []
    for i, start in enumerate(slot_starts):
        w = _finite(slot_watts[i]) if i < len(slot_watts) else None
        if w is None:
            w = 0.0
        band = bands.get(start)
        if band is None:
            p10.append(w)
            p50.append(w)
            p90.append(w)
        else:
            p10.append(w * band.p10)
            p50.append(w * band.p50)
            p90.append(w * band.p90)
    return tuple(p10), tuple(p50), tuple(p90)
