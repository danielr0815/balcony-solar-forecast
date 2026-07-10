"""Tests for Hay-Davies plane-of-array transposition.

Pure pytest, no Home Assistant imports (SPEC §4). Owner: irradiance.

Covers the SPEC §4 physics musts and silent-error traps:
  - all four components non-negative;
  - vertical-plane cases (70/80 deg planes are near-vertical);
  - Rb cap <= 10 and circumsolar = 0 below 3 deg (low-sun no-explosion);
  - ground term albedo*GHI*(1-cos tilt)/2 and the snow-albedo switch;
  - azimuth-sign trap on the 25 deg planes (behind-plane -> no beam);
  - energy-conservation bounds (POA diffuse+ground never exceeds a sane
    envelope of the horizontal inputs).
"""

from __future__ import annotations

import math

import pytest
from balcony_solar_forecast.const import (
    ALBEDO_DEFAULT,
    ALBEDO_SNOW,
    LOW_SUN_CUTOFF_DEG,
    RB_CAP,
)
from balcony_solar_forecast.core.transpose import (
    _cos_incidence,
    hay_davies_poa,
)

COMPONENTS = ("beam", "circumsolar", "isotropic", "ground")


def _poa(**kw) -> dict[str, float]:
    """Call hay_davies_poa with sensible clear-sky defaults, overridable."""
    args = dict(
        ghi=600.0,
        dni=800.0,
        dhi=150.0,
        sun_az=180.0,
        sun_el=45.0,
        plane_az=180.0,
        plane_tilt=70.0,
        albedo=ALBEDO_DEFAULT,
    )
    args.update(kw)
    return hay_davies_poa(**args)


# --- cos(incidence) helper ------------------------------------------------


def test_cos_incidence_vertical_south_sun_south():
    # Vertical S plane, sun due S at el 45: cos(theta) = cos(45) = 0.707.
    assert _cos_incidence(180.0, 45.0, 180.0, 90.0) == pytest.approx(
        math.cos(math.radians(45.0)), rel=1e-9
    )


def test_cos_incidence_horizontal_plane_equals_sin_elevation():
    # Flat plane (tilt 0): cos(theta) = sin(elevation) for any azimuth.
    for el in [10.0, 33.0, 70.0]:
        for az in [0.0, 90.0, 200.0, 359.0]:
            assert _cos_incidence(az, el, 123.0, 0.0) == pytest.approx(
                math.sin(math.radians(el)), rel=1e-9
            )


def test_cos_incidence_delta_azimuth_is_wraparound_safe():
    # Only the azimuth *difference* enters, and cos is 2pi-periodic, so the
    # internal 0=N frame needs no explicit wrap. sun_az 5, plane 355 -> delta
    # 10 deg, same as sun 15 / plane 5.
    a = _cos_incidence(5.0, 40.0, 355.0, 70.0)
    b = _cos_incidence(15.0, 40.0, 5.0, 70.0)
    assert a == pytest.approx(b, rel=1e-12)


# --- component sanity: all >= 0 ------------------------------------------


@pytest.mark.parametrize("sun_el", [-5.0, 0.0, 0.5, 2.9, 3.1, 20.0, 60.0, 89.0])
@pytest.mark.parametrize("plane_az", [25.0, 115.0, 205.0])
@pytest.mark.parametrize("plane_tilt", [70.0, 80.0])
def test_all_components_nonnegative(sun_el, plane_az, plane_tilt):
    c = hay_davies_poa(
        ghi=500.0,
        dni=700.0,
        dhi=180.0,
        sun_az=150.0,
        sun_el=sun_el,
        plane_az=plane_az,
        plane_tilt=plane_tilt,
        albedo=ALBEDO_DEFAULT,
    )
    assert set(c) == set(COMPONENTS)
    for k in COMPONENTS:
        assert c[k] >= 0.0, f"{k} negative"
        assert math.isfinite(c[k])


