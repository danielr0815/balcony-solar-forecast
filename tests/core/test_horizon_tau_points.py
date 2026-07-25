"""Tests for the inline elevation profile ``tau_points`` (ADR §2, v0.22).

Covers:
  - transmittance_at golden values: below the first knot, between knots, exactly
    on a knot, above the last knot but below the edge, above the edge (=last
    value / gated 1.0 by the engine), az-interpolation between a profiled row and
    a scalar row, and the wrap segment with a profile;
  - sun_el=None reproduces the elevation-agnostic top-knot fallback;
  - seasonal per-knot foliage blend (with and without tau_points_bare);
  - BACKWARD COMPATIBILITY: for any plane WITHOUT tau_points, transmittance_at
    and sky_view_factor are BIT-IDENTICAL to the reconstructed pre-0.22 path over
    a broad (az, el, doy) sample of the live default-site geometries;
  - SVF band integral vs. a brute-force fine quadrature (<0.5%);
  - SVF extremes: a constant-0 profile == the opaque scalar path bit-exact, a
    constant-1 profile == SVF 1.0 exactly;
  - SVF seasonal: winter (bare) SVF > summer (leafed) SVF with profiles.
"""

from __future__ import annotations

import math

import pytest
from balcony_solar_forecast.const import DEFAULT_SITE
from balcony_solar_forecast.core import horizon as H
from balcony_solar_forecast.core.types import HorizonRow, PlaneConfig, SiteConfig

# A synthetic east-canopy profile (the ADR §2.4 shape), edge at el 10.
PROFILE = ((4.5, 0.0), (5.5, 0.25), (6.5, 0.45), (8.0, 0.85), (9.5, 1.0))


def _plane(horizon=(), *, az=180.0, tilt=70.0, name="P"):
    return PlaneConfig(
        name=name, azimuth_deg=az, tilt_deg=tilt, wp=400.0, horizon=tuple(horizon)
    )


def _prow(az, *, elev=10.0, points=PROFILE, **kw):
    return HorizonRow(az, elev, 0.0, tau_points=points, **kw)


# ---------------------------------------------------------------------------
# transmittance_at golden values
# ---------------------------------------------------------------------------


def test_profile_below_first_knot_is_first_value():
    p = _plane((_prow(52.0), _prow(89.0)))
    # sun_el 3.0 is below the first knot 4.5 -> first value 0.0
    assert H.transmittance_at(p, 70.0, 200, sun_el=3.0) == pytest.approx(0.0)


def test_profile_between_knots_linear():
    p = _plane((_prow(52.0), _prow(89.0)))
    # sun_el 6.0 between (5.5,0.25) and (6.5,0.45): 0.25 + 0.20*0.5 = 0.35
    assert H.transmittance_at(p, 70.0, 200, sun_el=6.0) == pytest.approx(0.35)


def test_profile_exactly_on_knot():
    p = _plane((_prow(52.0), _prow(89.0)))
    assert H.transmittance_at(p, 70.0, 200, sun_el=6.5) == pytest.approx(0.45)


def test_profile_above_last_knot_holds_last_value():
    p = _plane((_prow(52.0), _prow(89.0)))
    # sun_el 12 is above the last knot 9.5 (and above the el-10 edge): the
    # profile holds its last value 1.0 (the engine's gate is what forces 1.0
    # above the horizon line; transmittance_at itself just clamps to the knot).
    assert H.transmittance_at(p, 70.0, 200, sun_el=12.0) == pytest.approx(1.0)
    assert H.transmittance_at(p, 70.0, 200, sun_el=9.5) == pytest.approx(1.0)


def test_profile_between_upper_knots():
    p = _plane((_prow(52.0), _prow(89.0)))
    # sun_el 9.0 between (8.0,0.85) and (9.5,1.0): 0.85 + 0.15*(1.0/1.5) = 0.95
    assert H.transmittance_at(p, 70.0, 200, sun_el=9.0) == pytest.approx(0.95)


