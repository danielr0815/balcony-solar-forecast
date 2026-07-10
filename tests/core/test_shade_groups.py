"""Pure-core tests for shade groups (SPEC §5) — HA-free.

Covers the two pure pieces of the READ-TIME shade-pooling feature:

  * ``PlaneConfig`` — the ``shade_group`` field round-trip (present / absent /
    blank -> None) and the ``shade_channel`` property (group vs. plane-name
    fallback), which is THE single definition of the plane -> group mapping;
  * ``shademap.effective_tau_pooled`` — the read-time pool: single channel is
    bit-identical to ``effective_tau``, several channels are n-weighted (exact
    arithmetic), missing-bin channels are skipped, an all-missing pool returns
    the static prior exactly, and a malformed bin is skipped (never raised).
"""

from __future__ import annotations

import math

import pytest
from balcony_solar_forecast.const import (
    CONF_AZIMUTH,
    CONF_PLANE_NAME,
    CONF_SHADE_GROUP,
    CONF_TILT,
    CONF_WP,
    SHADEMAP_SHRINKAGE_K,
)
from balcony_solar_forecast.core import shademap as S
from balcony_solar_forecast.core.types import (
    PlaneConfig,
    ShademapBin,
    ShademapState,
)


def _plane_dict(name: str, **extra) -> dict:
    d = {CONF_PLANE_NAME: name, CONF_AZIMUTH: 115.0, CONF_TILT: 70.0, CONF_WP: 370.0}
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# PlaneConfig.shade_group round-trip + shade_channel property
# ---------------------------------------------------------------------------


def test_plane_without_shade_group_roundtrips_without_key():
    p = PlaneConfig.from_dict(_plane_dict("M1"))
    assert p.shade_group is None
    # to_dict omits the key entirely when unset (backward compatible).
    assert CONF_SHADE_GROUP not in p.to_dict()
    # Round-trips back to the same config.
    assert PlaneConfig.from_dict(p.to_dict()) == p


def test_plane_with_shade_group_roundtrips():
    p = PlaneConfig.from_dict(_plane_dict("M1", **{CONF_SHADE_GROUP: "south"}))
    assert p.shade_group == "south"
    d = p.to_dict()
    assert d[CONF_SHADE_GROUP] == "south"
    assert PlaneConfig.from_dict(d) == p


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_plane_blank_shade_group_normalises_to_none(blank):
    p = PlaneConfig.from_dict(_plane_dict("M1", **{CONF_SHADE_GROUP: blank}))
    assert p.shade_group is None
    assert CONF_SHADE_GROUP not in p.to_dict()


def test_plane_shade_group_is_stripped():
    p = PlaneConfig.from_dict(_plane_dict("M1", **{CONF_SHADE_GROUP: "  south  "}))
    assert p.shade_group == "south"


def test_shade_channel_property_group_vs_fallback():
    grouped = PlaneConfig.from_dict(_plane_dict("M1", **{CONF_SHADE_GROUP: "south"}))
    plain = PlaneConfig.from_dict(_plane_dict("M2"))
    assert grouped.shade_channel == "south"   # group wins
    assert plain.shade_channel == "M2"         # falls back to the plane name


# ---------------------------------------------------------------------------
# shademap.effective_tau_pooled (read-time pooling)
# ---------------------------------------------------------------------------

# A concrete sun position + its canonical bin key (shared across the tests).
_AZ, _EL, _DOY = 200.0, 40.0, 172
_KEY = S.shademap_bin_key(_AZ, _EL, _DOY)


def _pooled(state, channels, prior):
    return S.effective_tau_pooled(
        state, channels=channels, sun_az=_AZ, sun_el=_EL, doy=_DOY,
        static_prior=prior,
    )


def _single(state, channel, prior):
    return S.effective_tau(
        state, channel=channel, sun_az=_AZ, sun_el=_EL, doy=_DOY,
        static_prior=prior,
    )


