"""Tests for the per-plane horizon module (pure, HA-free).

Covers (SPEC §4 step 5 / §13, task brief):
  - linear interpolation of the horizon-line elevation, including endpoints,
    midpoints and the 360 -> 0 wrap segment;
  - defensive sorting of a mis-ordered table;
  - seasonal tau: cosine foliage ramp between tau_bare and tau_leafed, ramp
    continuity across the year, correct plateaus, blend before interpolation;
  - building-wall rows (elev 90, tau 0);
  - sky-view factor: (0, 1], unobstructed == 1.0 at every tilt, monotonic in
    the horizon height (higher horizon -> lower SVF), wall reduces SVF;
  - the shipped operator default tables load and are internally consistent.
"""

from __future__ import annotations

import math

import pytest

from balcony_solar_forecast.const import DEFAULT_SITE
from balcony_solar_forecast.core import horizon as H
from balcony_solar_forecast.core.types import HorizonRow, PlaneConfig, SiteConfig


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _plane(horizon=(), *, az=180.0, tilt=70.0, name="P"):
    return PlaneConfig(
        name=name, azimuth_deg=az, tilt_deg=tilt, wp=400.0, horizon=tuple(horizon)
    )


def _rows(*triples):
    """Build a horizon tuple from (az, elev, tau) triples."""
    return tuple(HorizonRow(a, e, t) for a, e, t in triples)


# ---------------------------------------------------------------------------
# interp_elevation
# ---------------------------------------------------------------------------


def test_empty_table_is_flat_horizon():
    assert H.interp_elevation(_plane(), 123.0) == 0.0


def test_single_row_is_constant():
    p = _plane(_rows((90.0, 20.0, 0.0)))
    assert H.interp_elevation(p, 10.0) == 20.0
    assert H.interp_elevation(p, 300.0) == 20.0


def test_interp_at_breakpoints_and_midpoint():
    p = _plane(_rows((60.0, 10.0, 0.0), (100.0, 20.0, 0.0)))
    assert H.interp_elevation(p, 60.0) == pytest.approx(10.0)
    assert H.interp_elevation(p, 100.0) == pytest.approx(20.0)
    # halfway in azimuth -> halfway in elevation
    assert H.interp_elevation(p, 80.0) == pytest.approx(15.0)
    # quarter
    assert H.interp_elevation(p, 70.0) == pytest.approx(12.5)


def test_interp_wraps_across_360():
    # Rows at 350 and 10 -> the short way round is 20 deg through 0.
    p = _plane(_rows((10.0, 30.0, 0.0), (350.0, 10.0, 0.0)))
    # Exactly at 0 deg: midpoint of the 350->10 wrap segment -> 20.
    assert H.interp_elevation(p, 0.0) == pytest.approx(20.0)
    # 355 is a quarter from 350 toward 10 -> 10 + 0.25*(30-10) = 15.
    assert H.interp_elevation(p, 355.0) == pytest.approx(15.0)
    # 5 is three quarters -> 25.
    assert H.interp_elevation(p, 5.0) == pytest.approx(25.0)


def test_negative_azimuth_normalised():
    p = _plane(_rows((10.0, 30.0, 0.0), (350.0, 10.0, 0.0)))
    assert H.interp_elevation(p, -5.0) == H.interp_elevation(p, 355.0)
    assert H.interp_elevation(p, -360.0) == H.interp_elevation(p, 0.0)


def test_defensive_sort_of_misordered_table():
    ordered = _rows((60.0, 10.0, 0.0), (100.0, 20.0, 0.0), (150.0, 16.0, 0.0))
    shuffled = _rows((150.0, 16.0, 0.0), (60.0, 10.0, 0.0), (100.0, 20.0, 0.0))
    p_ok = _plane(ordered)
    p_bad = _plane(shuffled)
    for az in (55.0, 60.0, 80.0, 100.0, 125.0, 150.0, 200.0):
        assert H.interp_elevation(p_ok, az) == pytest.approx(
            H.interp_elevation(p_bad, az)
        )


def test_wall_row_gives_90():
    p = _plane(_rows((60.0, 10.0, 0.0), (212.0, 90.0, 0.0), (360.0, 90.0, 0.0)))
    assert H.interp_elevation(p, 250.0) == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# foliage_fraction (cosine ramp)
# ---------------------------------------------------------------------------


def test_foliage_plateaus():
    # Deep winter -> bare; high summer -> leafed.
    assert H.foliage_fraction(1) == 0.0
    assert H.foliage_fraction(200) == 1.0
    assert H.foliage_fraction(366) == 0.0


