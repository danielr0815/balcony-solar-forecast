"""Tests for the store schema v3 additive migration (owner: store).

The v2 -> v3 migration is the HIGH-RISK item of v0.4 (SPEC §14). The LIVE
install (entry 01KWT809F7MHH97F8XCKEJTZ0M) has a POPULATED v2 store on disk
RIGHT NOW: shademap 7 channels / 851 bins, day-ahead 12 cells, drift + rollback
+ trained_days + the three v1 rings + the hourly-actuals ring. A v2 -> v3
migration that DROPS or RESETS any of that learner state is a CRITICAL failure.

This module proves the migration is:

  * ADDITIVE: every v2 key is carried through BYTE-FAITHFUL (a realistic
    populated v2 dict mirroring the live install migrates with ALL of it
    intact — down to per-bin tau/n, RLS theta/covariance/n, drift streaks +
    disable flags + option-seen + collapse-freeze, the rollback ring and the
    trained-day markers);
  * the three new v3 sections (quantile ring, scoreboard ring, comparison ring)
    are default-injected EMPTY;
  * validate-and-clamp on load: a corrupt v3 quantile/scoreboard/comparison
    blob degrades that section to empty/neutral WITHOUT touching any preserved
    section and WITHOUT raising (SPEC §5);
  * the schema stamp advances to v3 and the on-disk store is scheduled for a
    write-back so a live v2 install is upgraded on first load.

``store.py`` imports Home Assistant (``Store``), so this whole module is
skipped where HA is not installed. The pure state functions
(``validate_state`` / the migration branches) are exercised directly and the
``ForecastStore`` load path is driven against a FakeStore.
"""

from __future__ import annotations

import copy

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    STORAGE_DATA_VERSION,
    STORAGE_DATA_VERSION_V2,
    STORAGE_DATA_VERSION_V3,
    STORE_KEY_ACTUALS_LOG,
    STORE_KEY_BIAS_STATE,
    STORE_KEY_COMPARISON_RING,
    STORE_KEY_DRIFT_STATE,
    STORE_KEY_HOURLY_ACTUALS,
    STORE_KEY_ISSUED_LOG,
    STORE_KEY_LAST_PAYLOAD,
    STORE_KEY_LEARNER_SNAPSHOTS,
    STORE_KEY_QUANTILE_STATE,
    STORE_KEY_SCOREBOARD_STATE,
    STORE_KEY_SHADEMAP_STATE,
    STORE_KEY_TRAINED_DAYS,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasState,
    DayScore,
    DriftState,
    LearnerSnapshot,
    QuantileState,
    ScoreboardState,
    ShademapState,
)
from custom_components.balcony_solar_forecast.store import (  # noqa: E402
    ForecastStore,
    _migrate_v2_to_v3,
    validate_state,
)

_SCHEMA_KEY = "schema_version"


# ===========================================================================
# Fake HA Store (records the async_* calls; no running hass needed)
# ===========================================================================


class FakeStore:
    def __init__(self, initial=None):
        self._initial = initial
        self.saved = None
        self.delay_saves = 0
        self.immediate_saves = 0
        self._last_delay_factory = None

    async def async_load(self):
        return self._initial

    def async_delay_save(self, data_func, delay):
        self.delay_saves += 1
        self._last_delay_factory = data_func

    async def async_save(self, data):
        self.immediate_saves += 1
        self.saved = data

    async def async_remove(self):  # pragma: no cover - unused here
        pass

    def pending_snapshot(self):
        return None if self._last_delay_factory is None else self._last_delay_factory()


def _store(initial=None) -> ForecastStore:
    return ForecastStore(None, "entry", store=FakeStore(initial))  # type: ignore[arg-type]


# ===========================================================================
# The live-install fixture: a REALISTIC, POPULATED v2 store dict
# ---------------------------------------------------------------------------
# Mirrors what the live entry 01KWT809F7MHH97F8XCKEJTZ0M has on disk: every v2
# section populated with clean, in-band data so the round-trip is the identity.
# (A smaller but structurally-faithful stand-in for the 7-channel / 851-bin /
# 12-cell live map — the migration path is bin-count-agnostic.)
# ===========================================================================

