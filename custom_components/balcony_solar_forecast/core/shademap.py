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
    effective_tau_pooled(state, *, channels, sun_az, sun_el, doy, static_prior) -> float

Extra pure helpers the engine / store / diagnostics call (all owned here):

    beam_referenced_t(p_measured, p_diffuse_modeled, p_beam_modeled) -> float
    apply_shademap_to_beam(beam_w, *, tau) -> float
    dump_polar_table(state) -> list[dict]
    ingest_bootstrap_shademap(raw, *, max_bin_n) -> ShademapState
    channel_similarity(state, channel_a, channel_b) -> dict
    suggest_shade_groups(state, plane_names, *, max_diff, min_common_bins) -> dict

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
    "effective_tau_pooled",
    "beam_referenced_t",
    "apply_shademap_to_beam",
    "dump_polar_table",
    "ingest_bootstrap_shademap",
    "channel_similarity",
    "suggest_shade_groups",
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


def effective_tau_pooled(
    state: ShademapState,
    *,
    channels: tuple[str, ...],
    sun_az: float,
    sun_el: float,
    doy: int,
    static_prior: float,
) -> float:
    """READ-TIME pooled transmittance over several channels/one bin (SPEC §5).

    Storage stays per plane (one channel each); GROUPING happens only here, at
    read time, and is therefore fully reversible. The SAME bin key is looked up
    in every listed channel; over the bins actually found the pooled learned tau
    is the n-weighted mean

        tau_pool = sum(n_i * tau_i) / sum(n_i),   n_pool = sum(n_i)

    and the SAME cold-start shrinkage blend as :func:`effective_tau` is then
    applied against the static prior with the POOLED count:

        tau = w * tau_pool + (1 - w) * static_prior,   w = n_pool / (n_pool + K)

    No bins found (empty pool, all channels missing/unvisited) -> exactly the
    static prior. With a single element in ``channels`` this is BIT-IDENTICAL to
    :func:`effective_tau` for that channel (the single found bin contributes its
    own tau verbatim, so no multiply/divide rounding creeps in). Validate-and-
    clamp ethos: a malformed bin (missing / non-numeric / non-finite tau or n) is
    skipped, never raised; a corrupt state degrades to the static prior.
    """
    prior = _clamp_tau(static_prior)
    key = shademap_bin_key(sun_az, sun_el, doy)
    contributions: list[tuple[int, float]] = []
    for channel in channels:
        bins = state.channels.get(channel)
        if not bins:
            continue
        binv = bins.get(key)
        if binv is None:
            continue
        try:
            n = int(binv.n)
            tau = float(binv.tau)
        except (AttributeError, TypeError, ValueError):
            continue  # malformed bin: skip, never raise
        if n <= 0 or not math.isfinite(tau):
            continue
        contributions.append((n, tau))
    if not contributions:
        return prior
    n_pool = sum(n for n, _ in contributions)
    if n_pool <= 0:
        return prior
    if len(contributions) == 1:
        # Single found bin: use its tau verbatim so the one-channel case is
        # bit-identical to effective_tau (no (n*tau)/n rounding).
        tau_pool = contributions[0][1]
    else:
        tau_pool = sum(n * tau for n, tau in contributions) / n_pool
    w = _shrinkage_weight(n_pool)
    if w <= 0.0:
        return prior
    blended = w * tau_pool + (1.0 - w) * prior
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


# ---------------------------------------------------------------------------
# Shade-group similarity + suggestion (SPEC §5, suggest_shade_groups service)
# ---------------------------------------------------------------------------


def _channel_bins(state: ShademapState, channel: str) -> dict[str, ShademapBin]:
    """The ``{bin_key: ShademapBin}`` map of one channel, or {} if absent.

    Duck-typed + validate-and-clamp: a state without a ``channels`` mapping, an
    absent channel, or a non-dict bins value all degrade to an empty map so the
    similarity math never raises on a corrupt / partial state.
    """
    channels = getattr(state, "channels", None)
    if not isinstance(channels, dict):
        return {}
    bins = channels.get(channel)
    return bins if isinstance(bins, dict) else {}


