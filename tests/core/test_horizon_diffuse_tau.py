"""Tests for the per-row diffuse override ``diffuse_tau`` (ADR §3, D2, v0.22).

Covers (ADR §3.8 unit part):
  - SVF golden: a genuine wall sector filling az 195..360 at el 90 (tau 0) with
    ``diffuse_tau 0.5`` lifts the sky-view factor, matches an independent
    brute-force quadrature, and hits the closed golden pair 0.423 -> 0.712 for
    this M4 tilt/azimuth; it obeys the exact wall blend identity
    SVF(rho) == rho + (1 - rho) * SVF0 because the wall is the ENTIRE blocked
    dome here.  NB: the live M4 diagnostic 0.288 -> ~0.64 is NOT reproduced by
    this wall-only synthetic — the real M4 table also carries trees/screens, so
    diffuse_tau lifts only the wall's share of that dome and the linear blend
    identity does not apply to the live figure (only to a wall-only dome);
  - ``diffuse_tau=None`` is BIT-IDENTICAL to the reconstructed pre-D2 diffuse
    path (the SVF resolved through the beam ``_row_tau_at`` resolver, which knows
    nothing of ``diffuse_tau``), not merely to a second identical call;
  - ``diffuse_tau=0`` is BIT-IDENTICAL to the plain opaque wall (the wall floor);
  - a semi-transparent (``tau_points``) tree row keeps its beam profile in the
    band integral while a walled row's ``diffuse_tau`` overrides only the diffuse;
  - ENGINE: the tau-independent beam POA decomposition (beam / circ /
    beam_poa_ungated / static_tau) is byte-identical with/without ``diffuse_tau``;
    diffuse_ref_watts (the SLOW-learner floor label) carries the new lift, while
    beam_ref_watts moves only through the shared Ross cell-temperature derate
    (total-POA coupling), bounded to a sub-percent second-order shift.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from balcony_solar_forecast.core import engine
from balcony_solar_forecast.core import horizon as H
from balcony_solar_forecast.core.types import (
    HorizonRow,
    InverterGroup,
    PlaneConfig,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)

# The ADR §3.5 wall geometry: an opaque house wall filling az 195..360 at el 90.
_WALL_LO = 195.0
_WALL_HI = 360.0

# Golden SVF pair for the wall-only dome at az 205 / tilt 70 (independently
# reproduced by the brute-force quadrature below): the opaque wall floors the
# sky-view at 0.423, and diffuse_tau 0.5 lifts it to 0.712 via the blend
# identity 0.5 + 0.5 * 0.423.  (These are NOT the live M4 0.288/0.64 numbers —
# see the module docstring: the live table is not a wall-only dome.)
_WALL_SVF_OPAQUE = 0.423
_WALL_SVF_HALF = 0.712


def _wall_plane(diffuse_tau=None, *, az=205.0, tilt=70.0):
    # A genuine wall filling az 195..360 at el 90 with an open eastern dome
    # elsewhere.  Terminator rows pin the edges so ``_sorted_rows`` cannot wrap
    # the az-360 row onto the az-0 key and silently move the wall onto az 0..195
    # (which is what a bare 0 / 195 / 360 table does): az 194.99 keeps the dome
    # open right up to 195, az 359.99 keeps the wall opaque right up to 360.
    rows = (
        HorizonRow(0.0, 0.0, 1.0),  # open eastern dome
        HorizonRow(_WALL_LO - 0.01, 0.0, 1.0),  # dome open up to the wall edge
        HorizonRow(_WALL_LO, 90.0, 0.0, diffuse_tau=diffuse_tau),
        HorizonRow(_WALL_HI - 0.01, 90.0, 0.0, diffuse_tau=diffuse_tau),
    )
    return PlaneConfig(
        name="M4", azimuth_deg=az, tilt_deg=tilt, wp=430.0, horizon=rows
    )


def _svf_brute(plane, doy=None, n_az=720, n_el=900):
    """Independent fine (az, el) Riemann quadrature of the cosine-weighted SVF.

    Resolves the wedge tau through :func:`H._row_diffuse_tau_at` exactly as the
    closed-form path does, so it validates the diffuse override end to end
    against a method that shares no code with the band/column closed forms.
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
                    rows, az_deg,
                    lambda r, _e=el_deg: H._row_diffuse_tau_at(r, _e, doy),
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
    return integral(True) / f_flat


# ---------------------------------------------------------------------------
# SVF golden + brute-force
# ---------------------------------------------------------------------------


