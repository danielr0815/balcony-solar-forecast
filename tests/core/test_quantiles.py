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

from datetime import UTC, date, datetime, timedelta

import pytest
from balcony_solar_forecast.const import (
    QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN,
    QUANTILE_MIN_DAYS,
    QUANTILE_MIN_FORECAST_WH,
    QUANTILE_MIN_SAMPLES,
    QUANTILE_NEUTRAL_MULT,
    QUANTILE_P_HIGH,
    QUANTILE_P_LOW,
    QUANTILE_P_MID,
    QUANTILE_REL_ERR_MAX,
    QUANTILE_REL_ERR_MIN,
    QUANTILE_RING_DAYS,
)
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


def _ring(value: float, count: int) -> list[list]:
    """A directly-constructed bin ring: ``count`` samples of ``value``, each on a
    DISTINCT ISO date so the QUANTILE_MIN_DAYS day-diversity gate is satisfied.

    Uses the ``[iso_date, relerr]`` pair storage shape. (Readers also tolerate
    bare floats, but dating keeps these conditioning fixtures clear of the day
    gate so they exercise the intended bin-selection / percentile behaviour.)
    """
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        [(base + timedelta(days=i)).date().isoformat(), value] for i in range(count)
    ]


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
        # At QUANTILE_MIN_SAMPLES samples AND with each on a distinct day (so the
        # day-diversity gate is satisfied), the bin emits its empirical spread.
        n = QUANTILE_MIN_SAMPLES
        base = datetime(2026, 1, 1, tzinfo=UTC)
        ring = [
            [(base + timedelta(days=i)).date().isoformat(), 0.5 + (i / (n - 1))]
            for i in range(n)
        ]  # 0.5 .. 1.5 spread across n distinct days
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
        out = train_quantiles(st, [s], training_date="2026-07-10")
        # Stored as a [iso_date, relerr] pair.
        assert out.bins == {"clear|noon": [["2026-07-10", pytest.approx(0.9)]]}

    def test_undated_call_stores_empty_date(self):
        # No training_date -> the sample is stored un-dated ("").
        st = QuantileState()
        s = QuantileSample("clear", "noon", 90.0, 100.0)
        out = train_quantiles(st, [s])
        assert out.bins == {"clear|noon": [["", pytest.approx(0.9)]]}

    def test_input_state_untouched(self):
        # Legacy bare-float input ring: input stays bare/untouched, output is a
        # fresh ring normalised to the pair shape (legacy -> ["", v]).
        st = QuantileState(bins={"clear|noon": [1.0]})
        s = QuantileSample("clear", "noon", 50.0, 100.0)
        out = train_quantiles(st, [s], training_date="2026-07-10")
        assert st.bins["clear|noon"] == [1.0]
        assert out.bins["clear|noon"] == [
            ["", pytest.approx(1.0)],
            ["2026-07-10", pytest.approx(0.5)],
        ]
        assert out.bins["clear|noon"] is not st.bins["clear|noon"]

    def test_relerr_computed_and_clamped_high(self):
        st = QuantileState()
        # measured 5000, corrected 100 -> relerr 50, clamp to REL_ERR_MAX.
        s = QuantileSample("clear", "noon", 5000.0, 100.0)
        out = train_quantiles(st, [s], training_date="2026-07-10")
        assert out.bins["clear|noon"] == [["2026-07-10", QUANTILE_REL_ERR_MAX]]

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
        out = train_quantiles(st, bad + [good], training_date="2026-07-10")
        assert out.bins == {"clear|noon": [["2026-07-10", pytest.approx(0.8)]]}

    def test_multiple_bins_and_multiple_samples(self):
        st = QuantileState()
        samples = [
            QuantileSample("clear", "noon", 100.0, 100.0),
            QuantileSample("clear", "noon", 80.0, 100.0),
            QuantileSample("fog", "morning", 50.0, 100.0),
        ]
        out = train_quantiles(st, samples, training_date="2026-07-10")
        assert out.bins["clear|noon"] == [
            ["2026-07-10", pytest.approx(1.0)],
            ["2026-07-10", pytest.approx(0.8)],
        ]
        assert out.bins["fog|morning"] == [["2026-07-10", pytest.approx(0.5)]]

    def test_fifo_cap_per_bin(self):
        from balcony_solar_forecast.core.quantiles import _BIN_RING_CAP

        # Legacy un-dated ring at the cap; feed a few undated more so we exceed it.
        st = QuantileState(bins={"clear|noon": [float(i) for i in range(_BIN_RING_CAP)]})
        extra = [
            QuantileSample("clear", "noon", 10.0, 100.0) for _ in range(3)
        ]
        out = train_quantiles(st, extra)  # undated call: no date trim, count cap only
        assert len(out.bins["clear|noon"]) == _BIN_RING_CAP
        # Oldest three (0,1,2) dropped (all un-dated -> plain FIFO); newest kept.
        assert out.bins["clear|noon"][0][1] == pytest.approx(3.0)

    def test_empty_samples_returns_equivalent_state(self):
        # A legacy bare-float ring is normalised to the pair shape on copy, but no
        # bin is touched so nothing is trimmed and the relerr values are preserved.
        st = QuantileState(bins={"clear|noon": [0.9, 1.1]})
        out = train_quantiles(st, [])
        assert out.bins == {"clear|noon": [["", 0.9], ["", 1.1]]}
        assert out.version == st.version

    def test_none_state_starts_empty(self):
        s = QuantileSample("clear", "noon", 90.0, 100.0)
        out = train_quantiles(None, [s], training_date="2026-07-10")  # type: ignore[arg-type]
        assert out.bins == {"clear|noon": [["2026-07-10", pytest.approx(0.9)]]}

    def test_roundtrip_through_store_dict(self):
        st = QuantileState()
        out = train_quantiles(
            st, [QuantileSample("clear", "noon", 90.0, 100.0)],
            training_date="2026-07-10",
        )
        restored = QuantileState.from_dict(out.to_dict())
        assert restored.bins == out.bins


