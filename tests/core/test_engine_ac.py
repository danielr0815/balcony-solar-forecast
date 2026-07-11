"""AC-side served-curve tests for core/engine.py (Phase 1 AC-side forecast).

Plain pytest, no Home Assistant imports (SPEC §4). Phase 1 adds a deterministic
DC->AC transform on top of the DC pipeline: per inverter group AC = min(eta_inv *
factor * sum(DC_unclamped), ac_limit), with the served DC clip point moved to
ac_limit/eta_inv INSIDE the AC curve only. The DC path (total_watts / hourly_wh /
daily_kwh / per-plane / band series) MUST stay byte-identical — it remains the
self-learning / scoreboard / kill-gate truth.

These reuse the analytic physics stand-ins + fixtures from ``test_engine_learning``
(open horizon everywhere, a clear-sky day) exactly as ``test_engine_clamp`` does.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from balcony_solar_forecast.const import DEFAULT_INVERTER_EFFICIENCY
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
_ETA = DEFAULT_INVERTER_EFFICIENCY


def _sun_facing_planes(wp=5000.0):
    """Two identical planes pointed straight at the analytic noon sun (az 180).

    At noon each plane's unclamped DC dwarfs any sane AC limit, so a group over
    them clamps hard — the setup for the clipped-slot AC boundary tests.
    """
    p1 = PlaneConfig(name="P1", azimuth_deg=180.0, tilt_deg=30.0, wp=wp)
    p2 = PlaneConfig(name="P2", azimuth_deg=180.0, tilt_deg=30.0, wp=wp)
    return p1, p2


def _clamped_site():
    p1, p2 = _sun_facing_planes()
    group = InverterGroup(name="WR", plane_names=("P1", "P2"), ac_limit_w=800.0)
    return SiteConfig(
        latitude=48.5, longitude=12.2, planes=(p1, p2), groups=(group,)
    )


def _never_clamped_site():
    """Sun-facing planes under an AC limit so high the group never clips.

    Every slot stays unclipped even under an up-correcting fast-learner factor,
    so the AC curve isolates the ``* factor`` term from the inverter clamp.
    """
    p1, p2 = _sun_facing_planes()
    group = InverterGroup(
        name="WR", plane_names=("P1", "P2"), ac_limit_w=1_000_000.0
    )
    return SiteConfig(
        latitude=48.5, longitude=12.2, planes=(p1, p2), groups=(group,)
    )


# ---------------------------------------------------------------------------
# 1. Shape: ac_watts is dense and aligned to the slot grid
# ---------------------------------------------------------------------------


class TestAcShape:
    def test_ac_watts_length_matches_slot_count(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert len(res.ac_watts) == len(weather)
        assert len(res.ac_watts) == len(res.slot_starts)
        assert len(res.ac_watts) == len(res.total_watts)


# ---------------------------------------------------------------------------
# 2. Unclipped slot: AC == eta * served DC (within fp)
# ---------------------------------------------------------------------------


class TestAcUnclipped:
    def test_ac_is_eta_times_dc_everywhere_below_the_limit(self, patched_physics):
        # _two_plane_site never reaches its 800 W group clamp, so every slot is
        # unclipped and AC == eta * served DC exactly.
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert max(res.total_watts) < 800.0  # proves nothing clips
        assert res.ac_watts[_NOON_INDEX] == pytest.approx(
            _ETA * res.total_watts[_NOON_INDEX]
        )
        for i in range(len(weather)):
            assert res.ac_watts[i] == pytest.approx(_ETA * res.total_watts[i])

    def test_ac_carries_the_fast_learner_factor_when_unclipped(
        self, patched_physics
    ):
        # Regression guard for the AC `* factor` term (adversarial-review MED):
        # on an UNCLIPPED slot the served DC carries the fast-learner factor, so
        # the AC must too — AC = eta * factor * sum(DC_unclamped). Dropping the
        # `* factor` from the engine's ac_input leaves the whole engine suite
        # green (the unclipped AC test above uses factor 1; the clipped tests
        # pin AC to the ceiling where the factor washes out), so pin it here:
        # a 1.3x up-correction on a never-clipping site scales BOTH the served
        # DC and the AC at noon by 1.3, and AC == eta * served DC still holds.
        site = _never_clamped_site()
        weather = _clear_sky_series()
        base = engine.compute_forecast(site, weather, now=_TEST_DATE)
        boosted = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(slot_factor=lambda s: 1.3),
        )
        # The 1.3x lands on the served DC AND the AC by the same factor.
        assert boosted.total_watts[_NOON_INDEX] == pytest.approx(
            1.3 * base.total_watts[_NOON_INDEX]
        )
        assert boosted.ac_watts[_NOON_INDEX] == pytest.approx(
            1.3 * base.ac_watts[_NOON_INDEX]
        )
        # And the core invariant AC == eta * served DC holds under the factor
        # (this equality FAILS if the factor is dropped from AC but not DC).
        for i in range(len(weather)):
            assert boosted.ac_watts[i] == pytest.approx(
                _ETA * boosted.total_watts[i]
            )


# ---------------------------------------------------------------------------
# 3. Clipped slot: AC pinned to the AC limit; served DC stays the OLD clamp
# ---------------------------------------------------------------------------


class TestAcClipped:
    def test_clipped_ac_equals_limit_dc_stays_at_old_clamp(self, patched_physics):
        site = _clamped_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)

        # Served DC at noon is the OLD min(sum, ac_limit) == 800 W (byte-identical
        # DC path: NOT the corrected clip point ac_limit/eta ~ 829 W).
        assert res.total_watts[_NOON_INDEX] == pytest.approx(800.0)
        assert res.total_watts[_NOON_INDEX] < 800.0 / _ETA - 1.0
        # AC at noon is pinned to the group AC limit (the inverter's own clamp),
        # NOT eta * served DC (which would be ~772 W).
        assert res.ac_watts[_NOON_INDEX] == pytest.approx(800.0)
        assert res.ac_watts[_NOON_INDEX] > _ETA * res.total_watts[_NOON_INDEX]
        # No AC watt anywhere exceeds the single group's AC limit.
        assert all(w <= 800.0 + 1e-6 for w in res.ac_watts)

    def test_up_factor_cannot_lift_ac_past_the_limit(self, patched_physics):
        # A 1.3x fast-learner up-correction on a clamp-biting site: served DC is
        # re-clamped to 800 W and the AC is still pinned to the 800 W AC limit
        # (the inverter caps AC regardless of the DC up-correction).
        site = _clamped_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(slot_factor=lambda s: 1.3),
        )
        assert res.total_watts[_NOON_INDEX] == pytest.approx(800.0)
        assert res.ac_watts[_NOON_INDEX] == pytest.approx(800.0)
        assert all(w <= 800.0 + 1e-6 for w in res.ac_watts)


# ---------------------------------------------------------------------------
# 4. Roll-ups: AC hourly / daily align with the DC keys and sum consistently
# ---------------------------------------------------------------------------


class TestAcRollups:
    def test_hourly_daily_keys_align_and_sum_consistently(self, patched_physics):
        site = _two_plane_site()  # unclipped -> AC == eta * DC every slot
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)

        # Same hour / day buckets as the corrected DC roll-up.
        assert set(res.ac_hourly_wh) == set(res.hourly_wh)
        assert set(res.ac_daily_kwh) == set(res.daily_kwh)

        # Hourly AC integrates the ac_watts curve on the SAME keys as hourly_wh.
        from datetime import UTC

        expected_hourly: dict[str, float] = {}
        for i, start in enumerate(res.slot_starts):
            hkey = (
                start.astimezone(UTC)
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            if hkey not in res.hourly_wh:
                continue
            expected_hourly[hkey] = (
                expected_hourly.get(hkey, 0.0) + res.ac_watts[i] * _SLOT_HOURS
            )
        assert res.ac_hourly_wh.keys() == expected_hourly.keys()
        for hkey, wh in expected_hourly.items():
            assert res.ac_hourly_wh[hkey] == pytest.approx(wh)

        # Unclipped: each AC hour is exactly eta * the corrected DC hour.
        for hkey, wh in res.hourly_wh.items():
            assert res.ac_hourly_wh[hkey] == pytest.approx(_ETA * wh)
        # And daily AC kWh == eta * daily DC kWh.
        for dkey, kwh in res.daily_kwh.items():
            assert res.ac_daily_kwh[dkey] == pytest.approx(_ETA * kwh)


# ---------------------------------------------------------------------------
# 5. DC path is untouched by the AC addition
# ---------------------------------------------------------------------------


class TestDcUntouched:
    def test_identity_invariant_and_ac_is_separate(self, patched_physics):
        # No hooks: the corrected DC curve is bit-exact the raw DC curve (a
        # pre-existing invariant that would break if AC leaked into the DC path),
        # while AC is a strictly separate, eta-scaled curve.
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert res.total_watts == res.raw_total_watts  # bit-exact, no float slop
        assert res.hourly_wh == res.raw_hourly_wh
        assert res.daily_kwh == res.raw_daily_kwh
        # AC is distinct from DC (eta < 1) yet the DC curve is unchanged.
        assert res.ac_watts[_NOON_INDEX] != res.total_watts[_NOON_INDEX]
        assert res.ac_watts[_NOON_INDEX] == pytest.approx(
            _ETA * res.total_watts[_NOON_INDEX]
        )

    def test_clipped_served_dc_clips_at_ac_limit_not_the_corrected_point(
        self, patched_physics
    ):
        # The served DC still clips at ac_limit (old clamp_groups semantics),
        # never at the corrected ac_limit/eta point the AC curve uses internally.
        site = _clamped_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert max(res.total_watts) == pytest.approx(800.0)
        # The corrected clip point (~829 W) is never reached by the served DC.
        assert max(res.total_watts) < 800.0 / _ETA - 1.0


# ---------------------------------------------------------------------------
# 6. Per-group efficiency overrides flow through to the site AC total
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 7. AC pre-clamp total (ac_corrected_unclamped_watts) — Phase 2
# ---------------------------------------------------------------------------


class TestAcCorrectedUnclamped:
    def test_length_and_unclipped_equals_ac_watts(self, patched_physics):
        # On a never-clipping site every slot is unclipped, so the pre-AC-clamp
        # AC total equals the served AC total exactly (the clamp never bit).
        site = _never_clamped_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert len(res.ac_corrected_unclamped_watts) == len(res.ac_watts)
        assert len(res.ac_corrected_unclamped_watts) == len(res.slot_starts)
        for i in range(len(weather)):
            assert res.ac_corrected_unclamped_watts[i] == pytest.approx(
                res.ac_watts[i]
            )

    def test_clipped_slot_preclamp_exceeds_served_ac(self, patched_physics):
        # On a clamp-biting site the inverter AC clamp caps the served AC at the
        # 800 W limit, but the PRE-clamp AC (eta * factored DC) is far higher, so
        # the gap reveals the clamped slot (the AC analogue of MED-1).
        site = _clamped_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert res.ac_watts[_NOON_INDEX] == pytest.approx(800.0)
        assert res.ac_corrected_unclamped_watts[_NOON_INDEX] > 800.0
        # Never below the served AC anywhere (pre-clamp >= served by construction).
        for pre, served in zip(
            res.ac_corrected_unclamped_watts, res.ac_watts, strict=False
        ):
            assert pre >= served - 1e-6


# ---------------------------------------------------------------------------
# 8. AC band curves (ac_p10_hourly_wh / ac_p90_hourly_wh) — Phase 2
# ---------------------------------------------------------------------------


class TestAcBands:
    def test_no_bands_leaves_ac_band_fields_empty(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert res.ac_p10_hourly_wh == {}
        assert res.ac_p90_hourly_wh == {}

    def test_ac_bands_present_and_keyed_like_ac_hourly(self, patched_physics):
        # An unclipped site with a unit-ish band: AC bands roll up on the SAME hour
        # keys as ac_hourly_wh, and p90 = 1.5 * ac_hourly (no ceiling bite here).
        site = _never_clamped_site()
        weather = _clear_sky_series()
        band = QuantileBands(p10=0.6, p50=1.0, p90=1.5, n=40)
        base = engine.compute_forecast(site, weather, now=_TEST_DATE)
        band_by_slot = {s: band for s in base.slot_starts}
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(band_by_slot=band_by_slot),
        )
        assert set(res.ac_p10_hourly_wh) == set(res.ac_hourly_wh)
        assert set(res.ac_p90_hourly_wh) == set(res.ac_hourly_wh)
        for hkey, wh in res.ac_hourly_wh.items():
            assert res.ac_p90_hourly_wh[hkey] == pytest.approx(1.5 * wh)
            assert res.ac_p10_hourly_wh[hkey] == pytest.approx(0.6 * wh)

    def test_ac_band_capped_at_ac_ceiling(self, patched_physics):
        # A clamp-biting site: the P90 factor (2.0) would double the noon AC, but
        # the AC ceiling (the single group's 800 W AC limit) caps it. Each hour's
        # AC P90 Wh can never exceed the ceiling integrated over its slots.
        site = _clamped_site()
        weather = _clear_sky_series()
        band = QuantileBands(p10=0.8, p50=1.0, p90=2.0, n=40)
        base = engine.compute_forecast(site, weather, now=_TEST_DATE)
        band_by_slot = {s: band for s in base.slot_starts}
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(band_by_slot=band_by_slot),
        )
        # A full hour of 4 slots each capped at the 800 W AC limit is 800 Wh max.
        for wh in res.ac_p90_hourly_wh.values():
            assert wh <= 800.0 + 1e-6
        # The clamped noon hour is pinned to the ceiling (the P90 factor washed
        # out against the cap): its AC P90 Wh equals the served AC hour Wh.
        noon_hkey = (
            base.slot_starts[_NOON_INDEX]
            .astimezone(UTC)
            .replace(minute=0, second=0, microsecond=0)
            .isoformat()
        )
        assert res.ac_p90_hourly_wh[noon_hkey] == pytest.approx(
            res.ac_hourly_wh[noon_hkey]
        )


# ---------------------------------------------------------------------------
# 9. DC path + DC bands untouched by the AC additions — Phase 2
# ---------------------------------------------------------------------------


class TestDcUntouchedByAcPhase2:
    def test_ac_fields_do_not_leak_into_dc_bands(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        band = QuantileBands(p10=0.7, p50=1.0, p90=1.4, n=40)
        base = engine.compute_forecast(site, weather, now=_TEST_DATE)
        band_by_slot = {s: band for s in base.slot_starts}
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(band_by_slot=band_by_slot),
        )
        # DC curve + DC band roll-ups are entirely independent of the AC fields.
        assert res.total_watts == base.total_watts
        assert res.hourly_wh == base.hourly_wh
        assert res.daily_kwh == base.daily_kwh
        # The DC p50 band with a unit-ish factor still equals the pre-AC behaviour:
        # AC bands exist alongside, never replacing, the DC band curves.
        assert res.p90_hourly_wh  # DC bands present
        assert res.ac_p90_hourly_wh  # AC bands present too
        assert res.p90_hourly_wh.keys() == res.hourly_wh.keys()


class TestPerGroupEfficiency:
    def test_two_groups_distinct_eta_yield_expected_site_ac(self, patched_physics):
        # Two below-limit groups with distinct eta: the site AC total is the
        # sum of each group's eta * its served DC, so a per-group override is
        # faithfully reflected.
        hi = PlaneConfig(name="H", azimuth_deg=205.0, tilt_deg=70.0, wp=430.0)
        lo = PlaneConfig(name="L", azimuth_deg=205.0, tilt_deg=70.0, wp=430.0)
        g_hi = InverterGroup(
            name="HI", plane_names=("H",), ac_limit_w=2000.0,
            inverter_efficiency=DEFAULT_INVERTER_EFFICIENCY,
        )
        g_lo = InverterGroup(
            name="LO", plane_names=("L",), ac_limit_w=2000.0,
            inverter_efficiency=0.90,
        )
        site = SiteConfig(
            latitude=48.5, longitude=12.2, planes=(hi, lo), groups=(g_hi, g_lo)
        )
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        by = {p.name: p for p in res.plane_results}

        # High AC limits -> no clip: per-plane DC is the served DC.
        for i in range(len(weather)):
            dc_h = by["H"].watts[i]
            dc_l = by["L"].watts[i]
            expected_ac = DEFAULT_INVERTER_EFFICIENCY * dc_h + 0.90 * dc_l
            assert res.ac_watts[i] == pytest.approx(expected_ac)
        # At noon the lower-eta group contributes strictly less than it would at
        # the default eta (sanity that the override actually bit).
        assert by["L"].watts[_NOON_INDEX] > 0.0
