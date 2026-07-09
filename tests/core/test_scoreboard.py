"""Pure tests for the skill scoreboard math (core/scoreboard.py).

Owner: scoreboard. These run with BARE pytest (no Home Assistant) — the module
imports nothing from HA. They cover the leak-free per-day scoring, the rolling-
window aggregation (engine + per-comparison daily-kWh MAE, engine hourly MAE),
the engine-vs-best-baseline percent, the per-weather stratification and the
kill-gate verdict (pending vs pass vs fail), plus the validate-and-clamp edges
(non-finite / negative inputs, empty windows, missing comparisons).
"""

from __future__ import annotations

import math

import pytest
from balcony_solar_forecast.const import (
    CLOUD_CLASS_CLEAR,
    CLOUD_CLASS_MIXED,
    CLOUD_CLASS_OVERCAST,
    DEFAULT_SCOREBOARD_GATE_MARGIN,
    SCOREBOARD_MIN_WINDOW_DAYS,
)
from balcony_solar_forecast.core import scoreboard as sb
from balcony_solar_forecast.core.types import (
    DayScore,
    ScoreboardState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(days: list[DayScore]) -> ScoreboardState:
    return ScoreboardState(days={d.iso_date: d for d in days})


def _day(
    iso: str,
    *,
    weather: str = CLOUD_CLASS_CLEAR,
    measured: float,
    engine: float,
    comparisons: dict[str, float] | None = None,
    hourly_mae: float | None = None,
) -> DayScore:
    """Build a DayScore via score_day (the leak-free arithmetic under test)."""
    return sb.score_day(
        iso_date=iso,
        weather_class=weather,
        measured_kwh=measured,
        engine_kwh=engine,
        comparison_kwh=comparisons or {},
        engine_hourly_mae=hourly_mae,
    )


# ---------------------------------------------------------------------------
# score_day — leak-free per-day arithmetic
# ---------------------------------------------------------------------------


def test_score_day_absolute_errors():
    d = _day(
        "2026-07-01",
        measured=10.0,
        engine=9.0,
        comparisons={"base": 12.0, "alt": 7.0},
    )
    assert d.engine_daily_abs_err == pytest.approx(1.0)
    assert d.comparison_daily_abs_err["base"] == pytest.approx(2.0)
    assert d.comparison_daily_abs_err["alt"] == pytest.approx(3.0)
    assert d.comparison_kwh["base"] == pytest.approx(12.0)


def test_score_day_engine_over_and_under_are_absolute():
    over = _day("2026-07-01", measured=8.0, engine=10.0)
    under = _day("2026-07-01", measured=8.0, engine=6.0)
    assert over.engine_daily_abs_err == pytest.approx(2.0)
    assert under.engine_daily_abs_err == pytest.approx(2.0)


def test_score_day_missing_comparison_is_absent_not_zero():
    # A comparison whose value is None (recorder had nothing) must be ABSENT,
    # never scored as |0 - measured| (which would flatter the engine).
    d = _day(
        "2026-07-01",
        measured=10.0,
        engine=9.5,
        comparisons={"present": 11.0, "missing": None},  # type: ignore[dict-item]
    )
    assert "present" in d.comparison_daily_abs_err
    assert "missing" not in d.comparison_daily_abs_err
    assert "missing" not in d.comparison_kwh


def test_score_day_drops_non_finite_and_negative_comparisons():
    # measured/engine degrade to 0.0 (day still scored), but a non-finite /
    # negative COMPARISON is DROPPED, never clamped to a fabricated 0.0 (which
    # would charge the baseline the whole measured energy and unfairly inflate
    # the engine's edge; a missing comparison is ABSENT, SPEC §9).
    d = _day(
        "2026-07-01",
        measured=float("nan"),
        engine=float("-inf"),
        comparisons={"c": float("inf"), "neg": -5.0, "ok": 8.0},
    )
    assert d.measured_kwh == 0.0
    assert d.engine_kwh == 0.0
    assert math.isfinite(d.engine_daily_abs_err)
    assert "c" not in d.comparison_kwh
    assert "neg" not in d.comparison_kwh
    assert d.comparison_kwh["ok"] == 8.0


def test_score_day_carries_hourly_mae():
    d = _day("2026-07-01", measured=10.0, engine=10.0, hourly_mae=42.0)
    assert d.engine_hourly_mae == pytest.approx(42.0)
    d2 = _day("2026-07-01", measured=10.0, engine=10.0, hourly_mae=None)
    assert d2.engine_hourly_mae is None


# ---------------------------------------------------------------------------
# hourly_mae
# ---------------------------------------------------------------------------


def test_hourly_mae_mean_absolute():
    issued = {"h1": 100.0, "h2": 200.0, "h3": 300.0}
    measured = {"h1": 90.0, "h2": 260.0, "h3": 300.0}
    # |10| + |60| + |0| = 70 over 3 hours
    assert sb.hourly_mae(issued, measured) == pytest.approx(70.0 / 3.0)


def test_hourly_mae_union_of_hours_counts_full_magnitude():
    # A modeled hour with no measurement contributes its full magnitude.
    issued = {"h1": 100.0, "h2": 50.0}
    measured = {"h1": 100.0}  # h2 unmeasured
    assert sb.hourly_mae(issued, measured) == pytest.approx(25.0)  # (0 + 50) / 2


def test_hourly_mae_none_when_no_hours():
    assert sb.hourly_mae({}, {}) is None


# ---------------------------------------------------------------------------
# trim_window
# ---------------------------------------------------------------------------


def test_trim_window_keeps_newest():
    days = [_day(f"2026-07-0{i}", measured=10.0, engine=10.0) for i in range(1, 6)]
    st = _state(days)
    trimmed = sb.trim_window(st, window_days=3)
    assert sorted(trimmed.days) == ["2026-07-03", "2026-07-04", "2026-07-05"]


def test_trim_window_non_positive_keeps_one():
    days = [_day(f"2026-07-0{i}", measured=10.0, engine=10.0) for i in range(1, 4)]
    st = _state(days)
    trimmed = sb.trim_window(st, window_days=0)
    assert sorted(trimmed.days) == ["2026-07-03"]


def test_trim_window_empty_state():
    trimmed = sb.trim_window(ScoreboardState(), window_days=14)
    assert trimmed.days == {}


# ---------------------------------------------------------------------------
# Rolling-window MAE aggregates
# ---------------------------------------------------------------------------


def test_engine_daily_kwh_mae_mean():
    days = [
        _day("2026-07-01", measured=10.0, engine=9.0),   # err 1
        _day("2026-07-02", measured=10.0, engine=13.0),  # err 3
    ]
    assert sb.engine_daily_kwh_mae(_state(days), window_days=14) == pytest.approx(2.0)


def test_engine_daily_kwh_mae_none_on_empty():
    assert sb.engine_daily_kwh_mae(ScoreboardState(), window_days=14) is None


def test_engine_daily_kwh_mae_respects_window():
    days = [
        _day("2026-07-01", measured=10.0, engine=0.0),   # err 10 (outside window)
        _day("2026-07-02", measured=10.0, engine=9.0),   # err 1
        _day("2026-07-03", measured=10.0, engine=11.0),  # err 1
    ]
    # window 2 keeps only the last two days -> MAE 1.0
    assert sb.engine_daily_kwh_mae(_state(days), window_days=2) == pytest.approx(1.0)


def test_comparison_mae_averaged_over_own_scored_days():
    # 'base' scored all 3 days; 'alt' only appears on day 3. 'alt' must be
    # averaged over ITS day only, not penalised for days 1-2.
    days = [
        _day("2026-07-01", measured=10.0, engine=10.0, comparisons={"base": 12.0}),
        _day("2026-07-02", measured=10.0, engine=10.0, comparisons={"base": 14.0}),
        _day(
            "2026-07-03",
            measured=10.0,
            engine=10.0,
            comparisons={"base": 12.0, "alt": 20.0},
        ),
    ]
    maes = sb.comparison_daily_kwh_mae(_state(days), window_days=14)
    assert maes["base"] == pytest.approx((2.0 + 4.0 + 2.0) / 3.0)
    assert maes["alt"] == pytest.approx(10.0)  # |20 - 10| over its single day


def test_comparison_mae_absent_when_never_scored():
    days = [_day("2026-07-01", measured=10.0, engine=10.0)]
    assert sb.comparison_daily_kwh_mae(_state(days), window_days=14) == {}


def test_engine_hourly_mae_skips_days_without_hourly():
    days = [
        _day("2026-07-01", measured=10.0, engine=10.0, hourly_mae=20.0),
        _day("2026-07-02", measured=10.0, engine=10.0, hourly_mae=None),
        _day("2026-07-03", measured=10.0, engine=10.0, hourly_mae=40.0),
    ]
    # mean of 20 and 40 (the None day is skipped, not counted as 0)
    assert sb.engine_hourly_mae(_state(days), window_days=14) == pytest.approx(30.0)


def test_engine_hourly_mae_none_when_no_hourly_days():
    days = [_day("2026-07-01", measured=10.0, engine=10.0, hourly_mae=None)]
    assert sb.engine_hourly_mae(_state(days), window_days=14) is None


# ---------------------------------------------------------------------------
# engine_vs_best_baseline_pct
# ---------------------------------------------------------------------------


def test_vs_best_baseline_positive_when_engine_better():
    # engine MAE 1, best baseline MAE 4 -> (4-1)/4 = 75% better.
    days = [
        _day("2026-07-01", measured=10.0, engine=9.0, comparisons={"b": 14.0}),
    ]
    pct = sb.engine_vs_best_baseline_pct(_state(days), window_days=14)
    assert pct == pytest.approx(75.0)


def test_vs_best_baseline_negative_when_engine_worse():
    # engine MAE 5, best baseline MAE 2 -> (2-5)/2 = -150%.
    days = [
        _day("2026-07-01", measured=10.0, engine=5.0, comparisons={"b": 12.0}),
    ]
    pct = sb.engine_vs_best_baseline_pct(_state(days), window_days=14)
    assert pct == pytest.approx(-150.0)


def test_vs_best_baseline_uses_smallest_comparison():
    # best baseline is the smallest MAE across comparisons.
    days = [
        _day(
            "2026-07-01",
            measured=10.0,
            engine=9.0,          # err 1
            comparisons={"good": 11.0, "bad": 20.0},  # errs 1 and 10
        ),
    ]
    # best baseline err = 1 -> (1-1)/1 = 0% (a tie, not a win)
    pct = sb.engine_vs_best_baseline_pct(_state(days), window_days=14)
    assert pct == pytest.approx(0.0)


def test_vs_best_baseline_none_without_comparison():
    days = [_day("2026-07-01", measured=10.0, engine=9.0)]
    assert sb.engine_vs_best_baseline_pct(_state(days), window_days=14) is None


def test_vs_best_baseline_none_when_baseline_zero():
    # A perfect baseline (MAE 0) makes the ratio undefined -> None.
    days = [
        _day("2026-07-01", measured=10.0, engine=9.0, comparisons={"perfect": 10.0}),
    ]
    assert sb.engine_vs_best_baseline_pct(_state(days), window_days=14) is None


# ---------------------------------------------------------------------------
# stratified_breakdown
# ---------------------------------------------------------------------------


def test_stratified_breakdown_per_class():
    days = [
        _day("2026-07-01", weather=CLOUD_CLASS_CLEAR, measured=10.0, engine=9.0,
             comparisons={"b": 12.0}),
        _day("2026-07-02", weather=CLOUD_CLASS_CLEAR, measured=10.0, engine=11.0,
             comparisons={"b": 13.0}),
        _day("2026-07-03", weather=CLOUD_CLASS_OVERCAST, measured=4.0, engine=6.0,
             comparisons={"b": 5.0}),
    ]
    strata = sb.stratified_breakdown(_state(days), window_days=14)
    assert set(strata) == {CLOUD_CLASS_CLEAR, CLOUD_CLASS_OVERCAST}
    assert strata[CLOUD_CLASS_CLEAR]["n"] == 2
    assert strata[CLOUD_CLASS_CLEAR]["engine_daily_kwh_mae"] == pytest.approx(1.0)
    assert strata[CLOUD_CLASS_CLEAR]["comparison_daily_kwh_mae"]["b"] == pytest.approx(
        (2.0 + 3.0) / 2.0
    )
    assert strata[CLOUD_CLASS_OVERCAST]["n"] == 1
    assert strata[CLOUD_CLASS_OVERCAST]["engine_daily_kwh_mae"] == pytest.approx(2.0)


def test_stratified_breakdown_absent_class_not_present():
    days = [_day("2026-07-01", weather=CLOUD_CLASS_MIXED, measured=10.0, engine=10.0)]
    strata = sb.stratified_breakdown(_state(days), window_days=14)
    assert set(strata) == {CLOUD_CLASS_MIXED}
    assert CLOUD_CLASS_CLEAR not in strata


# ---------------------------------------------------------------------------
# kill_gate_passed — pending / pass / fail
# ---------------------------------------------------------------------------


def test_kill_gate_pending_when_window_not_full():
    # window_days 14 but only 3 scored days -> None (undetermined).
    days = [
        _day(f"2026-07-0{i}", measured=10.0, engine=9.0, comparisons={"b": 14.0})
        for i in range(1, 4)
    ]
    assert sb.kill_gate_passed(_state(days), window_days=14, gate_margin=0.10) is None


def test_kill_gate_passes_full_window_margin_met():
    # 5 full days, engine MAE 1, baseline MAE 4 -> 75% > 10% -> True.
    days = [
        _day(f"2026-07-0{i}", measured=10.0, engine=9.0, comparisons={"b": 14.0})
        for i in range(1, 6)
    ]
    assert sb.kill_gate_passed(_state(days), window_days=5, gate_margin=0.10) is True


def test_kill_gate_fails_full_window_margin_not_met():
    # engine barely better: engine MAE 1.0, baseline MAE 1.05 -> ~4.8% < 10% -> False.
    days = [
        _day(f"2026-07-0{i}", measured=10.0, engine=9.0, comparisons={"b": 8.95})
        for i in range(1, 6)
    ]
    verdict = sb.kill_gate_passed(_state(days), window_days=5, gate_margin=0.10)
    assert verdict is False


def test_kill_gate_fails_when_engine_worse():
    days = [
        _day(f"2026-07-0{i}", measured=10.0, engine=5.0, comparisons={"b": 11.0})
        for i in range(1, 6)
    ]
    assert sb.kill_gate_passed(_state(days), window_days=5, gate_margin=0.10) is False


def test_kill_gate_undetermined_full_window_without_comparison():
    # A full window but NO comparison ever scored is "no baseline data", NOT an
    # engine loss: the verdict is UNDETERMINED (None), never a false FAIL (a
    # stock/empty-comparison install, a typo'd/renamed entity or a purged
    # recorder must not read as "engine lost"). SPEC §9.
    days = [
        _day(f"2026-07-0{i}", measured=10.0, engine=9.0) for i in range(1, 6)
    ]
    assert sb.kill_gate_passed(_state(days), window_days=5, gate_margin=0.10) is None


def test_kill_gate_margin_boundary_is_inclusive():
    # Exactly at the margin (10%) passes (>=).
    # engine MAE 9, baseline MAE 10 -> (10-9)/10 = 10.0% == margin*100.
    days = [
        _day(f"2026-07-0{i}", measured=0.0, engine=9.0, comparisons={"b": 10.0})
        for i in range(1, 6)
    ]
    assert sb.kill_gate_passed(_state(days), window_days=5, gate_margin=0.10) is True


def test_kill_gate_respects_min_window_days():
    # window_days smaller than SCOREBOARD_MIN_WINDOW_DAYS still requires the min.
    assert SCOREBOARD_MIN_WINDOW_DAYS >= 1
    empty = ScoreboardState()
    assert (
        sb.kill_gate_passed(empty, window_days=0, gate_margin=0.10) is None
    )


# ---------------------------------------------------------------------------
# scoreboard_summary — pure assembly
# ---------------------------------------------------------------------------


def test_scoreboard_summary_bundles_all_fields():
    days = [
        _day(f"2026-07-0{i}", measured=10.0, engine=9.0, comparisons={"b": 14.0},
             hourly_mae=50.0)
        for i in range(1, 6)
    ]
    summary = sb.scoreboard_summary(
        _state(days), window_days=5, gate_margin=DEFAULT_SCOREBOARD_GATE_MARGIN
    )
    assert summary["engine_daily_kwh_mae"] == pytest.approx(1.0)
    assert summary["engine_hourly_mae"] == pytest.approx(50.0)
    assert summary["comparison_daily_kwh_mae"]["b"] == pytest.approx(4.0)
    assert summary["engine_vs_best_baseline_pct"] == pytest.approx(75.0)
    assert summary["kill_gate_passed"] is True
    assert summary["window_days"] == 5
    assert summary["scored_days"] == 5
    assert CLOUD_CLASS_CLEAR in summary["strata"]


def test_scoreboard_summary_empty_state_is_neutral():
    summary = sb.scoreboard_summary(
        ScoreboardState(), window_days=14, gate_margin=0.10
    )
    assert summary["engine_daily_kwh_mae"] is None
    assert summary["engine_hourly_mae"] is None
    assert summary["comparison_daily_kwh_mae"] == {}
    assert summary["engine_vs_best_baseline_pct"] is None
    assert summary["kill_gate_passed"] is None
    assert summary["scored_days"] == 0
    assert summary["strata"] == {}