def test_zero_inputs_give_zero_components():
    c = hay_davies_poa(0.0, 0.0, 0.0, 180.0, 45.0, 180.0, 70.0, ALBEDO_DEFAULT)
    for k in COMPONENTS:
        assert c[k] == 0.0


def test_negative_inputs_clamped_not_propagated():
    # Defensive: bad upstream data must not yield negative irradiance.
    c = hay_davies_poa(-10.0, -20.0, -5.0, 180.0, 45.0, 180.0, 70.0, -0.3)
    for k in COMPONENTS:
        assert c[k] >= 0.0


# --- sun below / at horizon ----------------------------------------------


def test_below_horizon_only_diffuse_and_ground():
    c = hay_davies_poa(50.0, 0.0, 50.0, 180.0, -3.0, 180.0, 70.0, ALBEDO_DEFAULT)
    assert c["beam"] == 0.0
    assert c["circumsolar"] == 0.0
    # Isotropic diffuse and ground reflection persist at/after sunset.
    assert c["isotropic"] > 0.0
    assert c["ground"] > 0.0


def test_below_horizon_isotropic_is_full_dhi_projection():
    # With no sun, Ai is still DNI/E0n; here DNI=0 so isotropic = DHI*(1+cos)/2.
    tilt = 70.0
    dhi = 120.0
    c = hay_davies_poa(0.0, 0.0, dhi, 180.0, -1.0, 180.0, tilt, 0.0)
    expected = dhi * (1.0 + math.cos(math.radians(tilt))) / 2.0
    assert c["isotropic"] == pytest.approx(expected, rel=1e-12)


# --- vertical-plane cases -------------------------------------------------


def test_vertical_plane_ground_and_sky_factors_split_dhi():
    # For tilt 90: sky factor (1+cos90)/2 = 0.5, ground factor (1-cos90)/2=0.5.
    dhi, ghi, alb = 200.0, 500.0, 0.2
    # Put the sun behind the plane so beam/circumsolar drop out and we can
    # read the pure diffuse split.
    c = hay_davies_poa(ghi, 0.0, dhi, 0.0, 30.0, 180.0, 90.0, alb)
    assert c["isotropic"] == pytest.approx(dhi * 0.5, rel=1e-9)
    assert c["ground"] == pytest.approx(alb * ghi * 0.5, rel=1e-9)


def test_vertical_south_plane_beam_matches_dni_cos_theta():
    dni = 850.0
    c = hay_davies_poa(600.0, dni, 100.0, 180.0, 40.0, 180.0, 90.0, 0.2)
    expected_beam = dni * math.cos(math.radians(40.0))  # cos(theta)=cos(el)
    assert c["beam"] == pytest.approx(expected_beam, rel=1e-9)


# --- azimuth-sign trap on the 25 deg planes (SPEC Anhang A) ---------------


def test_azimuth_sign_trap_behind_plane_no_beam():
    # A 25 deg (NNE) plane cannot see a due-south sun: beam & circumsolar 0,
    # only diffuse + ground remain. A sign error on the 25 deg azimuth would
    # wrongly light this plane up.
    c = hay_davies_poa(600.0, 800.0, 150.0, 180.0, 45.0, 25.0, 70.0, 0.2)
    assert c["beam"] == 0.0
    assert c["circumsolar"] == 0.0
    assert c["isotropic"] > 0.0


def test_azimuth_25_plane_lit_by_morning_ene_sun():
    # The 25 deg plane *should* be lit by an ENE morning sun (az ~70).
    c = hay_davies_poa(300.0, 600.0, 120.0, 70.0, 20.0, 25.0, 70.0, 0.2)
    assert c["beam"] > 0.0


def test_205_plane_not_lit_by_north_sun():
    # SSW plane (205) vs. a due-north sun (az 0): behind the plane, no beam.
    c = hay_davies_poa(400.0, 700.0, 130.0, 0.0, 30.0, 205.0, 70.0, 0.2)
    assert c["beam"] == 0.0


# --- Rb cap & low-sun no-explosion (property test) -----------------------


