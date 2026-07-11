"""Engine tests for the learned inverter efficiency override (AC-side Phase 3).

Plain pytest, no Home Assistant imports (SPEC §4). ``LearnerHooks`` gained a
site-level LEARNED ``inverter_efficiency`` that OVERRIDES the per-group config
eta on the AC curve when present (None => the per-group config eta stands). The
DC path (total_watts / hourly_wh / daily_kwh / raw / per-plane / bands) MUST
stay byte-identical — the learned eta reshapes only the served AC.

Reuses the analytic physics stand-ins + fixtures from ``test_engine_learning``
(``_two_plane_site`` never reaches its 800 W group clamp, so every slot is
unclipped and AC == eta * served DC exactly).
"""

from __future__ import annotations

import pytest
from balcony_solar_forecast.const import DEFAULT_INVERTER_EFFICIENCY
from balcony_solar_forecast.core import engine
from balcony_solar_forecast.core.engine import LearnerHooks
from balcony_solar_forecast.core.types import (
    InverterGroup,
    PlaneConfig,
    SiteConfig,
)

from .test_engine_learning import (  # noqa: F401  (patched_physics is a fixture)
    _NOON_INDEX,
    _TEST_DATE,
    _clear_sky_series,
    _two_plane_site,
    patched_physics,
)

_LEARNED = 0.93  # deliberately != DEFAULT_INVERTER_EFFICIENCY (0.965)


def _mixed_site():
    """One GROUPED plane (never clips) + one UNGROUPED plane (no inverter group).

    A supported config (SiteConfig imposes no all-planes-grouped rule) that
    exercises the ungrouped-plane AC path: with a learned eta override the
    ungrouped plane's SERVED AC must use the same learned eta as its pre-clamp
    AC weighting, else the two diverge and the AC day-ahead clamp detection
    misfires on unclipped slots.
    """
    p1 = PlaneConfig(name="G1", azimuth_deg=180.0, tilt_deg=30.0, wp=300.0)
    p2 = PlaneConfig(name="U1", azimuth_deg=180.0, tilt_deg=30.0, wp=300.0)
    group = InverterGroup(
        name="WR", plane_names=("G1",), ac_limit_w=1_000_000.0
    )
    return SiteConfig(
        latitude=48.5, longitude=12.2, planes=(p1, p2), groups=(group,)
    )


class TestLearnedInverterEfficiency:
    def test_hook_eta_overrides_group_default_on_ac_curve(self, patched_physics):
        # With hooks.inverter_efficiency=0.93 the AC curve uses 0.93, NOT the
        # group's default 0.965. The site never clips, so AC == eta * served DC.
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(inverter_efficiency=_LEARNED),
        )
        assert max(res.total_watts) < 800.0  # proves nothing clips
        assert res.ac_watts[_NOON_INDEX] == pytest.approx(
            _LEARNED * res.total_watts[_NOON_INDEX]
        )
        for i in range(len(weather)):
            assert res.ac_watts[i] == pytest.approx(_LEARNED * res.total_watts[i])

    def test_learned_eta_applies_to_ungrouped_plane_pre_clamp_matches_served(
        self, patched_physics
    ):
        # Regression guard (adversarial-review MED): with a learned eta override
        # AND an UNGROUPED plane, the served AC (electrical.clamp_groups_ac) must
        # weight the ungrouped plane by the SAME learned eta as the pre-clamp AC
        # (plane_eta), so the invariant "pre-clamp AC == served AC on every
        # unclipped slot" holds. Before the fix the ungrouped plane's served AC
        # was hardcoded at the datasheet default, diverging from its pre-clamp
        # weighting and falsely tripping the AC day-ahead clamp detection.
        site = _mixed_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(inverter_efficiency=_LEARNED),
        )
        assert max(res.total_watts) < 1_000_000.0  # nothing clips
        for i in range(len(weather)):
            # The invariant: unclipped -> pre-clamp AC == served AC (bit-close).
            assert res.ac_corrected_unclamped_watts[i] == pytest.approx(
                res.ac_watts[i]
            )
            # And the whole site (grouped + ungrouped) uses the learned eta.
            assert res.ac_watts[i] == pytest.approx(_LEARNED * res.total_watts[i])

    def test_none_hook_uses_config_eta(self, patched_physics):
        # inverter_efficiency=None (default) => the engine uses the per-group
        # config eta (here the DEFAULT). Bit-identical to the pre-Phase-3 path.
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE, hooks=LearnerHooks(),
        )
        assert res.ac_watts[_NOON_INDEX] == pytest.approx(
            DEFAULT_INVERTER_EFFICIENCY * res.total_watts[_NOON_INDEX]
        )

    def test_pre_clamp_ac_total_uses_learned_eta(self, patched_physics):
        # The pre-AC-clamp AC total (ac_corrected_unclamped_watts) shares the
        # SAME eta source as the served AC: on an unclipped slot both equal the
        # learned-eta * served DC, so they stay mutually consistent.
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(inverter_efficiency=_LEARNED),
        )
        assert res.ac_corrected_unclamped_watts[_NOON_INDEX] == pytest.approx(
            res.ac_watts[_NOON_INDEX]
        )
        assert res.ac_corrected_unclamped_watts[_NOON_INDEX] == pytest.approx(
            _LEARNED * res.total_watts[_NOON_INDEX]
        )

    def test_dc_path_untouched_by_learned_eta(self, patched_physics):
        # The learned eta must NOT change any DC-side field (the DC total is the
        # learner / scoreboard / kill-gate truth). Compare a run WITH the override
        # against one WITHOUT: every DC curve is byte-identical; only AC differs.
        site = _two_plane_site()
        weather = _clear_sky_series()
        base = engine.compute_forecast(site, weather, now=_TEST_DATE)
        cal = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(inverter_efficiency=_LEARNED),
        )
        assert cal.total_watts == base.total_watts
        assert cal.raw_total_watts == base.raw_total_watts
        assert cal.hourly_wh == base.hourly_wh
        assert cal.daily_kwh == base.daily_kwh
        assert cal.raw_hourly_wh == base.raw_hourly_wh
        for c, b in zip(cal.plane_results, base.plane_results, strict=True):
            assert c.watts == b.watts
            assert c.raw_watts == b.raw_watts
        # AC, by contrast, DID change (0.93 vs 0.965).
        assert cal.ac_watts[_NOON_INDEX] != pytest.approx(
            base.ac_watts[_NOON_INDEX]
        )

    def test_out_of_band_hook_eta_is_clamped(self, patched_physics):
        # A hook eta below INVERTER_EFFICIENCY_MIN is floored by _clamp_eta before
        # it reshapes the AC curve (never trusts a garbage scalar blindly).
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(inverter_efficiency=0.10),  # absurd -> clamped
        )
        from balcony_solar_forecast.const import INVERTER_EFFICIENCY_MIN
        assert res.ac_watts[_NOON_INDEX] == pytest.approx(
            INVERTER_EFFICIENCY_MIN * res.total_watts[_NOON_INDEX]
        )