# ---------------------------------------------------------------------------
# QuantileState.from_dict — dated pairs + legacy bare-float tolerance
# ---------------------------------------------------------------------------


class TestQuantileStateFromDict:
    def test_legacy_bare_floats_become_undated_pairs_clamped(self):
        # Pre-fix blob: a bin is a plain list of numbers. Each normalises to an
        # undated ["", relerr] pair, clamped to the sane band.
        blob = {"version": 1, "bins": {"clear|noon": [0.8, 99.0, -5.0, 1.1]}}
        qs = QuantileState.from_dict(blob)
        assert qs.bins == {
            "clear|noon": [
                ["", 0.8],
                ["", QUANTILE_REL_ERR_MAX],  # 99 clamped
                ["", QUANTILE_REL_ERR_MIN],  # -5 clamped
                ["", 1.1],
            ]
        }
        # Round-trips to the pair form.
        assert QuantileState.from_dict(qs.to_dict()).bins == qs.bins

    def test_mixed_legacy_and_dated_entries_parse(self):
        blob = {
            "version": 1,
            "bins": {
                "clear|noon": [0.9, ["2026-07-10", 1.2], ("2026-07-11", 0.7)],
            },
        }
        qs = QuantileState.from_dict(blob)
        assert qs.bins == {
            "clear|noon": [
                ["", 0.9],
                ["2026-07-10", 1.2],
                ["2026-07-11", 0.7],
            ]
        }

    def test_malformed_entries_dropped(self):
        blob = {
            "version": 1,
            "bins": {
                "clear|noon": [
                    "junk",                     # non-numeric bare
                    ["2026-07-10"],             # wrong-length pair
                    ["2026-07-10", "x"],        # non-numeric relerr
                    [1, 2, 3],                  # 3-list
                    ["2026-07-10", 1.3],        # good
                ],
                "fog|morning": "not-a-list",    # whole bin dropped
            },
        }
        qs = QuantileState.from_dict(blob)
        assert qs.bins == {"clear|noon": [["2026-07-10", 1.3]]}

    def test_non_str_date_coerced_to_undated(self):
        blob = {"version": 1, "bins": {"clear|noon": [[123, 1.1]]}}
        qs = QuantileState.from_dict(blob)
        assert qs.bins == {"clear|noon": [["", 1.1]]}


