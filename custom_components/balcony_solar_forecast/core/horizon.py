"""Per-plane horizon: beam masking + diffuse sky-view factor (stdlib only).

Owner: horizon. Pure, HA-free. Encodes SPEC §5:
  - ``interp_elevation``: linear interpolation of the horizon-line elevation
    at a given sun azimuth from the plane's sorted (az, elev, tau) table,
    with 360-degree azimuth wrap.
  - ``transmittance_at``: transmittance at a sun azimuth, with seasonal
    foliage handled via a cosine ramp keyed on day-of-year (leaf-out in
    April, leaf-fall in November, tau_bare <-> tau_leafed).
  - ``sky_view_factor``: per-plane scale for the isotropic diffuse, derived
    from the horizon table (fixes E4 -- diffuse was never reduced). The sky
    below the horizon line is semi-transparent: it contributes the row's
    (seasonally-resolved) transmittance of its unobstructed value, so the SVF is
    doy-dependent wherever a seasonal row lives.

Application rule (in the engine): when the sun sits below the interpolated
horizon line, beam + circumsolar are multiplied by ``transmittance_at``; the
raw isotropic diffuse from ``hay_davies_poa`` is always multiplied by
``sky_view_factor`` (computed for the slot's day-of-year).

Azimuth convention here is INTERNAL throughout: 0 = North, clockwise
(90 = East, 180 = South, 270 = West).
"""

from __future__ import annotations

import math
from functools import lru_cache

from ..const import (
    FOLIAGE_LEAF_OFF_DOY,
    FOLIAGE_LEAF_ON_DOY,
    FOLIAGE_RAMP_DAYS,
)
from .types import HorizonRow, PlaneConfig

__all__ = ["interp_elevation", "transmittance_at", "sky_view_factor", "foliage_fraction"]


# ---------------------------------------------------------------------------
# Azimuth-wrapped linear interpolation helpers
# ---------------------------------------------------------------------------


def _wrap360(az: float) -> float:
    """Normalise an azimuth into [0, 360)."""
    a = math.fmod(az, 360.0)
    if a < 0.0:
        a += 360.0
    return a


@lru_cache(maxsize=256)
def _sorted_rows(rows: tuple[HorizonRow, ...]) -> tuple[HorizonRow, ...]:
    """Return the horizon rows sorted by wrapped ascending azimuth.

    The config boundary is expected to keep tables sorted, but we sort
    defensively here so a mis-ordered table (e.g. tree rows appended after a
    higher-azimuth far-field row) can never corrupt the interpolation. Frozen
    dataclasses are hashable, so this is memoised per distinct table.
    """
    return tuple(sorted(rows, key=lambda r: _wrap360(r.azimuth_deg)))


def _interp_rows(rows, sun_az, value):  # noqa: ANN001 - internal
    """Linear interpolation of a per-row scalar over the 360 azimuth circle.

    ``rows`` are the plane's horizon rows, assumed sorted by ascending
    ``azimuth_deg`` in [0, 360) (validated at the config boundary). ``value``
    is a closure ``HorizonRow -> float`` extracting the scalar to interpolate
    (elevation, or the seasonally-resolved tau). Interpolation wraps across
    360 -> 0 so the segment between the last and first row is continuous.

    Returns ``None`` for an empty table so callers can pick a sensible neutral
    default (0 deg horizon / tau 1.0).
    """
    n = len(rows)
    if n == 0:
        return None
    if n == 1:
        return value(rows[0])

    az = _wrap360(sun_az)
    first_az = _wrap360(rows[0].azimuth_deg)
    last_az = _wrap360(rows[-1].azimuth_deg)

    if az < first_az or az >= last_az:
        # Wrap segment: [last_az, 360) U [0, first_az), from rows[-1]->rows[0].
        lo, hi = rows[-1], rows[0]
        span = (first_az - last_az) % 360.0
        if span == 0.0:
            return value(lo)
        frac = ((az - last_az) % 360.0) / span
        return value(lo) + (value(hi) - value(lo)) * frac

    # Normal (non-wrap) bracketing.
    for i in range(n - 1):
        a0 = _wrap360(rows[i].azimuth_deg)
        a1 = _wrap360(rows[i + 1].azimuth_deg)
        if a0 <= az < a1:
            span = a1 - a0
            if span == 0.0:
                return value(rows[i])
            frac = (az - a0) / span
            return value(rows[i]) + (value(rows[i + 1]) - value(rows[i])) * frac

    # Exactly on the last breakpoint.
    return value(rows[-1])