# Cloud classes x day parts -> a realistic 12-cell day-ahead bias map.
_CLOUD_CLASSES = ("clear", "mixed", "overcast", "fog")
_DAY_PARTS = ("morning", "midday", "afternoon")


def _twelve_bias_cells() -> dict:
    """12 in-band RLS cells (4 classes x 3 day parts) — the live cell count."""
    cells = {}
    i = 0
    for cc in _CLOUD_CLASSES:
        for dp in _DAY_PARTS:
            i += 1
            cells[f"{cc}|{dp}"] = {
                # thetas kept inside [DAY_AHEAD_BIAS_MIN, MAX] so the round-trip
                # is a byte-identity (no clamp).
                "theta": 0.90 + 0.01 * i,
                "covariance": 0.5 + 0.02 * i,
                "n": 5 + i,
            }
    assert len(cells) == 12
    return cells


def _shademap_channels(n_channels: int = 7, bins_per_channel: int = 5) -> dict:
    """A multi-channel shademap (structurally the live 7-channel map).

    Every tau is inside [SHADEMAP_TAU_MIN, MAX] and every n is a plain int, so
    the clamping round-trip in the migration is the identity.
    """
    channels: dict = {}
    for ci in range(n_channels):
        chan = f"M{ci + 1}"
        bins: dict = {}
        for bi in range(bins_per_channel):
            az_idx = 5 + bi
            el_idx = 8 + bi
            half = bi % 2
            bins[f"{az_idx}:{el_idx}:{half}"] = {
                "tau": 0.30 + 0.05 * bi,  # in-band
                "n": 20 + bi,
            }
        channels[chan] = bins
    return channels


def _populated_v2_store() -> dict:
    """A realistic, fully-populated v2 store dict (the live-install shape)."""
    return {
        _SCHEMA_KEY: STORAGE_DATA_VERSION_V2,  # == 2
        # --- v1 rings ---
        STORE_KEY_LAST_PAYLOAD: {
            "fetched_at": "2026-07-06T10:00:00+00:00",
            "payload": {
                "minutely_15": {"shortwave_radiation": [1.0, 2.0, 3.0]},
                "hourly": {"cloud_cover_low": [10, 20]},
            },
        },
        STORE_KEY_ISSUED_LOG: {
            "2026-07-05": {
                "version": 2,
                "issued_at": "2026-07-05T01:30:00+00:00",
                "status": "fresh",
                "raw_hourly_wh": {"2026-07-05T10:00:00+00:00": 120.0},
                "corrected_hourly_wh": {"2026-07-05T10:00:00+00:00": 131.0},
                "raw_daily_kwh": {"2026-07-05": 6.1},
                "corrected_daily_kwh": {"2026-07-05": 6.6},
                "per_plane": {
                    "M1": {
                        "beam_wh": {"2026-07-05T10:00:00+00:00": 80.0},
                        "diffuse_wh": {"2026-07-05T10:00:00+00:00": 40.0},
                        "ghi": {"2026-07-05T10:00:00+00:00": 500.0},
                        "kc": {"2026-07-05T10:00:00+00:00": 0.9},
                    }
                },
                "cloud_class_by_hour": {"2026-07-05T10:00:00+00:00": "clear"},
            }
        },
        STORE_KEY_ACTUALS_LOG: {
            "2026-07-05": {"M1": 300.0, "M4": 250.0, "M7": 210.0},
        },
        # --- v2 learner sections ---
        STORE_KEY_HOURLY_ACTUALS: {
            "2026-07-05": {
                "M1": {"2026-07-05T10:00:00+00:00": 118.0},
                "M4": {"2026-07-05T10:00:00+00:00": 95.0},
            }
        },
        STORE_KEY_BIAS_STATE: {"version": 1, "cells": _twelve_bias_cells()},
        STORE_KEY_SHADEMAP_STATE: {
            "version": 1,
            "channels": _shademap_channels(),
        },
        STORE_KEY_DRIFT_STATE: {
            "version": 1,
            "daily_mae": {
                "2026-07-04": {"raw": 1.2, "corrected": 0.9, "baseline": 1.4},
                "2026-07-05": {"raw": 1.1, "corrected": 0.85, "baseline": 1.3},
            },
            "fast_loss_streak": 2,
            "slow_loss_streak": 1,
            "fast_disabled": False,
            "slow_disabled": True,
            "fast_option_seen": True,
            "slow_option_seen": False,
            "collapse_frozen_date": "2026-01-16",
        },
        STORE_KEY_LEARNER_SNAPSHOTS: [
            {
                "taken_at": "2026-07-04T01:30:00+00:00",
                "bias": {"version": 1, "cells": _twelve_bias_cells()},
                "shademap": {
                    "version": 1,
                    "channels": _shademap_channels(n_channels=2, bins_per_channel=2),
                },
            },
            {
                "taken_at": "2026-07-05T01:30:00+00:00",
                "bias": {"version": 1, "cells": _twelve_bias_cells()},
                "shademap": {
                    "version": 1,
                    "channels": _shademap_channels(n_channels=3, bins_per_channel=2),
                },
            },
        ],
        STORE_KEY_TRAINED_DAYS: ["2026-07-03", "2026-07-04", "2026-07-05"],
    }