# ---------------------------------------------------------------------------
# Day-diversity collapse gate (audit #15): distinct-day evidence, not hours
# ---------------------------------------------------------------------------


class TestDayGate:
    def test_bursty_single_day_stays_collapsed(self):
        # THE regression: many samples (>= QUANTILE_MIN_SAMPLES) all on ONE date
        # are strongly correlated hours of one sky -> still collapsed to neutral.
        n = QUANTILE_MIN_SAMPLES + 10
        ring = [["2026-07-10", 0.5 + 0.03 * i] for i in range(n)]
        st = QuantileState(bins={"clear|noon": ring})
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        assert b.collapsed
        assert b.p10 == b.p50 == b.p90 == QUANTILE_NEUTRAL_MULT

    def test_spread_over_min_days_uncollapses(self):
        # >= QUANTILE_MIN_SAMPLES samples spread over >= QUANTILE_MIN_DAYS distinct
        # days -> both gates satisfied, real spread emitted.
        base = datetime(2026, 6, 1, tzinfo=UTC)
        ring = []
        for d in range(QUANTILE_MIN_DAYS):
            iso = (base + timedelta(days=d)).date().isoformat()
            ring += [[iso, 0.5 + 0.05 * (d * 4 + h)] for h in range(4)]
        assert len(ring) == QUANTILE_MIN_SAMPLES  # 5 days x 4 = 20
        st = QuantileState(bins={"clear|noon": ring})
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        assert not b.collapsed
        assert b.p10 < b.p50 < b.p90

    def test_one_day_short_stays_collapsed(self):
        # QUANTILE_MIN_DAYS - 1 distinct days, still >= QUANTILE_MIN_SAMPLES: the
        # sample-count gate passes but the day gate does not -> collapsed.
        base = datetime(2026, 6, 1, tzinfo=UTC)
        ring = []
        for d in range(QUANTILE_MIN_DAYS - 1):
            iso = (base + timedelta(days=d)).date().isoformat()
            ring += [[iso, 0.5 + 0.03 * (d * 6 + h)] for h in range(6)]
        assert len(ring) >= QUANTILE_MIN_SAMPLES
        st = QuantileState(bins={"clear|noon": ring})
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        assert b.collapsed

    def test_grandfather_undated_lower_bound_passes(self):
        # A live install's pre-upgrade ring is all un-dated. 40 un-dated samples
        # give effective_days = ceil(40 / cap) = ceil(40/8) = 5 == QUANTILE_MIN_DAYS,
        # so the grandfathered ring keeps its band active across the upgrade.
        assert QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN == 8  # anchors the arithmetic
        n = 5 * QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN  # 40
        ring = [0.5 + 0.02 * i for i in range(n)]  # legacy bare floats, spread
        st = QuantileState(bins={"clear|noon": ring})
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        assert not b.collapsed
        assert b.p10 < b.p50 < b.p90

    def test_grandfather_one_short_stays_collapsed(self):
        # One un-dated sample short of the lower bound: ceil(39/8) = 5? No —
        # 39/8 = 4.875 -> ceil 5. Use 32 (= 4 x cap) -> ceil(32/8) = 4 < MIN_DAYS.
        n = (QUANTILE_MIN_DAYS - 1) * QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN  # 32
        ring = [0.5 + 0.02 * i for i in range(n)]
        assert n >= QUANTILE_MIN_SAMPLES  # sample-count gate would otherwise pass
        st = QuantileState(bins={"clear|noon": ring})
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        assert b.collapsed


# ---------------------------------------------------------------------------
# Date-window trim + count-cap backstop (audit #15)
# ---------------------------------------------------------------------------