def test_foliage_ramp_centres_are_half():
    # At each ramp centre the raised cosine passes through 0.5.
    from balcony_solar_forecast.const import (
        FOLIAGE_LEAF_OFF_DOY,
        FOLIAGE_LEAF_ON_DOY,
    )

    assert H.foliage_fraction(FOLIAGE_LEAF_ON_DOY) == pytest.approx(0.5)
    assert H.foliage_fraction(FOLIAGE_LEAF_OFF_DOY) == pytest.approx(0.5)


def test_foliage_is_continuous_all_year():
    # No day-to-day jump exceeds the analytic max slope of the cosine ramp.
    from balcony_solar_forecast.const import FOLIAGE_RAMP_DAYS

    max_slope = math.pi / (2.0 * (2.0 * FOLIAGE_RAMP_DAYS))  # d/dt of raised cos
    prev = H.foliage_fraction(1)
    for d in range(2, 367):
        cur = H.foliage_fraction(d)
        assert abs(cur - prev) <= max_slope + 1e-9
        prev = cur


def test_foliage_monotonic_within_ramps():
    from balcony_solar_forecast.const import FOLIAGE_LEAF_ON_DOY, FOLIAGE_RAMP_DAYS

    lo = FOLIAGE_LEAF_ON_DOY - FOLIAGE_RAMP_DAYS
    hi = FOLIAGE_LEAF_ON_DOY + FOLIAGE_RAMP_DAYS
    vals = [H.foliage_fraction(d) for d in range(lo, hi + 1)]
    assert all(b >= a - 1e-12 for a, b in zip(vals, vals[1:]))  # rising


# ---------------------------------------------------------------------------
# transmittance_at
# ---------------------------------------------------------------------------


def test_transmittance_empty_table_transparent():
    assert H.transmittance_at(_plane(), 150.0, 200) == 1.0


def test_transmittance_static_interpolation():
    p = _plane(_rows((100.0, 20.0, 0.2), (200.0, 20.0, 0.8)))
    assert H.transmittance_at(p, 100.0, 1) == pytest.approx(0.2)
    assert H.transmittance_at(p, 200.0, 1) == pytest.approx(0.8)
    assert H.transmittance_at(p, 150.0, 1) == pytest.approx(0.5)


def test_transmittance_clamped_to_unit_interval():
    p = _plane(_rows((100.0, 20.0, -0.5), (200.0, 20.0, 2.0)))
    assert H.transmittance_at(p, 100.0, 1) == 0.0
    assert H.transmittance_at(p, 200.0, 1) == 1.0


def test_seasonal_row_bare_vs_leafed():
    row = HorizonRow(
        150.0, 40.0, 0.45, seasonal=True, tau_leafed=0.45, tau_bare=0.8
    )
    p = _plane((row,))
    # winter (bare) -> 0.8, summer (leafed) -> 0.45
    assert H.transmittance_at(p, 150.0, 1) == pytest.approx(0.8)
    assert H.transmittance_at(p, 150.0, 200) == pytest.approx(0.45)
    # ramp centre -> halfway between
    from balcony_solar_forecast.const import FOLIAGE_LEAF_ON_DOY

    mid = H.transmittance_at(p, 150.0, FOLIAGE_LEAF_ON_DOY)
    assert mid == pytest.approx((0.8 + 0.45) / 2.0)


def test_seasonal_ramp_continuity_day_to_day():
    row = HorizonRow(
        150.0, 40.0, 0.45, seasonal=True, tau_leafed=0.45, tau_bare=0.8
    )
    p = _plane((row,))
    prev = H.transmittance_at(p, 150.0, 1)
    for d in range(2, 367):
        cur = H.transmittance_at(p, 150.0, d)
        assert abs(cur - prev) < 0.02  # smooth, no cliff
        prev = cur


def test_seasonal_resolved_before_interpolation():
    # A seasonal tree row next to a static far-field row: the seasonal tau is
    # resolved for the doy first, then blended with the neighbour.
    tree = HorizonRow(
        160.0, 40.0, 0.45, seasonal=True, tau_leafed=0.45, tau_bare=0.8
    )
    static = HorizonRow(120.0, 16.0, 0.0)
    p = _plane((static, tree))
    # midway az 140 in July: blend of 0.0 (static) and 0.45 (leafed tree)
    assert H.transmittance_at(p, 140.0, 200) == pytest.approx(0.225, abs=1e-6)
    # midway az 140 in January: blend of 0.0 and 0.8 (bare tree)
    assert H.transmittance_at(p, 140.0, 1) == pytest.approx(0.4, abs=1e-6)