# ---------------------------------------------------------------------------
# Horizon-line elevation
# ---------------------------------------------------------------------------


def interp_elevation(plane: PlaneConfig, sun_az: float) -> float:
    """Horizon-line elevation (deg) at ``sun_az`` for this plane.

    The horizon is treated as a closed 360-degree profile: linear
    interpolation between the plane's (defensively) sorted horizon rows (az in
    the INTERNAL 0=N convention), including the wrap segment between the
    last and first row across the 360 -> 0 boundary. So an azimuth that falls
    outside the explicitly listed span is interpolated across that wrap rather
    than clamped. Returns 0.0 for an empty table (no obstruction).
    """
    val = _interp_rows(
        _sorted_rows(plane.horizon), sun_az, lambda r: r.elevation_deg
    )
    return 0.0 if val is None else val


# ---------------------------------------------------------------------------
# Seasonal foliage ramp
# ---------------------------------------------------------------------------


def foliage_fraction(doy: int) -> float:
    """Leaf coverage in [0, 1] on day-of-year ``doy`` (1..366).

    0.0 = fully bare (winter), 1.0 = fully leafed (summer). A raised-cosine
    ramp of half-width FOLIAGE_RAMP_DAYS is centred on FOLIAGE_LEAF_ON_DOY
    (leaf-out, ~mid-April) and FOLIAGE_LEAF_OFF_DOY (leaf-fall, ~mid-Nov).
    The function is continuous and periodic across the year boundary, so a
    seasonal tau never jumps between adjacent days (ramp-continuity invariant).

    Ramp shape: f = (1 - cos(pi * t)) / 2 for t in [0, 1] across the leaf-out
    ramp (rising 0 -> 1) and its mirror across the leaf-fall ramp (1 -> 0).
    """
    # Work on a continuous day coordinate; clamp doy defensively.
    d = float(doy)

    on = float(FOLIAGE_LEAF_ON_DOY)
    off = float(FOLIAGE_LEAF_OFF_DOY)
    w = float(FOLIAGE_RAMP_DAYS)

    # Ramp windows (centre +/- half-width). Leaf-out rises, leaf-fall falls.
    on_start, on_end = on - w, on + w
    off_start, off_end = off - w, off + w

    if d <= on_start or d >= off_end:
        # Deep winter on both sides of the year boundary: fully bare.
        return 0.0
    if on_end <= d <= off_start:
        # High summer plateau: fully leafed.
        return 1.0
    if on_start < d < on_end:
        # Rising ramp (bare -> leafed) across leaf-out.
        t = (d - on_start) / (on_end - on_start)  # 0..1
        return (1.0 - math.cos(math.pi * t)) / 2.0
    # off_start < d < off_end: falling ramp (leafed -> bare) across leaf-fall.
    t = (d - off_start) / (off_end - off_start)  # 0..1
    return (1.0 + math.cos(math.pi * t)) / 2.0


def _row_tau(row: HorizonRow, doy: int) -> float:
    """Effective transmittance of one row on ``doy``.

    For a static row this is simply ``row.tau``. For a ``seasonal`` row the
    tau is blended between ``tau_bare`` (winter) and ``tau_leafed`` (summer)
    by the foliage fraction; missing bare/leafed values fall back to
    ``row.tau`` so a malformed row degrades gracefully.
    """
    if not row.seasonal:
        return row.tau
    bare = row.tau if row.tau_bare is None else row.tau_bare
    leafed = row.tau if row.tau_leafed is None else row.tau_leafed
    f = foliage_fraction(doy)
    return bare + (leafed - bare) * f


