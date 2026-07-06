"""Pure tests for core/quantiles.py — P10/P50/P90 historical simulation (SPEC §6/§10).

Plain pytest, no Home Assistant imports (SPEC §4). These cover:

  * empirical_percentile math on known samples (type-7 interpolation), the
    empty/single-element edge cases, and pct clamping;
  * bands monotonicity (p10 <= p50 <= p90) on a real ring;
  * COLD START neutral identity band (no fabricated spread AND no fabricated
    shift) below QUANTILE_MIN_SAMPLES, and for an empty/missing bin;
  * weather-class x day-part conditioning: bands_for_bin selects the right bin;
  * train_quantiles folds a night of errors correctly (clamp, threshold skip,
    junk skip, FIFO cap, input immutability, shared taxonomy with BiasState);
  * apply_bands / band_curve_from_corrected multiply correctly and pass-through
    unbanded hours/slots unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from balcony_solar_forecast.const import (
    QUANTILE_MIN_FORECAST_WH,
    QUANTILE_MIN_SAMPLES,
    QUANTILE_NEUTRAL_MULT,
    QUANTILE_P_HIGH,
    QUANTILE_P_LOW,
    QUANTILE_P_MID,
    QUANTILE_REL_ERR_MAX,
    QUANTILE_RING_DAYS,
)
from balcony_solar_forecast.core import quantiles
from balcony_solar_forecast.core.quantiles import (
    QuantileSample,
    apply_bands,
    band_curve_from_corrected,
    bands_for_bin,
    empirical_percentile,
    quantile_bin_key,
    train_quantiles,
)
from balcony_solar_forecast.core.types import (
    BiasState,
    QuantileBands,
    QuantileState,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# empirical_percentile
# ---------------------------------------------------------------------------


class TestEmpiricalPercentile:
    def test_empty_returns_neutral(self):
        assert empirical_percentile([], 50.0) == QUANTILE_NEUTRAL_MULT
        assert empirical_percentile([], 10.0) == QUANTILE_NEUTRAL_MULT

    def test_single_element_any_pct(self):
        assert empirical_percentile([0.73], 10.0) == 0.73
        assert empirical_percentile([0.73], 50.0) == 0.73
        assert empirical_percentile([0.73], 90.0) == 0.73

    def test_median_odd_count(self):
        # 5 values -> median is the middle element.
        vals = sorted([0.2, 0.4, 0.6, 0.8, 1.0])
        assert empirical_percentile(vals, 50.0) == pytest.approx(0.6)

    def test_median_even_count_interpolates(self):
        # Type-7: rank = (n-1)*0.5 = 1.5 -> midpoint of index 1 and 2.
        vals = sorted([0.0, 1.0, 2.0, 3.0])
        assert empirical_percentile(vals, 50.0) == pytest.approx(1.5)

    def test_known_type7_percentiles(self):
        # 0..10 inclusive, 11 samples, ranks land exactly on integers.
        vals = [float(i) for i in range(11)]  # 0..10
        assert empirical_percentile(vals, 10.0) == pytest.approx(1.0)
        assert empirical_percentile(vals, 50.0) == pytest.approx(5.0)
        assert empirical_percentile(vals, 90.0) == pytest.approx(9.0)

    def test_type7_interpolated_percentile(self):
        # 10 samples 1..10; P10 rank = (10-1)*0.1 = 0.9 -> between 1 and 2.
        vals = [float(i) for i in range(1, 11)]
        assert empirical_percentile(vals, 10.0) == pytest.approx(1.9)
        assert empirical_percentile(vals, 90.0) == pytest.approx(9.1)

    def test_pct_out_of_range_is_clamped(self):
        vals = [1.0, 2.0, 3.0]
        assert empirical_percentile(vals, -50.0) == pytest.approx(1.0)   # -> P0
        assert empirical_percentile(vals, 500.0) == pytest.approx(3.0)   # -> P100

    def test_monotone_in_pct(self):
        vals = sorted([0.5, 0.7, 0.9, 1.1, 1.3, 1.5, 1.7, 1.9])
        p10 = empirical_percentile(vals, QUANTILE_P_LOW)
        p50 = empirical_percentile(vals, QUANTILE_P_MID)
        p90 = empirical_percentile(vals, QUANTILE_P_HIGH)
        assert p10 <= p50 <= p90


# ---------------------------------------------------------------------------
# quantile_bin_key — shared taxonomy with BiasState
# ---------------------------------------------------------------------------


class TestBinKey:
    def test_key_format(self):
        assert quantile_bin_key("clear", "morning") == "clear|morning"

    def test_matches_biasstate_and_quantilestate(self):
        # One taxonomy across bias / quantiles / scoreboard.
        assert quantile_bin_key("fog", "afternoon") == BiasState.cell_key(
            "fog", "afternoon"
        )
        assert quantile_bin_key("overcast", "evening") == QuantileState.bin_key(
            "overcast", "evening"
        )


# ---------------------------------------------------------------------------
# bands_for_bin — cold start, monotonicity, conditioning
# ---------------------------------------------------------------------------


def _ring(value: float, count: int) -> list[float]:
    return [value] * count


class TestBandsForBin:
    def test_empty_state_is_neutral(self):
        b = bands_for_bin(QuantileState(), cloud_class="clear", day_part="noon")
        assert (b.p10, b.p50, b.p90) == (
            QUANTILE_NEUTRAL_MULT,
            QUANTILE_NEUTRAL_MULT,
            QUANTILE_NEUTRAL_MULT,
        )
        assert b.n == 0
        assert b.collapsed

    def test_missing_bin_is_neutral(self):
        st = QuantileState(bins={"clear|noon": _ring(0.9, 40)})
        b = bands_for_bin(st, cloud_class="fog", day_part="morning")
        assert b.collapsed
        assert b.p50 == QUANTILE_NEUTRAL_MULT

    def test_cold_start_returns_neutral_no_fake_shift(self):
        # Fewer than the min-sample threshold: NO fabricated spread AND no
        # fabricated SHIFT. A thin bin returns the neutral identity band (1.0),
        # not its unshrunk empirical median (which a single clamped outlier could
        # scale off the served curve with no statistical backing).
        n = QUANTILE_MIN_SAMPLES - 1
        ring = [0.5 + 0.02 * i for i in range(n)]  # a genuinely spread sample
        st = QuantileState(bins={"mixed|afternoon": ring})
        b = bands_for_bin(st, cloud_class="mixed", day_part="afternoon")
        assert b.collapsed
        assert b.p10 == b.p50 == b.p90 == QUANTILE_NEUTRAL_MULT

    def test_at_threshold_emits_spread(self):
        n = QUANTILE_MIN_SAMPLES
        ring = [0.5 + (i / (n - 1)) for i in range(n)]  # 0.5 .. 1.5 spread
        st = QuantileState(bins={"clear|noon": ring})
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        assert b.n == n
        assert not b.collapsed
        assert b.p10 < b.p50 < b.p90

    def test_bands_monotonic(self):
        ring = [0.3, 0.9, 1.5] * 20  # 60 samples, wide spread
        st = QuantileState(bins={"clear|noon": ring})
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        assert b.p10 <= b.p50 <= b.p90

    def test_weather_class_conditioning_selects_bin(self):
        # Two different bins with clearly different medians; the class selects.
        st = QuantileState(
            bins={
                "clear|noon": _ring(1.2, 40),   # engine under-forecasts on clear
                "overcast|noon": _ring(0.6, 40),  # over-forecasts on overcast
            }
        )
        clear = bands_for_bin(st, cloud_class="clear", day_part="noon")
        overcast = bands_for_bin(st, cloud_class="overcast", day_part="noon")
        assert clear.p50 == pytest.approx(1.2)
        assert overcast.p50 == pytest.approx(0.6)

    def test_day_part_conditioning_selects_bin(self):
        st = QuantileState(
            bins={
                "clear|morning": _ring(0.8, 30),
                "clear|evening": _ring(1.4, 30),
            }
        )
        morn = bands_for_bin(st, cloud_class="clear", day_part="morning")
        eve = bands_for_bin(st, cloud_class="clear", day_part="evening")
        assert morn.p50 == pytest.approx(0.8)
        assert eve.p50 == pytest.approx(1.4)

    def test_corrupt_ring_degrades_to_neutral(self):
        # Non-numeric junk in a directly-constructed state must not raise.
        st = QuantileState(bins={"clear|noon": ["oops", None, float("nan")]})  # type: ignore[list-item]
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        assert b.collapsed
        assert b.p50 == QUANTILE_NEUTRAL_MULT

    def test_none_state_is_neutral(self):
        b = bands_for_bin(None, cloud_class="clear", day_part="noon")  # type: ignore[arg-type]
        assert b.collapsed


# ---------------------------------------------------------------------------
# train_quantiles — nightly fold
# ---------------------------------------------------------------------------


class TestTrainQuantiles:
    def test_folds_one_sample_into_correct_bin(self):
        st = QuantileState()
        s = QuantileSample("clear", "noon", measured_wh=90.0, corrected_wh=100.0)
        out = train_quantiles(st, [s])
        assert out.bins == {"clear|noon": [pytest.approx(0.9)]}

    def test_input_state_untouched(self):
        st = QuantileState(bins={"clear|noon": [1.0]})
        s = QuantileSample("clear", "noon", 50.0, 100.0)
        out = train_quantiles(st, [s])
        # Original ring object unchanged; output is a fresh ring.
        assert st.bins["clear|noon"] == [1.0]
        assert out.bins["clear|noon"] == [1.0, pytest.approx(0.5)]
        assert out.bins["clear|noon"] is not st.bins["clear|noon"]

    def test_relerr_computed_and_clamped_high(self):
        st = QuantileState()
        # measured 5000, corrected 100 -> relerr 50, clamp to REL_ERR_MAX.
        s = QuantileSample("clear", "noon", 5000.0, 100.0)
        out = train_quantiles(st, [s])
        assert out.bins["clear|noon"] == [QUANTILE_REL_ERR_MAX]

    def test_below_threshold_forecast_skipped(self):
        st = QuantileState()
        s = QuantileSample(
            "clear", "noon", measured_wh=10.0,
            corrected_wh=QUANTILE_MIN_FORECAST_WH,  # not strictly greater
        )
        out = train_quantiles(st, [s])
        assert out.bins == {}

    def test_just_above_threshold_kept(self):
        st = QuantileState()
        s = QuantileSample("clear", "noon", 10.0, QUANTILE_MIN_FORECAST_WH + 0.001)
        out = train_quantiles(st, [s])
        assert "clear|noon" in out.bins

    def test_junk_samples_skipped(self):
        st = QuantileState()
        bad = [
            QuantileSample("", "noon", 10.0, 100.0),          # empty class
            QuantileSample("clear", "", 10.0, 100.0),         # empty part
            QuantileSample("clear", "noon", float("nan"), 100.0),  # NaN measured
            QuantileSample("clear", "noon", 10.0, float("inf")),   # inf corrected
            QuantileSample("clear", "noon", 10.0, 0.0),       # zero corrected (<= thr)
        ]
        good = QuantileSample("clear", "noon", 80.0, 100.0)
        out = train_quantiles(st, bad + [good])
        assert out.bins == {"clear|noon": [pytest.approx(0.8)]}

    def test_multiple_bins_and_multiple_samples(self):
        st = QuantileState()
        samples = [
            QuantileSample("clear", "noon", 100.0, 100.0),
            QuantileSample("clear", "noon", 80.0, 100.0),
            QuantileSample("fog", "morning", 50.0, 100.0),
        ]
        out = train_quantiles(st, samples)
        assert out.bins["clear|noon"] == [pytest.approx(1.0), pytest.approx(0.8)]
        assert out.bins["fog|morning"] == [pytest.approx(0.5)]

    def test_fifo_cap_per_bin(self):
        from balcony_solar_forecast.core.quantiles import _BIN_RING_CAP

        st = QuantileState(bins={"clear|noon": [float(i) for i in range(_BIN_RING_CAP)]})
        # Feed a few more so we exceed the cap; oldest must be dropped.
        extra = [
            QuantileSample("clear", "noon", 10.0, 100.0) for _ in range(3)
        ]
        out = train_quantiles(st, extra)
        assert len(out.bins["clear|noon"]) == _BIN_RING_CAP
        # Oldest three (0,1,2) dropped; newest three appended.
        assert out.bins["clear|noon"][0] == pytest.approx(3.0)

    def test_empty_samples_returns_equivalent_state(self):
        st = QuantileState(bins={"clear|noon": [0.9, 1.1]})
        out = train_quantiles(st, [])
        assert out.bins == {"clear|noon": [0.9, 1.1]}
        assert out.version == st.version

    def test_none_state_starts_empty(self):
        s = QuantileSample("clear", "noon", 90.0, 100.0)
        out = train_quantiles(None, [s])  # type: ignore[arg-type]
        assert out.bins == {"clear|noon": [pytest.approx(0.9)]}

    def test_roundtrip_through_store_dict(self):
        st = QuantileState()
        out = train_quantiles(
            st, [QuantileSample("clear", "noon", 90.0, 100.0)]
        )
        restored = QuantileState.from_dict(out.to_dict())
        assert restored.bins == out.bins


# ---------------------------------------------------------------------------
# apply_bands — hourly Wh application
# ---------------------------------------------------------------------------


class TestApplyBands:
    def test_multiplies_each_hour(self):
        hourly = {"2026-07-06T10:00:00+00:00": 100.0}
        band = QuantileBands(p10=0.8, p50=1.0, p90=1.3, n=40)
        p10, p50, p90 = apply_bands(hourly, {"2026-07-06T10:00:00+00:00": band})
        assert p10["2026-07-06T10:00:00+00:00"] == pytest.approx(80.0)
        assert p50["2026-07-06T10:00:00+00:00"] == pytest.approx(100.0)
        assert p90["2026-07-06T10:00:00+00:00"] == pytest.approx(130.0)

    def test_unbanded_hour_passes_through(self):
        hourly = {"h1": 50.0, "h2": 70.0}
        band = QuantileBands(p10=0.5, p50=1.0, p90=1.5, n=40)
        p10, p50, p90 = apply_bands(hourly, {"h1": band})
        # h1 banded, h2 pass-through in all three.
        assert p10["h1"] == pytest.approx(25.0)
        assert p10["h2"] == 70.0
        assert p50["h2"] == 70.0
        assert p90["h2"] == 70.0

    def test_input_untouched_and_keys_preserved(self):
        hourly = {"h1": 10.0, "h2": 20.0}
        p10, p50, p90 = apply_bands(hourly, {})
        assert hourly == {"h1": 10.0, "h2": 20.0}
        assert set(p10) == set(p50) == set(p90) == {"h1", "h2"}

    def test_empty_and_bad_inputs(self):
        assert apply_bands({}, {}) == ({}, {}, {})
        assert apply_bands(None, None) == ({}, {}, {})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# band_curve_from_corrected — 15-min watts frame (engine hook)
# ---------------------------------------------------------------------------


class TestBandCurveFromCorrected:
    def test_per_slot_multiplication(self):
        t0 = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)
        starts = [t0, t0 + timedelta(minutes=15)]
        watts = [400.0, 800.0]
        band = QuantileBands(p10=0.5, p50=1.0, p90=1.5, n=40)
        band_by_slot = {t0: band, starts[1]: band}
        p10, p50, p90 = band_curve_from_corrected(watts, starts, band_by_slot)
        assert p10 == (pytest.approx(200.0), pytest.approx(400.0))
        assert p50 == (pytest.approx(400.0), pytest.approx(800.0))
        assert p90 == (pytest.approx(600.0), pytest.approx(1200.0))

    def test_unbanded_slot_passes_through(self):
        t0 = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)
        starts = [t0, t0 + timedelta(minutes=15)]
        watts = [400.0, 800.0]
        band = QuantileBands(p10=0.5, p50=1.0, p90=1.5, n=40)
        # Only the first slot is banded.
        p10, p50, p90 = band_curve_from_corrected(watts, starts, {t0: band})
        assert p10 == (pytest.approx(200.0), 800.0)
        assert p90 == (pytest.approx(600.0), 800.0)

    def test_empty_band_map_is_identity(self):
        t0 = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)
        starts = [t0, t0 + timedelta(minutes=15)]
        watts = [400.0, 800.0]
        p10, p50, p90 = band_curve_from_corrected(watts, starts, {})
        assert p10 == p50 == p90 == (400.0, 800.0)

    def test_alignment_to_slot_starts(self):
        t0 = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)
        starts = [t0 + timedelta(minutes=15 * i) for i in range(4)]
        watts = [100.0, 200.0, 300.0, 400.0]
        band = QuantileBands(p10=0.9, p50=1.0, p90=1.1, n=40)
        band_by_slot = {s: band for s in starts}
        p10, p50, p90 = band_curve_from_corrected(watts, starts, band_by_slot)
        assert len(p10) == len(p50) == len(p90) == 4
        assert p90[3] == pytest.approx(440.0)


# ---------------------------------------------------------------------------
# End-to-end: train -> bands -> apply (nightly-update folds a day correctly)
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_nightly_update_then_bands(self):
        # A day of hourly errors in one bin, engine biased low by ~10%.
        st = QuantileState()
        samples = [
            QuantileSample("clear", "noon", 90.0 + i, 100.0)
            for i in range(QUANTILE_MIN_SAMPLES + 5)
        ]
        st = train_quantiles(st, samples)
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        assert not b.collapsed
        assert b.p10 <= b.p50 <= b.p90
        # Median relerr is around 0.9x .. > 1.0x given the ramp; sane band.
        assert 0.5 < b.p10 <= b.p90 < 2.0

    def test_train_then_apply_to_curve(self):
        st = QuantileState()
        st = train_quantiles(
            st,
            [QuantileSample("clear", "noon", 120.0, 100.0)] * QUANTILE_MIN_SAMPLES,
        )
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        # All samples identical -> collapsed to 1.2, no spread.
        assert b.p10 == b.p50 == b.p90 == pytest.approx(1.2)
        p10, p50, p90 = apply_bands({"h": 100.0}, {"h": b})
        assert p50["h"] == pytest.approx(120.0)