class TestDateWindowTrim:
    def test_old_dated_samples_evicted_new_kept(self):
        train_day = date(2026, 7, 10)
        inside = (train_day - timedelta(days=QUANTILE_RING_DAYS - 1)).isoformat()
        on_edge = (train_day - timedelta(days=QUANTILE_RING_DAYS)).isoformat()
        stale = (train_day - timedelta(days=QUANTILE_RING_DAYS + 1)).isoformat()
        st = QuantileState(
            bins={
                "clear|noon": [
                    [stale, 0.2],     # older than the window -> evicted
                    [on_edge, 0.3],   # exactly RING_DAYS old -> kept (not < cutoff)
                    [inside, 0.4],    # inside the window -> kept
                ]
            }
        )
        # Touch the bin with today's sample so it is trimmed.
        out = train_quantiles(
            st, [QuantileSample("clear", "noon", 90.0, 100.0)],
            training_date=train_day.isoformat(),
        )
        dates = [e[0] for e in out.bins["clear|noon"]]
        assert stale not in dates
        assert on_edge in dates
        assert inside in dates
        assert train_day.isoformat() in dates

    def test_undated_survive_date_trim(self):
        train_day = date(2026, 7, 10)
        st = QuantileState(
            bins={
                "clear|noon": [
                    0.2,  # legacy un-dated: unknown age, NOT date-trimmed
                    [(train_day - timedelta(days=QUANTILE_RING_DAYS + 5)).isoformat(), 0.9],
                ]
            }
        )
        out = train_quantiles(
            st, [QuantileSample("clear", "noon", 80.0, 100.0)],
            training_date=train_day.isoformat(),
        )
        ring = out.bins["clear|noon"]
        # The stale DATED sample is gone; the un-dated one survives.
        assert ["", 0.2] in ring
        assert all(e[0] != "" for e in ring if e != ["", 0.2])

    def test_count_cap_evicts_undated_before_dated(self):
        from balcony_solar_forecast.core.quantiles import _BIN_RING_CAP

        train_day = date(2026, 7, 10)
        recent = (train_day - timedelta(days=1)).isoformat()
        # Fill the ring exactly to the cap with recent DATED samples, then prepend
        # a few un-dated legacy samples so we are over the cap by that many.
        dated = [[recent, 1.0] for _ in range(_BIN_RING_CAP)]
        undated = [0.1, 0.2, 0.3]  # 3 legacy bare floats
        st = QuantileState(bins={"clear|noon": undated + dated})
        out = train_quantiles(
            st, [QuantileSample("clear", "noon", 90.0, 100.0)],
            training_date=train_day.isoformat(),
        )
        ring = out.bins["clear|noon"]
        assert len(ring) == _BIN_RING_CAP
        # All three un-dated samples evicted first (least trustworthy).
        assert all(e[0] != "" for e in ring)


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
        # A WEEK of hourly errors in one bin (engine biased low by ~10%), folded
        # one training day at a time — so both the sample-count AND the
        # day-diversity gate are cleared before the band emits a spread.
        st = QuantileState()
        base = datetime(2026, 6, 1, tzinfo=UTC)
        for d in range(6):  # 6 distinct days x 5 hourly samples = 30 samples
            iso = (base + timedelta(days=d)).date().isoformat()
            samples = [
                QuantileSample("clear", "noon", 90.0 + i, 100.0) for i in range(5)
            ]
            st = train_quantiles(st, samples, training_date=iso)
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        assert not b.collapsed
        assert b.p10 <= b.p50 <= b.p90
        # Median relerr is around 0.9x .. > 1.0x given the ramp; sane band.
        assert 0.5 < b.p10 <= b.p90 < 2.0

    def test_train_then_apply_to_curve(self):
        # Identical samples spread over QUANTILE_MIN_DAYS distinct days: the day
        # gate is cleared, so the band reflects the data-backed median (no spread,
        # but NOT the neutral 1.0).
        st = QuantileState()
        base = datetime(2026, 6, 1, tzinfo=UTC)
        per_day = QUANTILE_MIN_SAMPLES // QUANTILE_MIN_DAYS  # 4 samples x 5 days
        for d in range(QUANTILE_MIN_DAYS):
            iso = (base + timedelta(days=d)).date().isoformat()
            st = train_quantiles(
                st, [QuantileSample("clear", "noon", 120.0, 100.0)] * per_day,
                training_date=iso,
            )
        b = bands_for_bin(st, cloud_class="clear", day_part="noon")
        # All samples identical -> no spread, but the data-backed median 1.2.
        assert b.p10 == b.p50 == b.p90 == pytest.approx(1.2)
        p10, p50, p90 = apply_bands({"h": 100.0}, {"h": b})
        assert p50["h"] == pytest.approx(120.0)
