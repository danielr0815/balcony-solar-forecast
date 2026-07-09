"""Tests for the Haurwitz clear-sky model and clear-sky index.

Pure pytest, no Home Assistant imports (SPEC §4). Owner: irradiance.
"""

from __future__ import annotations

import math

import pytest
from balcony_solar_forecast.core.clearsky import (
    clear_sky_index,
    haurwitz_ghi,
)

# --- Haurwitz GHI ---------------------------------------------------------


def test_haurwitz_zero_at_and_below_horizon():
    assert haurwitz_ghi(0.0) == 0.0
    assert haurwitz_ghi(-0.001) == 0.0
    assert haurwitz_ghi(-30.0) == 0.0


def test_haurwitz_peak_at_zenith():
    # GHI = 1098 * 1 * exp(-0.059) ~= 1035.2 W/m^2 at the sub-solar point.
    expected = 1098.0 * math.exp(-0.059)
    assert haurwitz_ghi(90.0) == pytest.approx(expected, rel=1e-9)
    assert 1000.0 < haurwitz_ghi(90.0) < 1050.0


def test_haurwitz_monotonic_increasing_with_elevation():
    prev = -1.0
    for el in range(1, 91):
        val = haurwitz_ghi(float(el))
        assert val > prev, f"not monotonic at {el} deg"
        prev = val


def test_haurwitz_always_nonnegative():
    for el in [0.1, 1, 3, 5, 15, 30, 45, 60, 75, 90]:
        assert haurwitz_ghi(float(el)) >= 0.0


def test_haurwitz_low_sun_no_explosion():
    # exp(-0.059/cos_z) stays bounded and small at grazing incidence; must
    # never blow up or go negative.
    for el in [0.01, 0.1, 0.5, 1.0, 2.0, 3.0]:
        val = haurwitz_ghi(el)
        assert 0.0 <= val < haurwitz_ghi(90.0)
        assert math.isfinite(val)


def test_haurwitz_known_value_30deg():
    # cos_z = sin(30) = 0.5 -> 1098*0.5*exp(-0.118) = 487.9 W/m^2.
    assert haurwitz_ghi(30.0) == pytest.approx(1098.0 * 0.5 * math.exp(-0.118), rel=1e-9)


# --- clear-sky index ------------------------------------------------------


def test_kc_zero_when_reference_zero():
    assert clear_sky_index(800.0, 0.0) == 0.0
    assert clear_sky_index(800.0, -10.0) == 0.0


def test_kc_ratio_matches_reference():
    ref = haurwitz_ghi(45.0)
    assert clear_sky_index(ref, 45.0) == pytest.approx(1.0, rel=1e-9)
    assert clear_sky_index(ref * 0.5, 45.0) == pytest.approx(0.5, rel=1e-9)


def test_kc_nonnegative_and_finite():
    for el in [1, 5, 20, 45, 70, 90]:
        for ghi in [0.0, 50.0, 400.0, 1200.0]:
            k = clear_sky_index(ghi, float(el))
            assert k >= 0.0
            assert math.isfinite(k)


def test_kc_overcast_below_one_clear_around_one():
    # A heavily overcast slot: low GHI vs. clear reference -> k_c well below 1.
    k_overcast = clear_sky_index(80.0, 50.0)
    assert 0.0 < k_overcast < 0.5
    # A clear slot at the same elevation sits near 1 (Haurwitz is coarse, so
    # allow a broad band).
    k_clear = clear_sky_index(haurwitz_ghi(50.0) * 0.95, 50.0)
    assert 0.8 < k_clear < 1.1