def test_diffuse_tau_lifts_svf_matches_brute_force():
    base = H.sky_view_factor(_wall_plane(None))
    lifted = H.sky_view_factor(_wall_plane(0.5))
    # A bright wall (rho 0.5) roughly halves the blocked dome's darkness.
    assert base < lifted < 1.0
    # Golden closed values for this az-195..360 wall at az 205 / tilt 70.
    assert base == pytest.approx(_WALL_SVF_OPAQUE, abs=0.01)
    assert lifted == pytest.approx(_WALL_SVF_HALF, abs=0.01)
    # THE golden reference: agreement with the independent fine quadrature.
    assert lifted == pytest.approx(_svf_brute(_wall_plane(0.5)), rel=0.005)


def test_diffuse_tau_wall_blend_identity():
    # For a pure el-90 wall every blocked column contributes rho * open instead
    # of 0, so SVF is exactly linear in rho: SVF(rho) == rho + (1 - rho) * SVF0.
    # This holds because the wall IS the entire blocked dome here; it does NOT
    # apply to the live M4 table (trees/screens block the rest of the dome, where
    # diffuse_tau does not act), so the live 0.288 -> ~0.64 is a different figure.
    base = H.sky_view_factor(_wall_plane(None))
    for rho in (0.0, 0.3, 0.5, 0.8):
        got = H.sky_view_factor(_wall_plane(rho))
        assert got == pytest.approx(rho + (1.0 - rho) * base, abs=2e-3)


def test_diffuse_tau_none_bit_identical_to_pre_d2_path():
    # None must reproduce the PRE-D2 diffuse path bit-for-bit — not merely a
    # second identical call (tautological) or a dataclass-equal lru_cache hit.
    # Reconstruct the pre-D2 resolution by pointing the module's diffuse-tau
    # resolver back at the beam resolver ``_row_tau_at`` (which has no knowledge
    # of ``diffuse_tau``) and recomputing the SVF from scratch: for a table with
    # no diffuse_tau set the two must be identical, and a regression in the None
    # branch of ``_row_diffuse_tau_at`` would diverge here.
    plane = _wall_plane(None)
    real = H.sky_view_factor(plane)

    orig = H._row_diffuse_tau_at
    H._sky_view_factor_cached.cache_clear()
    try:
        H._row_diffuse_tau_at = H._row_tau_at  # the genuine pre-D2 resolver
        ref = H.sky_view_factor(plane)
    finally:
        H._row_diffuse_tau_at = orig
        H._sky_view_factor_cached.cache_clear()

    assert real == ref
    # Row-level: the None branch delegates verbatim to the beam resolver.
    wall_row = _wall_plane(None).horizon[2]
    assert H._row_diffuse_tau_at(wall_row, 30.0, 200) == H._row_tau_at(
        wall_row, 30.0, 200
    )


def test_diffuse_tau_zero_bit_identical_to_opaque_wall():
    # diffuse_tau 0 == an opaque wall (rho 0): the wedge stays black.
    assert H.sky_view_factor(_wall_plane(0.0)) == H.sky_view_factor(
        _wall_plane(None)
    )


def test_diffuse_tau_overrides_only_diffuse_not_beam_profile():
    # A semi-transparent tree row (tau_points) carrying diffuse_tau uses the
    # profile for the BEAM and diffuse_tau for the SVF wedge; the beam transmit-
    # tance is unaffected by diffuse_tau.
    prof = ((4.5, 0.0), (6.5, 0.45), (9.5, 1.0))
    row_no = HorizonRow(80.0, 10.0, 0.0, tau_points=prof)
    row_dt = HorizonRow(80.0, 10.0, 0.0, tau_points=prof, diffuse_tau=0.5)
    p_no = PlaneConfig(name="P", azimuth_deg=90.0, tilt_deg=70.0, wp=400.0,
                       horizon=(row_no,))
    p_dt = PlaneConfig(name="P", azimuth_deg=90.0, tilt_deg=70.0, wp=400.0,
                       horizon=(row_dt,))
    # Beam: identical for every sun elevation (diffuse_tau never touches beam).
    for el in (3.0, 6.5, 9.0):
        assert H.transmittance_at(p_no, 80.0, 200, sun_el=el) == (
            H.transmittance_at(p_dt, 80.0, 200, sun_el=el)
        )
    # Diffuse SVF: the override lifts the floor (0.5 > the profile mean here).
    assert H.sky_view_factor(p_dt) > H.sky_view_factor(p_no)


def test_diffuse_tau_band_matches_brute_force_mixed_geometry():
    # A tree profile (forces the band integral) PLUS a walled diffuse override.
    prof = ((4.5, 0.0), (6.5, 0.45), (9.5, 1.0))
    plane = PlaneConfig(
        name="M4", azimuth_deg=205.0, tilt_deg=70.0, wp=430.0,
        horizon=(
            HorizonRow(60.0, 10.0, 0.0, tau_points=prof),
            HorizonRow(89.0, 10.0, 0.0, tau_points=prof),
            HorizonRow(_WALL_LO, 90.0, 0.0, diffuse_tau=0.5),
            HorizonRow(_WALL_HI, 90.0, 0.0, diffuse_tau=0.5),
        ),
    )
    svf = H.sky_view_factor(plane, doy=200)
    assert svf == pytest.approx(_svf_brute(plane, doy=200), rel=0.005)