def _tau_at_el(points: tuple[tuple[float, float], ...], el: float) -> float:
    """Piecewise-linear tau at sun elevation ``el`` over sorted (el, tau) knots.

    Below the first knot the first value holds; above the last knot the last
    value holds (the profile only describes tau *below* the horizon edge, and
    the operator anchors the top knot on the edge). No monotonicity assumed.
    """
    if el <= points[0][0]:
        return points[0][1]
    if el >= points[-1][0]:
        return points[-1][1]
    for i in range(len(points) - 1):
        e0, t0 = points[i]
        e1, t1 = points[i + 1]
        if e0 <= el <= e1:
            span = e1 - e0
            if span <= 0.0:
                return t0
            return t0 + (t1 - t0) * (el - e0) / span
    return points[-1][1]  # pragma: no cover - covered by the >= last-knot guard


def _row_tau_at(row: HorizonRow, sun_el: float | None, doy: int | None) -> float:
    """Effective transmittance of one row at sun elevation ``sun_el`` on ``doy``.

    Without ``tau_points`` this is exactly :func:`_row_tau` (elevation-independent
    scalar), so a legacy row is bit-identical to the pre-0.22 behaviour. With
    ``tau_points`` the tau is a piecewise-linear function of ``sun_el``:

      - ``sun_el is None`` evaluates the profile at its topmost knot (== the last
        value), the elevation-agnostic fallback for callers that do not supply an
        elevation — so the old ``transmittance_at`` signature keeps reproducing a
        deterministic per-row value.
      - a non-seasonal row (or ``doy is None``) uses the leafed ``tau_points``
        directly (the static profile, analogous to the scalar ``tau`` fallback).
      - a seasonal row blends the bare and leafed profiles PER KNOT by the
        foliage fraction ("resolve before interpolate"), then evaluates the
        blended profile at ``sun_el``. ``tau_points_bare`` supplies the bare
        knots (same el raster, validated); when absent the scalar ``tau_bare``
        (or ``tau`` fallback) is the bare value at every knot.
    """
    pts = row.tau_points
    if pts is None:
        # Scalar fallback == the pre-0.22 path: a resolved seasonal/static tau
        # for an int doy, or the static ``tau`` when doy is None (the SVF
        # pure-caller default, matching :func:`_interp_diffuse_tau`).
        return row.tau if doy is None else _row_tau(row, doy)
    if sun_el is None:
        sun_el = pts[-1][0]
    if not row.seasonal or doy is None:
        return _tau_at_el(pts, sun_el)
    f = foliage_fraction(doy)
    bare_pts = row.tau_points_bare
    if bare_pts is None:
        bare_scalar = row.tau if row.tau_bare is None else row.tau_bare
        blended = tuple((el, bare_scalar + (t - bare_scalar) * f) for el, t in pts)
    else:
        blended = tuple(
            (el, bt + (t - bt) * f)
            for (el, t), (_be, bt) in zip(pts, bare_pts, strict=False)
        )
    return _tau_at_el(blended, sun_el)


def _row_diffuse_tau_at(
    row: HorizonRow, sun_el: float | None, doy: int | None
) -> float:
    """Effective DIFFUSE transmittance of one row for the SVF wedge (v0.22 D2).

    When ``diffuse_tau`` is set it OVERRIDES the beam tau in the diffuse
    sky-view integral only: the blocked wedge radiates ``diffuse_tau`` of the
    open sky instead of the beam ``tau`` / ``tau_points`` value — the effective
    radiance of the obstructed sector relative to open sky (a bright wall
    reflects ~0.5). It is NOT a transmission (SPEC §5.1) and never touches the
    beam gate. Default None == the diffuse keeps using the beam tau
    (:func:`_row_tau_at`), the pre-0.22 behaviour, so a row the operator did not
    mark is bit-identical and the beam path stays byte-untouched.
    """
    if row.diffuse_tau is not None:
        return row.diffuse_tau
    return _row_tau_at(row, sun_el, doy)


