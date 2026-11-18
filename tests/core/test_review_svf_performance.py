"""Preserve seasonal/profile quadrature and avoid redundant sky-view work."""

from dataclasses import replace

import pytest
from balcony_solar_forecast.core import horizon
from balcony_solar_forecast.core.types import HorizonRow, PlaneConfig


def _profile_plane():
    return PlaneConfig(
        name="Profile", azimuth_deg=330, tilt_deg=74, wp=100,
        horizon=(
            HorizonRow(73, 24, 0.25, tau_points=((0, 0.1), (13, 0.4), (24, 0.7))),
            HorizonRow(198, 73, 0.15, diffuse_tau=0.3),
            HorizonRow(311, 42, 0.15, tau_points=((0, 0.1), (17, 0.33), (42, 0.4))),
            HorizonRow(0.5, 39, 0.3, tau_points=((0, 0.15), (22, 0.5), (39, 0.45))),
        ),
    )


def test_static_profile_reuses_quadrature_across_dates():
    plane = _profile_plane()
    first = horizon.sky_view_factor(plane, doy=51)
    before = horizon._sky_view_factor_cached.cache_info()
    assert horizon.sky_view_factor(plane, doy=321) == first
    after = horizon._sky_view_factor_cached.cache_info()
    assert after.misses == before.misses


def test_profile_brackets_columns_once_instead_of_each_elevation_band(monkeypatch):
    plane = _profile_plane()
    horizon._sky_view_factor_cached.cache_clear()
    wraps = 0
    wrap = horizon._wrap360

    def counted_wrap(azimuth):
        nonlocal wraps
        wraps += 1
        return wrap(azimuth)

    monkeypatch.setattr(horizon, "_wrap360", counted_wrap)
    value = horizon.sky_view_factor(plane, doy=51)
    assert 0.0 < value < 1.0
    # A full row scan per column plus initial sorting fits this bound even in
    # the longest wrap-free segment. Repeating the scan per band does not.
    assert wraps <= 4 + 360 * 9


@pytest.mark.parametrize("doy", [None, 1, 100, 110, 200, 315, 366])
def test_profile_quadrature_matches_parent_at_wrap_and_seasonal_ramps(doy):
    plane = _profile_plane()
    seasonal = HorizonRow(
        198, 73, 0.15, seasonal=True, tau_bare=0.65,
        tau_points=((0, 0.13), (29, 0.05), (73, 0.2)),
        tau_points_bare=((0, 0.66), (29, 0.24), (73, 0.72)),
    )
    plane = replace(plane, horizon=(plane.horizon[0], seasonal, *plane.horizon[2:]))
    # Frozen outputs from the parent quadrature for this synthetic geometry.
    expected = {
        None: 0.5302496987204313,
        1: 0.5540091779705972,
        100: 0.5452041412113448,
        110: 0.5390547354796835,
        200: 0.5302496987204313,
        315: 0.5421294383455141,
        366: 0.5540091779705972,
    }
    assert horizon.sky_view_factor(plane, doy) == pytest.approx(expected[doy], rel=0, abs=1e-14)
