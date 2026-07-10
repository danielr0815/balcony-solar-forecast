"""Pure-core tests for shade groups (SPEC §5, Phase 5) — HA-free.

Covers the two pure pieces of the shared-shademap-channel feature:

  * ``PlaneConfig`` — the ``shade_group`` field round-trip (present / absent /
    blank -> None) and the ``shade_channel`` property (group vs. plane-name
    fallback), which is THE single definition of the plane -> channel mapping;
  * ``shademap.merge_channels`` — the migration merge that pools per-plane
    channels into a group channel: disjoint bins copied, overlapping bins
    combined by the n-weighted mean (exact arithmetic), sources dropped,
    unmapped channels untouched, empty state, and idempotence on re-run.
"""

from __future__ import annotations

import pytest
from balcony_solar_forecast.const import (
    CONF_AZIMUTH,
    CONF_PLANE_NAME,
    CONF_SHADE_GROUP,
    CONF_TILT,
    CONF_WP,
    SHADEMAP_TAU_MAX,
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
# shademap.merge_channels
# ---------------------------------------------------------------------------


def test_merge_disjoint_bins_are_copied():
    state = ShademapState(channels={
        "M1": {"a": ShademapBin(tau=0.4, n=3)},
        "M2": {"b": ShademapBin(tau=0.6, n=5)},
    })
    merged = S.merge_channels(state, {"M1": "south", "M2": "south"})
    assert set(merged.channels) == {"south"}
    south = merged.channels["south"]
    # Disjoint keys land side by side, unchanged.
    assert south["a"] == ShademapBin(tau=0.4, n=3)
    assert south["b"] == ShademapBin(tau=0.6, n=5)


def test_merge_overlapping_bin_is_n_weighted():
    state = ShademapState(channels={
        "M1": {"k": ShademapBin(tau=0.2, n=2)},
        "M2": {"k": ShademapBin(tau=0.8, n=6)},
    })
    merged = S.merge_channels(state, {"M1": "south", "M2": "south"})
    binv = merged.channels["south"]["k"]
    # n-weighted mean: (2*0.2 + 6*0.8) / (2+6) = (0.4 + 4.8) / 8 = 0.65; n = 8.
    assert binv.n == 8
    assert binv.tau == pytest.approx(0.65)


def test_merge_drops_source_keeps_targets_other_bins():
    state = ShademapState(channels={
        "M1": {"k": ShademapBin(tau=0.3, n=4)},
        "south": {"other": ShademapBin(tau=0.9, n=10)},
    })
    merged = S.merge_channels(state, {"M1": "south"})
    assert "M1" not in merged.channels
    south = merged.channels["south"]
    assert south["k"] == ShademapBin(tau=0.3, n=4)          # merged in
    assert south["other"] == ShademapBin(tau=0.9, n=10)     # untouched


def test_merge_leaves_unmapped_channels_untouched():
    state = ShademapState(channels={
        "M1": {"k": ShademapBin(tau=0.3, n=4)},
        "orphan": {"z": ShademapBin(tau=0.5, n=2)},
    })
    # Only M1 is in the mapping; "orphan" is not -> stays as-is.
    merged = S.merge_channels(state, {"M1": "M1"})
    assert merged.channels["M1"]["k"] == ShademapBin(tau=0.3, n=4)
    assert merged.channels["orphan"]["z"] == ShademapBin(tau=0.5, n=2)


def test_merge_empty_state_is_empty():
    merged = S.merge_channels(ShademapState(), {"M1": "south"})
    assert merged.channels == {}


def test_merge_is_pure_and_idempotent():
    state = ShademapState(channels={
        "M1": {"k": ShademapBin(tau=0.2, n=2)},
        "M2": {"k": ShademapBin(tau=0.8, n=6)},
    })
    mapping = {"M1": "south", "M2": "south"}
    merged = S.merge_channels(state, mapping)
    # Input untouched (pure).
    assert set(state.channels) == {"M1", "M2"}
    # Re-running the merge on the already-merged state (source channels gone,
    # "south" not in the mapping) is a no-op.
    again = S.merge_channels(merged, mapping)
    assert again.channels["south"]["k"] == merged.channels["south"]["k"]
    assert set(again.channels) == {"south"}


def test_merge_clamps_and_defaults_bin_from_dict_n_zero():
    # A directly-built n==0 bin (tau at the ceiling) must not divide-by-zero and
    # must stay clamped after the plain-mean fallback.
    state = ShademapState(channels={
        "M1": {"k": ShademapBin(tau=SHADEMAP_TAU_MAX, n=0)},
        "M2": {"k": ShademapBin(tau=SHADEMAP_TAU_MAX, n=0)},
    })
    merged = S.merge_channels(state, {"M1": "south", "M2": "south"})
    binv = merged.channels["south"]["k"]
    assert binv.n == 0
    assert binv.tau == pytest.approx(SHADEMAP_TAU_MAX)