def _valid_bin(binv: object) -> tuple[int, float] | None:
    """Extract ``(n, tau)`` from a bin, or None when malformed / evidence-free.

    Mirrors the :func:`effective_tau_pooled` guard: a bin whose ``n`` / ``tau``
    are non-numeric, whose ``tau`` is non-finite, or whose ``n <= 0`` (no
    evidence) is skipped rather than raising — a corrupt bin must never poison a
    similarity comparison.
    """
    try:
        n = int(binv.n)  # type: ignore[attr-defined]
        tau = float(binv.tau)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return None
    if n <= 0 or not math.isfinite(tau):
        return None
    return n, tau


def _channel_has_data(state: ShademapState, channel: str) -> bool:
    """True when a channel holds at least one evidence-bearing (valid) bin."""
    return any(_valid_bin(b) is not None for b in _channel_bins(state, channel).values())


def channel_similarity(
    state: ShademapState, channel_a: str, channel_b: str
) -> dict:
    """Bin-wise similarity of two shademap channels (SPEC §5).

    Compares the two channels over the INTERSECTION of their bin keys — the sun
    positions BOTH have actually learned. Each common bin contributes weight
    ``w = min(n_a, n_b)`` (the evidence the weaker side backs it with), and the
    headline metric is the n-weighted mean absolute transmittance difference::

        mean_abs_diff = sum(w * |tau_a - tau_b|) / sum(w)

    Returns a plain dict::

        {"common_bins": <count of shared, valid bins>,
         "weight":      <sum of w over those bins>,
         "mean_abs_diff": <weighted mean |Δtau|>  or None when no common bins,
         "max_abs_diff":  <max |Δtau| over common bins> or None}

    A malformed / evidence-free bin (non-numeric or non-finite tau, ``n <= 0``)
    is skipped on either side (validate-and-clamp ethos); a missing channel
    yields an empty bin map, so disjoint / absent channels return ``common_bins
    == 0`` with ``None`` diffs. Never raises.
    """
    bins_a = _channel_bins(state, channel_a)
    bins_b = _channel_bins(state, channel_b)
    common = 0
    weight_sum = 0.0
    wdiff_sum = 0.0
    max_diff: float | None = None
    for key in bins_a.keys() & bins_b.keys():
        va = _valid_bin(bins_a[key])
        vb = _valid_bin(bins_b[key])
        if va is None or vb is None:
            continue
        n_a, tau_a = va
        n_b, tau_b = vb
        w = float(min(n_a, n_b))
        diff = abs(tau_a - tau_b)
        common += 1
        weight_sum += w
        wdiff_sum += w * diff
        if max_diff is None or diff > max_diff:
            max_diff = diff
    # weight_sum > 0 whenever common > 0 (every valid bin carries n >= 1), so
    # the mean is None exactly when there is no common evidence.
    mean = wdiff_sum / weight_sum if weight_sum > 0 else None
    return {
        "common_bins": common,
        "weight": weight_sum,
        "mean_abs_diff": mean,
        "max_abs_diff": max_diff,
    }


def _verdict(sim: dict, *, max_diff: float, min_common_bins: int) -> str:
    """Classify one similarity dict: similar / different / insufficient."""
    mean = sim["mean_abs_diff"]
    if sim["common_bins"] < min_common_bins or mean is None:
        return "insufficient"
    return "similar" if mean <= max_diff else "different"