@pytest.mark.parametrize("prior", [0.0, 0.5, 0.73, 1.0])
def test_pooled_single_channel_is_bit_identical_to_effective_tau(prior):
    # A learned bin under one channel; the one-element pool must reproduce
    # effective_tau BIT-for-BIT (no (n*tau)/n rounding), for any static prior.
    state = ShademapState()
    for _ in range(37):
        state = S.update_bin(
            state, channel="M1", sun_az=_AZ, sun_el=_EL, doy=_DOY, measured_t=0.3
        )
    assert _pooled(state, ("M1",), prior) == _single(state, "M1", prior)


def test_pooled_single_empty_bin_returns_prior_like_effective_tau():
    # Unvisited bin -> both return exactly the prior.
    empty = ShademapState()
    assert _pooled(empty, ("M1",), 0.42) == _single(empty, "M1", 0.42) == 0.42


def test_pooled_two_channels_are_n_weighted_exact():
    # A(tau=0.2,n=2) + B(tau=0.8,n=6): tau_pool = (2*0.2+6*0.8)/8 = 0.65, n=8.
    # Blend vs prior 1.0 with K=20: (8*0.65 + 20*1.0)/28 = 25.2/28 = 0.9 exact.
    state = ShademapState(channels={
        "A": {_KEY: ShademapBin(tau=0.2, n=2)},
        "B": {_KEY: ShademapBin(tau=0.8, n=6)},
    })
    assert SHADEMAP_SHRINKAGE_K == 20.0
    assert _pooled(state, ("A", "B"), 1.0) == pytest.approx(0.9)
    # n-weighting, NOT a simple mean: the simple mean 0.5 would give 24/28≈0.857.
    assert _pooled(state, ("A", "B"), 1.0) != pytest.approx(24.0 / 28.0)


def test_pooled_skips_channels_without_the_bin():
    # Only A carries the bin; B is present but empty, C is entirely absent.
    # The pool must equal the single-A read (missing bins contribute nothing).
    state = ShademapState(channels={
        "A": {_KEY: ShademapBin(tau=0.2, n=5)},
        "B": {},
    })
    assert _pooled(state, ("A", "B", "C"), 0.9) == _single(state, "A", 0.9)


def test_pooled_all_missing_returns_prior_exactly():
    state = ShademapState(channels={"A": {_KEY: ShademapBin(tau=0.2, n=5)}})
    # None of the pooled channels holds this bin -> exactly the static prior.
    other_key_state = ShademapState(channels={"Z": {"0:0:0": ShademapBin(tau=0.1, n=9)}})
    assert _pooled(other_key_state, ("Z",), 0.66) == 0.66
    assert _pooled(state, ("B", "C"), 0.31) == 0.31


def test_pooled_skips_malformed_bin():
    # A malformed bin (non-finite tau) is skipped, never raised; the valid
    # channel alone drives the pooled result.
    good = ShademapBin(tau=0.2, n=4)
    state = ShademapState(channels={
        "A": {_KEY: good},
        "B": {_KEY: ShademapBin(tau=float("nan"), n=8)},
    })
    result = _pooled(state, ("A", "B"), 0.9)
    assert math.isfinite(result)
    # Identical to pooling A alone: the NaN bin contributed no weight.
    assert result == _pooled(state, ("A",), 0.9)


# ---------------------------------------------------------------------------
# shademap.channel_similarity (bin-wise per-channel comparison)
# ---------------------------------------------------------------------------


def test_channel_similarity_identical_channels_zero_diff():
    bins = {
        "0:0:0": ShademapBin(tau=0.3, n=5),
        "1:0:0": ShademapBin(tau=0.7, n=2),
        "2:1:1": ShademapBin(tau=0.5, n=9),
    }
    state = ShademapState(channels={"A": dict(bins), "B": dict(bins)})
    sim = S.channel_similarity(state, "A", "B")
    assert sim["common_bins"] == 3
    assert sim["weight"] == pytest.approx(5 + 2 + 9)
    assert sim["mean_abs_diff"] == 0.0
    assert sim["max_abs_diff"] == 0.0


