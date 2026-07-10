"""SLOW learner: geometric beam-transmittance field (shademap).

Owner: shademap. Pure, HA-free (stdlib only). Implements SPEC §5 "Langsamer
Lerner". Per measurement channel (plane / MPPT port), per bin
(sun azimuth SHADEMAP_AZ_BIN_DEG x sun elevation SHADEMAP_EL_BIN_DEG x
HALF-YEAR before/after summer solstice), an EMA (alpha SHADEMAP_EMA_ALPHA) of
the BEAM-REFERENCED transmittance

    T = (P_measured - P_diffuse_modeled) / P_beam_modeled

— deliberately NOT the total measured/modeled ratio: a shaded bin still
contains the diffuse floor, so a total ratio applied to the beam over-predicts
shade and misattributes diffuse-independent losses (soiling, eta error) to the
beam (SPEC §5).

Quasi-clear samples ONLY: an elevation-dependent clear-sky-index gate
(Haurwitz is crude at low sun, so the lower k_c bound relaxes toward
SHADEMAP_KC_LO_LOW_SUN below SHADEMAP_KC_PIVOT_ELEV_DEG), neighbour-slot
stability (relative change of the MEASURED/modeled energy ratio between adjacent
slots < SHADEMAP_NEIGHBOUR_STABILITY — the measured ratio, not the smooth
forecast k_c, catches a real cloud fluctuation), and a modeled beam share >
SHADEMAP_MIN_BEAM_SHARE of Wp.

The learned map REPLACES the static horizon transmittance of the bin; clamp
[SHADEMAP_TAU_MIN, SHADEMAP_TAU_MAX] (full occlusion representable — building
wall). Cold start: bins inherit the static horizon prior, blended via
shrinkage w = n / (n + SHADEMAP_SHRINKAGE_K) — no hard min-sample switch.
Applied to beam + circumsolar only.

Frozen public contract (7 implementers depend on these exact signatures):

    shademap_bin_key(sun_az, sun_el, doy) -> str
    half_year_index(doy) -> int
    is_quasi_clear(*, kc, sun_el, beam_share, stability_ratio, neighbour_ratio) -> bool
    update_bin(state, *, channel, sun_az, sun_el, doy, measured_t) -> ShademapState
    effective_tau(state, *, channel, sun_az, sun_el, doy, static_prior) -> float

Extra pure helpers the engine / store / diagnostics call (all owned here):

    beam_referenced_t(p_measured, p_diffuse_modeled, p_beam_modeled) -> float
    apply_shademap_to_beam(beam_w, *, tau) -> float
    dump_polar_table(state) -> list[dict]
    ingest_bootstrap_shademap(raw, *, max_bin_n) -> ShademapState

All tunables come from const. Every load/apply path is validate-and-clamp;
a corrupt/absent state degrades to the static prior, never an exception.
"""

from __future__ import annotations

import math

from ..const import (
    SHADEMAP_AZ_BIN_DEG,
    SHADEMAP_EL_BIN_DEG,
    SHADEMAP_EMA_ALPHA,
    SHADEMAP_KC_HI,
    SHADEMAP_KC_LO_HIGH_SUN,
    SHADEMAP_KC_LO_LOW_SUN,
    SHADEMAP_KC_PIVOT_ELEV_DEG,
    SHADEMAP_MIN_BEAM_SHARE,
    SHADEMAP_NEIGHBOUR_STABILITY,
    SHADEMAP_SHRINKAGE_K,
    SHADEMAP_TAU_MAX,
    SHADEMAP_TAU_MIN,
    SUMMER_SOLSTICE_DOY,
)
from .types import ShademapBin, ShademapState

__all__ = [
    "shademap_bin_key",
    "half_year_index",
    "is_quasi_clear",
    "update_bin",
    "effective_tau",
    "beam_referenced_t",
    "apply_shademap_to_beam",
    "dump_polar_table",
    "ingest_bootstrap_shademap",
]


# ---------------------------------------------------------------------------
# Small numeric helpers (stdlib only, NaN/inf-safe)
# ---------------------------------------------------------------------------


