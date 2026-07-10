"""Tests for the SLOW learner: shademap (pure, HA-free).

Covers (SPEC §5, task brief):
  - bin-key math: az 5 deg x el 2.5 deg x half-year, azimuth 360 wrap,
    below-horizon sun folds to el bin 0;
  - half-year separation: same sun position, opposite season -> different bins;
  - beam-referenced T with the negative-numerator guard (measured < modeled
    diffuse => T = 0, never negative) and the zero/negative-beam guard;
  - elevation-dependent clear-sky-index gate (relaxed lower bound at low sun),
    minimum beam share, neighbour-slot stability;
  - EMA update: fresh-bin seeding, blend, clamp, n increment, input immutability;
  - shrinkage blend with the static horizon prior; unvisited bins return the
    prior EXACTLY;
  - application to beam+circumsolar (pure multiply, non-negative);
  - polar-table dump (az/elev/halfyear/tau/n per channel, bin centres, sorted);
  - bootstrap ingestion with the n-credit cap;
  - property tests: tau in [0, 1.1]; prior pass-through; half-year separation.
"""

from __future__ import annotations

import math

import pytest
from balcony_solar_forecast.const import (
    BOOTSTRAP_MAX_BIN_N,
    SHADEMAP_AZ_BIN_DEG,
    SHADEMAP_EMA_ALPHA,
    SHADEMAP_KC_HI,
    SHADEMAP_KC_LO_HIGH_SUN,
    SHADEMAP_KC_LO_LOW_SUN,
    SHADEMAP_KC_PIVOT_ELEV_DEG,
    SHADEMAP_MIN_BEAM_SHARE,
    SHADEMAP_NEIGHBOUR_STABILITY,
    SHADEMAP_SHRINKAGE_K,
    SHADEMAP_TAU_MAX,
    SHADEMAP_TAU_MIN,
    SUMMER_SOLSTICE_DOY,
)
from balcony_solar_forecast.core import shademap as S
from balcony_solar_forecast.core.types import ShademapBin, ShademapState

# A day-of-year in each half-year (rising limb before solstice / falling after).
DOY_SPRING = 100   # ~April, before solstice -> half 0
DOY_SUMMER = 220   # ~August, after solstice -> half 1


# ---------------------------------------------------------------------------
# half_year_index
# ---------------------------------------------------------------------------


def test_half_year_before_and_after_solstice():
    assert S.half_year_index(DOY_SPRING) == 0
    assert S.half_year_index(DOY_SUMMER) == 1


def test_half_year_boundary_is_falling_limb():
    assert S.half_year_index(SUMMER_SOLSTICE_DOY - 1) == 0
    assert S.half_year_index(SUMMER_SOLSTICE_DOY) == 1
    assert S.half_year_index(SUMMER_SOLSTICE_DOY + 1) == 1


def test_half_year_extremes_and_bad_input():
    assert S.half_year_index(1) == 0
    assert S.half_year_index(366) == 1
    # defensive clamp / coercion never raises
    assert S.half_year_index(-5) == 0
    assert S.half_year_index(9999) == 1
    assert S.half_year_index("not-a-number") in (0, 1)  # coerced, no raise


# ---------------------------------------------------------------------------
# shademap_bin_key
# ---------------------------------------------------------------------------


def test_bin_key_format_and_indices():
    # az 47 deg -> idx floor(47/5) = 9; el 11 deg -> floor(11/2.5) = 4
    key = S.shademap_bin_key(47.0, 11.0, DOY_SPRING)
    assert key == "9:4:0"


def test_bin_key_azimuth_wraps_at_360():
    assert S.shademap_bin_key(0.0, 30.0, DOY_SPRING) == S.shademap_bin_key(
        360.0, 30.0, DOY_SPRING
    )
    # 365 deg wraps to 5 deg -> az idx 1
    assert S.shademap_bin_key(365.0, 30.0, DOY_SPRING).startswith("1:")
    # negative azimuth wraps into [0,360)
    assert S.shademap_bin_key(-5.0, 30.0, DOY_SPRING) == S.shademap_bin_key(
        355.0, 30.0, DOY_SPRING
    )