def test_channel_similarity_disjoint_bins_common_zero():
    state = ShademapState(channels={
        "A": {"0:0:0": ShademapBin(tau=0.3, n=5)},
        "B": {"1:0:0": ShademapBin(tau=0.3, n=5)},
    })
    sim = S.channel_similarity(state, "A", "B")
    assert sim["common_bins"] == 0
    assert sim["weight"] == 0.0
    assert sim["mean_abs_diff"] is None
    assert sim["max_abs_diff"] is None


def test_channel_similarity_partial_overlap_weighted_mean_exact():
    # A and B share only {0:0:0, 1:0:0}; each has one private bin.
    state = ShademapState(channels={
        "A": {
            "0:0:0": ShademapBin(tau=0.2, n=2),
            "1:0:0": ShademapBin(tau=0.5, n=4),
            "9:9:1": ShademapBin(tau=0.9, n=1),   # A-only
        },
        "B": {
            "0:0:0": ShademapBin(tau=0.4, n=6),
            "1:0:0": ShademapBin(tau=0.5, n=3),
            "8:8:0": ShademapBin(tau=0.1, n=5),   # B-only
        },
    })
    sim = S.channel_similarity(state, "A", "B")
    # 0:0:0 -> w=min(2,6)=2, |0.2-0.4|=0.2 -> 0.4 ; 1:0:0 -> w=min(4,3)=3, 0 -> 0
    # weight 5, mean 0.4/5 = 0.08, max 0.2.
    assert sim["common_bins"] == 2
    assert sim["weight"] == pytest.approx(5.0)
    assert sim["mean_abs_diff"] == pytest.approx(0.08)
    assert sim["max_abs_diff"] == pytest.approx(0.2)


def test_channel_similarity_skips_malformed_bin():
    state = ShademapState(channels={
        "A": {
            "0:0:0": ShademapBin(tau=0.2, n=4),
            "1:0:0": ShademapBin(tau=float("nan"), n=8),  # malformed on A
        },
        "B": {
            "0:0:0": ShademapBin(tau=0.3, n=4),
            "1:0:0": ShademapBin(tau=0.9, n=8),
        },
    })
    sim = S.channel_similarity(state, "A", "B")
    # The NaN bin is dropped: only 0:0:0 contributes (never raises).
    assert sim["common_bins"] == 1
    assert sim["mean_abs_diff"] == pytest.approx(0.1)
    assert sim["max_abs_diff"] == pytest.approx(0.1)


def test_channel_similarity_missing_channel_common_zero():
    state = ShademapState(channels={"A": {"0:0:0": ShademapBin(tau=0.2, n=4)}})
    sim = S.channel_similarity(state, "A", "ZZZ")
    assert sim["common_bins"] == 0
    assert sim["weight"] == 0.0
    assert sim["mean_abs_diff"] is None
    assert sim["max_abs_diff"] is None


# ---------------------------------------------------------------------------
# shademap.suggest_shade_groups (complete-linkage grouping suggestion)
# ---------------------------------------------------------------------------


def _uniform_channel(tau, *, n=10, keys=("0:0:0", "1:0:0", "2:0:0")):
    """A channel with the same tau/n across a fixed set of bins."""
    return {k: ShademapBin(tau=tau, n=n) for k in keys}


def test_suggest_groups_near_identical_and_deviant():
    keys = ("0:0:0", "1:0:0", "2:0:0")
    state = ShademapState(channels={
        "A": _uniform_channel(0.30, keys=keys),
        "B": _uniform_channel(0.31, keys=keys),   # ~identical to A (diff ~0.01)
        "C": _uniform_channel(0.60, keys=keys),   # deviates (diff 0.3 vs A/B)
    })
    res = S.suggest_shade_groups(
        state, ["A", "B", "C"], max_diff=0.06, min_common_bins=3
    )
    groups = {tuple(g["planes"]): g for g in res["groups"]}
    assert set(groups) == {("A", "B"), ("C",)}
    assert groups[("A", "B")]["suggested_group"] == "A"
    assert groups[("A", "B")]["insufficient_data"] is False
    assert groups[("C",)]["suggested_group"] is None
    assert groups[("C",)]["insufficient_data"] is False
    verdicts = {(p["a"], p["b"]): p["verdict"] for p in res["pairs"]}
    assert verdicts[("A", "B")] == "similar"
    assert verdicts[("A", "C")] == "different"
    assert verdicts[("B", "C")] == "different"
    assert res["thresholds"] == {"max_diff": 0.06, "min_common_bins": 3}