def test_low_sun_circumsolar_zeroed_below_cutoff():
    for el in [0.1, 1.0, 2.0, LOW_SUN_CUTOFF_DEG - 0.001]:
        c = hay_davies_poa(80.0, 400.0, 90.0, 180.0, el, 180.0, 70.0, 0.2)
        assert c["circumsolar"] == 0.0


def test_circumsolar_active_at_and_above_cutoff():
    c = hay_davies_poa(200.0, 500.0, 120.0, 180.0, LOW_SUN_CUTOFF_DEG + 0.5, 180.0, 70.0, 0.2)
    assert c["circumsolar"] > 0.0


def test_rb_cap_bounds_circumsolar():
    # At low elevation cos(zenith) is tiny; without the cap circumsolar would
    # explode. With Rb <= RB_CAP, circumsolar <= DHI * Ai * RB_CAP.
    dni, dhi = 900.0, 300.0
    el = LOW_SUN_CUTOFF_DEG + 0.01  # just above cutoff, worst case for Rb
    c = hay_davies_poa(30.0, dni, dhi, 180.0, el, 180.0, 90.0, 0.2)
    ai = dni / 1361.0
    assert c["circumsolar"] <= dhi * ai * RB_CAP + 1e-6


def test_low_sun_no_explosion_property():
    # Property: across a grid of low-sun geometries with extreme (even
    # non-physical) inputs, every component stays finite and bounded by its
    # closed-form cap -- no 1/cos(zenith) blow-up leaks through. The circumsolar
    # cap is DHI*Ai*RB_CAP; beam <= DNI; isotropic <= DHI; ground <= albedo*GHI.
    dni = dhi = 1000.0
    ghi = 50.0
    ai = dni / 1361.0
    circ_cap = dhi * ai * RB_CAP
    for el in [0.01, 0.1, 0.5, 1.0, 2.0, 3.0, 4.0, 6.0]:
        for az_delta in range(0, 360, 15):
            c = hay_davies_poa(
                ghi=ghi,
                dni=dni,
                dhi=dhi,
                sun_az=float(az_delta),
                sun_el=el,
                plane_az=0.0,
                plane_tilt=90.0,
                albedo=ALBEDO_DEFAULT,
            )
            for k in COMPONENTS:
                assert math.isfinite(c[k])
                assert c[k] >= 0.0
            assert c["beam"] <= dni + 1e-6
            assert c["circumsolar"] <= circ_cap + 1e-6
            assert c["isotropic"] <= dhi + 1e-6
            assert c["ground"] <= ALBEDO_DEFAULT * ghi + 1e-6


# --- ground term & snow-albedo switch ------------------------------------


def test_ground_term_formula():
    ghi, tilt, alb = 700.0, 70.0, ALBEDO_DEFAULT
    c = hay_davies_poa(ghi, 800.0, 150.0, 180.0, 45.0, 180.0, tilt, alb)
    expected = alb * ghi * (1.0 - math.cos(math.radians(tilt))) / 2.0
    assert c["ground"] == pytest.approx(expected, rel=1e-9)


def test_snow_albedo_raises_ground_term():
    base = _poa(albedo=ALBEDO_DEFAULT)
    snow = _poa(albedo=ALBEDO_SNOW)
    assert snow["ground"] > base["ground"]
    # Snow albedo is 2.5x default -> ground term scales linearly.
    assert snow["ground"] == pytest.approx(
        base["ground"] * (ALBEDO_SNOW / ALBEDO_DEFAULT), rel=1e-9
    )


def test_zero_albedo_zero_ground():
    c = _poa(albedo=0.0)
    assert c["ground"] == 0.0


def test_ground_grows_with_tilt():
    # Steeper plane sees more ground: (1-cos tilt)/2 increases with tilt.
    flat = _poa(plane_tilt=30.0)
    steep = _poa(plane_tilt=80.0)
    assert steep["ground"] > flat["ground"]


# --- energy-conservation / bounds ----------------------------------------