def test_bin_key_below_horizon_folds_to_el_zero():
    below = S.shademap_bin_key(100.0, -3.0, DOY_SPRING)
    at_zero = S.shademap_bin_key(100.0, 0.0, DOY_SPRING)
    assert below == at_zero
    assert below.split(":")[1] == "0"


def test_bin_key_half_year_separates_same_geometry():
    # Same az/el, different season -> keys differ ONLY in the half suffix.
    k_spring = S.shademap_bin_key(150.0, 40.0, DOY_SPRING)
    k_summer = S.shademap_bin_key(150.0, 40.0, DOY_SUMMER)
    assert k_spring != k_summer
    az_s, el_s, half_s = k_spring.split(":")
    az_u, el_u, half_u = k_summer.split(":")
    assert (az_s, el_s) == (az_u, el_u)
    assert {half_s, half_u} == {"0", "1"}


# ---------------------------------------------------------------------------
# is_quasi_clear
# ---------------------------------------------------------------------------


def _high_sun_kc_ok() -> float:
    # comfortably inside the high-sun band
    return (SHADEMAP_KC_LO_HIGH_SUN + SHADEMAP_KC_HI) / 2.0


def test_quasi_clear_accepts_clean_high_sun_sample():
    assert S.is_quasi_clear(kc=_high_sun_kc_ok(), sun_el=45.0, beam_share=0.5)


def test_quasi_clear_rejects_low_kc_at_high_sun():
    # Below the tight high-sun floor.
    assert not S.is_quasi_clear(
        kc=SHADEMAP_KC_LO_HIGH_SUN - 0.05, sun_el=40.0, beam_share=0.5
    )


def test_quasi_clear_relaxes_lower_bound_at_low_sun():
    # A k_c between the low-sun and high-sun floors: accepted at low elevation,
    # rejected at high elevation (the elevation-dependent gate).
    kc = (SHADEMAP_KC_LO_LOW_SUN + SHADEMAP_KC_LO_HIGH_SUN) / 2.0
    assert S.is_quasi_clear(kc=kc, sun_el=1.0, beam_share=0.5)
    assert not S.is_quasi_clear(
        kc=kc, sun_el=SHADEMAP_KC_PIVOT_ELEV_DEG + 5.0, beam_share=0.5
    )


def test_quasi_clear_rejects_high_kc_over_ceiling():
    assert not S.is_quasi_clear(kc=SHADEMAP_KC_HI + 0.1, sun_el=40.0, beam_share=0.5)


def test_quasi_clear_rejects_low_beam_share():
    assert not S.is_quasi_clear(
        kc=_high_sun_kc_ok(),
        sun_el=40.0,
        beam_share=SHADEMAP_MIN_BEAM_SHARE - 0.001,
    )
    # exactly at the threshold is NOT strictly greater -> rejected
    assert not S.is_quasi_clear(
        kc=_high_sun_kc_ok(), sun_el=40.0, beam_share=SHADEMAP_MIN_BEAM_SHARE
    )


def test_quasi_clear_neighbour_stability_on_ratio():
    kc = _high_sun_kc_ok()
    ratio = 0.95
    # A stable neighbour ratio (tiny relative change) -> accepted.
    assert S.is_quasi_clear(
        kc=kc, sun_el=40.0, beam_share=0.5,
        stability_ratio=ratio,
        neighbour_ratio=ratio * (1.0 + SHADEMAP_NEIGHBOUR_STABILITY / 2.0),
    )
    # A volatile neighbour ratio (large relative change) -> rejected: a lone
    # bright measured slot between shaded ones is a fluctuation.
    assert not S.is_quasi_clear(
        kc=kc, sun_el=40.0, beam_share=0.5,
        stability_ratio=ratio,
        neighbour_ratio=ratio * (1.0 + SHADEMAP_NEIGHBOUR_STABILITY * 2.0),
    )
    # The stability leg keys on the RATIO pair, not on k_c: an identical ratio
    # passes even though k_c is not used for stability.
    assert S.is_quasi_clear(
        kc=kc, sun_el=40.0, beam_share=0.5,
        stability_ratio=ratio, neighbour_ratio=ratio,
    )


