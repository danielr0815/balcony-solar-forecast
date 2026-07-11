"""Pure-core tests for the inverter DC->AC efficiency site calibration.

Plain pytest, no Home Assistant imports (SPEC §4). Covers the AC-side Phase 3
calibration primitives in ``core/inverter_cal.py`` + ``InverterCalState``:

  * ``eligible_ratio`` — the min-load / clip / finite / positive-DC gate, and
    that a valid ratio is returned RAW (out-of-band ratios are NOT clamped here);
  * ``update`` — adaptive warm-up (exact mean, then EMA), out-of-band DROP,
    band clamp, and the identity no-op when nothing is folded;
  * ``effective_eta`` — None below INVERTER_CAL_MIN_SAMPLES, then the clamped eta;
  * ``InverterCalState`` — tolerant from_dict (garbage -> neutral) + to_dict.
"""

from __future__ import annotations

import math

import pytest
from balcony_solar_forecast.const import (
    DEFAULT_INVERTER_EFFICIENCY,
    INVERTER_CAL_MAX,
    INVERTER_CAL_MIN,
    INVERTER_CAL_MIN_LOAD_W,
    INVERTER_CAL_MIN_SAMPLES,
)
from balcony_solar_forecast.core import inverter_cal
from balcony_solar_forecast.core.types import InverterCalState

# ---------------------------------------------------------------------------
# eligible_ratio: min-load / clip / finite / positive-DC gate
# ---------------------------------------------------------------------------


class TestEligibleRatio:
    def test_valid_slot_returns_raw_ratio(self):
        # Unclipped, well above the min load: AC/DC returned verbatim.
        assert inverter_cal.eligible_ratio(
            960.0, 1000.0, clip_headroom_ok=True
        ) == pytest.approx(0.96)

    def test_low_load_returns_none(self):
        # Summed DC below INVERTER_CAL_MIN_LOAD_W: the inverter self-consumption
        # / MPPT threshold distorts the ratio -> skip.
        below = INVERTER_CAL_MIN_LOAD_W - 1.0
        assert inverter_cal.eligible_ratio(
            below * 0.96, below, clip_headroom_ok=True
        ) is None

    def test_at_min_load_is_eligible(self):
        # The bound is inclusive (>= MIN_LOAD_W).
        r = inverter_cal.eligible_ratio(
            INVERTER_CAL_MIN_LOAD_W * 0.95, INVERTER_CAL_MIN_LOAD_W,
            clip_headroom_ok=True,
        )
        assert r == pytest.approx(0.95)

    def test_clipped_slot_returns_none(self):
        # A clipped hour's AC is capped -> its ratio understates eta -> reject.
        assert inverter_cal.eligible_ratio(
            760.0, 1000.0, clip_headroom_ok=False
        ) is None

    def test_zero_dc_returns_none(self):
        assert inverter_cal.eligible_ratio(0.0, 0.0, clip_headroom_ok=True) is None

    def test_negative_dc_returns_none(self):
        assert inverter_cal.eligible_ratio(
            100.0, -500.0, clip_headroom_ok=True
        ) is None

    def test_nonfinite_inputs_return_none(self):
        assert inverter_cal.eligible_ratio(
            float("nan"), 1000.0, clip_headroom_ok=True
        ) is None
        assert inverter_cal.eligible_ratio(
            500.0, float("inf"), clip_headroom_ok=True
        ) is None

    def test_out_of_band_ratio_is_returned_raw_not_clamped(self):
        # A meter that also sees house load yields a low ratio; eligible_ratio
        # returns it RAW (0.5) so the update DROP gate — not a clamp here — sees
        # the out-of-band value and rejects it.
        r = inverter_cal.eligible_ratio(500.0, 1000.0, clip_headroom_ok=True)
        assert r == pytest.approx(0.5)
        assert r < INVERTER_CAL_MIN