def suggest_shade_groups(
    state: ShademapState,
    plane_names,
    *,
    max_diff: float,
    min_common_bins: int,
) -> dict:
    """Data-driven shade-group suggestion from the per-plane shademaps (SPEC §5).

    Each plane's learned shading is stored INDIVIDUALLY (its own channel, keyed
    by the plane name since v0.13.0). This compares every plane pair via
    :func:`channel_similarity` and proposes a grouping by GREEDY, COMPLETE-
    LINKAGE agglomeration:

      * a pair is ``"similar"`` when it has ``>= min_common_bins`` common bins
        AND ``mean_abs_diff <= max_diff``, ``"different"`` when it clears the
        evidence bar but exceeds ``max_diff``, and ``"insufficient"`` otherwise;
      * clusters start as singletons; similar pairs are visited in ASCENDING
        ``mean_abs_diff`` order and two clusters merge only when EVERY cross-pair
        between them is ``"similar"`` (complete linkage) — this prevents chaining
        A~B~C into one group when A and C are themselves too different;
      * a plane whose channel is missing / carries no evidence-bearing bin stays
        a singleton flagged ``insufficient_data``.

    The suggested group name for a multi-plane cluster is the FIRST member in
    config order (the v0.12.0 "named after a member" idiom); singletons get a
    ``null`` ``suggested_group``. Returns::

        {"groups": [{"planes": [...], "suggested_group": <name|None>,
                     "insufficient_data": <bool>}, ...],
         "pairs":  [{"a", "b", "common_bins", "mean_abs_diff",
                     "max_abs_diff", "verdict"}, ...],
         "thresholds": {"max_diff": ..., "min_common_bins": ...}}

    Groups are ordered by their first member's config index; pairs preserve the
    upper-triangular config order. Pure + total (never raises).
    """
    names = list(plane_names)
    n = len(names)

    # Pairwise similarity over the upper triangle, in config order.
    pairs_out: list[dict] = []
    sim_by_pair: dict[tuple[int, int], tuple[dict, str]] = {}
    for i in range(n):
        for j in range(i + 1, n):
            sim = channel_similarity(state, names[i], names[j])
            verdict = _verdict(
                sim, max_diff=max_diff, min_common_bins=min_common_bins
            )
            sim_by_pair[(i, j)] = (sim, verdict)
            pairs_out.append(
                {
                    "a": names[i],
                    "b": names[j],
                    "common_bins": sim["common_bins"],
                    "mean_abs_diff": sim["mean_abs_diff"],
                    "max_abs_diff": sim["max_abs_diff"],
                    "verdict": verdict,
                }
            )

    def _pair_similar(a: int, b: int) -> bool:
        entry = sim_by_pair.get((a, b) if a < b else (b, a))
        return entry is not None and entry[1] == "similar"

    # Only planes with evidence-bearing data participate in clustering; the rest
    # are insufficient-data singletons.
    data_idx = [i for i in range(n) if _channel_has_data(state, names[i])]
    data_set = set(data_idx)
    clusters: list[set[int]] = [{i} for i in data_idx]

    # Greedy merge candidates: similar pairs (both data planes), ascending diff.
    candidates = [
        (sim["mean_abs_diff"], i, j)
        for (i, j), (sim, verdict) in sim_by_pair.items()
        if verdict == "similar" and i in data_set and j in data_set
    ]
    candidates.sort(key=lambda t: (t[0], t[1], t[2]))

    def _cluster_index(idx: int) -> int:
        for k, cluster in enumerate(clusters):
            if idx in cluster:
                return k
        return -1

    for _mad, i, j in candidates:
        ci = _cluster_index(i)
        cj = _cluster_index(j)
        if ci == cj:
            continue
        # Complete linkage: only merge when EVERY cross-pair is similar.
        if all(_pair_similar(a, b) for a in clusters[ci] for b in clusters[cj]):
            merged = clusters[ci] | clusters[cj]
            for k in sorted((ci, cj), reverse=True):
                del clusters[k]
            clusters.append(merged)

    # Assemble groups: data clusters + insufficient singletons, each as a sorted
    # (config-order) index list, then ordered by first member.
    index_groups: list[list[int]] = [sorted(c) for c in clusters]
    index_groups.extend([i] for i in range(n) if i not in data_set)
    index_groups.sort(key=lambda idxs: idxs[0])

    groups_out: list[dict] = []
    for idxs in index_groups:
        is_insufficient = len(idxs) == 1 and idxs[0] not in data_set
        groups_out.append(
            {
                "planes": [names[i] for i in idxs],
                "suggested_group": names[idxs[0]] if len(idxs) > 1 else None,
                "insufficient_data": is_insufficient,
            }
        )

    return {
        "groups": groups_out,
        "pairs": pairs_out,
        "thresholds": {
            "max_diff": max_diff,
            "min_common_bins": min_common_bins,
        },
    }