def test_quasi_clear_stability_leg_skipped_without_both_values():
    kc = _high_sun_kc_ok()
    # Only one side of the ratio pair given -> the stability leg is skipped, so a
    # band-valid sample is still accepted.
    assert S.is_quasi_clear(kc=kc, sun_el=40.0, beam_share=0.5, stability_ratio=0.5)
    assert S.is_quasi_clear(kc=kc, sun_el=40.0, beam_share=0.5, neighbour_ratio=0.5)


def test_quasi_clear_rejects_non_finite():
    assert not S.is_quasi_clear(kc=float("nan"), sun_el=40.0, beam_share=0.5)
    assert not S.is_quasi_clear(
        kc=_high_sun_kc_ok(), sun_el=40.0, beam_share=float("inf")
    )
    # A non-finite ratio in the stability leg also fails the gate.
    assert not S.is_quasi_clear(
        kc=_high_sun_kc_ok(), sun_el=40.0, beam_share=0.5,
        stability_ratio=float("nan"), neighbour_ratio=0.9,
    )


# ---------------------------------------------------------------------------
# beam_referenced_t (incl. negative-numerator guard)
# ---------------------------------------------------------------------------


def test_beam_t_basic_ratio():
    # (P_meas - P_diff) / P_beam = (120 - 20) / 200 = 0.5
    t = S.beam_referenced_t(120.0, 20.0, 200.0)
    assert t == pytest.approx(0.5)


def test_beam_t_full_clear_is_one():
    t = S.beam_referenced_t(220.0, 20.0, 200.0)
    assert t == pytest.approx(1.0)


def test_beam_t_negative_numerator_guard_returns_zero():
    # Measurement below the modeled diffuse floor => T pinned to 0, NOT negative.
    t = S.beam_referenced_t(10.0, 30.0, 200.0)
    assert t == SHADEMAP_TAU_MIN == 0.0
    # exactly at the floor -> also zero (no usable beam)
    assert S.beam_referenced_t(30.0, 30.0, 200.0) == 0.0


def test_beam_t_nonpositive_beam_returns_none():
    assert S.beam_referenced_t(120.0, 20.0, 0.0) is None
    assert S.beam_referenced_t(120.0, 20.0, -5.0) is None


def test_beam_t_clamps_to_ceiling():
    # Enhancement / over-measurement can push the ratio above 1 -> clamp to MAX.
    t = S.beam_referenced_t(400.0, 20.0, 200.0)  # raw 1.9
    assert t == SHADEMAP_TAU_MAX


def test_beam_t_non_finite_inputs_safe():
    assert S.beam_referenced_t(float("nan"), 20.0, 200.0) == 0.0  # numer<=0 -> 0
    assert S.beam_referenced_t(120.0, 20.0, float("nan")) is None


# ---------------------------------------------------------------------------
# update_bin (EMA)
# ---------------------------------------------------------------------------


def test_update_seeds_fresh_bin_at_measured():
    st = S.update_bin(
        ShademapState(), channel="M4", sun_az=150.0, sun_el=40.0,
        doy=DOY_SPRING, measured_t=0.3,
    )
    key = S.shademap_bin_key(150.0, 40.0, DOY_SPRING)
    binv = st.channels["M4"][key]
    assert binv.tau == pytest.approx(0.3)
    assert binv.n == 1


def test_update_blends_existing_bin():
    st = S.update_bin(
        ShademapState(), channel="M4", sun_az=150.0, sun_el=40.0,
        doy=DOY_SPRING, measured_t=0.4,
    )
    st = S.update_bin(
        st, channel="M4", sun_az=150.0, sun_el=40.0,
        doy=DOY_SPRING, measured_t=0.8,
    )
    key = S.shademap_bin_key(150.0, 40.0, DOY_SPRING)
    binv = st.channels["M4"][key]
    expected = (1 - SHADEMAP_EMA_ALPHA) * 0.4 + SHADEMAP_EMA_ALPHA * 0.8
    assert binv.tau == pytest.approx(expected)
    assert binv.n == 2