# ---------------------------------------------------------------------------
# Engine: beam untouched, diffuse floor lifted
# ---------------------------------------------------------------------------

_BASE = datetime(2024, 6, 21, 3, 0, tzinfo=UTC)


def _weather(n: int = 40) -> WeatherSeries:
    slots = []
    for i in range(n):
        frac = (i - n / 2) / (n / 2)
        bump = max(0.0, math.cos(frac * (math.pi / 2.0)))
        slots.append(
            WeatherSlot(
                start=_BASE + timedelta(minutes=15 * i),
                ghi=900.0 * bump, dni=780.0 * bump, dhi=140.0 * bump,
                temp_c=20.0,
            )
        )
    return WeatherSeries(slots=tuple(slots))


def _site(diffuse_tau=None) -> SiteConfig:
    plane = _wall_plane(diffuse_tau)
    return SiteConfig(
        latitude=48.0, longitude=11.0, planes=(plane,),
        groups=(InverterGroup(name="inv", plane_names=("M4",), ac_limit_w=800.0),),
    )


def test_engine_beam_poa_byte_identical_diffuse_floor_lifted():
    # The TRUE beam invariant (ADR §3.8 "Beam byte-identisch"): the diffuse
    # override touches ONLY the SVF, so the tau-independent beam POA decomposition
    # (beam / circ / beam_poa_ungated / static_tau) is byte-for-byte identical.
    # (The beam DC then couples to the diffuse floor ONLY through the shared Ross
    # cell-temperature derate, which is driven by TOTAL POA — see the DC test
    # below; the beam POA itself never moves.)
    from balcony_solar_forecast.core import solpos
    from balcony_solar_forecast.core.engine import (
        BEAM_GAIN_DEFAULT,
        _plane_poa_components,
        _slot_albedo,
    )

    p_no = _wall_plane(None)
    p_dt = _wall_plane(0.5)
    lat, lon = 48.0, 11.0
    moved = False
    for slot in _weather():
        mid = slot.midpoint
        sun_az, sun_el = solpos.sun_position(mid, lat, lon)
        doy = mid.timetuple().tm_yday
        albedo = _slot_albedo(slot)
        c_no = _plane_poa_components(
            p_no, H.sky_view_factor(p_no, doy=doy), slot, sun_az, sun_el,
            albedo, doy, BEAM_GAIN_DEFAULT,
        )
        c_dt = _plane_poa_components(
            p_dt, H.sky_view_factor(p_dt, doy=doy), slot, sun_az, sun_el,
            albedo, doy, BEAM_GAIN_DEFAULT,
        )
        assert c_dt.beam == c_no.beam
        assert c_dt.circ == c_no.circ
        assert c_dt.beam_poa_ungated == c_no.beam_poa_ungated
        assert c_dt.static_tau == c_no.static_tau
        # The diffuse floor is what moves (>= always; strictly on lit wall slots).
        assert c_dt.diffuse_poa >= c_no.diffuse_poa - 1e-12
        if c_dt.diffuse_poa > c_no.diffuse_poa:
            moved = True
    assert moved  # the override actually lifted the diffuse floor somewhere


def test_engine_diffuse_floor_lifted_beam_dc_only_ross_coupled():
    weather = _weather()
    res_no = engine.compute_forecast(_site(None), weather, now=_BASE, tz=UTC)
    res_dt = engine.compute_forecast(_site(0.5), weather, now=_BASE, tz=UTC)
    pr_no = res_no.plane_results[0]
    pr_dt = res_dt.plane_results[0]

    # The diffuse floor reference (the SLOW-learner label denominator's floor)
    # carries the lift: strictly more diffuse energy, never a drop.
    assert sum(pr_dt.diffuse_ref_watts) > sum(pr_no.diffuse_ref_watts)
    assert any(d > n for d, n in zip(pr_dt.diffuse_ref_watts,
                                     pr_no.diffuse_ref_watts, strict=True))
    assert all(d >= n - 1e-9 for d, n in zip(pr_dt.diffuse_ref_watts,
                                             pr_no.diffuse_ref_watts, strict=True))

    # The beam reference moves ONLY through the shared Ross derate (total-POA
    # cell heating): the beam POA is byte-identical (test above), so the beam DC
    # shift is bounded to a sub-percent second-order coupling, never the diffuse
    # override entering the beam gate.
    for d, n in zip(pr_dt.beam_ref_watts, pr_no.beam_ref_watts, strict=True):
        assert d == pytest.approx(n, rel=5e-3)
