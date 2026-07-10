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