def test_update_clamps_measured_into_band():
    st = S.update_bin(
        ShademapState(), channel="M1", sun_az=90.0, sun_el=20.0,
        doy=DOY_SPRING, measured_t=5.0,  # way over ceiling
    )
    key = S.shademap_bin_key(90.0, 20.0, DOY_SPRING)
    assert st.channels["M1"][key].tau == SHADEMAP_TAU_MAX


def test_update_does_not_mutate_input_state():
    st0 = ShademapState()
    st1 = S.update_bin(
        st0, channel="M1", sun_az=90.0, sun_el=20.0, doy=DOY_SPRING, measured_t=0.5
    )
    assert st0.channels == {}  # original untouched
    assert st1 is not st0
    # updating st1 again must not retro-mutate st1's earlier snapshot dict
    st2 = S.update_bin(
        st1, channel="M1", sun_az=90.0, sun_el=20.0, doy=DOY_SPRING, measured_t=0.9
    )
    key = S.shademap_bin_key(90.0, 20.0, DOY_SPRING)
    assert st1.channels["M1"][key].n == 1
    assert st2.channels["M1"][key].n == 2


def test_update_separates_channels_and_seasons():
    st = ShademapState()
    st = S.update_bin(st, channel="M4", sun_az=150.0, sun_el=40.0,
                      doy=DOY_SPRING, measured_t=0.45)
    st = S.update_bin(st, channel="M4", sun_az=150.0, sun_el=40.0,
                      doy=DOY_SUMMER, measured_t=0.80)
    st = S.update_bin(st, channel="M8", sun_az=150.0, sun_el=40.0,
                      doy=DOY_SPRING, measured_t=0.99)
    # M4 has two distinct half-year bins; M8 is a separate channel.
    assert len(st.channels["M4"]) == 2
    assert len(st.channels["M8"]) == 1


# ---------------------------------------------------------------------------
# effective_tau (shrinkage blend + prior pass-through)
# ---------------------------------------------------------------------------


def test_effective_tau_unvisited_returns_prior_exactly():
    st = ShademapState()
    for prior in (0.0, 0.2, 0.45, 0.8, 1.0):
        got = S.effective_tau(
            st, channel="M4", sun_az=150.0, sun_el=40.0, doy=DOY_SPRING,
            static_prior=prior,
        )
        assert got == prior


def test_effective_tau_absent_channel_returns_prior():
    st = S.update_bin(
        ShademapState(), channel="OTHER", sun_az=150.0, sun_el=40.0,
        doy=DOY_SPRING, measured_t=0.1,
    )
    got = S.effective_tau(
        st, channel="M4", sun_az=150.0, sun_el=40.0, doy=DOY_SPRING,
        static_prior=0.7,
    )
    assert got == 0.7


def test_effective_tau_shrinkage_blend_formula():
    # Build a bin with a known learned tau and n, then check the blend weight.
    learned = 0.2
    n = 10
    key = S.shademap_bin_key(150.0, 40.0, DOY_SPRING)
    st = ShademapState(channels={"M4": {key: ShademapBin(tau=learned, n=n)}})
    prior = 0.8
    w = n / (n + SHADEMAP_SHRINKAGE_K)
    expected = w * learned + (1 - w) * prior
    got = S.effective_tau(
        st, channel="M4", sun_az=150.0, sun_el=40.0, doy=DOY_SPRING,
        static_prior=prior,
    )
    assert got == pytest.approx(expected)
    # w < 1 always => the learned value never fully dominates a small n.
    assert 0.0 < w < 1.0


def test_effective_tau_more_samples_pull_toward_learned():
    key = S.shademap_bin_key(150.0, 40.0, DOY_SPRING)
    prior, learned = 0.9, 0.1

    def blend(n):
        st = ShademapState(channels={"M4": {key: ShademapBin(tau=learned, n=n)}})
        return S.effective_tau(
            st, channel="M4", sun_az=150.0, sun_el=40.0, doy=DOY_SPRING,
            static_prior=prior,
        )

    few, many = blend(2), blend(200)
    # More evidence => closer to the learned (shaded) value.
    assert abs(many - learned) < abs(few - learned)