# ---------------------------------------------------------------------------
# update: adaptive warm-up (mean -> EMA), out-of-band DROP, clamp, no-op
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_first_sample_seeds_eta_wiping_default_prior(self):
        # At n==0 the adaptive alpha is 1.0, so the first in-band sample REPLACES
        # the DEFAULT prior instead of blending with it.
        state = inverter_cal.update(InverterCalState(), [0.95])
        assert state.n == 1
        assert state.eta == pytest.approx(0.95)

    def test_warmup_is_exact_arithmetic_mean(self):
        # While 1/(n+1) exceeds the fixed alpha (the first floor(1/alpha)
        # samples), the stored eta is the EXACT mean of the folded ratios.
        ratios = [0.95, 0.96, 0.97]
        state = inverter_cal.update(InverterCalState(), ratios)
        assert state.n == 3
        assert state.eta == pytest.approx(sum(ratios) / len(ratios))

    def test_transitions_to_ema_after_warmup(self):
        # After the warm-up window the fixed alpha takes over: fold 10 samples at
        # 0.95 (mean 0.95), then one 0.97 -> EMA step 0.95 + 0.10*(0.97-0.95).
        state = inverter_cal.update(InverterCalState(), [0.95] * 10)
        assert state.n == 10
        assert state.eta == pytest.approx(0.95)
        stepped = inverter_cal.update(state, [0.97])
        assert stepped.n == 11
        assert stepped.eta == pytest.approx(0.95 + 0.10 * (0.97 - 0.95))

    def test_out_of_band_ratios_are_dropped_not_folded(self):
        # 0.50 (< MIN) and 1.50 (> MAX) are implausible inverter etas: dropped,
        # so only the two in-band ratios fold and n counts just them.
        state = inverter_cal.update(
            InverterCalState(), [0.95, 0.50, 1.50, 0.97]
        )
        assert state.n == 2
        assert state.eta == pytest.approx(0.96)

    def test_all_out_of_band_is_identity_noop(self):
        base = InverterCalState(eta=0.94, n=5)
        assert inverter_cal.update(base, [0.50, 1.80]) is base

    def test_empty_and_none_ratios_are_identity_noop(self):
        base = InverterCalState(eta=0.94, n=5)
        assert inverter_cal.update(base, []) is base
        assert inverter_cal.update(base, None) is base

    def test_nonfinite_ratios_are_skipped(self):
        state = inverter_cal.update(
            InverterCalState(), [float("nan"), 0.96, float("inf")]
        )
        assert state.n == 1
        assert state.eta == pytest.approx(0.96)

    def test_stored_eta_is_clamped_to_band_after_fold(self):
        # A garbage/legacy starting eta above MAX is pulled back into the band as
        # soon as an in-band sample folds (defensive clamp).
        state = inverter_cal.update(InverterCalState(eta=1.5, n=5), [0.95])
        assert state.n == 6
        assert INVERTER_CAL_MIN <= state.eta <= INVERTER_CAL_MAX

    def test_never_raises_on_garbage(self):
        # Pure + total: a non-numeric iterable element is simply skipped.
        state = inverter_cal.update(InverterCalState(), ["junk", 0.96, None])
        assert state.n == 1


# ---------------------------------------------------------------------------
# effective_eta: trust gate + band clamp
# ---------------------------------------------------------------------------


class TestEffectiveEta:
    def test_none_when_neutral(self):
        assert inverter_cal.effective_eta(InverterCalState()) is None

    def test_none_just_below_min_samples(self):
        state = InverterCalState(eta=0.95, n=INVERTER_CAL_MIN_SAMPLES - 1)
        assert inverter_cal.effective_eta(state) is None

    def test_value_at_min_samples(self):
        state = InverterCalState(eta=0.95, n=INVERTER_CAL_MIN_SAMPLES)
        assert inverter_cal.effective_eta(state) == pytest.approx(0.95)

    def test_trusted_after_folding_enough_hours(self):
        # Folding INVERTER_CAL_MIN_SAMPLES identical in-band ratios trips the gate.
        state = inverter_cal.update(
            InverterCalState(), [0.955] * INVERTER_CAL_MIN_SAMPLES
        )
        assert state.n == INVERTER_CAL_MIN_SAMPLES
        assert inverter_cal.effective_eta(state) == pytest.approx(0.955)

    def test_clamps_to_band(self):
        hi = InverterCalState(eta=1.5, n=INVERTER_CAL_MIN_SAMPLES + 5)
        lo = InverterCalState(eta=0.5, n=INVERTER_CAL_MIN_SAMPLES + 5)
        assert inverter_cal.effective_eta(hi) == pytest.approx(INVERTER_CAL_MAX)
        assert inverter_cal.effective_eta(lo) == pytest.approx(INVERTER_CAL_MIN)


# ---------------------------------------------------------------------------
# InverterCalState: tolerant (de)serialisation
# ---------------------------------------------------------------------------


class TestInverterCalState:
    def test_neutral_defaults(self):
        s = InverterCalState()
        assert s.eta == pytest.approx(DEFAULT_INVERTER_EFFICIENCY)
        assert s.n == 0

    def test_roundtrip(self):
        s = InverterCalState(eta=0.94, n=42)
        assert InverterCalState.from_dict(s.to_dict()) == s

    def test_from_dict_missing_keys_is_neutral(self):
        s = InverterCalState.from_dict({})
        assert s.eta == pytest.approx(DEFAULT_INVERTER_EFFICIENCY)
        assert s.n == 0

    def test_from_dict_garbage_is_neutral(self):
        s = InverterCalState.from_dict("not-a-dict")
        assert s.eta == pytest.approx(DEFAULT_INVERTER_EFFICIENCY)
        assert s.n == 0

    def test_from_dict_tolerates_bad_values(self):
        s = InverterCalState.from_dict({"eta": "junk", "n": -3})
        assert s.eta == pytest.approx(DEFAULT_INVERTER_EFFICIENCY)
        assert s.n == 0  # negative floored to 0
        assert math.isfinite(s.eta)
