"""Dual-curve + learner-hook tests for core/engine.py (SPEC §5 / §9).

Plain pytest, no Home Assistant imports (SPEC §4). These tests exercise the
v0.2.0/v0.3.0 additions to ``compute_forecast``:

  * RAW vs CORRECTED dual curve, with both learner hooks neutral => corrected
    == raw BIT-EXACT (the identity invariant);
  * SLOW learner (shademap ``beam_tau`` hook) at the transposition stage:
    tau=0 on a wall bin kills the beam but NOT the diffuse floor;
  * FAST learner (``slot_factor`` hook) at the aggregation stage: a per-slot
    factor scales the corrected 15-min / hourly / daily curves consistently,
    and an intraday-style linear decay toward 1.0 over the ~6 h horizon is
    faithfully applied;
  * attribution split: per-plane ``beam_watts + diffuse_watts == watts`` and
    the raw curve is never touched by either hook;
  * robustness: a throwing ``slot_factor`` degrades to the neutral factor.

The sibling physics modules are owned by other tasks, so — exactly like
``test_engine.py`` — small analytic stand-ins are monkeypatched onto the names
the engine calls (``engine.solpos.sun_position`` etc.). Reusing the same
stand-ins keeps these learner tests decoupled from the real physics merge.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from balcony_solar_forecast.const import (
    CORRECTION_SOURCE_BOTH,
    CORRECTION_SOURCE_INTRADAY,
    CORRECTION_SOURCE_NONE,
    CORRECTION_SOURCE_SHADEMAP,
    DEFAULT_SITE,
)
from balcony_solar_forecast.core import engine
from balcony_solar_forecast.core.engine import LearnerHooks
from balcony_solar_forecast.core.types import (
    InverterGroup,
    PlaneConfig,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)

_TEST_DATE = datetime(2024, 6, 21, 0, 0, tzinfo=UTC)  # summer solstice-ish
_NOON_INDEX = 48
_MAX_ELEV = 62.0


# --------------------------------------------------------------------------
# Analytic stand-ins (shared with test_engine.py's style)
# --------------------------------------------------------------------------


def _slot_index(dt: datetime) -> int:
    delta = dt - _TEST_DATE
    return int(delta.total_seconds() // 900)


def fake_sun_position(dt_utc, lat, lon):
    """Azimuth sweeps E->S->W; elevation is a noon sine bump."""
    idx = _slot_index(dt_utc)
    day_span = 24
    frac = (idx - _NOON_INDEX) / day_span
    if abs(frac) >= 1.0:
        return (90.0 if frac < 0 else 270.0, -5.0)
    elev = _MAX_ELEV * math.cos(frac * (math.pi / 2.0))
    az = 180.0 + frac * 135.0
    return (az % 360.0, elev)


def _ang_diff(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def fake_hay_davies_poa(ghi, dni, dhi, sun_az, sun_el, plane_az, plane_tilt, albedo):
    """Directional POA stand-in with a clearly separable beam vs. diffuse.

    ``beam`` scales with how squarely the plane faces the sun and dies beyond
    90 deg off-azimuth; ``circumsolar`` is a small share of the beam;
    ``isotropic`` and ``ground`` are strictly diffuse (independent of the beam
    geometry), so the shademap-kills-beam-not-diffuse assertion has a nonzero
    diffuse floor to survive.
    """
    if sun_el <= 0.0:
        return {"beam": 0.0, "circumsolar": 0.0, "isotropic": 0.4 * dhi,
                "ground": albedo * ghi * 0.05}
    off = _ang_diff(sun_az, plane_az)
    facing = max(0.0, math.cos(math.radians(off)))
    elev_weight = 0.3 + 0.7 * max(0.0, math.sin(math.radians(sun_el)))
    beam = dni * facing * elev_weight
    circ = 0.0 if sun_el < 3.0 else 0.10 * beam
    iso = 0.4 * dhi
    ground = albedo * ghi * (1.0 - math.cos(math.radians(plane_tilt))) / 2.0
    return {"beam": beam, "circumsolar": circ, "isotropic": iso, "ground": ground}


def make_fake_horizon(wall_planes, wall_az, wall_from_el=90.0):
    def fake_interp_elevation(plane, sun_az):
        if plane.name in wall_planes and sun_az >= wall_az:
            return wall_from_el
        return 0.0

    def fake_transmittance_at(plane, sun_az, doy):
        if plane.name in wall_planes and sun_az >= wall_az:
            return 0.0
        return 1.0

    def fake_sky_view_factor(plane):
        return 0.7 if plane.name in wall_planes else 0.9

    return fake_interp_elevation, fake_transmittance_at, fake_sky_view_factor


@pytest.fixture
def patched_physics(monkeypatch):
    """Open horizon everywhere (no static wall) so learner effects are isolated."""
    monkeypatch.setattr(engine.solpos, "sun_position", fake_sun_position)
    monkeypatch.setattr(engine.transpose, "hay_davies_poa", fake_hay_davies_poa)
    interp, tau, svf = make_fake_horizon(set(), wall_az=9999.0)
    monkeypatch.setattr(engine.horizon, "interp_elevation", interp)
    monkeypatch.setattr(engine.horizon, "transmittance_at", tau)
    monkeypatch.setattr(engine.horizon, "sky_view_factor", svf)
    return monkeypatch


def _clear_sky_series(n=96, temp=20.0):
    slots = []
    for i in range(n):
        frac = (i - _NOON_INDEX) / 24.0
        if abs(frac) >= 1.0:
            ghi = dni = dhi = 0.0
        else:
            bump = math.cos(frac * (math.pi / 2.0))
            ghi = 900.0 * bump
            dni = 800.0 * bump
            dhi = 120.0 * bump
        start = _TEST_DATE + timedelta(minutes=15 * i)
        slots.append(WeatherSlot(start=start, ghi=ghi, dni=dni, dhi=dhi, temp_c=temp))
    return WeatherSeries(slots=tuple(slots))


def _two_plane_site():
    n_plane = PlaneConfig(name="N", azimuth_deg=25.0, tilt_deg=70.0, wp=430.0)
    s_plane = PlaneConfig(name="S", azimuth_deg=205.0, tilt_deg=70.0, wp=430.0)
    group = InverterGroup(name="WR", plane_names=("N", "S"), ac_limit_w=800.0)
    return SiteConfig(
        latitude=48.5, longitude=12.2, planes=(n_plane, s_plane), groups=(group,)
    )


# --------------------------------------------------------------------------
# 1. Identity invariant: neutral hooks => corrected == raw bit-exact
# --------------------------------------------------------------------------


class TestIdentityInvariant:
    def test_no_hooks_corrected_equals_raw_bit_exact(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)

        # Site total curves identical, element for element (no float slop).
        assert res.total_watts == res.raw_total_watts
        assert res.hourly_wh == res.raw_hourly_wh
        assert res.daily_kwh == res.raw_daily_kwh
        # Per-plane corrected == raw.
        for pr in res.plane_results:
            assert pr.watts == pr.raw_watts

    def test_explicit_neutral_hooks_object_is_identity(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks()
        )
        assert res.total_watts == res.raw_total_watts
        assert res.correction_source == CORRECTION_SOURCE_NONE

    def test_identity_hooks_that_echo_inputs_are_bit_exact(self, patched_physics):
        """A beam_tau that returns static_prior and a slot_factor of 1.0 must
        reproduce the raw curve exactly, proving the corrected path is a true
        superset that collapses to physics."""
        site = _two_plane_site()
        weather = _clear_sky_series()

        def echo_tau(channel, sun_az, sun_el, doy, static_prior):
            return static_prior

        def one_factor(slot_start):
            return 1.0

        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(beam_tau=echo_tau, slot_factor=one_factor,
                               correction_source=CORRECTION_SOURCE_BOTH),
        )
        assert res.total_watts == res.raw_total_watts
        assert res.hourly_wh == res.raw_hourly_wh
        for pr in res.plane_results:
            assert pr.watts == pr.raw_watts
        # correction_source is echoed verbatim (informational only).
        assert res.correction_source == CORRECTION_SOURCE_BOTH

    def test_raw_curve_matches_a_learnerless_run(self, patched_physics):
        """The RAW curve computed alongside an ACTIVE learner must equal the
        total curve of a plain learner-free run — proving the raw pass is
        genuinely untouched by the hooks."""
        site = _two_plane_site()
        weather = _clear_sky_series()

        plain = engine.compute_forecast(site, weather, now=_TEST_DATE)

        def darken(channel, sun_az, sun_el, doy, static_prior):
            return 0.3  # aggressive shade

        active = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(beam_tau=darken, slot_factor=lambda s: 1.7),
        )
        assert active.raw_total_watts == plain.total_watts
        assert active.raw_hourly_wh == plain.hourly_wh
        for pra, prp in zip(active.plane_results, plain.plane_results, strict=False):
            assert pra.raw_watts == prp.watts


# --------------------------------------------------------------------------
# 2. SLOW learner (shademap beam_tau): kills beam, keeps diffuse
# --------------------------------------------------------------------------


class TestShademapBeamGate:
    def test_wall_bin_kills_beam_but_not_diffuse(self, patched_physics):
        """A beam_tau of 0.0 for the S plane in the afternoon (its wall bin)
        must zero the beam-attributable DC but leave the diffuse floor intact.
        """
        site = _two_plane_site()
        weather = _clear_sky_series()

        def wall_tau(channel, sun_az, sun_el, doy, static_prior):
            # Occlude the S plane once the sun is in the afternoon sky.
            if channel == "S" and sun_az >= 200.0:
                return 0.0
            return static_prior

        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(beam_tau=wall_tau,
                               correction_source=CORRECTION_SOURCE_SHADEMAP),
        )
        s = next(p for p in res.plane_results if p.name == "S")

        # Find an afternoon slot where the sun is past az 200 and the plane
        # would otherwise have beam (raw beam > 0 there).
        idx = None
        for i in range(_NOON_INDEX, len(weather)):
            az, el = fake_sun_position(weather.slots[i].midpoint, 48.5, 12.2)
            if el > 0 and az >= 200.0 and s.raw_watts[i] > 0.0:
                idx = i
                break
        assert idx is not None

        # Beam-attributable DC is zeroed in the occluded slot ...
        assert s.beam_watts[idx] == pytest.approx(0.0, abs=1e-9)
        # ... but the diffuse floor survives (strictly positive).
        assert s.diffuse_watts[idx] > 0.0
        # Corrected plane power == its surviving diffuse (beam gone).
        assert s.watts[idx] == pytest.approx(s.diffuse_watts[idx])
        # And it is strictly less than the raw (which still had the beam).
        assert s.watts[idx] < s.raw_watts[idx]

    def test_wall_bin_leaves_other_plane_untouched(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()

        def wall_tau(channel, sun_az, sun_el, doy, static_prior):
            if channel == "S" and sun_az >= 200.0:
                return 0.0
            return static_prior

        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(beam_tau=wall_tau)
        )
        n = next(p for p in res.plane_results if p.name == "N")
        # The N plane is never occluded by this hook -> corrected == raw.
        assert n.watts == n.raw_watts

    def test_beam_tau_can_amplify_above_static(self, patched_physics):
        """tau > 1 (up to SHADEMAP_TAU_MAX) is representable; the learned beam
        can exceed the static-horizon beam when a bin learns it is clearer than
        the hand table assumed."""
        site = _two_plane_site()
        weather = _clear_sky_series()

        def brighten(channel, sun_az, sun_el, doy, static_prior):
            return 1.1  # SHADEMAP_TAU_MAX

        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(beam_tau=brighten)
        )
        # At noon the site total should exceed the raw (beam boosted 10%).
        assert res.total_watts[_NOON_INDEX] > res.raw_total_watts[_NOON_INDEX]

    def test_beam_tau_receives_static_prior(self, patched_physics):
        """The hook must be passed the plane's static horizon tau as
        ``static_prior`` (1.0 above the horizon line here) so a shrinkage blend
        has the prior to fall back on."""
        site = _two_plane_site()
        weather = _clear_sky_series()
        seen = []

        def spy(channel, sun_az, sun_el, doy, static_prior):
            seen.append(static_prior)
            return static_prior

        engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(beam_tau=spy)
        )
        # Open horizon everywhere -> every prior handed in is 1.0.
        assert seen  # the hook was actually consulted
        assert all(p == 1.0 for p in seen)


# --------------------------------------------------------------------------
# 3. FAST learner (slot_factor): scaling + decay over the horizon
# --------------------------------------------------------------------------


class TestSlotFactor:
    def test_constant_factor_scales_corrected_only(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(slot_factor=lambda s: 2.0,
                               correction_source=CORRECTION_SOURCE_INTRADAY),
        )
        # Corrected == 2 * raw everywhere (site total and per plane).
        for c, r in zip(res.total_watts, res.raw_total_watts, strict=False):
            assert c == pytest.approx(2.0 * r)
        for pr in res.plane_results:
            for c, r in zip(pr.watts, pr.raw_watts, strict=False):
                assert c == pytest.approx(2.0 * r)
        # Hourly / daily corrected == 2 * raw too.
        for k in res.raw_hourly_wh:
            assert res.hourly_wh[k] == pytest.approx(2.0 * res.raw_hourly_wh[k])
        assert res.correction_source == CORRECTION_SOURCE_INTRADAY

    def test_factor_reclamped_within_ac_limit(self, patched_physics):
        """The slot factor is applied to the already-clamped site power and then
        the groups are clamped AGAIN, so a factor > 1 can NEVER push the served
        corrected total above the inverter AC limit. This documents the ordering:
        clamp, factor, re-clamp (the physical ceiling bounds the served estimate
        too, not only the raw curve)."""
        n_plane = PlaneConfig(name="N", azimuth_deg=180.0, tilt_deg=30.0, wp=5000.0)
        s_plane = PlaneConfig(name="S", azimuth_deg=180.0, tilt_deg=30.0, wp=5000.0)
        group = InverterGroup(name="WR", plane_names=("N", "S"), ac_limit_w=800.0)
        site = SiteConfig(
            latitude=48.5, longitude=12.2, planes=(n_plane, s_plane), groups=(group,)
        )
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(slot_factor=lambda s: 1.5)
        )
        # Raw stays clamped at 800 W; a 1.5x up-correction on the already-clamped
        # peak is re-clamped back to the 800 W AC limit (NOT 1200 W).
        assert max(res.raw_total_watts) == pytest.approx(800.0)
        assert max(res.total_watts) == pytest.approx(800.0)
        assert all(w <= 800.0 + 1e-6 for w in res.total_watts)

    def test_intraday_linear_decay_over_horizon(self, patched_physics):
        """An intraday-style factor that starts at 1.5 at ``now`` and ramps
        linearly to 1.0 over a 6 h horizon must be applied per slot with the
        correct, monotonically shrinking multiplier — and leave slots beyond
        the horizon at exactly the raw value."""
        site = _two_plane_site()
        weather = _clear_sky_series()

        now = _TEST_DATE + timedelta(hours=6)  # 06:00, sun just up
        horizon_min = 360.0
        start_scalar = 1.5

        def decaying(slot_start):
            age = (slot_start - now).total_seconds() / 60.0
            if age < 0.0 or age >= horizon_min:
                return 1.0
            # linear decay from start_scalar (age 0) to 1.0 (age horizon).
            return start_scalar + (1.0 - start_scalar) * (age / horizon_min)

        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(slot_factor=decaying)
        )

        for i, start in enumerate(res.slot_starts):
            expected_f = decaying(start)
            raw = res.raw_total_watts[i]
            cor = res.total_watts[i]
            assert cor == pytest.approx(expected_f * raw)

        # A slot at ``now`` gets the full 1.5x; a slot 3 h later gets ~1.25x;
        # a slot beyond 6 h is untouched (factor 1.0).
        def _slot_at(dt):
            return next(i for i, s in enumerate(res.slot_starts) if s == dt)

        i0 = _slot_at(now)
        i3 = _slot_at(now + timedelta(hours=3))
        i7 = _slot_at(now + timedelta(hours=7))
        if res.raw_total_watts[i0] > 0:
            assert res.total_watts[i0] == pytest.approx(1.5 * res.raw_total_watts[i0])
        if res.raw_total_watts[i3] > 0:
            assert res.total_watts[i3] == pytest.approx(1.25 * res.raw_total_watts[i3])
        assert res.total_watts[i7] == pytest.approx(res.raw_total_watts[i7])

    def test_factor_zero_zeros_corrected_keeps_raw(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(slot_factor=lambda s: 0.0)
        )
        assert all(w == 0.0 for w in res.total_watts)
        assert sum(res.hourly_wh.values()) == pytest.approx(0.0)
        # Raw is unaffected and still produces at midday.
        assert res.raw_total_watts[_NOON_INDEX] > 0.0

    def test_throwing_slot_factor_degrades_to_neutral(self, patched_physics):
        """A slot_factor that raises must not crash the engine; that slot falls
        back to the neutral 1.0 factor (SPEC §5: never silently degrade INTO a
        crash)."""
        site = _two_plane_site()
        weather = _clear_sky_series()

        def boom(slot_start):
            raise ValueError("bad state")

        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(slot_factor=boom)
        )
        # Neutral fallback -> corrected == raw.
        assert res.total_watts == res.raw_total_watts

    def test_non_numeric_slot_factor_degrades_to_neutral(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()

        def bad(slot_start):
            return None  # not a number

        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(slot_factor=bad)
        )
        assert res.total_watts == res.raw_total_watts


# --------------------------------------------------------------------------
# 4. Attribution split (beam + diffuse == watts; conservation)
# --------------------------------------------------------------------------


class TestAttributionSplit:
    def test_beam_plus_diffuse_equals_plane_watts(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        for pr in res.plane_results:
            for i in range(len(weather)):
                assert pr.beam_watts[i] + pr.diffuse_watts[i] == pytest.approx(
                    pr.watts[i]
                )

    def test_split_holds_under_slot_factor(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(slot_factor=lambda s: 1.3)
        )
        for pr in res.plane_results:
            for i in range(len(weather)):
                assert pr.beam_watts[i] + pr.diffuse_watts[i] == pytest.approx(
                    pr.watts[i]
                )

    def test_split_holds_under_ac_clamp(self, patched_physics):
        # Oversized planes so the clamp bites; the beam/diffuse split must still
        # sum to the clamped plane watts.
        n_plane = PlaneConfig(name="N", azimuth_deg=180.0, tilt_deg=30.0, wp=5000.0)
        s_plane = PlaneConfig(name="S", azimuth_deg=180.0, tilt_deg=30.0, wp=5000.0)
        group = InverterGroup(name="WR", plane_names=("N", "S"), ac_limit_w=800.0)
        site = SiteConfig(
            latitude=48.5, longitude=12.2, planes=(n_plane, s_plane), groups=(group,)
        )
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        for pr in res.plane_results:
            for i in range(len(weather)):
                assert pr.beam_watts[i] + pr.diffuse_watts[i] == pytest.approx(
                    pr.watts[i]
                )
        # And the clamp still holds on the raw (physical) total.
        assert max(res.raw_total_watts) == pytest.approx(800.0)

    def test_kc_series_populated_and_aligned(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        for pr in res.plane_results:
            assert len(pr.kc) == len(weather)
        # kc is positive around midday (clear sky) and zero at night.
        kc = res.plane_results[0].kc
        assert kc[_NOON_INDEX] > 0.0
        assert kc[0] == 0.0

    def test_beam_and_diffuse_series_aligned(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        for pr in res.plane_results:
            assert len(pr.beam_watts) == len(weather)
            assert len(pr.diffuse_watts) == len(weather)
            assert len(pr.raw_watts) == len(weather)
            assert len(pr.beam_ref_watts) == len(weather)
            assert len(pr.diffuse_ref_watts) == len(weather)


# --------------------------------------------------------------------------
# 4b. SLOW-learner beam reference series (FIX-3): ungated, unclamped, unfactored
# --------------------------------------------------------------------------


class TestBeamRefSeries:
    def _site_wall(self, wall_planes, wall_az):
        """Build a site + monkeypatched horizon with a static wall on S."""
        return wall_planes, wall_az

    def test_beam_ref_is_ungated(self, monkeypatch):
        """Within a run, the gated beam == static_tau * beam_ref: the reference
        is the UNGATED beam that the static horizon tau attenuates to the raw
        beam (linear in tau at the run's own operating point, FIX-3)."""
        monkeypatch.setattr(engine.solpos, "sun_position", fake_sun_position)
        monkeypatch.setattr(engine.transpose, "hay_davies_poa", fake_hay_davies_poa)
        monkeypatch.setattr(engine.horizon, "sky_view_factor", lambda p: 0.9)
        weather = _clear_sky_series()
        site = _two_plane_site()

        static_tau = 0.4

        # S plane gated at static_tau whenever the sun is past az 200.
        def interp_a(plane, sun_az):
            return 90.0 if (plane.name == "S" and sun_az >= 200.0) else 0.0

        def tau_a(plane, sun_az, doy):
            return static_tau if (plane.name == "S" and sun_az >= 200.0) else 1.0

        monkeypatch.setattr(engine.horizon, "interp_elevation", interp_a)
        monkeypatch.setattr(engine.horizon, "transmittance_at", tau_a)
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        s = next(p for p in res.plane_results if p.name == "S")

        # Find an occluded afternoon slot where the plane is actually gated.
        idx = None
        for i in range(_NOON_INDEX, len(weather)):
            az, el = fake_sun_position(weather.slots[i].midpoint, 48.5, 12.2)
            if el > 0 and az >= 200.0 and s.beam_ref_watts[i] > 0.0:
                idx = i
                break
        assert idx is not None
        # The RAW (static-gated) beam == static_tau * ungated reference beam.
        assert s.beam_watts[idx] == pytest.approx(
            static_tau * s.beam_ref_watts[idx], rel=1e-6
        )
        # And the reference beam strictly exceeds the gated beam (tau < 1).
        assert s.beam_ref_watts[idx] > s.beam_watts[idx]

    def test_beam_ref_independent_of_hooks_clamp_factor(self, patched_physics):
        """beam_ref/diffuse_ref are byte-identical with and without an
        aggressive learner hook + AC clamp, and exceed the clamped raw on the
        clipped slot."""
        n_plane = PlaneConfig(name="N", azimuth_deg=180.0, tilt_deg=30.0, wp=5000.0)
        s_plane = PlaneConfig(name="S", azimuth_deg=180.0, tilt_deg=30.0, wp=5000.0)
        group = InverterGroup(name="WR", plane_names=("N", "S"), ac_limit_w=800.0)
        site = SiteConfig(
            latitude=48.5, longitude=12.2, planes=(n_plane, s_plane), groups=(group,)
        )
        weather = _clear_sky_series()

        plain = engine.compute_forecast(site, weather, now=_TEST_DATE)
        active = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(beam_tau=lambda *a: 0.0, slot_factor=lambda s: 2.0),
        )
        for pp, pa in zip(plain.plane_results, active.plane_results, strict=False):
            assert pp.beam_ref_watts == pa.beam_ref_watts
            assert pp.diffuse_ref_watts == pa.diffuse_ref_watts
        # On the clipped peak slot the reference beam exceeds the clamped raw.
        s = next(p for p in plain.plane_results if p.name == "S")
        assert s.beam_ref_watts[_NOON_INDEX] > s.raw_watts[_NOON_INDEX]


