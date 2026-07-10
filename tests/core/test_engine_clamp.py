"""Re-clamp tests for core/engine.py: learner corrections + quantile bands
never exceed the configured inverter AC limits (FIX A).

Plain pytest, no Home Assistant imports (SPEC §4). The fast-learner slot factor
is applied to the ALREADY-clamped per-plane watts and the groups are clamped
AGAIN afterwards, so an up-correction (factor > 1) or a P90 band factor > 1 can
never lift the served curve above what the inverters can physically deliver.

These reuse the analytic physics stand-ins + fixtures from ``test_engine_learning``
(open horizon everywhere, a clear-sky two-plane day) so the re-clamp is tested
against the same deterministic pipeline as the other engine tests.
"""

from __future__ import annotations

import pytest
from balcony_solar_forecast.core import engine
from balcony_solar_forecast.core.engine import LearnerHooks
from balcony_solar_forecast.core.types import (
    InverterGroup,
    PlaneConfig,
    QuantileBands,
    SiteConfig,
)

# Reuse the exact analytic stand-ins + fixtures the learner tests use.
from .test_engine_learning import (  # noqa: F401  (patched_physics is a fixture)
    _NOON_INDEX,
    _TEST_DATE,
    _clear_sky_series,
    _two_plane_site,
    patched_physics,
)

_SLOT_HOURS = 0.25


def _sun_facing_planes(wp=5000.0):
    """Two identical planes pointed straight at the analytic noon sun (az 180).

    At noon each plane's unclamped DC dwarfs any sane AC limit, so a group over
    them clamps hard — the setup for the re-clamp boundary tests.
    """
    p1 = PlaneConfig(name="P1", azimuth_deg=180.0, tilt_deg=30.0, wp=wp)
    p2 = PlaneConfig(name="P2", azimuth_deg=180.0, tilt_deg=30.0, wp=wp)
    return p1, p2


# ---------------------------------------------------------------------------
# 1. factor > 1 AT the clamp boundary: re-clamp holds the group limits
# ---------------------------------------------------------------------------


class TestFactorAtBoundary:
    def test_reclamp_caps_corrected_at_sum_of_group_limits(self, patched_physics):
        """Two saturated groups (limits 800 + 500) with a 1.2x slot factor: the
        corrected site total is re-clamped to the SUM of the group limits
        (1300 W), NOT the pre-clamp product 1300 * 1.2 = 1560 W."""
        p1, p2 = _sun_facing_planes()
        g1 = InverterGroup(name="G1", plane_names=("P1",), ac_limit_w=800.0)
        g2 = InverterGroup(name="G2", plane_names=("P2",), ac_limit_w=500.0)
        site = SiteConfig(
            latitude=48.5, longitude=12.2, planes=(p1, p2), groups=(g1, g2)
        )
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(slot_factor=lambda s: 1.2),
        )
        by = {p.name: p.watts for p in res.plane_results}

        # At noon both groups saturate: corrected total == sum of limits.
        assert res.total_watts[_NOON_INDEX] == pytest.approx(1300.0)
        # Not the buggy over-limit product.
        assert res.total_watts[_NOON_INDEX] < 1300.0 * 1.2 - 1.0
        # Per-plane (== per-group here) totals respect each group's own limit.
        for i in range(len(weather)):
            assert by["P1"][i] <= 800.0 + 1e-6
            assert by["P2"][i] <= 500.0 + 1e-6
        # And the raw (physical) curve is likewise within the limits.
        assert max(res.raw_total_watts) == pytest.approx(1300.0)


# ---------------------------------------------------------------------------
# 2. factor > 1 BELOW the boundary: the re-clamp must not bite early
# ---------------------------------------------------------------------------


