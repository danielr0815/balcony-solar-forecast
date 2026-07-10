"""Engine-hook tests for the quantile bands in core/engine.py (SPEC §6/§10).

Plain pytest, no Home Assistant imports (SPEC §4). These exercise the v0.4
addition to ``compute_forecast``: the ``LearnerHooks.band_by_slot`` quantile
hook.

  * NEUTRAL: no band map (None or empty) => the band fields stay empty and every
    consumer treats "no band" as band == corrected. BIT-EXACT with the
    pre-quantile path (the corrected curve itself is untouched).
  * ACTIVE: a per-slot band map produces p10/p50/p90 15-min watts curves aligned
    to ``slot_starts`` and their hourly Wh roll-ups keyed by the SAME UTC hour as
    ``hourly_wh``.
  * The band watts sit in the SAME instantaneous frame as ``total_watts``
    (p50 with a 1.0 band == total_watts), and p10 <= p50 <= p90 pointwise.
  * A band map is orthogonal to the learner hooks: it multiplies the CORRECTED
    curve, never the RAW curve.

Reuses the analytic physics stand-ins from ``test_engine_learning`` so the
quantile hook is tested against the same clear-sky two-plane site.
"""

from __future__ import annotations

from datetime import UTC

import pytest
from balcony_solar_forecast.core import engine
from balcony_solar_forecast.core.engine import LearnerHooks
from balcony_solar_forecast.core.types import QuantileBands

# Reuse the exact analytic stand-ins + fixtures the learner tests use.
from .test_engine_learning import (  # noqa: F401  (patched_physics is a fixture)
    _TEST_DATE,
    _clear_sky_series,
    _two_plane_site,
    patched_physics,
)

_SLOT_HOURS = 0.25


# ---------------------------------------------------------------------------
# Neutral: no band map => empty band fields, corrected curve bit-exact
# ---------------------------------------------------------------------------


class TestNeutralNoBands:
    def test_default_hooks_leave_band_fields_empty(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert res.p10_watts == ()
        assert res.p50_watts == ()
        assert res.p90_watts == ()
        assert res.p10_hourly_wh == {}
        assert res.p50_hourly_wh == {}
        assert res.p90_hourly_wh == {}

    def test_empty_band_map_leaves_fields_empty(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(band_by_slot={}),
        )
        assert res.p10_watts == ()
        assert res.p50_hourly_wh == {}

    def test_band_hook_does_not_disturb_corrected_curve(self, patched_physics):
        """Injecting bands must not change total_watts / hourly_wh at all —
        the bands are a derived overlay, not a modification of the served curve."""
        site = _two_plane_site()
        weather = _clear_sky_series()
        base = engine.compute_forecast(site, weather, now=_TEST_DATE)

        band = QuantileBands(p10=0.7, p50=1.0, p90=1.4, n=40)
        band_by_slot = {s: band for s in base.slot_starts}
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(band_by_slot=band_by_slot),
        )
        assert res.total_watts == base.total_watts
        assert res.hourly_wh == base.hourly_wh
        assert res.daily_kwh == base.daily_kwh
        assert res.raw_total_watts == base.raw_total_watts


# ---------------------------------------------------------------------------
# Active: a per-slot band map produces aligned band curves
# ---------------------------------------------------------------------------