def test_profile_az_interp_between_profile_and_scalar_row():
    # az 100 profile row, az 200 scalar row (tau 0.5, elevation-independent).
    p = _plane((_prow(100.0), HorizonRow(200.0, 10.0, 0.5)))
    # at az 150 (halfway) with sun_el 6.5: profile gives 0.45, scalar gives 0.5
    # -> az-mean 0.475
    assert H.transmittance_at(p, 150.0, 200, sun_el=6.5) == pytest.approx(0.475)


def test_profile_wrap_segment():
    p = _plane((_prow(10.0), _prow(350.0)))
    # az 0 is the midpoint of the 350->10 wrap; both rows identical profiles, so
    # the wrap interpolation is flat at the profile value for sun_el 6.5.
    assert H.transmittance_at(p, 0.0, 200, sun_el=6.5) == pytest.approx(0.45)


def test_profile_sun_el_none_evaluates_top_knot():
    p = _plane((_prow(52.0), _prow(89.0)))
    # No sun_el supplied -> topmost knot (last value) = 1.0, deterministic.
    assert H.transmittance_at(p, 70.0, 200) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# seasonal per-knot foliage blend
# ---------------------------------------------------------------------------

BARE = ((4.5, 0.2), (5.5, 0.5), (6.5, 0.7), (8.0, 0.95), (9.5, 1.0))


def test_seasonal_profile_with_bare_points_blends_per_knot():
    row = _prow(
        80.0, seasonal=True, tau_leafed=0.0, tau_bare=0.5,
        points=PROFILE, tau_points_bare=BARE,
    )
    p = _plane((row,))
    # winter (doy 1, bare): el 6.5 -> 0.7
    assert H.transmittance_at(p, 80.0, 1, sun_el=6.5) == pytest.approx(0.7)
    # summer (doy 200, leafed): el 6.5 -> 0.45
    assert H.transmittance_at(p, 80.0, 200, sun_el=6.5) == pytest.approx(0.45)
    # ramp centre: halfway between bare 0.7 and leafed 0.45
    from balcony_solar_forecast.const import FOLIAGE_LEAF_ON_DOY

    mid = H.transmittance_at(p, 80.0, FOLIAGE_LEAF_ON_DOY, sun_el=6.5)
    assert mid == pytest.approx((0.7 + 0.45) / 2.0)


def test_seasonal_profile_without_bare_points_uses_scalar_bare():
    # No tau_points_bare -> the bare value at every knot is the scalar tau_bare.
    row = _prow(
        80.0, seasonal=True, tau_leafed=0.0, tau_bare=0.8, points=PROFILE,
    )
    p = _plane((row,))
    # winter: blend of scalar bare 0.8 with leafed knot at f=0 -> 0.8 everywhere
    assert H.transmittance_at(p, 80.0, 1, sun_el=6.5) == pytest.approx(0.8)
    assert H.transmittance_at(p, 80.0, 1, sun_el=3.0) == pytest.approx(0.8)
    # summer: leafed profile -> el 6.5 = 0.45
    assert H.transmittance_at(p, 80.0, 200, sun_el=6.5) == pytest.approx(0.45)


def test_seasonal_ramp_continuity_with_profile():
    row = _prow(
        80.0, seasonal=True, tau_leafed=0.0, tau_bare=0.5,
        points=PROFILE, tau_points_bare=BARE,
    )
    p = _plane((row,))
    prev = H.transmittance_at(p, 80.0, 1, sun_el=6.5)
    for d in range(2, 367):
        cur = H.transmittance_at(p, 80.0, d, sun_el=6.5)
        assert abs(cur - prev) < 0.02
        prev = cur


# ---------------------------------------------------------------------------
# Backward-compatibility property test: rows WITHOUT tau_points are bit-exact
# ---------------------------------------------------------------------------


def _old_transmittance(plane, sun_az, doy):
    """Reconstruction of the pre-0.22 scalar transmittance_at algorithm."""
    val = H._interp_rows(
        H._sorted_rows(plane.horizon), sun_az, lambda r: H._row_tau(r, doy)
    )
    if val is None:
        return 1.0
    return 0.0 if val < 0.0 else 1.0 if val > 1.0 else val