class TestFactorBelowBoundary:
    def test_factor_scales_when_under_the_limit(self, patched_physics):
        """A site well under its AC limit with a 1.2x factor: the corrected curve
        is exactly 1.2 * the (unclamped) raw curve everywhere — the re-clamp is a
        no-op because nothing reaches the ceiling."""
        site = _two_plane_site()  # 430 Wp planes, 800 W group -> clamp never bites
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(slot_factor=lambda s: 1.2),
        )
        # corrected == 1.2 * raw, bit-exact (re-clamp no-op below the ceiling).
        for pr in res.plane_results:
            for i in range(len(weather)):
                assert pr.watts[i] == 1.2 * pr.raw_watts[i]
        # The site never approaches the 800 W clamp, proving no early bite.
        assert max(res.total_watts) < 800.0


# ---------------------------------------------------------------------------
# 3. factor <= 1: bit-exact with the previous behaviour (clamped * factor)
# ---------------------------------------------------------------------------


class TestFactorAtOrBelowOne:
    def test_down_factor_is_bit_exact_clamped_times_factor(self, patched_physics):
        """With a clamp that DOES bite (5000 Wp into an 800 W group) and a 0.8x
        down-factor, the corrected per-plane watts equal the clamped raw watts
        times the factor, BIT-EXACT — the re-clamp changes nothing for
        factor <= 1 (the pre-fix common path)."""
        p1, p2 = _sun_facing_planes()
        group = InverterGroup(name="WR", plane_names=("P1", "P2"), ac_limit_w=800.0)
        site = SiteConfig(
            latitude=48.5, longitude=12.2, planes=(p1, p2), groups=(group,)
        )
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(slot_factor=lambda s: 0.8),
        )
        for pr in res.plane_results:
            for i in range(len(weather)):
                # raw_watts is the clamped physical curve; corrected == it * 0.8.
                assert pr.watts[i] == pr.raw_watts[i] * 0.8
        # Corrected peak is 0.8 * the 800 W clamp.
        assert max(res.total_watts) == pytest.approx(640.0)


# ---------------------------------------------------------------------------
# 4. Attribution holds through the DOUBLE clamp (beam + diffuse == watts)
# ---------------------------------------------------------------------------


class TestAttributionUnderDoubleClamp:
    def test_beam_plus_diffuse_equals_watts_after_reclamp(self, patched_physics):
        """A 1.3x factor on a clamp-biting site forces the re-clamp to bite; the
        beam/diffuse attribution is redistributed by the second clamp so it still
        sums to each plane's final watts, every plane, every slot."""
        p1, p2 = _sun_facing_planes()
        group = InverterGroup(name="WR", plane_names=("P1", "P2"), ac_limit_w=800.0)
        site = SiteConfig(
            latitude=48.5, longitude=12.2, planes=(p1, p2), groups=(group,)
        )
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(slot_factor=lambda s: 1.3),
        )
        for pr in res.plane_results:
            for i in range(len(weather)):
                assert pr.beam_watts[i] + pr.diffuse_watts[i] == pytest.approx(
                    pr.watts[i]
                )
        # The re-clamp really bit (corrected group sum held at the 800 W limit,
        # not the 800 * 1.3 the factor alone would have produced).
        assert max(res.total_watts) == pytest.approx(800.0)


# ---------------------------------------------------------------------------
# 5. Ungrouped plane: factor > 1 passes through unclamped (no configured ceiling)
# ---------------------------------------------------------------------------


class TestUngroupedPlaneNoCeiling:
    def test_ungrouped_plane_up_corrected_uncapped(self, patched_physics):
        """A plane in a group is re-clamped to its limit, but a plane in NO group
        has no configured ceiling: a 1.5x factor scales it through unclamped."""
        grouped = PlaneConfig(name="G", azimuth_deg=180.0, tilt_deg=30.0, wp=5000.0)
        free = PlaneConfig(name="U", azimuth_deg=180.0, tilt_deg=30.0, wp=5000.0)
        group = InverterGroup(name="WR", plane_names=("G",), ac_limit_w=800.0)
        site = SiteConfig(
            latitude=48.5, longitude=12.2, planes=(grouped, free), groups=(group,)
        )
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(slot_factor=lambda s: 1.5),
        )
        by = {p.name: p for p in res.plane_results}

        # Grouped plane: re-clamped to its 800 W ceiling (factor cannot lift it).
        assert max(by["G"].watts) == pytest.approx(800.0)
        # Ungrouped plane: factor passes straight through, every slot uncapped.
        pu = by["U"]
        for i in range(len(weather)):
            assert pu.watts[i] == 1.5 * pu.raw_watts[i]
        # It really exceeds the group's ceiling at midday (proving no cap on it).
        assert pu.watts[_NOON_INDEX] > 800.0