def test_suggest_groups_complete_linkage_prevents_chaining():
    keys = ("0:0:0", "1:0:0", "2:0:0")
    state = ShademapState(channels={
        "A": _uniform_channel(0.30, keys=keys),
        "B": _uniform_channel(0.33, keys=keys),   # A-B 0.03 (smallest)
        "C": _uniform_channel(0.38, keys=keys),   # B-C 0.05 similar, A-C 0.08 far
    })
    res = S.suggest_shade_groups(
        state, ["A", "B", "C"], max_diff=0.06, min_common_bins=3
    )
    plane_sets = sorted(tuple(g["planes"]) for g in res["groups"])
    # A-B merges first (smallest diff); A vs C exceeds max_diff, so complete
    # linkage refuses to fold C in -> {A,B} + {C}, NOT one chained cluster.
    assert plane_sets == [("A", "B"), ("C",)]
    ab = next(g for g in res["groups"] if g["planes"] == ["A", "B"])
    assert ab["suggested_group"] == "A"
    verdicts = {(p["a"], p["b"]): p["verdict"] for p in res["pairs"]}
    assert verdicts[("A", "B")] == "similar"
    assert verdicts[("B", "C")] == "similar"
    assert verdicts[("A", "C")] == "different"


def test_suggest_groups_insufficient_common_bins_stays_singleton():
    keys = ("0:0:0", "1:0:0")  # only two shared bins
    state = ShademapState(channels={
        "A": _uniform_channel(0.30, keys=keys),
        "B": _uniform_channel(0.30, keys=keys),   # identical tau ...
    })
    res = S.suggest_shade_groups(
        state, ["A", "B"], max_diff=0.06, min_common_bins=5
    )
    # ... but too few shared bins to be evidence -> not grouped, verdict insuff.
    plane_sets = sorted(tuple(g["planes"]) for g in res["groups"])
    assert plane_sets == [("A",), ("B",)]
    for g in res["groups"]:
        assert g["insufficient_data"] is False   # they DO have data, just few bins
        assert g["suggested_group"] is None
    assert res["pairs"][0]["verdict"] == "insufficient"


def test_suggest_groups_empty_state_all_insufficient_singletons():
    res = S.suggest_shade_groups(
        ShademapState(), ["A", "B", "C"], max_diff=0.06, min_common_bins=3
    )
    assert [g["planes"] for g in res["groups"]] == [["A"], ["B"], ["C"]]
    for g in res["groups"]:
        assert g["insufficient_data"] is True
        assert g["suggested_group"] is None
    assert all(p["verdict"] == "insufficient" for p in res["pairs"])
    assert all(p["common_bins"] == 0 for p in res["pairs"])


def test_suggest_groups_suggested_name_is_first_in_config_order():
    keys = ("0:0:0", "1:0:0", "2:0:0")
    chan = _uniform_channel(0.40, keys=keys)
    state = ShademapState(channels={
        "A": dict(chan), "B": dict(chan), "C": dict(chan),
    })
    # Config order is [C, A, B]; the single merged cluster is named after C.
    res = S.suggest_shade_groups(
        state, ["C", "A", "B"], max_diff=0.06, min_common_bins=3
    )
    assert len(res["groups"]) == 1
    g = res["groups"][0]
    assert g["planes"] == ["C", "A", "B"]        # preserved config order
    assert g["suggested_group"] == "C"
    assert g["insufficient_data"] is False