def _old_svf(plane, doy):
    """Reconstruction of the pre-0.22 scalar SVF quadrature (no band path)."""
    beta = math.radians(plane.tilt_deg)
    az_p = math.radians(plane.azimuth_deg)
    rows = H._sorted_rows(plane.horizon)
    n = H._SVF_AZ_SAMPLES
    daz = 2.0 * math.pi / n

    def integral(use_horizon):
        acc = 0.0
        for i in range(n):
            az_deg = (i + 0.5) * (360.0 / n)
            az_rad = math.radians(az_deg)
            if use_horizon:
                h = H.interp_elevation(plane, az_deg)
                tau = H._interp_diffuse_tau(rows, az_deg, doy)
                acc += H._semi_transparent_column(h, tau, az_rad, az_p, beta)
            else:
                acc += H._inner_elevation_integral(0.0, az_rad, az_p, beta)
        return acc * daz / math.pi

    if not rows:
        return 1.0
    f_flat = integral(False)
    if f_flat <= 0.0:
        return 1.0
    svf = integral(True) / f_flat
    return 1.0 if svf >= 1.0 else 1e-6 if svf <= 0.0 else svf


def test_no_tau_points_transmittance_bit_identical_to_old():
    site = SiteConfig.from_dict(DEFAULT_SITE)  # no plane carries tau_points
    for p in site.planes:
        for doy in (1, 105, 200, 320):
            for az in range(0, 360, 7):
                azf = float(az)
                old = _old_transmittance(p, azf, doy)
                # sun_el is irrelevant for a non-profiled plane: every elevation
                # (and None) must return the identical scalar, bit-for-bit.
                assert H.transmittance_at(p, azf, doy) == old
                for el in (0.0, 5.0, 12.0, 45.0, 89.0):
                    assert H.transmittance_at(p, azf, doy, sun_el=el) == old


def test_no_tau_points_svf_bit_identical_to_old():
    site = SiteConfig.from_dict(DEFAULT_SITE)
    for p in site.planes:
        for doy in (None, 1, 200, 320):
            assert H.sky_view_factor(p, doy) == _old_svf(p, doy)


# ---------------------------------------------------------------------------
# SVF band integral: accuracy + extremes + seasonal
# ---------------------------------------------------------------------------


def _svf_brute(plane, doy, n_az=360, n_el=600):
    """Brute-force fine (az, el) Riemann quadrature of the cosine-weighted SVF.

    Independent of the closed-form band integral: samples the wedge elevation
    finely and multiplies each point by its az/el-interpolated tau, so it
    validates the midpoint-per-segment band approximation end to end.
    """
    beta = math.radians(plane.tilt_deg)
    az_p = math.radians(plane.azimuth_deg)
    rows = H._sorted_rows(plane.horizon)
    daz = 2.0 * math.pi / n_az
    del_el_deg = 90.0 / n_el

    def column(az_deg, use_horizon):
        az_rad = math.radians(az_deg)
        h = H.interp_elevation(plane, az_deg) if use_horizon else 0.0
        g = 0.0
        for j in range(n_el):
            el_deg = (j + 0.5) * del_el_deg
            el_rad = math.radians(el_deg)
            cos_t = (
                math.sin(beta) * math.cos(el_rad) * math.cos(az_rad - az_p)
                + math.cos(beta) * math.sin(el_rad)
            )
            w = cos_t * math.cos(el_rad)
            if use_horizon and el_deg < h:
                tau = H._interp_rows(
                    rows, az_deg, lambda r, _e=el_deg: H._row_tau_at(r, _e, doy)
                )
                tau = 1.0 if tau is None else max(0.0, min(1.0, tau))
                w *= tau
            g += w
        g *= math.radians(del_el_deg)
        return g if g > 0.0 else 0.0

    def integral(use_horizon):
        acc = 0.0
        for i in range(n_az):
            az_deg = (i + 0.5) * (360.0 / n_az)
            acc += column(az_deg, use_horizon)
        return acc * daz / math.pi

    f_flat = integral(False)
    f_obs = integral(True)
    return f_obs / f_flat