# ---------------------------------------------------------------------------
# 6. Quantile bands capped at the physical ceiling (P90 factor > 1)
# ---------------------------------------------------------------------------


class TestBandCeilingCap:
    def _clamped_site(self):
        p1, p2 = _sun_facing_planes()
        group = InverterGroup(name="WR", plane_names=("P1", "P2"), ac_limit_w=800.0)
        return SiteConfig(
            latitude=48.5, longitude=12.2, planes=(p1, p2), groups=(group,)
        )

    def test_p90_capped_at_ceiling_p10_untouched(self, patched_physics):
        """On a clamped midday slot the corrected total sits at the 800 W ceiling;
        a P90 band factor of 1.3 is capped to the ceiling (800 W), NOT 1040 W,
        while the P10 (0.7) stays well under and is unaffected."""
        site = self._clamped_site()
        weather = _clear_sky_series()
        base = engine.compute_forecast(site, weather, now=_TEST_DATE)
        band = QuantileBands(p10=0.7, p50=1.0, p90=1.3, n=40)
        band_by_slot = {s: band for s in base.slot_starts}
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(band_by_slot=band_by_slot),
        )

        # Sanity: the site is clamped at 800 W at noon.
        assert base.total_watts[_NOON_INDEX] == pytest.approx(800.0)
        # P90 curve is capped at the ceiling, not the factored 1040 W.
        assert res.p90_watts[_NOON_INDEX] == pytest.approx(800.0)
        assert res.p90_watts[_NOON_INDEX] < 1.3 * 800.0 - 1.0
        # P10 is well under the ceiling -> unaffected by the cap.
        assert res.p10_watts[_NOON_INDEX] == pytest.approx(
            0.7 * base.total_watts[_NOON_INDEX]
        )
        # No band watt ever exceeds its slot's physical ceiling (800 W here).
        assert all(w <= 800.0 + 1e-6 for w in res.p90_watts)

    def test_hourly_p90_reflects_capped_curve_p10_intact(self, patched_physics):
        """The hourly P90 Wh integrates the CAPPED p90 watts, so the peak hour is
        strictly below the naive 1.3 * corrected; the hourly P10 (never capped)
        is exactly 0.7 * the corrected hourly."""
        site = self._clamped_site()
        weather = _clear_sky_series()
        base = engine.compute_forecast(site, weather, now=_TEST_DATE)
        band = QuantileBands(p10=0.7, p50=1.0, p90=1.3, n=40)
        band_by_slot = {s: band for s in base.slot_starts}
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(band_by_slot=band_by_slot),
        )
        # Recompute the expected capped p90 hourly Wh from the capped watts curve.
        from datetime import UTC

        expected_p90: dict[str, float] = {}
        for i, start in enumerate(res.slot_starts):
            hkey = (
                start.astimezone(UTC)
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            if hkey not in base.hourly_wh:
                continue
            expected_p90[hkey] = (
                expected_p90.get(hkey, 0.0) + res.p90_watts[i] * _SLOT_HOURS
            )
        assert res.p90_hourly_wh.keys() == expected_p90.keys()
        for hkey, wh in expected_p90.items():
            assert res.p90_hourly_wh[hkey] == pytest.approx(wh)

        # The peak hour is capped: strictly below the naive 1.3x roll-up.
        peak_hkey = max(base.hourly_wh, key=lambda k: base.hourly_wh[k])
        assert res.p90_hourly_wh[peak_hkey] < 1.3 * base.hourly_wh[peak_hkey]
        # P10 hourly is never capped -> exactly 0.7 * corrected hourly.
        for hkey, wh in base.hourly_wh.items():
            assert res.p10_hourly_wh[hkey] == pytest.approx(0.7 * wh)
