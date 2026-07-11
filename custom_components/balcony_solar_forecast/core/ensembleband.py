"""Ensemble-weather uncertainty band factors (v0.16, SPEC §6).

Owner: quantiles (band family). Pure, HA-free (stdlib only). Two functions:

``ensemble_band_factors`` turns Open-Meteo ensemble member GHI into a per-hour
RELATIVE spread — ``(f10, f90)`` = the type-7 0.1 / 0.9 percentiles of the
member factors ``f_m = clamp(member_ghi / deterministic_ghi)``. This is a per
slot RELATIVE spread, NOT a full engine pass per member: the beam / diffuse
recomposition per member is second-order and deliberately approximated away
(the ensemble contributes the SHAPE of today's uncertainty, not an absolute
curve). An hour with too few usable members, or with a deterministic GHI below
the noise floor, is skipped — that slot falls back to the learned band.

``fuse_bands`` folds one hour's ensemble spread into the learned QuantileBands by
ENVELOPE-MAX: the wider band wins per slot. It is NEVER multiplied with the
learned band — the learned residual ring already contains the average weather
error for the class, so multiplying would double count the weather share; taking
the envelope adds only the extra spread the ensemble sees TODAY beyond what the
climatology already knew. (The dispersion-calibration refinement — scaling the
ensemble spread by a learned reliability factor before fusing — stays a
documented future path.) The GHI-proportionality of the factor is itself an
approximation: a member's GHI ratio is applied to the DC power band as if power
scaled linearly with GHI, which is only true to first order (temperature, IAM
and the beam/diffuse split all bend it) — acceptable because the ensemble is
never load-bearing (P50 / headline / scoreboard / kill-gate are untouched) and
any absence degrades seamlessly to the learned band.

Every path is pure and total: it never raises, always clamps, and preserves the
band monotonicity ``p10 <= p50 <= p90``.
"""

from __future__ import annotations

import math

# Reuse the SAME percentile pair as the learned bands so the two share one
# notion of "the 80% central interval" (SPEC §6): the ensemble 0.1 / 0.9 and the
# residual-ring P10 / P90 are directly envelope-comparable.
from ..const import QUANTILE_P_HIGH, QUANTILE_P_LOW
from .quantiles import empirical_percentile
from .types import QuantileBands

__all__ = ["ensemble_band_factors", "fuse_bands"]


def ensemble_band_factors(
    members_by_hour: dict[str, list[float]],
    det_ghi_by_hour: dict[str, float],
    *,
    min_members: int,
    min_det_ghi: float,
    f_min: float,
    f_max: float,
) -> dict[str, tuple[float, float]]:
    """Per-hour ``(f10, f90)`` ensemble spread factors (SPEC §6).

    For each hour present in BOTH maps: skip when the deterministic GHI is
    missing / non-finite / below ``min_det_ghi`` (the ratio is noise there);
    otherwise form each member factor ``f_m = member_ghi / det_ghi`` clamped to
    ``[f_min, f_max]`` and, once at least ``min_members`` usable members remain,
    take the type-7 ``QUANTILE_P_LOW`` / ``QUANTILE_P_HIGH`` (0.1 / 0.9)
    percentiles as ``(f10, f90)``. Hours failing any gate are simply absent from
    the result (the caller then leaves those slots on the learned band). Never
    raises; keys mirror the input hour keys (ISO-UTC interval starts).
    """
    out: dict[str, tuple[float, float]] = {}
    if not isinstance(members_by_hour, dict) or not isinstance(det_ghi_by_hour, dict):
        return out
    for hkey, members in members_by_hour.items():
        det = det_ghi_by_hour.get(hkey)
        try:
            detf = float(det)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if not math.isfinite(detf) or detf < min_det_ghi:
            continue
        factors: list[float] = []
        for m in members or ():
            try:
                mv = float(m)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(mv):
                continue
            f = mv / detf
            if f < f_min:
                f = f_min
            elif f > f_max:
                f = f_max
            factors.append(f)
        if len(factors) < min_members:
            continue
        factors.sort()
        f10 = empirical_percentile(factors, QUANTILE_P_LOW)
        f90 = empirical_percentile(factors, QUANTILE_P_HIGH)
        if f10 > f90:  # defensive: sorted input keeps this ordered, but clamp
            f10, f90 = f90, f10
        out[hkey] = (f10, f90)
    return out


def fuse_bands(
    learned: QuantileBands, ens: tuple[float, float] | None
) -> QuantileBands:
    """Fold one hour's ensemble spread into the learned band by ENVELOPE-MAX.

    Contract (SPEC §6):

      * ``ens is None`` -> ``learned`` returned UNCHANGED (bit-identical): a slot
        the ensemble does not cover stays exactly on the learned band.
      * learned is the neutral identity band (all 1.0) and ``ens`` present -> the
        COLD-START WIN: ``QuantileBands(p10=min(f10, 1.0), p50=1.0,
        p90=max(f90, 1.0))`` — real weather spread around P50 == 1.0 before the
        residual ring has any evidence.
      * both present -> ENVELOPE: ``p10 = min(learned.p10, f10)``,
        ``p90 = max(learned.p90, f90)``, ``p50 = learned.p50`` (the learned
        median is never moved by the ensemble). The band only ever WIDENS.

    Never multiplied (no double counting of the climatological weather share),
    never narrows, never raises; clamps to keep ``p10 <= p50 <= p90``.
    """
    if ens is None:
        return learned
    try:
        f10 = float(ens[0])
        f90 = float(ens[1])
    except (TypeError, ValueError, IndexError):
        return learned
    if not (math.isfinite(f10) and math.isfinite(f90)):
        return learned
    if f10 > f90:
        f10, f90 = f90, f10

    # Cold-start win: the learned band carries no information yet (neutral 1.0),
    # so the ensemble supplies the whole spread around an unshifted P50 == 1.0.
    if learned.p10 == learned.p50 == learned.p90 == 1.0:
        return QuantileBands(
            p10=min(f10, 1.0), p50=1.0, p90=max(f90, 1.0), n=learned.n
        )

    # Envelope-max: the wider of {learned, ensemble} wins on each edge, keeping
    # the learned median. min/max already guarantee lo <= learned.p10 <= p50 and
    # hi >= learned.p90 >= p50, but clamp against p50 defensively for any slop.
    p50 = learned.p50
    lo = min(learned.p10, f10)
    hi = max(learned.p90, f90)
    if lo > p50:
        lo = p50
    if hi < p50:
        hi = p50
    return QuantileBands(p10=lo, p50=p50, p90=hi, n=learned.n)