def _finite(v: float, default: float = 0.0) -> float:
    """Coerce ``v`` to a finite float, returning ``default`` on NaN/inf/error."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _clamp_tau(v: float) -> float:
    """Clamp a transmittance into [SHADEMAP_TAU_MIN, SHADEMAP_TAU_MAX].

    NaN/inf collapse to the lower bound (opaque) — a corrupt sample must never
    brighten a bin above its physical ceiling nor leak a non-finite value into
    the persisted EMA.
    """
    f = _finite(v, SHADEMAP_TAU_MIN)
    if f < SHADEMAP_TAU_MIN:
        return SHADEMAP_TAU_MIN
    if f > SHADEMAP_TAU_MAX:
        return SHADEMAP_TAU_MAX
    return f


def _wrap360(az: float) -> float:
    """Normalise an azimuth into [0, 360)."""
    a = math.fmod(_finite(az, 0.0), 360.0)
    if a < 0.0:
        a += 360.0
    return a


# ---------------------------------------------------------------------------
# Bin keying (SPEC §5: az 5 deg x el 2.5 deg x half-year)
# ---------------------------------------------------------------------------


def half_year_index(doy: int) -> int:
    """Half-year bucket for a day-of-year: 0 before, 1 after summer solstice.

    Splits the year at SUMMER_SOLSTICE_DOY so leaf-off (April, rising limb) and
    leaf-on (August, falling limb) never alias into the same sun-position bin
    (SPEC §5). The rising limb (day-of-year < solstice, foliage growing toward
    full) is half 0; the falling limb (>= solstice, foliage decaying toward
    bare) is half 1. Two days at the same solar geometry but opposite foliage
    regimes therefore land in distinct bins.

    doy is clamped defensively to [1, 366]; the boundary day (== solstice) is
    assigned to the falling limb (half 1) so the split is well-defined.
    """
    try:
        d = int(doy)
    except (TypeError, ValueError):
        d = SUMMER_SOLSTICE_DOY
    if d < 1:
        d = 1
    elif d > 366:
        d = 366
    return 0 if d < SUMMER_SOLSTICE_DOY else 1


def shademap_bin_key(sun_az: float, sun_el: float, doy: int) -> str:
    """Canonical bin key ``f"{az_idx}:{el_idx}:{half}"`` (SPEC §5).

    ``az_idx = floor(wrap360(sun_az) / SHADEMAP_AZ_BIN_DEG)`` (azimuth is the
    INTERNAL 0=N convention, wrapped so 360 -> 0),
    ``el_idx = floor(max(0, sun_el) / SHADEMAP_EL_BIN_DEG)`` (below-horizon sun
    folds into the elevation-0 bin), ``half = half_year_index(doy)``. This is
    the exact key format stored in ``ShademapState.channels[channel]`` and read
    back by :func:`effective_tau` / :func:`dump_polar_table`.
    """
    az = _wrap360(sun_az)
    el = max(0.0, _finite(sun_el, 0.0))
    az_idx = int(math.floor(az / SHADEMAP_AZ_BIN_DEG))
    el_idx = int(math.floor(el / SHADEMAP_EL_BIN_DEG))
    half = half_year_index(doy)
    return f"{az_idx}:{el_idx}:{half}"


def _bin_centre(bin_key: str) -> tuple[float, float, int] | None:
    """Reverse a bin key to (centre_az_deg, centre_el_deg, half) or None.

    Used only by the polar-table dump: returns the geometric centre of the bin
    so the exported table is visually comparable against known obstacles.
    Returns None for a malformed key so a corrupt state never raises.
    """
    parts = bin_key.split(":")
    if len(parts) != 3:
        return None
    try:
        az_idx = int(parts[0])
        el_idx = int(parts[1])
        half = int(parts[2])
    except (TypeError, ValueError):
        return None
    az_c = (az_idx + 0.5) * SHADEMAP_AZ_BIN_DEG
    el_c = (el_idx + 0.5) * SHADEMAP_EL_BIN_DEG
    return (az_c, el_c, half)


# ---------------------------------------------------------------------------
# Quasi-clear sample gate (SPEC §5)
# ---------------------------------------------------------------------------


def _kc_lo_bound(sun_el: float) -> float:
    """Elevation-dependent lower k_c bound for the quasi-clear gate.

    Ramps linearly from SHADEMAP_KC_LO_LOW_SUN at 0 deg elevation up to
    SHADEMAP_KC_LO_HIGH_SUN at (and above) SHADEMAP_KC_PIVOT_ELEV_DEG. Haurwitz
    clear-sky is crude at low sun, so the acceptance band is deliberately looser
    (lower floor) near the horizon and tightens as the sun climbs.
    """
    el = max(0.0, _finite(sun_el, 0.0))
    pivot = SHADEMAP_KC_PIVOT_ELEV_DEG
    if pivot <= 0.0 or el >= pivot:
        return SHADEMAP_KC_LO_HIGH_SUN
    frac = el / pivot  # 0 at horizon .. 1 at pivot
    return SHADEMAP_KC_LO_LOW_SUN + (SHADEMAP_KC_LO_HIGH_SUN - SHADEMAP_KC_LO_LOW_SUN) * frac


def is_quasi_clear(
    *,
    kc: float,
    sun_el: float,
    beam_share: float,
    stability_ratio: float | None = None,
    neighbour_ratio: float | None = None,
) -> bool:
    """Quasi-clear sample gate for training a bin (SPEC §5).

    True only when ALL hold:
      * k_c within [lo(sun_el), SHADEMAP_KC_HI], where lo ramps from
        SHADEMAP_KC_LO_LOW_SUN at 0 deg elevation to SHADEMAP_KC_LO_HIGH_SUN at
        or above SHADEMAP_KC_PIVOT_ELEV_DEG (Haurwitz is crude at low sun);
      * modeled ``beam_share`` > SHADEMAP_MIN_BEAM_SHARE (a bin with almost no
        modeled beam cannot inform a *beam*-referenced transmittance);
      * neighbour-slot stability: when BOTH ``stability_ratio`` (this slot's
        measured/modeled energy ratio) and ``neighbour_ratio`` (the adjacent
        slot's) are given, the relative change
        ``abs(stability_ratio - neighbour_ratio) / max(...)`` <
        SHADEMAP_NEIGHBOUR_STABILITY must hold. Gating on the MEASURED ratio
        sequence — not the smooth forecast k_c — is what rejects a lone bright
        measured slot between shaded ones (a real cloud fluctuation, not a
        clear-sky reference). Either value None (e.g. no usable neighbour) skips
        the leg.

    Non-finite inputs fail the gate (never train on garbage). Both the live
    nightly trainer and the offline backfill pass the same ratio pair, so the
    two training paths accept identical samples.
    """
    kc_f = _finite(kc, -1.0)
    if kc_f < 0.0:
        return False
    if not (_kc_lo_bound(sun_el) <= kc_f <= SHADEMAP_KC_HI):
        return False

    beam_f = _finite(beam_share, -1.0)
    if beam_f <= SHADEMAP_MIN_BEAM_SHARE:
        return False

    if stability_ratio is not None and neighbour_ratio is not None:
        cur = _finite(stability_ratio, -1.0)
        nb = _finite(neighbour_ratio, -1.0)
        if cur < 0.0 or nb < 0.0:
            return False
        denom = max(cur, nb)
        if denom <= 0.0:
            return False
        if abs(cur - nb) / denom >= SHADEMAP_NEIGHBOUR_STABILITY:
            return False

    return True


# ---------------------------------------------------------------------------
# Beam-referenced transmittance (SPEC §5)
# ---------------------------------------------------------------------------


def beam_referenced_t(
    p_measured: float,
    p_diffuse_modeled: float,
    p_beam_modeled: float,
) -> float | None:
    """Beam-referenced transmittance ``(P_meas - P_diff_model) / P_beam_model``.

    SPEC §5: the sample references the BEAM only; the diffuse floor is
    subtracted from the measurement so a shaded bin (measurement ~= diffuse
    floor) yields T ~= 0 rather than the total ratio (which would over-predict
    shade and blame diffuse-independent losses on the beam).

    Guards:
      * a non-positive modeled beam (``p_beam_modeled <= 0``) makes the ratio
        undefined -> returns None (caller drops the sample; the >5% beam-share
        gate normally prevents this);
      * NEGATIVE NUMERATOR guard: if the measurement falls below the modeled
        diffuse floor (``P_meas < P_diff_model``, e.g. sensor noise or a
        diffuse over-estimate in deep shade) the true beam contribution is
        zero, so T is clamped to 0 — a *zero* sample, never a negative one that
        would drag the EMA below full occlusion;
      * the result is clamped to [SHADEMAP_TAU_MIN, SHADEMAP_TAU_MAX].

    Returns the clamped T, or None when the sample is undefined.
    """
    beam = _finite(p_beam_modeled, 0.0)
    if beam <= 0.0:
        return None
    meas = _finite(p_measured, 0.0)
    diff = _finite(p_diffuse_modeled, 0.0)
    numer = meas - diff
    if numer <= 0.0:
        # Measurement at or below the modeled diffuse floor: no usable beam.
        return SHADEMAP_TAU_MIN
    return _clamp_tau(numer / beam)


# ---------------------------------------------------------------------------
# EMA update (SPEC §5)
# ---------------------------------------------------------------------------


def update_bin(
    state: ShademapState,
    *,
    channel: str,
    sun_az: float,
    sun_el: float,
    doy: int,
    measured_t: float,
) -> ShademapState:
    """EMA-update one channel's bin with a measured beam-referenced T.

    ``measured_t`` is the already-computed beam-referenced transmittance for a
    quasi-clear sample (caller gates via :func:`is_quasi_clear` and computes T
    via :func:`beam_referenced_t`). A fresh bin seeds at the clamped
    ``measured_t``; an existing bin blends
    ``tau_new = (1 - alpha)*tau_old + alpha*clamp(measured_t)`` with an ADAPTIVE
    warm-up ``alpha = max(SHADEMAP_EMA_ALPHA, 1/(n_old + 1))``, where ``n_old``
    is the bin's stored count BEFORE this sample. While ``1/(n_old+1)`` exceeds
    the fixed alpha — the first ``floor(1/SHADEMAP_EMA_ALPHA)`` samples — the bin
    tau is the EXACT arithmetic mean of its clamped samples (the test invariant),
    so one noisy seed no longer dominates a young bin for weeks; the update then
    transitions seamlessly to the standard fixed-alpha EMA. tau is clamped to
    [SHADEMAP_TAU_MIN, SHADEMAP_TAU_MAX] and ``n`` incremented (n drives the
    cold-start shrinkage in :func:`effective_tau`).

    Returns a NEW ShademapState; the input is never mutated (frozen dataclasses
    with shallow-copied channel/bin dicts). The coordinator caps *backfilled*
    ``n`` at BOOTSTRAP_MAX_BIN_N via :func:`ingest_bootstrap_shademap`; live
    updates here are uncapped.
    """
    sample = _clamp_tau(measured_t)
    key = shademap_bin_key(sun_az, sun_el, doy)

    # Shallow-copy the nested dicts so the input state stays untouched.
    channels: dict[str, dict[str, ShademapBin]] = {
        ch: dict(bins) for ch, bins in state.channels.items()
    }
    bins = channels.setdefault(channel, {})
    old = bins.get(key)
    if old is None:
        new_tau = sample
        new_n = 1
    else:
        # Adaptive warm-up alpha: 1/(n_old+1) while it exceeds the fixed EMA
        # alpha (young bins -> exact arithmetic mean), then the fixed alpha.
        a = max(SHADEMAP_EMA_ALPHA, 1.0 / (old.n + 1))
        new_tau = _clamp_tau((1.0 - a) * old.tau + a * sample)
        new_n = old.n + 1
    bins[key] = ShademapBin(tau=new_tau, n=new_n)

    return ShademapState(channels=channels, version=state.version)


# ---------------------------------------------------------------------------
# Shrinkage blend + application (SPEC §5)
# ---------------------------------------------------------------------------


def _shrinkage_weight(n: int) -> float:
    """Cold-start shrinkage weight w = n / (n + SHADEMAP_SHRINKAGE_K) in [0, 1).

    n == 0 -> 0 (pure static prior); n grows -> the learned tau dominates.
    """
    nn = max(0, int(n))
    return nn / (nn + SHADEMAP_SHRINKAGE_K)


def effective_tau(
    state: ShademapState,
    *,
    channel: str,
    sun_az: float,
    sun_el: float,
    doy: int,
    static_prior: float,
) -> float:
    """Blended transmittance for one channel/bin: learned vs. static prior.

    Shrinkage blend ``w = n / (n + SHADEMAP_SHRINKAGE_K)``:
        tau = w * learned_tau + (1 - w) * static_prior
    where ``static_prior`` is the plane's static horizon transmittance for this
    sun azimuth/doy (from :func:`horizon.transmittance_at`) and learned_tau/n
    come from the matched bin. An empty / missing bin (or an absent channel)
    gives w = 0 -> the result is EXACTLY the static prior (property-tested:
    unvisited bins return the prior unchanged). The result is clamped to
    [SHADEMAP_TAU_MIN, SHADEMAP_TAU_MAX].

    This REPLACES the static tau in the engine's beam+circumsolar gate when the
    slow learner is active; the engine multiplies beam+circumsolar power by it
    via :func:`apply_shademap_to_beam`.
    """
    prior = _clamp_tau(static_prior)
    bins = state.channels.get(channel)
    if not bins:
        return prior
    key = shademap_bin_key(sun_az, sun_el, doy)
    binv = bins.get(key)
    if binv is None or binv.n <= 0:
        return prior
    w = _shrinkage_weight(binv.n)
    if w <= 0.0:
        return prior
    blended = w * binv.tau + (1.0 - w) * prior
    return _clamp_tau(blended)


def apply_shademap_to_beam(beam_w: float, *, tau: float) -> float:
    """Attenuate a modeled beam+circumsolar power by the effective tau.

    Pure multiply the engine calls after resolving :func:`effective_tau`:
    ``beam_w * tau``, with tau clamped to the shademap band and a non-negative
    result (a negative modeled beam is coerced to 0). Only beam+circumsolar is
    passed here — the isotropic diffuse is scaled by the static sky-view factor
    elsewhere (SPEC §5: the learned map applies to beam only).
    """
    b = _finite(beam_w, 0.0)
    if b <= 0.0:
        return 0.0
    return b * _clamp_tau(tau)


# ---------------------------------------------------------------------------
# Diagnostics: polar-table dump (SPEC §5 "Polartabelle")
# ---------------------------------------------------------------------------


def dump_polar_table(state: ShademapState) -> list[dict]:
    """Export the learned map as a flat polar table (SPEC §5 diagnostic).

    One row per populated bin per channel, each a plain dict:
        {"channel", "az": centre_az_deg, "elev": centre_el_deg,
         "halfyear": 0|1, "tau", "n"}
    ``az``/``elev`` are the bin *centres* (0=N internal) so the table can be
    plotted straight onto a polar sun-path chart and checked against known
    obstacles (hill / trees / building wall). Sorted by (channel, az, elev,
    halfyear) for a stable, diff-friendly export. Malformed bin keys are
    skipped rather than raising, so a partially-corrupt state still dumps.
    """
    rows: list[dict] = []
    for channel in sorted(state.channels):
        bins = state.channels[channel]
        for bin_key, binv in bins.items():
            centre = _bin_centre(bin_key)
            if centre is None:
                continue
            az_c, el_c, half = centre
            rows.append(
                {
                    "channel": channel,
                    "az": az_c,
                    "elev": el_c,
                    "halfyear": half,
                    "tau": binv.tau,
                    "n": binv.n,
                }
            )
    rows.sort(key=lambda r: (r["channel"], r["az"], r["elev"], r["halfyear"]))
    return rows


# ---------------------------------------------------------------------------
# Bootstrap ingestion with n-credit cap (SPEC §6)
# ---------------------------------------------------------------------------


def ingest_bootstrap_shademap(raw: object, *, max_bin_n: int) -> ShademapState:
    """Validate + clamp a bootstrap shademap blob, capping each bin's n-credit.

    ``raw`` is the ``BOOTSTRAP_KEY_SHADEMAP`` object from a backfill JSON (a
    :class:`ShademapState` dict, see SPEC §6). It is parsed through
    ``ShademapState.from_dict`` (which already validates + clamps tau and drops
    malformed channels/bins), then EVERY bin's ``n`` is capped at ``max_bin_n``
    (const BOOTSTRAP_MAX_BIN_N). Hourly-smeared backfilled bins are less
    trustworthy than live 15-min samples, so the cap keeps the cold-start
    shrinkage weight low and lets live data override the backfill quickly.

    Never raises: a non-dict / unknown-shape blob yields an empty
    ShademapState. The import service still enforces the top-level schema
    version; this helper only sanitises the shademap payload itself.

    Returns a fresh, capped ShademapState (a NEW object; ``raw`` untouched).
    """
    cap = max(0, int(max_bin_n))
    base = ShademapState.from_dict(raw if isinstance(raw, dict) else {})
    channels: dict[str, dict[str, ShademapBin]] = {}
    for channel, bins in base.channels.items():
        capped_bins: dict[str, ShademapBin] = {}
        for bin_key, binv in bins.items():
            capped_bins[bin_key] = ShademapBin(tau=binv.tau, n=min(binv.n, cap))
        channels[channel] = capped_bins
    return ShademapState(channels=channels, version=base.version)
