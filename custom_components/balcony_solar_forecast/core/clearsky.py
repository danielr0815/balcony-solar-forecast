"""Clear-sky GHI (Haurwitz) and clear-sky index (stdlib math only).

Owner: irradiance. Used only as a learning gate / normaliser, never as a
forecast source (SPEC §4 step 3, §5). HA-free, pure.

Haurwitz (1945) is a single-parameter clear-sky model driven only by the
solar zenith angle:

    GHI_clear = 1098 * cos(z) * exp(-0.059 / cos(z))     [W/m^2]

with ``cos(z) = sin(elevation)``. It is deliberately coarse (no turbidity,
no altitude), which is exactly why the SPEC uses k_c only as a *gate* and
normaliser, elevation-conditioned, never as a prognosis. Below the horizon
the reference is 0.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

__all__ = ["haurwitz_ghi", "clear_sky_index", "hourly_kc"]

# Haurwitz coefficients (Reno & Hansen clear-sky review; original 1945 fit).
_HAURWITZ_A = 1098.0
_HAURWITZ_B = 0.059


def haurwitz_ghi(elevation_deg: float) -> float:
    """Haurwitz clear-sky global horizontal irradiance.

    Args:
        elevation_deg: solar elevation, degrees above horizon.

    Returns:
        Estimated clear-sky GHI in W/m^2 (0 when the sun is at or below
        the horizon).
    """
    if elevation_deg <= 0.0:
        return 0.0
    cos_zenith = math.sin(math.radians(elevation_deg))
    # sin(elevation) > 0 here, so the division is safe.
    ghi = _HAURWITZ_A * cos_zenith * math.exp(-_HAURWITZ_B / cos_zenith)
    # Numerically the exp term keeps this positive, but guard anyway.
    return ghi if ghi > 0.0 else 0.0


def clear_sky_index(ghi: float, elevation_deg: float) -> float:
    """Clear-sky index k_c = GHI / Haurwitz(elevation).

    Returns 0.0 when the clear-sky reference is 0 (sun at/below horizon).
    Haurwitz is coarse at low sun, so callers gate k_c elevation-dependently
    (SPEC §5).
    """
    reference = haurwitz_ghi(elevation_deg)
    if reference <= 0.0:
        return 0.0
    k_c = ghi / reference
    return k_c if k_c > 0.0 else 0.0


def hourly_kc(samples: Iterable[tuple[float, float]]) -> float:
    """Clear-sky-energy-weighted k_c over an hour's ``(ghi, elevation)`` samples.

    THE one hourly-kc reduction shared by the live nightly shademap trainer and
    the offline backfill, so both training paths gate quasi-clear on the same
    estimator:

        k_c(hour) = sum(ghi_i) / sum(haurwitz_ghi(el_i))

    over the samples whose clear-sky reference is positive (sun up). Weighting
    by the clear-sky energy makes the reduction robust at dawn/dusk — a
    near-horizon slot with a crude Haurwitz reference contributes almost
    nothing, instead of (as a plain last-write or mean would) dominating the
    hour. A single sample reduces exactly to :func:`clear_sky_index`, which is
    what the hourly-resolution backfill passes. Returns 0.0 for an empty /
    all-below-horizon hour, mirroring :func:`clear_sky_index`.
    """
    ghi_total = 0.0
    ref_total = 0.0
    for ghi, elevation_deg in samples:
        reference = haurwitz_ghi(elevation_deg)
        if reference <= 0.0:
            continue
        ghi_total += max(ghi, 0.0)
        ref_total += reference
    if ref_total <= 0.0:
        return 0.0
    k_c = ghi_total / ref_total
    return k_c if k_c > 0.0 else 0.0