# ===========================================================================
# CRITICAL: populated v2 -> v3 preserves EVERY learner section byte-faithful
# ===========================================================================


def test_populated_v2_migrates_with_all_learner_state_intact():
    """The live-install fixture migrates to v3 with every section preserved.

    This is the CRITICAL v0.4 invariant (SPEC §14): the migration is the
    identity on clean data for every preserved section, and injects only the
    three new v3 sections empty.
    """
    v2 = _populated_v2_store()
    before = copy.deepcopy(v2)  # guard against in-place mutation of the input

    state = validate_state(v2)

    # 1. Schema stamp advanced to v3.
    assert state[_SCHEMA_KEY] == STORAGE_DATA_VERSION_V3

    # 2. The three v1 rings carried through byte-faithful.
    assert state[STORE_KEY_LAST_PAYLOAD] == before[STORE_KEY_LAST_PAYLOAD]
    assert state[STORE_KEY_ISSUED_LOG] == before[STORE_KEY_ISSUED_LOG]
    assert state[STORE_KEY_ACTUALS_LOG] == before[STORE_KEY_ACTUALS_LOG]

    # 3. The hourly-actuals ring carried through byte-faithful.
    assert state[STORE_KEY_HOURLY_ACTUALS] == before[STORE_KEY_HOURLY_ACTUALS]

    # 4. Day-ahead bias — all 12 cells preserved down to theta/covariance/n.
    bias = BiasState.from_dict(state[STORE_KEY_BIAS_STATE])
    assert len(bias.cells) == 12
    for key, raw in before[STORE_KEY_BIAS_STATE]["cells"].items():
        cell = bias.cells[key]
        assert cell.theta == pytest.approx(raw["theta"])
        assert cell.covariance == pytest.approx(raw["covariance"])
        assert cell.n == raw["n"]
    # to_dict is the identity on the persisted bias section.
    assert state[STORE_KEY_BIAS_STATE] == before[STORE_KEY_BIAS_STATE]

    # 5. Shademap — all 7 channels + every bin's tau/n preserved.
    shade = ShademapState.from_dict(state[STORE_KEY_SHADEMAP_STATE])
    raw_channels = before[STORE_KEY_SHADEMAP_STATE]["channels"]
    assert set(shade.channels) == set(raw_channels)
    assert len(shade.channels) == 7
    total_bins = 0
    for chan, raw_bins in raw_channels.items():
        assert set(shade.channels[chan]) == set(raw_bins)
        for bk, rb in raw_bins.items():
            b = shade.channels[chan][bk]
            assert b.tau == pytest.approx(rb["tau"])
            assert b.n == rb["n"]
            total_bins += 1
    assert total_bins == 7 * 5
    assert state[STORE_KEY_SHADEMAP_STATE] == before[STORE_KEY_SHADEMAP_STATE]

    # 6. Drift — MAE window + streaks + disable flags + option-seen + freeze.
    drift = DriftState.from_dict(state[STORE_KEY_DRIFT_STATE])
    assert drift.daily_mae["2026-07-05"]["corrected"] == pytest.approx(0.85)
    assert drift.fast_loss_streak == 2
    assert drift.slow_loss_streak == 1
    assert drift.fast_disabled is False
    assert drift.slow_disabled is True
    assert drift.fast_option_seen is True
    assert drift.slow_option_seen is False
    assert drift.collapse_frozen_date == "2026-01-16"

    # 7. Rollback ring — both snapshots preserved (order + nested state).
    snaps = state[STORE_KEY_LEARNER_SNAPSHOTS]
    assert len(snaps) == 2
    assert snaps == before[STORE_KEY_LEARNER_SNAPSHOTS]
    parsed = [LearnerSnapshot.from_dict(s) for s in snaps]
    assert parsed[0].taken_at == "2026-07-04T01:30:00+00:00"
    assert parsed[1].taken_at == "2026-07-05T01:30:00+00:00"
    assert len(parsed[1].shademap.channels) == 3

    # 8. Trained-day markers preserved.
    assert state[STORE_KEY_TRAINED_DAYS] == ["2026-07-03", "2026-07-04", "2026-07-05"]

    # 9. The three NEW v3 sections are present + EMPTY.
    assert state[STORE_KEY_QUANTILE_STATE] == QuantileState().to_dict()
    assert state[STORE_KEY_SCOREBOARD_STATE] == ScoreboardState().to_dict()
    assert state[STORE_KEY_COMPARISON_RING] == {}
    assert QuantileState.from_dict(state[STORE_KEY_QUANTILE_STATE]).bins == {}
    assert ScoreboardState.from_dict(state[STORE_KEY_SCOREBOARD_STATE]).days == {}

    # 10. The input dict was NOT mutated in place.
    assert v2 == before


