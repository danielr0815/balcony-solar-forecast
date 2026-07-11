"""Pure tests for core/ensembleband.py — ensemble spread + fusion (v0.16, SPEC §6).

Plain pytest, no Home Assistant (SPEC §4). Covers:

  * ensemble_band_factors: hand-computed type-7 0.1/0.9 percentiles, member
    clamps, and the min-members / min-det-GHI / missing-det skips;
  * fuse_bands: None -> learned bit-identical; collapsed learned + ensemble ->
    the cold-start ensemble band; envelope-max widths; never narrows; P50 kept;
    monotonicity and totality (never raises).
"""

from __future__ import annotations

import pytest
from balcony_solar_forecast.core.ensembleband import (
    ensemble_band_factors,
    fuse_bands,
)
from balcony_solar_forecast.core.types import QuantileBands

_KW = {
    "min_members": 10,
    "min_det_ghi": 20.0,
    "f_min": 0.0,
    "f_max": 3.0,
}


# ---------------------------------------------------------------------------
# ensemble_band_factors
# ---------------------------------------------------------------------------


def test_factors_type7_percentiles_hand_computed():
    # det = 100 => factors = member/100. Ten members map to 0.5 .. 1.4 step 0.1.
    members = [50, 60, 70, 80, 90, 100, 110, 120, 130, 140]
    out = ensemble_band_factors(
        {"h": members}, {"h": 100.0}, **_KW
    )
    f10, f90 = out["h"]
    # Type-7: P10 rank = 9*0.1 = 0.9 -> 0.5 + 0.9*(0.6-0.5) = 0.59.
    #         P90 rank = 9*0.9 = 8.1 -> 1.3 + 0.1*(1.4-1.3) = 1.31.
    assert f10 == pytest.approx(0.59)
    assert f90 == pytest.approx(1.31)


def test_factors_divide_by_det():
    # Members are GHI; det scales them into factors.
    members = [400.0] * 5 + [600.0] * 5  # /500 -> 0.8 x5, 1.2 x5
    out = ensemble_band_factors({"h": members}, {"h": 500.0}, **_KW)
    f10, f90 = out["h"]
    assert 0.8 <= f10 <= 1.2
    assert 0.8 <= f90 <= 1.2
    assert f10 <= f90


def test_factors_clamp_member_ratio_to_f_max():
    # One freak member 100x brighter than det -> clamped to f_max (3.0), not 100.
    members = [500.0] * 11 + [50000.0]
    out = ensemble_band_factors({"h": members}, {"h": 500.0}, **_KW)
    _f10, f90 = out["h"]
    assert f90 <= 3.0


def test_factors_skip_below_min_members():
    # det is valid (>= 20) so min-members is the ONLY reason to skip.
    out = ensemble_band_factors(
        {"h": [100.0] * 9}, {"h": 100.0}, **_KW  # 9 < 10
    )
    assert "h" not in out


def test_factors_skip_below_min_det_ghi():
    out = ensemble_band_factors(
        {"h": [1.0] * 20}, {"h": 5.0}, **_KW  # det 5 < 20
    )
    assert "h" not in out


def test_factors_skip_missing_det():
    out = ensemble_band_factors({"h": [1.0] * 20}, {}, **_KW)
    assert out == {}


def test_factors_drop_nonfinite_members():
    members = [100.0] * 10 + [float("nan"), float("inf")]
    out = ensemble_band_factors({"h": members}, {"h": 100.0}, **_KW)
    # The two junk members are dropped; the 10 clean ones still qualify.
    assert "h" in out


def test_factors_non_dict_inputs_are_total():
    assert ensemble_band_factors(None, {}, **_KW) == {}
    assert ensemble_band_factors({}, None, **_KW) == {}


# ---------------------------------------------------------------------------
# fuse_bands
# ---------------------------------------------------------------------------


def test_fuse_none_is_bit_identical_learned():
    learned = QuantileBands(p10=0.8, p50=1.0, p90=1.2, n=30)
    assert fuse_bands(learned, None) is learned


def test_fuse_cold_start_neutral_learned_takes_ensemble():
    learned = QuantileBands.neutral()  # 1/1/1
    fused = fuse_bands(learned, (0.8, 1.3))
    assert (fused.p10, fused.p50, fused.p90) == (0.8, 1.0, 1.3)


def test_fuse_cold_start_clamps_monotonic_when_factors_one_sided():
    # Ensemble both above 1.0 -> p10 must not exceed the fixed P50 == 1.0.
    fused = fuse_bands(QuantileBands.neutral(), (1.1, 1.4))
    assert fused.p10 == 1.0
    assert fused.p50 == 1.0
    assert fused.p90 == 1.4
    assert fused.p10 <= fused.p50 <= fused.p90


def test_fuse_envelope_widens_both_edges():
    learned = QuantileBands(p10=0.9, p50=1.0, p90=1.1, n=40)
    fused = fuse_bands(learned, (0.7, 1.3))
    assert fused.p10 == 0.7  # ensemble wider on the low edge
    assert fused.p90 == 1.3  # ensemble wider on the high edge
    assert fused.p50 == 1.0  # learned median unmoved


def test_fuse_never_narrows_when_ensemble_tighter():
    learned = QuantileBands(p10=0.6, p50=1.0, p90=1.5, n=40)
    fused = fuse_bands(learned, (0.8, 1.2))  # tighter than learned
    assert fused.p10 == 0.6  # kept the wider learned edge
    assert fused.p90 == 1.5


def test_fuse_preserves_shifted_p50():
    learned = QuantileBands(p10=0.8, p50=1.05, p90=1.2, n=40)
    fused = fuse_bands(learned, (0.7, 1.3))
    assert fused.p50 == 1.05


def test_fuse_swaps_inverted_factor_pair():
    # A (f90, f10) mis-ordered pair is normalised, never inverts the band.
    fused = fuse_bands(QuantileBands.neutral(), (1.3, 0.8))
    assert fused.p10 == 0.8
    assert fused.p90 == 1.3


def test_fuse_malformed_ensemble_returns_learned():
    learned = QuantileBands(p10=0.9, p50=1.0, p90=1.1, n=5)
    assert fuse_bands(learned, (None, 1.0)) is learned  # type: ignore[arg-type]
    assert fuse_bands(learned, (float("nan"), 1.0)) is learned
