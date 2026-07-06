"""Tests for the FAST learner (intraday scalar + day-ahead RLS bias).

Pure pytest, no Home Assistant imports (SPEC §4). Owner: bias.

Covers every edge case the contract calls out:
  - intraday: no data / too little coverage -> neutral; k_c-space geometry
    cancellation (plane mix invariance); exponential recency weighting;
    modeled-Wh gate; future / stale-window / clock-jump samples; clamps;
    linear forward decay in apply; past hours untouched; corrupt keys/values.
  - day-ahead: cloud classification (fog precedence, thresholds, NaN inputs);
    day-part mapping incl. out-of-range hours; RLS convergence toward the true
    multiplicative bias, numerical stability, clamps, min-sample gate, junk /
    dark-day rejection, purity (input state untouched), unknown class/part.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from balcony_solar_forecast.const import (
    CLOUD_CLASS_CLEAR,
    CLOUD_CLASS_FOG,
    CLOUD_CLASS_MIXED,
    CLOUD_CLASS_OVERCAST,
    DAY_AHEAD_BIAS_MAX,
    DAY_AHEAD_BIAS_MIN,
    DAY_AHEAD_BIAS_NEUTRAL,
    DAY_PART_AFTERNOON,
    DAY_PART_MIDDAY,
    DAY_PART_MORNING,
    INTRADAY_APPLY_HORIZON_MINUTES,
    INTRADAY_MIN_MODELED_WH,
    INTRADAY_NEUTRAL,
    INTRADAY_SCALAR_MAX,
    INTRADAY_SCALAR_MIN,
    INTRADAY_TAU_MINUTES,
    RLS_MIN_SAMPLES,
)
from balcony_solar_forecast.core.bias import (
    DayAheadSample,
    IntradaySample,
    apply_day_ahead_bias,
    apply_intraday_scalar,
    classify_cloud,
    compute_intraday_scalar,
    day_part_for_hour,
    train_day_ahead_bias,
)
from balcony_solar_forecast.core.types import BiasCell, BiasState

UTC = timezone.utc


# ===========================================================================
# Helpers
# ===========================================================================


def _now() -> datetime:
    return datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _trailing_samples(
    now: datetime,
    *,
    ratio: float,
    n: int = 12,
    step_min: int = 15,
    modeled_kc: float = 1.0,
    modeled_wh: float = 100.0,
    start_age_min: int = 5,
) -> list[IntradaySample]:
    """Build a run of samples covering the trailing window at a fixed ratio.

    ``ratio`` = measured_kc / modeled_kc for every sample. Ages run from
    ``start_age_min`` back in ``step_min`` increments (so coverage spans
    (n-1)*step_min minutes).
    """
    out: list[IntradaySample] = []
    for i in range(n):
        age = start_age_min + i * step_min
        out.append(
            IntradaySample(
                at=now - timedelta(minutes=age),
                measured_kc=modeled_kc * ratio,
                modeled_kc=modeled_kc,
                modeled_wh=modeled_wh,
            )
        )
    return out


# ===========================================================================
# compute_intraday_scalar
# ===========================================================================


def test_intraday_empty_is_neutral():
    assert compute_intraday_scalar([], now=_now()) == INTRADAY_NEUTRAL


def test_intraday_insufficient_coverage_is_neutral():
    now = _now()
    # Two very recent samples only -> span < INTRADAY_MIN_TRAILING_MINUTES.
    samples = _trailing_samples(now, ratio=1.4, n=2, step_min=15)
    assert compute_intraday_scalar(samples, now=now) == INTRADAY_NEUTRAL


def test_intraday_constant_ratio_recovers_ratio():
    now = _now()
    samples = _trailing_samples(now, ratio=1.4, n=12, step_min=15)
    s = compute_intraday_scalar(samples, now=now)
    assert s == pytest.approx(1.4, rel=1e-9)


def test_intraday_ratio_below_one():
    now = _now()
    samples = _trailing_samples(now, ratio=0.6, n=12, step_min=15)
    assert compute_intraday_scalar(samples, now=now) == pytest.approx(0.6, rel=1e-9)


def test_intraday_geometry_cancels_plane_mix_invariance():
    """k_c-space ratio-of-sums must be identical for different plane mixes.

    Same measured/modeled proportion, wildly different modeled magnitudes per
    slot (simulating different plane geometry contributing different energy):
    the recovered scalar is unchanged.
    """
    now = _now()
    ratio = 1.25
    uniform = _trailing_samples(now, ratio=ratio, n=12, step_min=15, modeled_kc=1.0)
    # Now a very uneven "plane mix": vary modeled_kc per slot but keep the same
    # measured/modeled proportion everywhere.
    uneven: list[IntradaySample] = []
    for i, s in enumerate(uniform):
        mk = 0.3 + 0.5 * (i % 4)  # 0.3, 0.8, 1.3, 1.8 cycling
        uneven.append(
            IntradaySample(
                at=s.at,
                measured_kc=mk * ratio,
                modeled_kc=mk,
                modeled_wh=100.0,
            )
        )
    su = compute_intraday_scalar(uniform, now=now)
    sv = compute_intraday_scalar(uneven, now=now)
    assert su == pytest.approx(ratio, rel=1e-9)
    assert sv == pytest.approx(ratio, rel=1e-9)
    assert su == pytest.approx(sv, rel=1e-9)


def test_intraday_recency_weighting():
    """Recent samples dominate: a step change is pulled toward the recent value."""
    now = _now()
    samples: list[IntradaySample] = []
    # Older half ratio 0.5, recent half ratio 1.5, evenly spread over window.
    for i in range(12):
        age = 5 + i * 18  # spans ~200 min
        ratio = 1.5 if age < 100 else 0.5
        samples.append(
            IntradaySample(
                at=now - timedelta(minutes=age),
                measured_kc=ratio,
                modeled_kc=1.0,
                modeled_wh=100.0,
            )
        )
    s = compute_intraday_scalar(samples, now=now)
    # Recency-weighted mean must sit strictly above the unweighted mean (1.0)
    # because the recent samples (1.5) carry more weight than the old (0.5).
    assert s > 1.0


def test_intraday_tau_weighting_matches_manual():
    """Two samples: verify the exact exp-weighted ratio-of-sums formula."""
    now = _now()
    # One at age 60 min (ratio 2.0), one at age 0 min (ratio 1.0). Coverage
    # only 60 min < min-trailing, so add filler at the extremes for coverage.
    a_age, a_ratio = 0.0, 1.0
    b_age, b_ratio = 130.0, 2.0
    samples = [
        IntradaySample(at=now - timedelta(minutes=a_age), measured_kc=a_ratio,
                       modeled_kc=1.0, modeled_wh=100.0),
        IntradaySample(at=now - timedelta(minutes=b_age), measured_kc=b_ratio,
                       modeled_kc=1.0, modeled_wh=100.0),
    ]
    wa = math.exp(-a_age / INTRADAY_TAU_MINUTES)
    wb = math.exp(-b_age / INTRADAY_TAU_MINUTES)
    expected = (wa * a_ratio + wb * b_ratio) / (wa + wb)
    s = compute_intraday_scalar(samples, now=now)
    assert s == pytest.approx(expected, rel=1e-9)


def test_intraday_clamp_high():
    now = _now()
    samples = _trailing_samples(now, ratio=10.0, n=12, step_min=15)
    assert compute_intraday_scalar(samples, now=now) == INTRADAY_SCALAR_MAX


def test_intraday_clamp_low():
    now = _now()
    samples = _trailing_samples(now, ratio=0.01, n=12, step_min=15)
    assert compute_intraday_scalar(samples, now=now) == INTRADAY_SCALAR_MIN


def test_intraday_modeled_wh_gate_excludes_dim_slots():
    """Slots at/below the modeled-Wh gate must not contribute."""
    now = _now()
    # A run of good samples at ratio 1.0 for coverage, plus one huge-ratio slot
    # that is BELOW the Wh gate and must be ignored.
    samples = _trailing_samples(now, ratio=1.0, n=12, step_min=15, modeled_wh=100.0)
    samples.append(
        IntradaySample(
            at=now - timedelta(minutes=30),
            measured_kc=50.0,  # absurd ratio
            modeled_kc=1.0,
            modeled_wh=INTRADAY_MIN_MODELED_WH,  # at the gate -> excluded (<=)
        )
    )
    s = compute_intraday_scalar(samples, now=now)
    assert s == pytest.approx(1.0, rel=1e-9)


def test_intraday_future_samples_dropped():
    now = _now()
    samples = _trailing_samples(now, ratio=1.3, n=12, step_min=15)
    # Add a future sample with a poison ratio; must be dropped.
    samples.append(
        IntradaySample(at=now + timedelta(minutes=30), measured_kc=100.0,
                       modeled_kc=1.0, modeled_wh=100.0)
    )
    assert compute_intraday_scalar(samples, now=now) == pytest.approx(1.3, rel=1e-9)


def test_intraday_stale_samples_beyond_window_dropped():
    now = _now()
    # All samples older than the trailing window -> nothing in-window -> neutral.
    samples = [
        IntradaySample(at=now - timedelta(hours=10 + i), measured_kc=2.0,
                       modeled_kc=1.0, modeled_wh=100.0)
        for i in range(6)
    ]
    assert compute_intraday_scalar(samples, now=now) == INTRADAY_NEUTRAL


def test_intraday_clock_jump_backwards_collapses_to_neutral():
    """A backwards clock jump makes every sample 'future' -> neutral, not stale act."""
    real_now = _now()
    samples = _trailing_samples(real_now, ratio=1.8, n=12, step_min=15)
    jumped_now = real_now - timedelta(hours=3)  # clock went back
    assert compute_intraday_scalar(samples, now=jumped_now) == INTRADAY_NEUTRAL


def test_intraday_nonfinite_samples_skipped():
    now = _now()
    good = _trailing_samples(now, ratio=1.2, n=12, step_min=15)
    bad = [
        IntradaySample(at=now - timedelta(minutes=20), measured_kc=float("nan"),
                       modeled_kc=1.0, modeled_wh=100.0),
        IntradaySample(at=now - timedelta(minutes=25), measured_kc=1.0,
                       modeled_kc=float("inf"), modeled_wh=100.0),
        IntradaySample(at=now - timedelta(minutes=30), measured_kc=1.0,
                       modeled_kc=1.0, modeled_wh=float("nan")),
    ]
    s = compute_intraday_scalar(good + bad, now=now)
    assert s == pytest.approx(1.2, rel=1e-9)
    assert math.isfinite(s)


def test_intraday_zero_modeled_kc_skipped():
    now = _now()
    good = _trailing_samples(now, ratio=1.1, n=12, step_min=15)
    good.append(
        IntradaySample(at=now - timedelta(minutes=20), measured_kc=5.0,
                       modeled_kc=0.0, modeled_wh=100.0)
    )
    assert compute_intraday_scalar(good, now=now) == pytest.approx(1.1, rel=1e-9)


def test_intraday_result_always_in_band():
    now = _now()
    for ratio in [0.001, 0.3, 1.0, 1.7, 3.0, 100.0]:
        samples = _trailing_samples(now, ratio=ratio, n=12, step_min=15)
        s = compute_intraday_scalar(samples, now=now)
        assert INTRADAY_SCALAR_MIN <= s <= INTRADAY_SCALAR_MAX


# ===========================================================================
# apply_intraday_scalar
# ===========================================================================


def _hourly(now: datetime, hours: int = 12, wh: float = 100.0) -> dict[str, float]:
    """Hourly curve keyed by ISO UTC hour start, from the hour containing now."""
    base = now.replace(minute=0, second=0, microsecond=0)
    return {
        (base + timedelta(hours=h)).isoformat(): wh
        for h in range(hours)
    }


def test_apply_neutral_scalar_is_identity():
    now = _now()
    curve = _hourly(now)
    out = apply_intraday_scalar(curve, INTRADAY_NEUTRAL, now=now)
    assert out == curve
    assert out is not curve  # new dict


def test_apply_does_not_mutate_input():
    now = _now()
    curve = _hourly(now)
    snapshot = dict(curve)
    apply_intraday_scalar(curve, 1.5, now=now)
    assert curve == snapshot


def test_apply_full_scalar_at_current_hour():
    now = _now()
    curve = _hourly(now, hours=12, wh=100.0)
    out = apply_intraday_scalar(curve, 1.5, now=now)
    base = now.replace(minute=0, second=0, microsecond=0)
    # Hour containing now (start == base, age 0) gets full scalar.
    assert out[base.isoformat()] == pytest.approx(150.0)


def test_apply_linear_decay_to_one_over_horizon():
    now = _now()
    curve = _hourly(now, hours=12, wh=100.0)
    scalar = 1.5
    out = apply_intraday_scalar(curve, scalar, now=now)
    base = now.replace(minute=0, second=0, microsecond=0)
    horizon = INTRADAY_APPLY_HORIZON_MINUTES
    for h in range(12):
        key = (base + timedelta(hours=h)).isoformat()
        age_min = h * 60.0
        if age_min >= horizon:
            expected_factor = 1.0
        else:
            expected_factor = 1.0 + (scalar - 1.0) * (1.0 - age_min / horizon)
        assert out[key] == pytest.approx(100.0 * expected_factor, rel=1e-9)


def test_apply_beyond_horizon_unchanged():
    now = _now()
    curve = _hourly(now, hours=12, wh=100.0)
    out = apply_intraday_scalar(curve, 2.0, now=now)
    base = now.replace(minute=0, second=0, microsecond=0)
    # Hours at/after the horizon are exactly unchanged.
    horizon_h = int(INTRADAY_APPLY_HORIZON_MINUTES // 60)
    for h in range(horizon_h, 12):
        key = (base + timedelta(hours=h)).isoformat()
        assert out[key] == pytest.approx(100.0)


def test_apply_past_hours_untouched():
    now = _now()
    base = now.replace(minute=0, second=0, microsecond=0)
    # A past hour (start well before now) must not be scaled.
    past_key = (base - timedelta(hours=3)).isoformat()
    curve = {past_key: 100.0, base.isoformat(): 100.0}
    out = apply_intraday_scalar(curve, 2.0, now=now)
    assert out[past_key] == pytest.approx(100.0)
    assert out[base.isoformat()] == pytest.approx(200.0)


def test_apply_factor_stays_in_band_even_for_extreme_scalar():
    now = _now()
    curve = _hourly(now, hours=12, wh=100.0)
    # Scalar already clamped by compute, but apply must clamp too.
    out = apply_intraday_scalar(curve, 99.0, now=now)
    base = now.replace(minute=0, second=0, microsecond=0)
    for h in range(12):
        key = (base + timedelta(hours=h)).isoformat()
        factor = out[key] / 100.0
        assert INTRADAY_SCALAR_MIN <= factor <= INTRADAY_SCALAR_MAX + 1e-9


def test_apply_unparseable_key_passes_through():
    now = _now()
    curve = {"not-a-date": 100.0, now.replace(minute=0, second=0, microsecond=0).isoformat(): 100.0}
    out = apply_intraday_scalar(curve, 1.5, now=now)
    assert out["not-a-date"] == pytest.approx(100.0)


def test_apply_z_suffix_key_parsed():
    now = _now()
    base = now.replace(minute=0, second=0, microsecond=0)
    key = base.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = apply_intraday_scalar({key: 100.0}, 1.5, now=now)
    assert out[key] == pytest.approx(150.0)


def test_apply_non_dict_returns_empty():
    assert apply_intraday_scalar(None, 1.5, now=_now()) == {}  # type: ignore[arg-type]


def test_apply_nonfinite_wh_passed_through():
    now = _now()
    base = now.replace(minute=0, second=0, microsecond=0)
    curve = {base.isoformat(): float("nan")}
    out = apply_intraday_scalar(curve, 1.5, now=now)
    assert math.isnan(out[base.isoformat()])


# ===========================================================================
# classify_cloud
# ===========================================================================


def test_classify_clear():
    assert classify_cloud(cloud_low=5, cloud_mid=5, cloud_high=5,
                          visibility_m=20000, month=6) == CLOUD_CLASS_CLEAR


def test_classify_overcast():
    assert classify_cloud(cloud_low=90, cloud_mid=90, cloud_high=90,
                          visibility_m=20000, month=6) == CLOUD_CLASS_OVERCAST


def test_classify_mixed():
    assert classify_cloud(cloud_low=50, cloud_mid=50, cloud_high=50,
                          visibility_m=20000, month=6) == CLOUD_CLASS_MIXED


def test_classify_fog_by_visibility_overrides_cover():
    # Low visibility -> fog even with clear skies and in summer.
    assert classify_cloud(cloud_low=0, cloud_mid=0, cloud_high=0,
                          visibility_m=500, month=6) == CLOUD_CLASS_FOG


def test_classify_fog_by_winter_low_cloud():
    # High low-cloud in a fog month -> fog, even with good visibility.
    assert classify_cloud(cloud_low=90, cloud_mid=0, cloud_high=0,
                          visibility_m=20000, month=12) == CLOUD_CLASS_FOG


def test_classify_no_fog_high_low_cloud_summer():
    # Same high low-cloud but a summer month is NOT the fog rule -> falls to
    # cover split (mean = 30 -> mixed).
    assert classify_cloud(cloud_low=90, cloud_mid=0, cloud_high=0,
                          visibility_m=20000, month=6) == CLOUD_CLASS_MIXED


def test_classify_fog_precedence_over_overcast():
    # Very low visibility with full overcast still classifies as fog (fog first).
    assert classify_cloud(cloud_low=100, cloud_mid=100, cloud_high=100,
                          visibility_m=100, month=1) == CLOUD_CLASS_FOG


def test_classify_nan_visibility_does_not_fire_fog():
    # Unusable visibility must not spuriously trigger fog.
    r = classify_cloud(cloud_low=5, cloud_mid=5, cloud_high=5,
                       visibility_m=float("nan"), month=6)
    assert r == CLOUD_CLASS_CLEAR


def test_classify_nan_cover_treated_as_zero():
    r = classify_cloud(cloud_low=float("nan"), cloud_mid=float("nan"),
                       cloud_high=float("nan"), visibility_m=20000, month=6)
    assert r == CLOUD_CLASS_CLEAR


def test_classify_returns_known_class():
    for args in [
        dict(cloud_low=0, cloud_mid=0, cloud_high=0, visibility_m=20000, month=6),
        dict(cloud_low=100, cloud_mid=100, cloud_high=100, visibility_m=20000, month=6),
        dict(cloud_low=40, cloud_mid=40, cloud_high=40, visibility_m=20000, month=6),
        dict(cloud_low=0, cloud_mid=0, cloud_high=0, visibility_m=100, month=6),
    ]:
        assert classify_cloud(**args) in (
            CLOUD_CLASS_CLEAR, CLOUD_CLASS_MIXED, CLOUD_CLASS_OVERCAST, CLOUD_CLASS_FOG
        )


# ===========================================================================
# day_part_for_hour
# ===========================================================================


def test_day_part_morning():
    for h in [0, 5, 8, 9]:
        assert day_part_for_hour(h) == DAY_PART_MORNING


def test_day_part_midday():
    for h in [10, 11, 12, 13]:
        assert day_part_for_hour(h) == DAY_PART_MIDDAY


def test_day_part_afternoon():
    for h in [14, 17, 20, 23]:
        assert day_part_for_hour(h) == DAY_PART_AFTERNOON


def test_day_part_out_of_range_wraps():
    # 25 % 24 == 1 -> morning; -1 % 24 == 23 -> afternoon.
    assert day_part_for_hour(25) == DAY_PART_MORNING
    assert day_part_for_hour(-1) == DAY_PART_AFTERNOON


def test_day_part_non_int_safe():
    assert day_part_for_hour("nope") in (  # type: ignore[arg-type]
        DAY_PART_MORNING, DAY_PART_MIDDAY, DAY_PART_AFTERNOON
    )


# ===========================================================================
# train_day_ahead_bias / apply_day_ahead_bias (RLS)
# ===========================================================================


def test_train_returns_new_state_input_untouched():
    state = BiasState()
    samples = [
        DayAheadSample(cloud_class=CLOUD_CLASS_CLEAR, day_part=DAY_PART_MIDDAY,
                       measured_wh=120.0, modeled_wh=100.0)
    ]
    out = train_day_ahead_bias(state, samples)
    assert out is not state
    assert state.cells == {}  # original untouched


def test_train_converges_to_true_bias():
    """Feed a consistent measured = 1.2 * modeled; theta -> ~1.2."""
    state = BiasState()
    for _ in range(30):
        state = train_day_ahead_bias(
            state,
            [DayAheadSample(cloud_class=CLOUD_CLASS_MIXED, day_part=DAY_PART_MORNING,
                            measured_wh=1200.0, modeled_wh=1000.0)],
        )
    bias = state.get_bias(CLOUD_CLASS_MIXED, DAY_PART_MORNING)
    assert bias == pytest.approx(1.2, abs=0.02)


def test_train_converges_to_bias_below_one():
    state = BiasState()
    for _ in range(30):
        state = train_day_ahead_bias(
            state,
            [DayAheadSample(cloud_class=CLOUD_CLASS_OVERCAST, day_part=DAY_PART_AFTERNOON,
                            measured_wh=700.0, modeled_wh=1000.0)],
        )
    bias = state.get_bias(CLOUD_CLASS_OVERCAST, DAY_PART_AFTERNOON)
    assert bias == pytest.approx(0.7, abs=0.02)


def test_train_min_samples_gate():
    """Below RLS_MIN_SAMPLES trained days the applied bias is neutral."""
    state = BiasState()
    for _ in range(RLS_MIN_SAMPLES - 1):
        state = train_day_ahead_bias(
            state,
            [DayAheadSample(cloud_class=CLOUD_CLASS_CLEAR, day_part=DAY_PART_MIDDAY,
                            measured_wh=1500.0, modeled_wh=1000.0)],
        )
    assert state.get_bias(CLOUD_CLASS_CLEAR, DAY_PART_MIDDAY) == DAY_AHEAD_BIAS_NEUTRAL
    # One more crosses the gate -> non-neutral.
    state = train_day_ahead_bias(
        state,
        [DayAheadSample(cloud_class=CLOUD_CLASS_CLEAR, day_part=DAY_PART_MIDDAY,
                        measured_wh=1500.0, modeled_wh=1000.0)],
    )
    assert state.get_bias(CLOUD_CLASS_CLEAR, DAY_PART_MIDDAY) != DAY_AHEAD_BIAS_NEUTRAL


def test_train_clamps_extreme_bias():
    """A wildly high measured/modeled ratio is clamped into the band."""
    state = BiasState()
    for _ in range(40):
        state = train_day_ahead_bias(
            state,
            [DayAheadSample(cloud_class=CLOUD_CLASS_CLEAR, day_part=DAY_PART_MORNING,
                            measured_wh=10000.0, modeled_wh=1000.0)],  # ratio 10
        )
    bias = state.get_bias(CLOUD_CLASS_CLEAR, DAY_PART_MORNING)
    assert bias == DAY_AHEAD_BIAS_MAX


def test_train_clamps_low_bias():
    state = BiasState()
    for _ in range(40):
        state = train_day_ahead_bias(
            state,
            [DayAheadSample(cloud_class=CLOUD_CLASS_FOG, day_part=DAY_PART_MORNING,
                            measured_wh=10.0, modeled_wh=1000.0)],  # ratio 0.01
        )
    bias = state.get_bias(CLOUD_CLASS_FOG, DAY_PART_MORNING)
    assert bias == DAY_AHEAD_BIAS_MIN


def test_train_skips_dark_day_zero_modeled():
    """A modeled ~0 day carries no information and must not create/advance a cell."""
    state = BiasState()
    state = train_day_ahead_bias(
        state,
        [DayAheadSample(cloud_class=CLOUD_CLASS_CLEAR, day_part=DAY_PART_MIDDAY,
                        measured_wh=0.0, modeled_wh=0.0)],
    )
    key = BiasState.cell_key(CLOUD_CLASS_CLEAR, DAY_PART_MIDDAY)
    assert key not in state.cells


def test_train_skips_nonfinite_energies():
    state = BiasState()
    state = train_day_ahead_bias(
        state,
        [
            DayAheadSample(cloud_class=CLOUD_CLASS_CLEAR, day_part=DAY_PART_MIDDAY,
                           measured_wh=float("nan"), modeled_wh=1000.0),
            DayAheadSample(cloud_class=CLOUD_CLASS_CLEAR, day_part=DAY_PART_MIDDAY,
                           measured_wh=1000.0, modeled_wh=float("inf")),
        ],
    )
    key = BiasState.cell_key(CLOUD_CLASS_CLEAR, DAY_PART_MIDDAY)
    assert key not in state.cells


def test_train_skips_unknown_class_or_part():
    state = BiasState()
    state = train_day_ahead_bias(
        state,
        [
            DayAheadSample(cloud_class="bogus", day_part=DAY_PART_MIDDAY,
                           measured_wh=1200.0, modeled_wh=1000.0),
            DayAheadSample(cloud_class=CLOUD_CLASS_CLEAR, day_part="whenever",
                           measured_wh=1200.0, modeled_wh=1000.0),
        ],
    )
    assert state.cells == {}


def test_train_independent_cells():
    """Different (class, part) cells train independently."""
    state = BiasState()
    for _ in range(30):
        state = train_day_ahead_bias(state, [
            DayAheadSample(cloud_class=CLOUD_CLASS_CLEAR, day_part=DAY_PART_MORNING,
                           measured_wh=1300.0, modeled_wh=1000.0),
            DayAheadSample(cloud_class=CLOUD_CLASS_OVERCAST, day_part=DAY_PART_AFTERNOON,
                           measured_wh=800.0, modeled_wh=1000.0),
        ])
    assert state.get_bias(CLOUD_CLASS_CLEAR, DAY_PART_MORNING) == pytest.approx(1.3, abs=0.03)
    assert state.get_bias(CLOUD_CLASS_OVERCAST, DAY_PART_AFTERNOON) == pytest.approx(0.8, abs=0.03)


def test_train_numerical_stability_many_steps():
    """Long training run stays finite and in-band (covariance never explodes)."""
    state = BiasState()
    for i in range(500):
        # Noisy true bias 1.1 +/- small oscillation.
        noise = 0.05 * math.sin(i)
        state = train_day_ahead_bias(
            state,
            [DayAheadSample(cloud_class=CLOUD_CLASS_MIXED, day_part=DAY_PART_MIDDAY,
                            measured_wh=1000.0 * (1.1 + noise), modeled_wh=1000.0)],
        )
    cell = state.cells[BiasState.cell_key(CLOUD_CLASS_MIXED, DAY_PART_MIDDAY)]
    assert math.isfinite(cell.theta)
    assert math.isfinite(cell.covariance)
    assert cell.covariance > 0.0
    assert DAY_AHEAD_BIAS_MIN <= cell.clamped_theta() <= DAY_AHEAD_BIAS_MAX
    assert state.get_bias(CLOUD_CLASS_MIXED, DAY_PART_MIDDAY) == pytest.approx(1.1, abs=0.05)


def test_train_empty_samples_noop():
    state = BiasState(cells={"clear|midday": BiasCell(theta=1.2, covariance=5.0, n=4)})
    out = train_day_ahead_bias(state, [])
    assert out.cells == state.cells


def test_train_preserves_version():
    state = BiasState(version=1)
    out = train_day_ahead_bias(state, [
        DayAheadSample(cloud_class=CLOUD_CLASS_CLEAR, day_part=DAY_PART_MIDDAY,
                       measured_wh=1200.0, modeled_wh=1000.0)
    ])
    assert out.version == 1


# --- apply_day_ahead_bias --------------------------------------------------


def test_apply_bias_neutral_when_untrained():
    state = BiasState()
    assert apply_day_ahead_bias(state, cloud_class=CLOUD_CLASS_CLEAR,
                                day_part=DAY_PART_MIDDAY, wh=500.0) == pytest.approx(500.0)


def test_apply_bias_scales_trained_cell():
    state = BiasState()
    for _ in range(30):
        state = train_day_ahead_bias(state, [
            DayAheadSample(cloud_class=CLOUD_CLASS_MIXED, day_part=DAY_PART_MORNING,
                           measured_wh=1200.0, modeled_wh=1000.0)
        ])
    out = apply_day_ahead_bias(state, cloud_class=CLOUD_CLASS_MIXED,
                               day_part=DAY_PART_MORNING, wh=1000.0)
    assert out == pytest.approx(1200.0, abs=20.0)


def test_apply_bias_nonfinite_wh_returns_zero():
    state = BiasState()
    assert apply_day_ahead_bias(state, cloud_class=CLOUD_CLASS_CLEAR,
                                day_part=DAY_PART_MIDDAY, wh=float("nan")) == 0.0


def test_apply_bias_never_negative():
    state = BiasState()
    assert apply_day_ahead_bias(state, cloud_class=CLOUD_CLASS_CLEAR,
                                day_part=DAY_PART_MIDDAY, wh=-50.0) == 0.0


def test_apply_bias_missing_cell_neutral():
    state = BiasState(cells={"clear|midday": BiasCell(theta=1.3, covariance=1.0, n=10)})
    # A different cell -> neutral.
    assert apply_day_ahead_bias(state, cloud_class=CLOUD_CLASS_FOG,
                                day_part=DAY_PART_AFTERNOON, wh=100.0) == pytest.approx(100.0)


# ===========================================================================
# Restart semantics: intraday scalar is never persisted (store contract).
# ===========================================================================


def test_intraday_has_no_persisted_state_type():
    """SPEC §5: the intraday scalar is transient. Assert there is no dataclass
    for it in types and that BiasState (the ONLY persisted FAST state) carries
    day-ahead cells only — never an intraday field."""
    import balcony_solar_forecast.core.types as types_mod

    # No 'IntradayState'-like persisted type exists.
    assert not hasattr(types_mod, "IntradayState")
    # BiasState fields are exactly the day-ahead RLS cells + version.
    fields = set(BiasState.__dataclass_fields__.keys())
    assert fields == {"cells", "version"}
    assert "scalar" not in fields
    assert "intraday" not in fields