class TestActiveBands:
    def _run_with_band(self, band):
        site = _two_plane_site()
        weather = _clear_sky_series()
        base = engine.compute_forecast(site, weather, now=_TEST_DATE)
        band_by_slot = {s: band for s in base.slot_starts}
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(band_by_slot=band_by_slot),
        )
        return base, res

    def test_p50_unit_band_equals_total_watts(self, patched_physics):
        band = QuantileBands(p10=0.8, p50=1.0, p90=1.25, n=40)
        base, res = self._run_with_band(band)
        # A P50 multiplier of 1.0 reproduces the served curve exactly.
        assert res.p50_watts == base.total_watts

    def test_bands_scale_watts_pointwise(self, patched_physics):
        band = QuantileBands(p10=0.5, p50=1.0, p90=2.0, n=40)
        base, res = self._run_with_band(band)
        for i, w in enumerate(base.total_watts):
            assert res.p10_watts[i] == pytest.approx(0.5 * w)
            assert res.p50_watts[i] == pytest.approx(1.0 * w)
            assert res.p90_watts[i] == pytest.approx(2.0 * w)

    def test_bands_monotonic_pointwise(self, patched_physics):
        band = QuantileBands(p10=0.6, p50=0.95, p90=1.4, n=40)
        _, res = self._run_with_band(band)
        for a, b, c in zip(res.p10_watts, res.p50_watts, res.p90_watts, strict=False):
            assert a <= b <= c

    def test_curves_aligned_to_slot_starts(self, patched_physics):
        band = QuantileBands(p10=0.9, p50=1.0, p90=1.1, n=40)
        _, res = self._run_with_band(band)
        n = len(res.slot_starts)
        assert len(res.p10_watts) == n
        assert len(res.p50_watts) == n
        assert len(res.p90_watts) == n

    def test_hourly_rollup_keys_match_corrected(self, patched_physics):
        band = QuantileBands(p10=0.8, p50=1.0, p90=1.3, n=40)
        base, res = self._run_with_band(band)
        # Same hour keys as the corrected hourly curve.
        assert set(res.p50_hourly_wh) == set(base.hourly_wh)
        assert set(res.p10_hourly_wh) == set(base.hourly_wh)
        assert set(res.p90_hourly_wh) == set(base.hourly_wh)

    def test_p50_unit_hourly_equals_corrected_hourly(self, patched_physics):
        band = QuantileBands(p10=0.8, p50=1.0, p90=1.3, n=40)
        base, res = self._run_with_band(band)
        for hkey, wh in base.hourly_wh.items():
            assert res.p50_hourly_wh[hkey] == pytest.approx(wh)

    def test_hourly_rollup_is_slot_integral_of_band_watts(self, patched_physics):
        band = QuantileBands(p10=0.5, p50=1.0, p90=1.5, n=40)
        base, res = self._run_with_band(band)
        # Recompute the expected p90 hourly Wh from the p90 watts curve, over the
        # SAME dense hour key set as the corrected hourly curve (dark slots that
        # short-circuit never create a corrected hour bucket).
        expected: dict[str, float] = {}
        for i, start in enumerate(res.slot_starts):
            hkey = (
                start.astimezone(UTC)
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            if hkey not in base.hourly_wh:
                continue
            expected[hkey] = expected.get(hkey, 0.0) + res.p90_watts[i] * _SLOT_HOURS
        assert res.p90_hourly_wh.keys() == expected.keys()
        for hkey, wh in expected.items():
            assert res.p90_hourly_wh[hkey] == pytest.approx(wh)

    def test_hourly_bands_ordered(self, patched_physics):
        band = QuantileBands(p10=0.6, p50=0.9, p90=1.4, n=40)
        base, res = self._run_with_band(band)
        for hkey in base.hourly_wh:
            assert (
                res.p10_hourly_wh[hkey]
                <= res.p50_hourly_wh[hkey]
                <= res.p90_hourly_wh[hkey]
            )

    def test_bands_multiply_corrected_not_raw(self, patched_physics):
        """With an ACTIVE shademap-style corrected/raw split, the bands scale the
        corrected curve, and the raw curve is entirely independent of them."""
        site = _two_plane_site()
        weather = _clear_sky_series()

        # A slot_factor that pulls the corrected curve away from raw, so
        # corrected != raw and we can prove the band tracks corrected.
        def half_factor(slot_start):
            return 0.5

        from balcony_solar_forecast.const import CORRECTION_SOURCE_INTRADAY

        base = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(
                slot_factor=half_factor,
                correction_source=CORRECTION_SOURCE_INTRADAY,
            ),
        )
        band = QuantileBands(p10=1.0, p50=1.0, p90=1.0, n=40)  # unit band
        band_by_slot = {s: band for s in base.slot_starts}
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(
                slot_factor=half_factor,
                correction_source=CORRECTION_SOURCE_INTRADAY,
                band_by_slot=band_by_slot,
            ),
        )
        # Unit band over the corrected curve reproduces total_watts (corrected),
        # NOT raw_total_watts (which is 2x the corrected here).
        assert res.p50_watts == res.total_watts
        # Sanity: raw really differs from corrected in this setup.
        assert res.raw_total_watts != res.total_watts

    def test_partial_band_map_passes_unbanded_slots_through(self, patched_physics):
        """A band map covering only some slots leaves the rest at corrected."""
        site = _two_plane_site()
        weather = _clear_sky_series()
        base = engine.compute_forecast(site, weather, now=_TEST_DATE)

        band = QuantileBands(p10=0.5, p50=2.0, p90=3.0, n=40)
        # Band only the first half of the slots.
        half = len(base.slot_starts) // 2
        band_by_slot = {s: band for s in base.slot_starts[:half]}
        res = engine.compute_forecast(
            site, weather, now=_TEST_DATE,
            hooks=LearnerHooks(band_by_slot=band_by_slot),
        )
        for i, w in enumerate(base.total_watts):
            if i < half:
                assert res.p50_watts[i] == pytest.approx(2.0 * w)
            else:
                assert res.p50_watts[i] == pytest.approx(w)
