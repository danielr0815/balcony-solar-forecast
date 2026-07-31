"""Pure tests for the skill scoreboard math (core/scoreboard.py).

Owner: scoreboard. These run with BARE pytest (no Home Assistant) — the module
imports nothing from HA. They cover the leak-free per-day scoring, the rolling-
window aggregation (engine daily-kWh MAE, engine hourly MAE), the per-weather
stratification, plus the validate-and-clamp edges (non-finite / negative
inputs, empty windows) and the legacy-load tolerance for pre-removal stores
whose DayScore blobs still carry the external-comparison fields.
"""

from __future__ import annotations

import pytest
from balcony_solar_forecast.const import (
    CLOUD_CLASS_CLEAR,
    CLOUD_CLASS_MIXED,
    CLOUD_CLASS_OVERCAST,
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
    hourly_mae: float | None = None,
) -> DayScore:
    """Build a DayScore via score_day (the leak-free arithmetic under test)."""
    return sb.score_day(
        iso_date=iso,
        weather_class=weather,
        measured_kwh=measured,
        engine_kwh=engine,
        engine_hourly_mae=hourly_mae,
    )


# ---------------------------------------------------------------------------
# score_day — leak-free per-day arithmetic
# ---------------------------------------------------------------------------


def test_score_day_absolute_errors():
    d = _day("2026-07-01", measured=10.0, engine=9.0)
    assert d.engine_daily_abs_err == pytest.approx(1.0)


def test_score_day_engine_over_and_under_are_absolute():
    over = _day("2026-07-01", measured=8.0, engine=10.0)
    under = _day("2026-07-01", measured=8.0, engine=6.0)
    assert over.engine_daily_abs_err == pytest.approx(2.0)
    assert under.engine_daily_abs_err == pytest.approx(2.0)


def test_score_day_unscorable_when_measured_or_engine_garbage():
    # A non-finite or negative MEASURED / ENGINE number leaves the day
    # UNSCORED (None), mirroring the _finite_or_none contract: the old 0.0
    # clamp fabricated a day that never happened — measured=NaN scored as
    # |engine - 0|, the engine's worst possible day, straight into the
    # scoreboard window (SPEC §15.2).
    def _scored(**kw):
        args = dict(
            iso_date="2026-07-01",
            weather_class=CLOUD_CLASS_CLEAR,
            measured_kwh=10.0,
            engine_kwh=9.0,
        )
        args.update(kw)
        return sb.score_day(**args)

    assert _scored(measured_kwh=float("nan")) is None
    assert _scored(measured_kwh=float("inf")) is None
    assert _scored(measured_kwh=-1.0) is None
    assert _scored(engine_kwh=float("nan")) is None
    assert _scored(engine_kwh=float("-inf")) is None
    assert _scored(engine_kwh=-0.5) is None
    # Valid numbers still score (no over-eager discard).
    assert _scored() is not None


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
# stratified_breakdown
# ---------------------------------------------------------------------------


def test_stratified_breakdown_per_class():
    days = [
        _day("2026-07-01", weather=CLOUD_CLASS_CLEAR, measured=10.0, engine=9.0),
        _day("2026-07-02", weather=CLOUD_CLASS_CLEAR, measured=10.0, engine=11.0),
        _day("2026-07-03", weather=CLOUD_CLASS_OVERCAST, measured=4.0, engine=6.0),
    ]
    strata = sb.stratified_breakdown(_state(days), window_days=14)
    assert set(strata) == {CLOUD_CLASS_CLEAR, CLOUD_CLASS_OVERCAST}
    assert strata[CLOUD_CLASS_CLEAR]["n"] == 2
    assert strata[CLOUD_CLASS_CLEAR]["engine_daily_kwh_mae"] == pytest.approx(1.0)
    assert strata[CLOUD_CLASS_OVERCAST]["n"] == 1
    assert strata[CLOUD_CLASS_OVERCAST]["engine_daily_kwh_mae"] == pytest.approx(2.0)


def test_stratified_breakdown_absent_class_not_present():
    days = [_day("2026-07-01", weather=CLOUD_CLASS_MIXED, measured=10.0, engine=10.0)]
    strata = sb.stratified_breakdown(_state(days), window_days=14)
    assert set(strata) == {CLOUD_CLASS_MIXED}
    assert CLOUD_CLASS_CLEAR not in strata


def test_stratified_breakdown_low_n_flag():
    # C1/SPEC-5: a stratum below SCOREBOARD_STRATUM_MIN_N scored days is marked
    # low_n=True so consumers can hide the row (on a tiny sample the figures
    # were wildly noisy, e.g. a -480 % row on n=2).
    days = [
        _day("2026-07-01", weather=CLOUD_CLASS_OVERCAST, measured=4.0, engine=6.0),
        _day("2026-07-02", weather=CLOUD_CLASS_OVERCAST, measured=4.0, engine=6.0),
    ]
    strata = sb.stratified_breakdown(_state(days), window_days=14)
    row = strata[CLOUD_CLASS_OVERCAST]
    assert row["n"] == 2
    assert row["low_n"] is True