def test_effective_tau_wall_bin_can_reach_full_occlusion():
    key = S.shademap_bin_key(215.0, 30.0, DOY_SUMMER)
    st = ShademapState(channels={"M8": {key: ShademapBin(tau=0.0, n=10_000)}})
    got = S.effective_tau(
        st, channel="M8", sun_az=215.0, sun_el=30.0, doy=DOY_SUMMER,
        static_prior=0.0,
    )
    assert got == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# apply_shademap_to_beam
# ---------------------------------------------------------------------------


def test_apply_beam_multiplies_by_tau():
    assert S.apply_shademap_to_beam(200.0, tau=0.4) == pytest.approx(80.0)


def test_apply_beam_clamps_tau_and_floors_negative_beam():
    assert S.apply_shademap_to_beam(100.0, tau=5.0) == pytest.approx(
        100.0 * SHADEMAP_TAU_MAX
    )
    assert S.apply_shademap_to_beam(-50.0, tau=0.5) == 0.0
    assert S.apply_shademap_to_beam(0.0, tau=0.5) == 0.0


# ---------------------------------------------------------------------------
# dump_polar_table
# ---------------------------------------------------------------------------


def test_dump_polar_table_rows_and_centres():
    st = ShademapState()
    st = S.update_bin(st, channel="M4", sun_az=150.0, sun_el=40.0,
                      doy=DOY_SPRING, measured_t=0.45)
    st = S.update_bin(st, channel="M8", sun_az=215.0, sun_el=30.0,
                      doy=DOY_SUMMER, measured_t=0.0)
    table = S.dump_polar_table(st)
    assert len(table) == 2
    row = next(r for r in table if r["channel"] == "M4")
    assert set(row) == {"channel", "az", "elev", "halfyear", "tau", "n"}
    # az 150 -> idx 30 -> centre (30 + 0.5)*5 = 152.5; el 40 -> idx 16 ->
    # centre (16 + 0.5)*2.5 = 41.25
    assert row["az"] == pytest.approx(152.5)
    assert row["elev"] == pytest.approx(41.25)
    assert row["halfyear"] == 0
    assert row["tau"] == pytest.approx(0.45)
    assert row["n"] == 1


def test_dump_polar_table_empty_state():
    assert S.dump_polar_table(ShademapState()) == []


def test_dump_polar_table_skips_malformed_keys_and_sorts():
    st = ShademapState(
        channels={
            "M4": {
                "garbage": ShademapBin(tau=0.5, n=3),  # malformed key -> skipped
                "10:4:0": ShademapBin(tau=0.6, n=2),
                "2:4:0": ShademapBin(tau=0.7, n=1),
            }
        }
    )
    table = S.dump_polar_table(st)
    assert len(table) == 2  # malformed dropped, no raise
    # sorted by az ascending -> az idx 2 (centre 12.5) before az idx 10 (52.5)
    assert [r["az"] for r in table] == sorted(r["az"] for r in table)
    assert table[0]["az"] == pytest.approx(12.5)


# ---------------------------------------------------------------------------
# ingest_bootstrap_shademap (n-credit cap)
# ---------------------------------------------------------------------------


def test_bootstrap_caps_bin_n():
    raw = {
        "version": 1,
        "channels": {
            "M4": {
                "10:4:0": {"tau": 0.4, "n": 999},
                "11:4:0": {"tau": 0.6, "n": 2},
            }
        },
    }
    st = S.ingest_bootstrap_shademap(raw, max_bin_n=BOOTSTRAP_MAX_BIN_N)
    assert st.channels["M4"]["10:4:0"].n == BOOTSTRAP_MAX_BIN_N  # capped
    assert st.channels["M4"]["10:4:0"].tau == pytest.approx(0.4)
    assert st.channels["M4"]["11:4:0"].n == 2  # under the cap, untouched


def test_bootstrap_clamps_tau_and_survives_corruption():
    raw = {
        "channels": {
            "M4": {"10:4:0": {"tau": 9.0, "n": 100}},  # tau over ceiling
            "bad_channel": "not-a-dict",  # dropped by from_dict
        }
    }
    st = S.ingest_bootstrap_shademap(raw, max_bin_n=5)
    assert st.channels["M4"]["10:4:0"].tau == SHADEMAP_TAU_MAX
    assert st.channels["M4"]["10:4:0"].n == 5
    assert "bad_channel" not in st.channels