def test_migrate_v2_to_v3_direct_call_is_identity_on_clean_data():
    """The migration branch, called directly, preserves clean data verbatim."""
    v2 = _populated_v2_store()
    state = _migrate_v2_to_v3(copy.deepcopy(v2))
    assert state[_SCHEMA_KEY] == STORAGE_DATA_VERSION_V3
    # Each preserved section is byte-identical to the v2 input.
    for key in (
        STORE_KEY_LAST_PAYLOAD,
        STORE_KEY_ISSUED_LOG,
        STORE_KEY_ACTUALS_LOG,
        STORE_KEY_HOURLY_ACTUALS,
        STORE_KEY_BIAS_STATE,
        STORE_KEY_SHADEMAP_STATE,
        STORE_KEY_DRIFT_STATE,
        STORE_KEY_LEARNER_SNAPSHOTS,
        STORE_KEY_TRAINED_DAYS,
    ):
        assert state[key] == v2[key], f"section {key} not preserved"


# ===========================================================================
# The store LOAD path upgrades a live v2 install to v3 and writes it back
# ===========================================================================


async def test_load_v2_migrates_to_v3_and_schedules_writeback():
    """Loading the populated v2 blob migrates to v3 AND schedules a write-back.

    A live v2 install (schema differs from v3 on disk) must have its upgrade
    persisted so the next load is already at v3.
    """
    fake = FakeStore(_populated_v2_store())
    store = ForecastStore(None, "e", store=fake)  # type: ignore[arg-type]
    await store.async_load()

    # Migration written back to disk (schema advanced from 2 -> 3).
    assert fake.delay_saves == 1
    pending = fake.pending_snapshot()
    assert pending[_SCHEMA_KEY] == STORAGE_DATA_VERSION_V3

    # Learner state is queryable through the store accessors post-migration.
    assert store.get_shademap_state().channels["M4"]["6:9:1"].n == 21
    assert store.get_bias_state().cells["clear|midday"].n == 7
    assert store.get_drift_state().slow_disabled is True
    assert store.get_actuals("2026-07-05") == {"M1": 300.0, "M4": 250.0, "M7": 210.0}
    assert store.is_day_trained("2026-07-04")
    # v3 sections are present + empty.
    assert store.get_quantile_state().bins == {}
    assert store.get_scoreboard_state().days == {}
    assert store.get_comparison("2026-07-05") is None