def transmittance_at(
    plane: PlaneConfig, sun_az: float, doy: int, sun_el: float | None = None
) -> float:
    """Beam transmittance (0..1) at ``sun_az`` on day-of-year ``doy``.

    Interpolates the effective tau between horizon rows (with 360 wrap).
    Rows flagged ``seasonal`` have their tau resolved for ``doy`` *before*
    interpolation (via the cosine foliage ramp), so a mixed seasonal/static
    neighbourhood blends correctly. Returns 1.0 (fully transparent) for an
    empty table. The result is clamped to [0, 1].

    ``sun_el`` (v0.22) resolves a row's inline ``tau_points`` elevation profile:
    for each az-bracketing row the tau is first evaluated at ``sun_el`` (piecewise
    linear in elevation), THEN interpolated linearly in azimuth — a row without a
    profile contributes its scalar tau unchanged. ``sun_el=None`` reproduces the
    exact pre-0.22 behaviour: legacy rows use their scalar tau, a profiled row is
    evaluated at its topmost knot.
    """
    val = _interp_rows(
        _sorted_rows(plane.horizon), sun_az, lambda r: _row_tau_at(r, sun_el, doy)
    )
    if val is None:
        return 1.0
    return 0.0 if val < 0.0 else 1.0 if val > 1.0 else val


# ---------------------------------------------------------------------------
# Sky-view factor for the isotropic diffuse
# ---------------------------------------------------------------------------
#
# The transpose step (`hay_davies_poa`) already returns the *unobstructed*
# isotropic-diffuse component for the tilted plane, i.e. it carries the
# standard tilted-surface view factor F0 = (1 + cos(beta)) / 2 for tilt beta.
# Our job here is only the *relative* reduction caused by the near/far horizon
# on top of that -- returning F0 again would double-count the tilt geometry.
#
# Derivation (isotropic sky, uniform-per-azimuth-sector horizon):
#   For an isotropic sky the diffuse view factor of a surface with unit normal
#   n is the cosine-weighted solid angle of visible sky:
#       F = (1/pi) * integral_{visible sky} cos(theta_i) dOmega
#   where theta_i is the angle between the sky direction and the surface
#   normal, and dOmega = cos(el) d(el) d(az) in horizon coordinates
#   (el = elevation, az = azimuth). For a plane of tilt beta and azimuth
#   az_p the normal is n = (sin(beta) sin(az_p), sin(beta) cos(az_p),
#   cos(beta)) and a sky direction at (az, el) is
#   s = (sin(az) cos(el), cos(az) cos(el), sin(el)), so
#       cos(theta_i) = n . s
#                    = sin(beta) cos(el) cos(az - az_p) + cos(beta) sin(el).
#   We integrate over az in [0, 360) and el in [0, 90], where the sky ABOVE the
#   horizon line h(az) contributes fully and the wedge BELOW it contributes the
#   row's (seasonally-resolved) transmittance tau(az) of its value -- the diffuse
#   is semi-transparent through a tree line exactly as the beam is (a tau=0 wall
#   still fully blocks, a tau=1 line is invisible to the diffuse). Only sky where
#   cos(theta_i) > 0 counts (the plane cannot see sky behind itself). We take the
#   SVF as the ratio of this integral with the real horizon to the SAME integral
#   over a flat 0-deg horizon:
#       SVF = F(horizon) / F(flat)           (bounded to (0, 1]).
#   Normalising by F(flat) rather than the analytic F0 = (1 + cos beta)/2
#   cancels the small error of the constant-elevation self-shadow cut used
#   here, so an unobstructed plane returns exactly 1.0 at every tilt.
#
# We evaluate F by numerical quadrature over azimuth (the horizon is a
# piecewise-linear function of az, cheap to sample) with the closed-form inner
# elevation integral:
#       G(az) = integral_{el=h..90} cos(theta_i) cos(el) del
#             = A * J2(h) + B * J1(h),
#   with A = sin(beta) cos(az - az_p), B = cos(beta), and
#       J1(h) = integral_{h..90} sin(el) cos(el) del = (1 - sin^2 h) / 2
#       J2(h) = integral_{h..90} cos^2(el) del
#             = (pi/2 - h)/2 + sin(2*90)/4 - sin(2h)/4
#             = (pi/2 - h)/2 - sin(2h)/4.
#   The plane cannot see the half of the dome behind itself; we clamp the
#   *per-azimuth* inner integral G to be non-negative so those sectors (and
#   grazing ones) never subtract. Because dOmega already carries the cos(el)
#   factor and the (1/pi) is the Lambertian normalisation, the outer azimuth
#   integral is simply
#       F = (1/pi) * integral_{az=0..2pi} G(az) d(az).
#
# Building-wall rows (elevation 90, tau 0) drive h -> 90 in their sector, so
# G -> 0 there and that whole sector stops contributing to the diffuse -- the
# hard wall correctly darkens both beam AND diffuse.