def test_bootstrap_non_dict_yields_empty_state():
    assert S.ingest_bootstrap_shademap(None, max_bin_n=5).channels == {}
    assert S.ingest_bootstrap_shademap("garbage", max_bin_n=5).channels == {}
    assert S.ingest_bootstrap_shademap([1, 2, 3], max_bin_n=5).channels == {}


# ---------------------------------------------------------------------------
# PROPERTY TESTS (task brief)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("measured", [-3.0, -0.1, 0.0, 0.3, 1.0, 1.1, 2.5, 100.0])
def test_property_tau_stays_in_band_after_updates(measured):
    """No sequence of measured T's can push a bin's tau outside [0, 1.1]."""
    st = ShademapState()
    for _ in range(50):
        st = S.update_bin(
            st, channel="M4", sun_az=150.0, sun_el=40.0,
            doy=DOY_SPRING, measured_t=measured,
        )
    for bins in st.channels.values():
        for binv in bins.values():
            assert SHADEMAP_TAU_MIN <= binv.tau <= SHADEMAP_TAU_MAX


def test_property_effective_tau_always_in_band():
    """effective_tau result is always clamped to the band, for any prior/bin."""
    key = S.shademap_bin_key(150.0, 40.0, DOY_SPRING)
    for learned in (0.0, 0.5, 1.1):
        for n in (0, 1, 5, 100):
            for prior in (-1.0, 0.0, 0.5, 1.0, 2.0):
                st = ShademapState(
                    channels={"M4": {key: ShademapBin(tau=learned, n=n)}}
                )
                got = S.effective_tau(
                    st, channel="M4", sun_az=150.0, sun_el=40.0,
                    doy=DOY_SPRING, static_prior=prior,
                )
                assert SHADEMAP_TAU_MIN <= got <= SHADEMAP_TAU_MAX


def test_property_unvisited_bin_returns_prior():
    """Any prior in the band round-trips exactly through an empty state."""
    st = ShademapState()
    for prior in [i / 20.0 for i in range(0, 23)]:  # 0.0 .. 1.1
        got = S.effective_tau(
            st, channel="ANY", sun_az=210.0, sun_el=15.0, doy=DOY_SUMMER,
            static_prior=prior,
        )
        assert got == prior


def test_property_half_year_separation_same_position():
    """Same sun position, different season => independently learned bins.

    Training the spring half must not leak into the summer half and vice versa
    (SPEC §5: April laublos vs. August belaubt must not alias).
    """
    st = ShademapState()
    # Train ONLY the spring half heavily toward a shaded (leafed-out=bare here)
    # value; leave summer untouched, then train summer toward a bright value.
    for _ in range(30):
        st = S.update_bin(st, channel="M4", sun_az=150.0, sun_el=40.0,
                          doy=DOY_SPRING, measured_t=0.8)
    for _ in range(30):
        st = S.update_bin(st, channel="M4", sun_az=150.0, sun_el=40.0,
                          doy=DOY_SUMMER, measured_t=0.3)
    spring = S.effective_tau(st, channel="M4", sun_az=150.0, sun_el=40.0,
                             doy=DOY_SPRING, static_prior=0.5)
    summer = S.effective_tau(st, channel="M4", sun_az=150.0, sun_el=40.0,
                             doy=DOY_SUMMER, static_prior=0.5)
    # Distinct outcomes prove the bins did not share state.
    assert spring > summer
    # And each pulled toward its own trained value.
    assert spring > 0.5   # trained toward 0.8
    assert summer < 0.5   # trained toward 0.3


def test_property_bin_key_is_stable_and_total():
    """Bin key never raises across the full sun-position domain and wraps az."""
    for az in range(-360, 721, 17):
        for el in range(-10, 90, 7):
            for doy in (1, 100, 172, 220, 366):
                key = S.shademap_bin_key(float(az), float(el), doy)
                parts = key.split(":")
                assert len(parts) == 3
                az_idx, el_idx, half = (int(p) for p in parts)
                assert 0 <= az_idx < math.ceil(360.0 / SHADEMAP_AZ_BIN_DEG)
                assert el_idx >= 0
                assert half in (0, 1)