def test_isotropic_bounded_by_dhi():
    # Isotropic on any plane <= DHI (the sky-view factor (1+cos)/2 <= 1 and
    # (1-Ai) <= 1).
    for tilt in [0.0, 45.0, 70.0, 90.0]:
        c = hay_davies_poa(600.0, 800.0, 200.0, 180.0, 50.0, 180.0, tilt, 0.2)
        assert c["isotropic"] <= 200.0 + 1e-9


def test_beam_bounded_by_dni():
    # beam = DNI*cos(theta) <= DNI since cos(theta) in [0,1].
    for az in [25.0, 115.0, 205.0, 180.0]:
        c = hay_davies_poa(600.0, 900.0, 150.0, 180.0, 55.0, az, 70.0, 0.2)
        assert c["beam"] <= 900.0 + 1e-9


def test_diffuse_split_conserves_dhi_when_low_sun():
    # Below the cutoff circumsolar is 0 AND Ai is forced to 0 first, so the
    # distrusted circumsolar share collapses into the isotropic dome:
    # isotropic = DHI*(1+cos tilt)/2 (the full DHI projection, no (1-Ai) leak).
    dhi = 180.0
    tilt = 70.0
    c = hay_davies_poa(40.0, 300.0, dhi, 180.0, 2.0, 180.0, tilt, 0.2)
    assert c["circumsolar"] == 0.0
    expected_iso = dhi * (1.0 + math.cos(math.radians(tilt))) / 2.0
    # Energy-conserving: no dhi*Ai*tilt_factor of real diffuse is dropped.
    assert c["isotropic"] == pytest.approx(expected_iso, rel=1e-12)
    assert c["isotropic"] <= dhi


def test_flat_plane_poa_matches_ghi_scale():
    # A horizontal plane (tilt 0) under clear sky: beam+circumsolar+isotropic
    # should reconstruct roughly GHI (ground term is 0 at tilt 0). This is the
    # interval-mean-vs-instant sanity check for a clear morning slot.
    ghi, dni, dhi, el = 500.0, 780.0, 90.0, 40.0
    # For a consistent clear-sky slot GHI ~= DNI*sin(el) + DHI.
    ghi = dni * math.sin(math.radians(el)) + dhi
    c = hay_davies_poa(ghi, dni, dhi, 150.0, el, 0.0, 0.0, 0.2)
    total = c["beam"] + c["circumsolar"] + c["isotropic"] + c["ground"]
    assert c["ground"] == 0.0  # (1-cos0)/2 = 0
    assert total == pytest.approx(ghi, rel=0.02)


def test_total_poa_within_closed_form_envelope():
    # Total POA is bounded by the sum of each component's closed-form cap:
    #   beam <= DNI, circumsolar <= DHI*Ai*RB_CAP, isotropic <= DHI,
    #   ground <= albedo*GHI. Guards against runaway Rb / double-counting while
    #   still allowing the real steep-tilt gains (POA can exceed GHI).
    ghi, dni, dhi, alb = 700.0, 850.0, 160.0, ALBEDO_SNOW
    ai = dni / 1361.0
    cap = dni + dhi * ai * RB_CAP + dhi + alb * ghi
    for tilt in [70.0, 80.0, 90.0]:
        for el in [5.0, 20.0, 45.0, 70.0]:
            c = hay_davies_poa(ghi, dni, dhi, 180.0, el, 180.0, tilt, alb)
            total = sum(c.values())
            assert math.isfinite(total)
            assert 0.0 <= total <= cap + 1e-6

    # Sanity: at a mid elevation, well away from the Rb cap, the actual total
    # stays physically reasonable on a steep snow-lit plane. The steep-tilt +
    # snow-albedo (0.5) gain legitimately pushes POA above GHI; bound at 1.6x.
    c = hay_davies_poa(ghi, dni, dhi, 180.0, 45.0, 180.0, 70.0, alb)
    assert sum(c.values()) < 1.6 * ghi