_SVF_AZ_SAMPLES = 360  # 1-degree azimuth quadrature; horizon is piecewise linear


def _inner_elevation_integral(h_deg: float, az_rad: float,
                              az_p_rad: float, beta_rad: float) -> float:
    """Closed-form inner integral G(az) over elevation h..90 (radians math).

    Returns the cosine-weighted visible-sky contribution of one azimuth
    column above horizon elevation ``h_deg``. Clamped to >= 0 so the back
    hemisphere and grazing sectors (where the plane barely sees the sky)
    never subtract.
    """
    h = math.radians(max(0.0, min(90.0, h_deg)))
    a = math.sin(beta_rad) * math.cos(az_rad - az_p_rad)
    b = math.cos(beta_rad)
    # J1 = int_h^{pi/2} sin(el)cos(el) del = (1 - sin^2 h)/2
    sin_h = math.sin(h)
    j1 = (1.0 - sin_h * sin_h) / 2.0
    # J2 = int_h^{pi/2} cos^2(el) del = (pi/2 - h)/2 - sin(2h)/4
    j2 = (math.pi / 2.0 - h) / 2.0 - math.sin(2.0 * h) / 4.0
    g = a * j2 + b * j1
    return g if g > 0.0 else 0.0


def _interp_diffuse_tau(rows, az_deg: float, doy: int | None) -> float:  # noqa: ANN001
    """Interpolated diffuse transmittance (0..1) at ``az_deg``, doy-aware.

    Same effective-tau interpolation as :func:`transmittance_at` (seasonal rows
    resolved for ``doy`` BEFORE interpolation via the foliage ramp), but accepts
    ``doy=None`` to interpolate each row's STATIC ``tau`` — the sky-view factor's
    pure-caller default. A row's ``diffuse_tau`` (v0.22 D2), when set, OVERRIDES
    its beam tau here (:func:`_row_diffuse_tau_at`); a row without it is
    bit-identical to the pre-0.22 path (a wall-free / diffuse-free table
    round-trips unchanged). Returns 1.0 (fully transparent) for an empty table.
    """
    val = _interp_rows(rows, az_deg, lambda r: _row_diffuse_tau_at(r, None, doy))
    if val is None:
        return 1.0
    return 0.0 if val < 0.0 else 1.0 if val > 1.0 else val


def _semi_transparent_column(h_deg: float, tau: float, az_rad: float,
                             az_p_rad: float, beta_rad: float) -> float:
    """Per-azimuth visible-sky contribution with a semi-transparent horizon.

    The sky ABOVE the horizon elevation ``h_deg`` contributes fully; the wedge
    BELOW it contributes ``tau`` of its unobstructed value (the diffuse now uses
    the row transmittance exactly as the beam does). Written as a tau-blend of
    the fully-open column (``_inner_elevation_integral(0)``) and the fully-opaque
    one (``_inner_elevation_integral(h)``) — algebraically ``above + tau*below``
    — which keeps the two physical extremes BIT-EXACT: ``tau <= 0`` returns the
    opaque column unchanged (old behaviour) and ``tau >= 1`` returns the
    fully-open column, identical to the flat-horizon normaliser (so a fully
    transmissive line yields SVF == 1 exactly).
    """
    above = _inner_elevation_integral(h_deg, az_rad, az_p_rad, beta_rad)
    if tau <= 0.0:
        return above
    full = _inner_elevation_integral(0.0, az_rad, az_p_rad, beta_rad)
    if tau >= 1.0:
        return full
    return tau * full + (1.0 - tau) * above


def _profile_knot_elevations(
    rows: tuple[HorizonRow, ...],
) -> tuple[float, ...]:
    """Sorted unique interior knot elevations across all rows' ``tau_points``.

    These are the elevations at which the diffuse band-integral segments the
    blocked wedge (SPEC §5.3). Empty when no row carries a profile — the caller then
    takes the fast scalar path. A superset (knots from every row, not just the
    two az-bracketing ones) only ADDS segment boundaries where tau is already
    continuous, so the midpoint quadrature stays correct.
    """
    els: set[float] = set()
    for r in rows:
        if r.tau_points is not None:
            for el, _t in r.tau_points:
                els.add(el)
    return tuple(sorted(els))


