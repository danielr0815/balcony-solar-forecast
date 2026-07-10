"""Module-level SVF memo tests (Phase-D optimisation #2, audit #9b).

Plain pytest, no Home Assistant imports (SPEC §4).

``horizon.sky_view_factor`` now delegates to a module-level
``functools.lru_cache`` keyed on the plane GEOMETRY (horizon rows tuple, tilt,
azimuth) + day-of-year, so the O(360) quadrature runs once per distinct
(geometry, day) for the whole process — surviving across the 15-min recompute
cycles instead of a per-``compute_forecast``-call memo. These tests pin the two
behaviours the memo must have: a repeated (rows, tilt, doy) call returns the
SAME object (a real cache hit), and a different doy on a SEASONAL horizon
produces a different value (structural key on doy, no stale reuse).
"""

from __future__ import annotations

from balcony_solar_forecast.core import horizon as H
from balcony_solar_forecast.core.types import HorizonRow, PlaneConfig


def _obstructed_plane(tau: float = 0.5) -> PlaneConfig:
    """A south plane behind a 40-deg tree ring at ``tau`` (SVF strictly < 1)."""
    rows = (
        HorizonRow(0.0, 40.0, tau),
        HorizonRow(180.0, 40.0, tau),
    )
    return PlaneConfig(name="S", azimuth_deg=180.0, tilt_deg=70.0, wp=430.0,
                       horizon=rows)


def _seasonal_plane() -> PlaneConfig:
    """A south plane behind a SEASONAL tree ring (bare 0.8 / leafed 0.45)."""
    rows = (
        HorizonRow(0.0, 40.0, 0.45, seasonal=True, tau_leafed=0.45, tau_bare=0.8),
        HorizonRow(180.0, 40.0, 0.45, seasonal=True, tau_leafed=0.45, tau_bare=0.8),
    )
    return PlaneConfig(name="S", azimuth_deg=180.0, tilt_deg=70.0, wp=430.0,
                       horizon=rows)


def test_repeated_call_returns_same_cached_object():
    """A repeated (rows, tilt, azimuth, doy) call is a cache HIT -> same object.

    The plane is obstructed so the SVF is a computed float strictly < 1 (not the
    ``1.0`` early-return literal), so object identity genuinely proves the value
    came from the cache rather than being recomputed.
    """
    plane = _obstructed_plane()
    a = H.sky_view_factor(plane, doy=100)
    b = H.sky_view_factor(plane, doy=100)
    assert 0.0 < a < 1.0
    assert a is b  # identical object -> served from the memo


def test_cache_info_records_a_hit_on_repeat():
    """A second call with the same geometry/doy increments the lru_cache hits."""
    # Unique geometry so the first call is guaranteed a MISS regardless of any
    # entries other tests left in the shared module-level cache.
    rows = (HorizonRow(12.0, 33.0, 0.61), HorizonRow(211.0, 27.0, 0.44))
    plane = PlaneConfig(name="U", azimuth_deg=137.0, tilt_deg=64.0, wp=300.0,
                        horizon=rows)
    before = H._sky_view_factor_cached.cache_info()
    H.sky_view_factor(plane, doy=175)          # miss (populates)
    mid = H._sky_view_factor_cached.cache_info()
    H.sky_view_factor(plane, doy=175)          # hit
    after = H._sky_view_factor_cached.cache_info()
    assert mid.misses == before.misses + 1
    assert after.hits == mid.hits + 1
    assert after.misses == mid.misses  # no extra miss on the repeat


def test_different_doy_seasonal_different_value_and_object():
    """A seasonal horizon keys on doy: winter (bare) != summer (leafed).

    Different doys must NOT collide in the cache — bare winter lets more diffuse
    past the tree line than leafed summer, so the values differ (and are distinct
    objects), while each doy is itself stably memoised.
    """
    plane = _seasonal_plane()
    winter = H.sky_view_factor(plane, doy=1)     # bare
    summer = H.sky_view_factor(plane, doy=200)   # leafed
    assert winter != summer
    assert winter > summer            # bare transmits more diffuse (see test_horizon)
    assert 0.0 < summer < winter < 1.0
    # Each doy is independently memoised (its own key).
    assert H.sky_view_factor(plane, doy=1) is winter
    assert H.sky_view_factor(plane, doy=200) is summer


def test_geometry_change_is_a_new_key_no_stale_reuse():
    """Changing tilt / azimuth / rows yields a different key (structural
    invalidation), so a reconfigured plane never serves the old SVF."""
    base = _obstructed_plane(tau=0.5)
    v0 = H.sky_view_factor(base, doy=100)
    # Same rows + azimuth, different tilt -> different key -> recomputed value.
    tilted = PlaneConfig(name="S", azimuth_deg=180.0, tilt_deg=30.0, wp=430.0,
                         horizon=base.horizon)
    v1 = H.sky_view_factor(tilted, doy=100)
    assert v0 != v1
    # A denser/darker ring (different rows tuple) is also a distinct key.
    darker = _obstructed_plane(tau=0.1)
    assert H.sky_view_factor(darker, doy=100) != v0
