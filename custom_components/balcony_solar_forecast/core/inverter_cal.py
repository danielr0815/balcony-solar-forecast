"""Inverter DC->AC efficiency site calibration (AC-side Phase 3, stdlib only).

Owner: inverter_cal. Pure, HA-free. Learns ONE site-level scalar eta_inv from
the site's TOTAL-AC meter (SiteConfig.ac_actual_entity) so the AC forecast
tracks the real inverter conversion instead of the datasheet
DEFAULT_INVERTER_EFFICIENCY. A single scalar fits every group: the operator has
only a whole-site AC meter and the HMS-800W-2T inverters are identical.

The learned eta is NEVER load-bearing. It is used only when TRUSTED
(``effective_eta`` returns None below INVERTER_CAL_MIN_SAMPLES), so absent an AC
meter, with too little data, or after only out-of-band samples the engine falls
back to the per-group config / default eta. The DC self-learning + scoreboard
are untouched — this reshapes the AC curve alone.

Calibration math (mirrors the shademap adaptive-warm-up EMA):
  * per eligible hour form the raw ratio ``r = P_ac / P_dc_total`` (Wh over one
    hour == mean W, so the ratio is the hour's mean DC->AC efficiency);
  * fold each ratio via ``alpha = max(INVERTER_CAL_EMA_ALPHA, 1/(n+1))`` — while
    ``1/(n+1)`` exceeds the fixed alpha (the first floor(1/alpha) samples) the
    stored eta is the EXACT arithmetic mean of the folded ratios, so a single
    seed cannot dominate a young calibration; it then transitions to the fixed
    EMA;
  * a ratio OUTSIDE [INVERTER_CAL_MIN, INVERTER_CAL_MAX] is DROPPED (not a
    plausible inverter eta — a meter that also sees house load or is net-metered)
    and does not advance ``n``; the stored eta is clamped to the band after each
    fold.

Eligibility / clip gate (``eligible_ratio``): a slot contributes only when the
summed DC is meaningful (>= INVERTER_CAL_MIN_LOAD_W — below it the inverter
self-consumption / MPPT start threshold distorts the ratio) AND the slot is
UNCLIPPED (``clip_headroom_ok`` — a clipped hour's AC is capped at the inverter
limit, so its ratio understates eta). The raw ratio is NOT clamped here so an
out-of-band day stays visible to the drop gate in ``update``.

Frozen public contract (the nightly trainer + coordinator depend on these):

    eligible_ratio(p_ac, p_dc_total, *, clip_headroom_ok) -> float | None
    update(state, ratios) -> InverterCalState
    effective_eta(state) -> float | None

Every function is pure and NEVER raises (validate-and-clamp ethos, SPEC §5):
garbage inputs degrade to None / an unchanged state, never an exception.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from ..const import (
    INVERTER_CAL_EMA_ALPHA,
    INVERTER_CAL_MAX,
    INVERTER_CAL_MIN,
    INVERTER_CAL_MIN_LOAD_W,
    INVERTER_CAL_MIN_SAMPLES,
)
from .types import InverterCalState

__all__ = ["eligible_ratio", "update", "effective_eta"]


def _is_finite(x: object) -> bool:
    """True iff ``x`` coerces to a finite real number."""
    try:
        f = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _clamp_band(eta: float) -> float:
    """Clamp ``eta`` into [INVERTER_CAL_MIN, INVERTER_CAL_MAX]."""
    if eta < INVERTER_CAL_MIN:
        return INVERTER_CAL_MIN
    if eta > INVERTER_CAL_MAX:
        return INVERTER_CAL_MAX
    return eta


def eligible_ratio(
    p_ac: float, p_dc_total: float, *, clip_headroom_ok: bool
) -> float | None:
    """Raw measured-AC / modeled-DC ratio for one slot, or None if ineligible.

    Returns ``p_ac / p_dc_total`` when the slot is a valid calibration sample:
    ``clip_headroom_ok`` (the slot is UNCLIPPED — a clipped hour's AC is capped
    so the ratio would understate eta), the summed DC is meaningful
    (``p_dc_total`` >= INVERTER_CAL_MIN_LOAD_W and > 0, below which the inverter
    self-consumption / MPPT start threshold distorts the ratio), and both inputs
    are finite. Otherwise None.

    The ratio is deliberately NOT clamped to the plausible band here: clamping
    happens in :func:`update`, so an out-of-band day stays visible to the drop
    gate (which rejects it as an implausible inverter eta rather than folding a
    saturated value). Never raises.
    """
    if not clip_headroom_ok:
        return None
    if not (_is_finite(p_ac) and _is_finite(p_dc_total)):
        return None
    dc = float(p_dc_total)
    if dc <= 0.0 or dc < INVERTER_CAL_MIN_LOAD_W:
        return None
    return float(p_ac) / dc


def update(
    state: InverterCalState, ratios: Iterable[float] | None
) -> InverterCalState:
    """Fold eligible ratios into the calibration EMA (pure; never raises).

    Each finite ratio inside [INVERTER_CAL_MIN, INVERTER_CAL_MAX] is folded via
    the adaptive-warm-up EMA ``alpha = max(INVERTER_CAL_EMA_ALPHA, 1/(n+1))``
    (mirroring the shademap: the first floor(1/alpha) folded samples form the
    exact arithmetic mean, then the fixed EMA), and the stored eta is clamped to
    the band after each fold. A non-finite or out-of-band ratio is DROPPED — not
    folded — so ``n`` counts only accepted, plausible samples. Returns a NEW
    state; when nothing was folded the ORIGINAL ``state`` is returned unchanged
    (identity), so an empty / all-ineligible day is a true no-op.
    """
    eta = state.eta
    n = state.n
    folded = 0
    for r in ratios or ():
        if not _is_finite(r):
            continue
        rf = float(r)
        # A ratio outside the plausible inverter band is not a valid eta: DROP it
        # (do not fold, do not advance n) — it is a meter/DC-labeling artefact.
        if rf < INVERTER_CAL_MIN or rf > INVERTER_CAL_MAX:
            continue
        # Adaptive warm-up: 1/(n+1) while it exceeds the fixed EMA alpha (young
        # calibration -> exact arithmetic mean; at n==0 alpha==1.0 seeds the
        # first sample, wiping the DEFAULT prior), then the fixed alpha.
        alpha = max(INVERTER_CAL_EMA_ALPHA, 1.0 / (n + 1))
        eta = _clamp_band((1.0 - alpha) * eta + alpha * rf)
        n += 1
        folded += 1

    if folded == 0:
        return state
    return InverterCalState(eta=eta, n=n)


def effective_eta(state: InverterCalState) -> float | None:
    """The calibrated eta to USE, or None when not yet trusted.

    Returns ``state.eta`` clamped to [INVERTER_CAL_MIN, INVERTER_CAL_MAX] once at
    least INVERTER_CAL_MIN_SAMPLES eligible hours have been folded
    (``state.n`` >= threshold); below that it returns None so the caller keeps
    the per-group config / default eta (the learned eta is never load-bearing).
    Never raises: a garbage state degrades to None.
    """
    try:
        n = int(state.n)
    except (TypeError, ValueError):
        return None
    if n < INVERTER_CAL_MIN_SAMPLES:
        return None
    if not _is_finite(state.eta):
        return None
    return _clamp_band(float(state.eta))