# ---------------------------------------------------------------------------
# sky_view_factor
# ---------------------------------------------------------------------------


def test_svf_unobstructed_is_one_at_all_tilts():
    for tilt in (0.0, 15.0, 30.0, 45.0, 70.0, 80.0, 90.0):
        p = _plane((), tilt=tilt)
        assert H.sky_view_factor(p) == 1.0  # empty table -> exactly 1.0


def test_svf_flat_horizon_rows_still_unity():
    # A table that is all-zero elevation is not "empty" but must still be ~1.0.
    p = _plane(_rows((0.0, 0.0, 0.0), (180.0, 0.0, 0.0)), tilt=70.0)
    assert H.sky_view_factor(p) == pytest.approx(1.0, abs=1e-6)


def test_svf_in_unit_interval():
    site = SiteConfig.from_dict(DEFAULT_SITE)
    for p in site.planes:
        svf = H.sky_view_factor(p)
        assert 0.0 < svf <= 1.0


def test_svf_monotonic_in_horizon_height():
    # Raising a uniform horizon ring must never increase the SVF.
    prev = None
    for h in (0.0, 5.0, 10.0, 20.0, 40.0, 60.0, 80.0):
        p = _plane(_rows((0.0, h, 0.0), (180.0, h, 0.0)), az=180.0, tilt=70.0)
        svf = H.sky_view_factor(p)
        if prev is not None:
            assert svf <= prev + 1e-9
        prev = svf


def test_svf_wall_reduces_below_open():
    open_plane = _plane(_rows((0.0, 5.0, 0.0), (180.0, 5.0, 0.0)), az=205.0, tilt=70.0)
    walled = _plane(
        _rows((0.0, 5.0, 0.0), (180.0, 5.0, 0.0), (212.0, 90.0, 0.0), (360.0, 90.0, 0.0)),
        az=205.0,
        tilt=70.0,
    )
    assert H.sky_view_factor(walled) < H.sky_view_factor(open_plane)


def test_svf_full_wall_dome_floors_positive():
    # Wall all the way around: SVF collapses but stays strictly > 0.
    p = _plane(_rows((0.0, 90.0, 0.0), (180.0, 90.0, 0.0)), tilt=70.0)
    svf = H.sky_view_factor(p)
    assert 0.0 < svf < 0.01


def test_svf_higher_tilt_more_wall_sensitive_but_bounded():
    # Same wall, two tilts: both in range, both < 1.
    for tilt in (70.0, 80.0):
        p = _plane(
            _rows((0.0, 5.0, 0.0), (180.0, 5.0, 0.0), (212.0, 90.0, 0.0), (360.0, 90.0, 0.0)),
            az=205.0,
            tilt=tilt,
        )
        assert 0.0 < H.sky_view_factor(p) < 1.0


# ---------------------------------------------------------------------------
# Operator default site: load + internal consistency
# ---------------------------------------------------------------------------


def test_default_site_loads_and_all_functions_run():
    site = SiteConfig.from_dict(DEFAULT_SITE)
    assert len(site.planes) == 8
    for p in site.planes:
        # every plane: SVF valid, elevation finite everywhere, tau in [0,1]
        assert 0.0 < H.sky_view_factor(p) <= 1.0
        for az in range(0, 360, 5):
            el = H.interp_elevation(p, az)
            assert -90.0 <= el <= 90.0
            tau = H.transmittance_at(p, float(az), 200)
            assert 0.0 <= tau <= 1.0


def test_default_farfield_slope_matches_spec():
    # SPEC §13.4: az 60..100 -> 13 deg, az 100..150 -> 16 deg on all planes.
    site = SiteConfig.from_dict(DEFAULT_SITE)
    m1 = site.plane_by_name("M1")  # north/front plane, far-field only
    assert H.interp_elevation(m1, 80.0) == pytest.approx(13.0)
    assert H.interp_elevation(m1, 125.0) == pytest.approx(16.0)


def test_default_south_planes_have_seasonal_trees_and_wall():
    site = SiteConfig.from_dict(DEFAULT_SITE)
    for name, tree_elev in (("M4", 40.0), ("M8", 30.0)):
        p = site.plane_by_name(name)
        # tree sector peak elevation present
        assert H.interp_elevation(p, 135.0) == pytest.approx(tree_elev)
        assert H.interp_elevation(p, 175.0) == pytest.approx(tree_elev)
        # building wall above az 212
        assert H.interp_elevation(p, 250.0) == pytest.approx(90.0)
        # seasonal tree tau: bare (winter) > leafed (summer) in the tree core
        bare = H.transmittance_at(p, 135.0, 1)
        leafed = H.transmittance_at(p, 135.0, 200)
        assert bare == pytest.approx(0.8)
        assert leafed == pytest.approx(0.45)
        assert bare > leafed
        # wall sector fully opaque year round
        assert H.transmittance_at(p, 220.0, 1) == pytest.approx(0.0)
        assert H.transmittance_at(p, 220.0, 200) == pytest.approx(0.0)