def test_svf_band_matches_brute_force_within_half_percent():
    # South plane, a semi-transparent canopy ring rising to el 40.
    prof = ((5.0, 0.0), (15.0, 0.3), (25.0, 0.6), (40.0, 1.0))
    rows = (_prow(0.0, elev=40.0, points=prof), _prow(180.0, elev=40.0, points=prof))
    p = _plane(rows, az=180.0, tilt=70.0)
    svf = H.sky_view_factor(p, doy=None)
    brute = _svf_brute(p, doy=None)
    assert svf == pytest.approx(brute, rel=0.005)
    assert 0.0 < svf < 1.0


def test_svf_constant_zero_profile_bit_exact_opaque_scalar():
    prof0 = ((5.0, 0.0), (40.0, 0.0))
    profiled = _plane(
        (_prow(0.0, elev=40.0, points=prof0), _prow(180.0, elev=40.0, points=prof0)),
        az=180.0, tilt=70.0,
    )
    opaque_scalar = _plane(
        (HorizonRow(0.0, 40.0, 0.0), HorizonRow(180.0, 40.0, 0.0)),
        az=180.0, tilt=70.0,
    )
    assert H.sky_view_factor(profiled) == H.sky_view_factor(opaque_scalar)


def test_svf_constant_one_profile_is_exactly_one():
    prof1 = ((5.0, 1.0), (40.0, 1.0))
    p = _plane(
        (_prow(0.0, elev=40.0, points=prof1), _prow(180.0, elev=40.0, points=prof1)),
        az=180.0, tilt=70.0,
    )
    assert H.sky_view_factor(p) == 1.0


def test_svf_profile_between_opaque_and_open():
    prof0 = ((5.0, 0.0), (40.0, 0.0))
    prof_half = ((5.0, 0.5), (40.0, 0.5))
    prof1 = ((5.0, 1.0), (40.0, 1.0))

    def svf(prof):
        p = _plane(
            (_prow(0.0, elev=40.0, points=prof), _prow(180.0, elev=40.0, points=prof)),
            az=180.0, tilt=70.0,
        )
        return H.sky_view_factor(p)

    opaque = svf(prof0)
    half = svf(prof_half)
    clear = svf(prof1)
    assert opaque < half < clear
    assert clear == 1.0


def test_svf_seasonal_profile_winter_higher_than_summer():
    leafed = ((5.0, 0.0), (25.0, 0.3), (40.0, 1.0))
    bare = ((5.0, 0.4), (25.0, 0.7), (40.0, 1.0))
    row0 = HorizonRow(
        0.0, 40.0, 0.0, seasonal=True, tau_leafed=0.0, tau_bare=0.4,
        tau_points=leafed, tau_points_bare=bare,
    )
    row1 = HorizonRow(
        180.0, 40.0, 0.0, seasonal=True, tau_leafed=0.0, tau_bare=0.4,
        tau_points=leafed, tau_points_bare=bare,
    )
    p = _plane((row0, row1), az=180.0, tilt=70.0)
    winter = H.sky_view_factor(p, doy=1)     # bare, more transparent
    summer = H.sky_view_factor(p, doy=200)   # leafed, more opaque
    assert winter > summer
    assert 0.0 < summer < winter < 1.0


def test_svf_memo_keyed_on_tau_points():
    # Two planes identical but for the profile must NOT collide in the lru_cache.
    prof_a = ((5.0, 0.2), (40.0, 1.0))
    prof_b = ((5.0, 0.8), (40.0, 1.0))
    pa = _plane(
        (_prow(0.0, elev=40.0, points=prof_a), _prow(180.0, elev=40.0, points=prof_a)),
        az=180.0, tilt=70.0,
    )
    pb = _plane(
        (_prow(0.0, elev=40.0, points=prof_b), _prow(180.0, elev=40.0, points=prof_b)),
        az=180.0, tilt=70.0,
    )
    assert H.sky_view_factor(pa) != H.sky_view_factor(pb)