def test_stratified_breakdown_at_min_n_not_low_n():
    # At the SCOREBOARD_STRATUM_MIN_N floor (3 scored days) low_n clears.
    days = [
        _day(f"2026-07-0{i}", weather=CLOUD_CLASS_CLEAR, measured=10.0, engine=9.0)
        for i in range(1, 4)
    ]
    strata = sb.stratified_breakdown(_state(days), window_days=14)
    row = strata[CLOUD_CLASS_CLEAR]
    assert row["n"] == 3
    assert row["low_n"] is False


# ---------------------------------------------------------------------------
# scoreboard_summary — pure assembly (engine-only key set)
# ---------------------------------------------------------------------------


def test_scoreboard_summary_bundles_all_fields():
    days = [
        _day(f"2026-07-0{i}", measured=10.0, engine=9.0, hourly_mae=50.0)
        for i in range(1, 6)
    ]
    summary = sb.scoreboard_summary(_state(days), window_days=5)
    assert summary["engine_daily_kwh_mae"] == pytest.approx(1.0)
    assert summary["engine_hourly_mae"] == pytest.approx(50.0)
    assert summary["window_days"] == 5
    assert summary["scored_days"] == 5
    assert summary["newest_scored_date"] == "2026-07-05"
    assert CLOUD_CLASS_CLEAR in summary["strata"]


def test_scoreboard_summary_exact_engine_key_set():
    # The summary carries EXACTLY the engine keys — the removed kill-gate /
    # external-comparison keys must not reappear (they broke the contract for
    # every summary consumer when the machinery was removed).
    days = [_day("2026-07-01", measured=10.0, engine=9.0, hourly_mae=50.0)]
    summary = sb.scoreboard_summary(_state(days), window_days=14)
    assert set(summary) == {
        "engine_daily_kwh_mae",
        "engine_hourly_mae",
        "window_days",
        "scored_days",
        "newest_scored_date",
        "strata",
    }


def test_scoreboard_summary_empty_state_is_neutral():
    summary = sb.scoreboard_summary(ScoreboardState(), window_days=14)
    assert summary["engine_daily_kwh_mae"] is None
    assert summary["engine_hourly_mae"] is None
    assert summary["scored_days"] == 0
    assert summary["newest_scored_date"] is None
    assert summary["strata"] == {}


# ---------------------------------------------------------------------------
# ScoreboardState.from_dict — version guard (SPEC §16.1)
# ---------------------------------------------------------------------------


def test_scoreboard_state_from_dict_discards_unknown_version(caplog):
    """An unknown / FUTURE section version is discarded to the empty state
    with a warning — never guessed at (SPEC §16.1)."""
    import logging

    blob = {
        "version": 99,
        "days": {
            "2026-07-01": {
                "iso_date": "2026-07-01",
                "weather_class": CLOUD_CLASS_CLEAR,
                "measured_kwh": 10.0,
                "engine_kwh": 9.0,
                "engine_daily_abs_err": 1.0,
            }
        },
    }
    with caplog.at_level(logging.WARNING):
        st = ScoreboardState.from_dict(blob)
    assert st.days == {}
    assert st.version == 1
    assert any("version" in r.message.lower() for r in caplog.records)
    # The current version round-trips untouched (no over-eager discard).
    ok = ScoreboardState.from_dict(blob | {"version": 1})
    assert ok.days["2026-07-01"].engine_kwh == pytest.approx(9.0)


def test_day_score_from_dict_ignores_legacy_comparison_fields():
    """SPEC §16.1 store-migration invariant: a DayScore blob written BEFORE the
    external-comparison machinery was removed still carries ``comparison_kwh``
    / ``comparison_daily_abs_err`` — it must load cleanly, with the legacy keys
    ignored (never resurrected in the round-trip)."""
    legacy = {
        "iso_date": "2026-07-01",
        "weather_class": CLOUD_CLASS_CLEAR,
        "measured_kwh": 10.0,
        "engine_kwh": 9.0,
        "engine_daily_abs_err": 1.0,
        "comparison_kwh": {"8-Entry Baseline": 12.0},
        "comparison_daily_abs_err": {"8-Entry Baseline": 2.0},
        "engine_hourly_mae": 42.0,
    }
    d = DayScore.from_dict(legacy)
    assert d.iso_date == "2026-07-01"
    assert d.measured_kwh == pytest.approx(10.0)
    assert d.engine_daily_abs_err == pytest.approx(1.0)
    assert d.engine_hourly_mae == pytest.approx(42.0)
    out = d.to_dict()
    assert "comparison_kwh" not in out
    assert "comparison_daily_abs_err" not in out