async def test_load_v3_no_needless_writeback():
    """A blob already at v3 loads without a migration write-back."""
    fake = FakeStore(validate_state(_populated_v2_store()))  # already v3
    store = ForecastStore(None, "e", store=fake)  # type: ignore[arg-type]
    await store.async_load()
    assert fake.delay_saves == 0
    # And the learner state survived the v3 in-place validation identically.
    assert len(store.get_shademap_state().channels) == 7
    assert len(store.get_bias_state().cells) == 12


# ===========================================================================
# v1 -> (v2) -> v3 goes straight to v3 with the three new sections empty
# ===========================================================================


def test_v1_migrates_all_the_way_to_v3():
    v1 = {
        _SCHEMA_KEY: STORAGE_DATA_VERSION,  # == 1
        STORE_KEY_LAST_PAYLOAD: {
            "fetched_at": "2026-07-06T10:00:00+00:00",
            "payload": {"minutely_15": {"shortwave_radiation": [1.0]}},
        },
        STORE_KEY_ISSUED_LOG: {"2026-07-05": {"status": "fresh"}},
        STORE_KEY_ACTUALS_LOG: {"2026-07-05": {"M1": 300.0}},
    }
    state = validate_state(v1)
    assert state[_SCHEMA_KEY] == STORAGE_DATA_VERSION_V3
    # v1 rings preserved.
    assert state[STORE_KEY_ACTUALS_LOG] == {"2026-07-05": {"M1": 300.0}}
    # Learner + v3 sections neutral/empty.
    assert state[STORE_KEY_BIAS_STATE] == BiasState().to_dict()
    assert state[STORE_KEY_SHADEMAP_STATE] == ShademapState().to_dict()
    assert state[STORE_KEY_QUANTILE_STATE] == QuantileState().to_dict()
    assert state[STORE_KEY_SCOREBOARD_STATE] == ScoreboardState().to_dict()
    assert state[STORE_KEY_COMPARISON_RING] == {}


# ===========================================================================
# Validate-and-clamp: corrupt v3 sections degrade WITHOUT touching preserved
# state and WITHOUT raising (SPEC §5)
# ===========================================================================


def _v3_with_corrupt_new_sections() -> dict:
    """A well-formed v3 store whose THREE new sections are garbage."""
    base = validate_state(_populated_v2_store())  # clean v3
    base = copy.deepcopy(base)
    base[STORE_KEY_QUANTILE_STATE] = "not-a-dict"
    base[STORE_KEY_SCOREBOARD_STATE] = [1, 2, 3]
    base[STORE_KEY_COMPARISON_RING] = 3.14159
    return base


def test_corrupt_quantile_blob_degrades_to_empty_preserving_learners():
    """A corrupt quantile section -> empty ring; learner state untouched."""
    corrupt = _v3_with_corrupt_new_sections()
    # Snapshot the preserved sections BEFORE the validate pass.
    preserved_before = {
        k: copy.deepcopy(corrupt[k])
        for k in (
            STORE_KEY_BIAS_STATE,
            STORE_KEY_SHADEMAP_STATE,
            STORE_KEY_DRIFT_STATE,
            STORE_KEY_LEARNER_SNAPSHOTS,
            STORE_KEY_TRAINED_DAYS,
            STORE_KEY_ISSUED_LOG,
            STORE_KEY_ACTUALS_LOG,
            STORE_KEY_HOURLY_ACTUALS,
            STORE_KEY_LAST_PAYLOAD,
        )
    }

    state = validate_state(corrupt)  # must NOT raise

    # The three corrupt new sections degraded to empty/neutral.
    assert state[STORE_KEY_QUANTILE_STATE] == QuantileState().to_dict()
    assert state[STORE_KEY_SCOREBOARD_STATE] == ScoreboardState().to_dict()
    assert state[STORE_KEY_COMPARISON_RING] == {}
    assert QuantileState.from_dict(state[STORE_KEY_QUANTILE_STATE]).bins == {}

    # EVERY preserved learner section is byte-identical (untouched).
    for key, val in preserved_before.items():
        assert state[key] == val, f"corrupt-quantile load disturbed {key}"