def _band_column(
    h_deg: float, az_deg: float, az_rad: float, az_p_rad: float, beta_rad: float,
    rows: tuple[HorizonRow, ...], doy: int | None, knot_els: tuple[float, ...],
) -> float:
    """Per-azimuth visible-sky contribution with an ELEVATION-PROFILED horizon.

    The band generalisation of :func:`_semi_transparent_column`: the blocked
    wedge ``[0, h]`` is segmented at the profile knots and each segment carries
    its midpoint tau (az-interpolated at the segment-midpoint elevation via the
    same machinery as :func:`transmittance_at`). Uses the closed-form
    ``_inner_elevation_integral`` differences, so a segment ``[e0, e1]``
    contributes ``tau_mid * (inner(e0) - inner(e1))``. The all-open / all-opaque
    columns are returned by the same expressions the flat normaliser and the
    opaque path use, so a uniform tau=1 profile yields SVF == 1 and a uniform
    tau=0 profile reproduces the opaque reduction bit-exactly (the ADR extremes).
    """
    above = _inner_elevation_integral(h_deg, az_rad, az_p_rad, beta_rad)
    if h_deg <= 0.0:
        return above  # no wedge below the line (== the flat/open column)

    bounds = [0.0]
    for el in knot_els:
        if 0.0 < el < h_deg:
            bounds.append(el)
    bounds.append(h_deg)

    wedge = 0.0
    all_zero = True
    all_one = True
    for i in range(len(bounds) - 1):
        e0 = bounds[i]
        e1 = bounds[i + 1]
        if e1 <= e0:
            continue
        el_mid = 0.5 * (e0 + e1)
        # Diffuse wedge: a row's ``diffuse_tau`` (v0.22 D2) overrides its beam tau
        # here; a row without it keeps the beam ``tau_points`` profile unchanged,
        # so a profile-only config is bit-identical to the pre-D2 band integral.
        tau_mid = _interp_rows(
            rows, az_deg, lambda r, _el=el_mid: _row_diffuse_tau_at(r, _el, doy)
        )
        if tau_mid is None:
            tau_mid = 1.0
        elif tau_mid < 0.0:
            tau_mid = 0.0
        elif tau_mid > 1.0:
            tau_mid = 1.0
        if tau_mid != 0.0:
            all_zero = False
        if tau_mid != 1.0:
            all_one = False
        band = (
            _inner_elevation_integral(e0, az_rad, az_p_rad, beta_rad)
            - _inner_elevation_integral(e1, az_rad, az_p_rad, beta_rad)
        )
        wedge += tau_mid * band
    if all_zero:
        return above  # opaque wedge, bit-identical to the fully-opaque column
    if all_one:
        # fully-open column, computed like the flat normaliser so SVF == 1 exactly
        return _inner_elevation_integral(0.0, az_rad, az_p_rad, beta_rad)
    return above + wedge


def _diffuse_view_integral(
    plane: PlaneConfig, use_horizon: bool, doy: int | None = None
) -> float:
    """Cosine-weighted visible-sky integral F for this plane.

    ``use_horizon=True`` integrates above the plane's horizon table, with the
    sky below the horizon line contributing the row's (seasonally-resolved)
    transmittance of its value (:func:`_semi_transparent_column`);
    ``use_horizon=False`` integrates above a flat 0-deg horizon (the same
    quadrature, so it serves as the exact self-consistent normaliser for the
    unobstructed case). ``doy`` resolves seasonal rows via the foliage ramp;
    None uses each row's static tau. Midpoint azimuth quadrature; the horizon is
    piecewise linear so 1-deg steps are ample.

    When any row carries an elevation profile (``tau_points``) the blocked wedge
    is integrated as an elevation BAND (:func:`_band_column`); otherwise the
    fast scalar column (:func:`_semi_transparent_column`) is used unchanged, so a
    plane without any profile is BIT-IDENTICAL to the pre-0.22 quadrature.
    """
    beta = math.radians(plane.tilt_deg)
    az_p = math.radians(plane.azimuth_deg)
    rows = _sorted_rows(plane.horizon)
    knot_els = _profile_knot_elevations(rows) if use_horizon else ()
    profiled = bool(knot_els)
    daz = 2.0 * math.pi / _SVF_AZ_SAMPLES
    acc = 0.0
    for i in range(_SVF_AZ_SAMPLES):
        az_deg = (i + 0.5) * (360.0 / _SVF_AZ_SAMPLES)
        az_rad = math.radians(az_deg)
        if not use_horizon:
            acc += _inner_elevation_integral(0.0, az_rad, az_p, beta)
        elif profiled:
            h = interp_elevation(plane, az_deg)
            acc += _band_column(h, az_deg, az_rad, az_p, beta, rows, doy, knot_els)
        else:
            h = interp_elevation(plane, az_deg)
            tau = _interp_diffuse_tau(rows, az_deg, doy)
            acc += _semi_transparent_column(h, tau, az_rad, az_p, beta)
    return acc * daz / math.pi