def test_default_south_tree_sector_is_flat_and_seasonal_throughout():
    """Regression (const §13.4): the far-field 150-deg row must NOT sit inside
    the tree sector, and the tree plateau must stay flat at its elevation with
    seasonal tau across its whole 135..175 span (previously the line dipped to
    16 deg at az 150 and the tau lost its seasonal foliage there)."""
    site = SiteConfig.from_dict(DEFAULT_SITE)
    for name, tree_elev in (("M4", 40.0), ("M8", 30.0)):
        p = site.plane_by_name(name)
        for az in (135.0, 150.0, 160.0, 165.0, 175.0):
            assert H.interp_elevation(p, az) == pytest.approx(tree_elev), (
                f"{name} az {az} should sit on the tree plateau"
            )
            # tau must stay seasonal (bare 0.8 winter, leafed 0.45 summer)
            assert H.transmittance_at(p, az, 1) == pytest.approx(0.8)
            assert H.transmittance_at(p, az, 200) == pytest.approx(0.45)


def test_default_south_prime_sector_is_ungated_below_wall():
    """Regression (const §13.4): between the tree top (175) and the wall (212)
    the plane's own prime-output azimuths (~175..212, azimuth 205) must be
    ungated — full beam transmission (tau 1) below the tree elevation — not a
    phantom ramp from el 40 up to the el-90 wall (which killed the operator's
    measured June peak at az ~200)."""
    site = SiteConfig.from_dict(DEFAULT_SITE)
    for name, tree_elev in (("M4", 40.0), ("M8", 30.0)):
        p = site.plane_by_name(name)
        for az in (190.0, 200.0, 205.0, 210.0):
            # horizon line is at (or near) 0 here: a sun at el >> tree_elev is
            # well above it, so beam is fully transmitted.
            assert H.interp_elevation(p, az) < tree_elev, (
                f"{name} az {az} must be below the tree plateau (ungated)"
            )
            assert H.transmittance_at(p, az, 200) == pytest.approx(1.0)
            assert H.transmittance_at(p, az, 1) == pytest.approx(1.0)
        # the wall then bites as a hard step at 212.
        assert H.interp_elevation(p, 212.0) == pytest.approx(90.0)


def test_default_front_planes_open_outside_east_slope():
    """Regression (const §13.4): the far-field east slope (60..150) must NOT be
    smeared over the whole circle — azimuths west/north/south of it fall back to
    an OPEN horizon (elev 0, tau 1) until real PVGIS rows are imported."""
    site = SiteConfig.from_dict(DEFAULT_SITE)
    for name in ("M1", "M2", "M3", "M5", "M6", "M7"):
        p = site.plane_by_name(name)
        # inside the slope: the measured elevations hold
        assert H.interp_elevation(p, 80.0) == pytest.approx(13.0)
        assert H.interp_elevation(p, 125.0) == pytest.approx(16.0)
        # outside the slope: open, transparent horizon everywhere
        for az in (0.0, 30.0, 200.0, 250.0, 270.0, 300.0, 340.0):
            assert H.interp_elevation(p, az) == pytest.approx(0.0), (
                f"{name} az {az} should be an open horizon"
            )
            assert H.transmittance_at(p, az, 200) == pytest.approx(1.0)


def test_default_south_svf_lower_than_north():
    # South planes carry a building wall over ~half their dome, so their
    # diffuse sky-view factor must be markedly lower than the front/north ones.
    site = SiteConfig.from_dict(DEFAULT_SITE)
    south = H.sky_view_factor(site.plane_by_name("M4"))
    north = H.sky_view_factor(site.plane_by_name("M1"))
    assert south < north


def test_default_site_round_trips_through_dict():
    site = SiteConfig.from_dict(DEFAULT_SITE)
    again = SiteConfig.from_dict(site.to_dict())
    for a, b in zip(site.planes, again.planes):
        assert H.sky_view_factor(a) == pytest.approx(H.sky_view_factor(b))
        for az in (80.0, 155.0, 220.0):
            assert H.interp_elevation(a, az) == pytest.approx(
                H.interp_elevation(b, az)
            )