# --------------------------------------------------------------------------
# 5. Energy conservation of the dual curve
# --------------------------------------------------------------------------


class TestDualCurveEnergy:
    def test_raw_hourly_sums_to_raw_slot_energy(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        expected = sum(res.raw_total_watts) * (15.0 / 60.0)
        assert sum(res.raw_hourly_wh.values()) == pytest.approx(expected)

    def test_corrected_hourly_sums_to_corrected_slot_energy(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(slot_factor=lambda s: 1.4)
        )
        expected = sum(res.total_watts) * (15.0 / 60.0)
        assert sum(res.hourly_wh.values()) == pytest.approx(expected)

    def test_raw_daily_matches_raw_hourly(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(slot_factor=lambda s: 1.4)
        )
        assert sum(res.raw_daily_kwh.values()) == pytest.approx(
            sum(res.raw_hourly_wh.values()) / 1000.0
        )
        assert sum(res.daily_kwh.values()) == pytest.approx(
            sum(res.hourly_wh.values()) / 1000.0
        )


# --------------------------------------------------------------------------
# 6. Default operator site smoke test with active learners
# --------------------------------------------------------------------------


class TestDefaultSiteLearning:
    def test_default_site_dual_curve_runs(self, patched_physics):
        site = SiteConfig.from_dict(DEFAULT_SITE)
        weather = _clear_sky_series()

        def darken_south(channel, sun_az, sun_el, doy, static_prior):
            if channel in ("M4", "M8") and sun_az >= 200.0:
                return 0.0
            return static_prior

        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(beam_tau=darken_south, slot_factor=lambda s: 1.1,
                               correction_source=CORRECTION_SOURCE_BOTH),
        )
        assert len(res.plane_results) == 8
        # Attribution split holds for every plane and slot.
        for pr in res.plane_results:
            for i in range(len(weather)):
                assert pr.beam_watts[i] + pr.diffuse_watts[i] == pytest.approx(
                    pr.watts[i]
                )
        # Raw curve is a valid clamped physics curve (every WR pair <= 800 W).
        by_raw = {p.name: p.raw_watts for p in res.plane_results}
        for a, b in [("M1", "M2"), ("M3", "M4"), ("M5", "M6"), ("M7", "M8")]:
            for i in range(len(weather)):
                assert by_raw[a][i] + by_raw[b][i] <= 800.0 + 1e-6
        assert res.correction_source == CORRECTION_SOURCE_BOTH

    def test_default_site_neutral_is_identity(self, patched_physics):
        site = SiteConfig.from_dict(DEFAULT_SITE)
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert res.total_watts == res.raw_total_watts
        for pr in res.plane_results:
            assert pr.watts == pr.raw_watts