def test_corrupt_quantile_bins_clamped_not_dropped():
    """Individual out-of-band relerrs are clamped; a garbage bin is dropped."""
    from custom_components.balcony_solar_forecast.const import (
        QUANTILE_REL_ERR_MAX,
        QUANTILE_REL_ERR_MIN,
    )

    blob = validate_state(_populated_v2_store())
    blob = copy.deepcopy(blob)
    blob[STORE_KEY_QUANTILE_STATE] = {
        "version": 1,
        "bins": {
            "clear|midday": [0.8, 99.0, -5.0, "junk", 1.1],  # clamp + drop junk
            "fog|morning": "not-a-list",  # dropped entirely
            42: [1.0],  # non-str key -> dropped
        },
    }
    state = validate_state(blob)
    qs = QuantileState.from_dict(state[STORE_KEY_QUANTILE_STATE])
    assert set(qs.bins) == {"clear|midday"}
    vals = qs.bins["clear|midday"]
    # 99 -> MAX, -5 -> MIN, "junk" dropped, others kept in order.
    assert vals == [
        0.8,
        QUANTILE_REL_ERR_MAX,
        QUANTILE_REL_ERR_MIN,
        1.1,
    ]


def test_corrupt_scoreboard_day_degrades_but_keeps_good_days():
    """A garbage DayScore entry degrades to a neutral score, good days kept."""
    blob = validate_state(_populated_v2_store())
    blob = copy.deepcopy(blob)
    good_day = DayScore(
        iso_date="2026-07-05",
        weather_class="clear",
        measured_kwh=6.4,
        engine_kwh=6.6,
        engine_daily_abs_err=0.2,
        comparison_kwh={"8-Entry Baseline": 6.0},
        comparison_daily_abs_err={"8-Entry Baseline": 0.4},
        engine_hourly_mae=12.0,
    )
    blob[STORE_KEY_SCOREBOARD_STATE] = {
        "version": 1,
        "days": {
            "2026-07-05": good_day.to_dict(),
            "2026-07-04": "not-a-dict",  # garbage -> neutral DayScore
        },
    }
    state = validate_state(blob)
    sb = ScoreboardState.from_dict(state[STORE_KEY_SCOREBOARD_STATE])
    assert set(sb.days) == {"2026-07-05", "2026-07-04"}
    # Good day round-trips faithfully.
    d = sb.days["2026-07-05"]
    assert d.weather_class == "clear"
    assert d.engine_daily_abs_err == pytest.approx(0.2)
    assert d.comparison_daily_abs_err["8-Entry Baseline"] == pytest.approx(0.4)
    assert d.engine_hourly_mae == pytest.approx(12.0)
    # Garbage day degraded to a neutral score (never raised).
    assert sb.days["2026-07-04"].measured_kwh == 0.0


def test_v3_comparison_ring_validated_and_trimmed():
    """The comparison ring keeps well-formed rows, drops garbage, trims."""
    blob = validate_state(_populated_v2_store())
    blob = copy.deepcopy(blob)
    ring = {
        f"2026-{m:02d}-{d:02d}": {"8-Entry Baseline": 6.0, "Alt 1600W": 5.5}
        for m in range(1, 5)
        for d in range(1, 26)
    }
    # 4 * 25 = 100 rows -> trimmed to the 90-day actuals window.
    ring["2026-07-05"] = {"8-Entry Baseline": "bogus", 42: 1.0}  # coerced/filtered
    ring["2026-07-06"] = "not-a-dict"  # dropped
    blob[STORE_KEY_COMPARISON_RING] = ring
    state = validate_state(blob)
    kept = state[STORE_KEY_COMPARISON_RING]
    assert len(kept) == 90  # trimmed
    assert "2026-07-06" not in kept  # non-dict row dropped
    # The bogus/int entries in a kept row are filtered out (row survives empty).
    if "2026-07-05" in kept:
        assert kept["2026-07-05"] == {}


def test_roundtrip_v3_state_is_stable():
    """validate_state is idempotent on an already-clean v3 state."""
    once = validate_state(_populated_v2_store())
    twice = validate_state(once)
    assert once == twice