@lru_cache(maxsize=512)
def _sky_view_factor_cached(
    horizon_rows: tuple[HorizonRow, ...],
    tilt_deg: float,
    azimuth_deg: float,
    doy: int | None,
) -> float:
    """SVF for one plane GEOMETRY on one day-of-year (module-level memo).

    Keyed on the hashable geometry the quadrature actually depends on — the
    horizon rows tuple (frozen ``HorizonRow`` => hashable), tilt, azimuth and doy
    — so the O(360) integral runs at most once per distinct (geometry, day) for
    the WHOLE process, not once per plane per 15-min recompute. A config change
    yields a different rows tuple / tilt / azimuth => a different key, so
    invalidation is STRUCTURAL (no manual clearing needed). maxsize 512 easily
    covers a handful of planes x the ~3 forecast-window days x seasonal doys.
    See :func:`sky_view_factor` for the physics and the module derivation above.
    """
    if not horizon_rows:
        return 1.0
    # Rebuild a minimal plane (only tilt / azimuth / horizon feed the quadrature;
    # name / wp / etc. are irrelevant) so the existing view integral is reused
    # verbatim — the result is byte-identical to the pre-memo per-plane call.
    plane = PlaneConfig(
        name="", azimuth_deg=azimuth_deg, tilt_deg=tilt_deg, wp=0.0,
        horizon=horizon_rows,
    )
    f_flat = _diffuse_view_integral(plane, use_horizon=False)
    if f_flat <= 0.0:
        # Degenerate geometry (e.g. tilt >= 180); nothing sensible to scale.
        return 1.0
    f_obs = _diffuse_view_integral(plane, use_horizon=True, doy=doy)

    svf = f_obs / f_flat
    if svf >= 1.0:
        return 1.0
    if svf <= 0.0:
        return 1e-6
    return svf


def sky_view_factor(plane: PlaneConfig, doy: int | None = None) -> float:
    """Isotropic-diffuse sky-view factor in (0, 1] for this plane.

    The fraction of the plane's *unobstructed* tilted-sky view that survives
    its horizon table: SVF = F(horizon) / F(flat), where F is the
    cosine-weighted visible-sky integral (see the module derivation). Both
    numerator and denominator use the same quadrature and the same tilted
    self-shadow approximation, so an empty / all-zero horizon returns exactly
    1.0 and any obstruction returns a true *relative* reduction. Multiply the
    raw isotropic diffuse from ``hay_davies_poa`` by this value.

    The horizon is SEMI-TRANSPARENT to the diffuse: the sky below the horizon
    line at each azimuth contributes the row's transmittance of its value, so a
    tree line with tau 0.5 halves that wedge instead of blocking it like a wall.
    ``doy`` resolves the seasonal foliage ramp (a leafed-summer line blocks more
    diffuse than a bare-winter one, so the summer SVF is lower); ``doy=None``
    uses each row's static tau (the pure-caller default). A tau=1 line yields
    exactly 1.0; a tau=0 line reproduces the old fully-opaque reduction.

    Building-wall sectors (horizon 90 deg, tau 0) contribute nothing, so a plane
    with a wall over part of its dome gets a proportionally smaller SVF. Result
    is bounded to (0, 1]; a fully walled dome floors at a tiny positive epsilon
    rather than exactly zero.
    """
    return _sky_view_factor_cached(
        plane.horizon, plane.tilt_deg, plane.azimuth_deg, doy
    )
