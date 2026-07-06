"""Tests for the versioned store, schema v2 (owner: store).

Covers the store contract (SPEC §5/§6/§9):

  * v1 -> v2 migration is lossless (existing live install survives; the three
    v1 rings are preserved byte-for-byte, learner sections injected neutral);
  * a corrupt learner blob loads to NEUTRAL factors, never raising;
  * an unknown/future schema is discarded to an empty, well-formed state;
  * bootstrap ingestion validates the schema, clamps insane values, and caps
    the backfilled shademap bin credit (BOOTSTRAP_MAX_BIN_N);
  * the last-good-payload write gating is unchanged (time-gated, eMMC budget);
  * ring trims, idempotent record_*, snapshot ring, getters/setters.

``store.py`` imports Home Assistant (``Store``), so this whole module is
skipped where HA is not installed. The class is driven end-to-end against a
FakeStore (recording the async_* calls) — no running ``hass`` is needed — and
the pure state functions are exercised directly.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    BOOTSTRAP_KEY_BIAS,
    BOOTSTRAP_KEY_SCHEMA,
    BOOTSTRAP_KEY_SHADEMAP,
    BOOTSTRAP_MAX_BIN_N,
    BOOTSTRAP_SCHEMA_VERSION,
    DAY_AHEAD_BIAS_MAX,
    DAY_AHEAD_BIAS_MIN,
    LEARNER_SNAPSHOT_RING,
    PAYLOAD_MIN_SAVE_INTERVAL_SECONDS,
    RLS_MIN_SAMPLES,
    SHADEMAP_TAU_MAX,
    SHADEMAP_TAU_MIN,
    STORAGE_DATA_VERSION,
    STORAGE_DATA_VERSION_V2,
    STORE_KEY_ACTUALS_LOG,
    STORE_KEY_BIAS_STATE,
    STORE_KEY_DRIFT_STATE,
    STORE_KEY_ISSUED_LOG,
    STORE_KEY_LAST_PAYLOAD,
    STORE_KEY_LEARNER_SNAPSHOTS,
    STORE_KEY_SHADEMAP_STATE,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasCell,
    BiasState,
    DriftState,
    LearnerSnapshot,
    ShademapBin,
    ShademapState,
)
from custom_components.balcony_solar_forecast.store import (  # noqa: E402
    ForecastStore,
    _cap_shademap_credit,
    ingest_bootstrap,
    validate_state,
)

_SCHEMA_KEY = "schema_version"


# ===========================================================================
# Fake HA Store (records the four async_* methods; no hass needed)
# ===========================================================================


class FakeStore:
    """Minimal stand-in for homeassistant.helpers.storage.Store.

    Records delayed/immediate saves so the tests can assert the write gating
    and the migration-on-load behaviour without a running event loop's disk.
    """

    def __init__(self, initial=None):
        self._initial = initial
        self.saved = None  # last snapshot handed to async_save
        self.delay_saves = 0  # count of async_delay_save calls
        self.immediate_saves = 0
        self.removed = False
        self._last_delay_factory = None

    async def async_load(self):
        return self._initial

    def async_delay_save(self, data_func, delay):
        self.delay_saves += 1
        self._last_delay_factory = data_func

    async def async_save(self, data):
        self.immediate_saves += 1
        self.saved = data

    async def async_remove(self):
        self.removed = True

    def pending_snapshot(self):
        """Materialise the last delayed-save data factory (what would hit disk)."""
        return None if self._last_delay_factory is None else self._last_delay_factory()


def _store(initial=None) -> ForecastStore:
    """A ForecastStore wired to a FakeStore (no hass)."""
    return ForecastStore(None, "entry", store=FakeStore(initial))  # type: ignore[arg-type]


# ===========================================================================
# Fixtures: representative v1 / v2 payloads
# ===========================================================================


def _v1_payload():
    """A realistic v1 blob (rings only, no learner sections)."""
    return {
        _SCHEMA_KEY: STORAGE_DATA_VERSION,  # == 1
        STORE_KEY_LAST_PAYLOAD: {
            "fetched_at": "2026-07-06T10:00:00+00:00",
            "payload": {"minutely_15": {"shortwave_radiation": [1.0, 2.0]}},
        },
        STORE_KEY_ISSUED_LOG: {
            "2026-07-05": {
                "issued_at": "2026-07-05T01:30:00+00:00",
                "hourly_wh": {"2026-07-05T10:00:00+00:00": 123.0},
                "daily_kwh": {"2026-07-05": 6.5},
                "status": "fresh",
            }
        },
        STORE_KEY_ACTUALS_LOG: {
            "2026-07-05": {"M1": 300.0, "M4": 250.0},
        },
    }


# ===========================================================================
# Migration: v1 -> v2 lossless + schema advance
# ===========================================================================


def test_v1_migrates_losslessly():
    v1 = _v1_payload()
    state = validate_state(v1)

    # Schema advanced to v2.
    assert state[_SCHEMA_KEY] == STORAGE_DATA_VERSION_V2

    # The three v1 rings are preserved exactly.
    assert state[STORE_KEY_LAST_PAYLOAD] == v1[STORE_KEY_LAST_PAYLOAD]
    assert state[STORE_KEY_ISSUED_LOG] == v1[STORE_KEY_ISSUED_LOG]
    assert state[STORE_KEY_ACTUALS_LOG] == v1[STORE_KEY_ACTUALS_LOG]

    # Learner sections injected at neutral defaults.
    assert state[STORE_KEY_BIAS_STATE] == BiasState().to_dict()
    assert state[STORE_KEY_SHADEMAP_STATE] == ShademapState().to_dict()
    assert state[STORE_KEY_DRIFT_STATE] == DriftState().to_dict()
    assert state[STORE_KEY_LEARNER_SNAPSHOTS] == []


async def test_load_migrates_and_schedules_writeback():
    """Loading a v1 blob migrates it AND schedules a save so disk advances."""
    fake = FakeStore(_v1_payload())
    store = ForecastStore(None, "e", store=fake)  # type: ignore[arg-type]
    await store.async_load()

    assert fake.delay_saves == 1  # migration written back
    # The pending snapshot is at the current schema and keeps the rings.
    pending = fake.pending_snapshot()
    assert pending[_SCHEMA_KEY] == STORAGE_DATA_VERSION_V2
    assert store.get_actuals("2026-07-05") == {"M1": 300.0, "M4": 250.0}
    assert store.get_issued("2026-07-05") is not None


async def test_load_v2_no_needless_writeback():
    """A blob already at v2 loads without a migration writeback."""
    fake = FakeStore(validate_state(_v1_payload()))  # already v2
    store = ForecastStore(None, "e", store=fake)  # type: ignore[arg-type]
    await store.async_load()
    assert fake.delay_saves == 0


async def test_first_ever_empty_load_is_lazy():
    """A brand-new install (async_load -> None) writes nothing eagerly."""
    fake = FakeStore(None)
    store = ForecastStore(None, "e", store=fake)  # type: ignore[arg-type]
    await store.async_load()
    assert fake.delay_saves == 0
    assert store.get_last_payload() is None
    assert store.get_bias_state().cells == {}


# ===========================================================================
# Validate-and-clamp: corrupt / unknown blobs never crash
# ===========================================================================


@pytest.mark.parametrize("junk", [None, 42, "nope", [], {"no_schema": 1}])
def test_non_dict_or_schemaless_blob_yields_empty(junk):
    state = validate_state(junk)
    assert state[_SCHEMA_KEY] == STORAGE_DATA_VERSION_V2
    assert state[STORE_KEY_BIAS_STATE] == BiasState().to_dict()


def test_unknown_future_schema_discarded():
    state = validate_state({_SCHEMA_KEY: 999, STORE_KEY_ACTUALS_LOG: {"x": {"M1": 1}}})
    # Discarded to empty (do NOT trust an unknown future shape).
    assert state[STORE_KEY_ACTUALS_LOG] == {}
    assert state[_SCHEMA_KEY] == STORAGE_DATA_VERSION_V2


def test_corrupt_learner_blob_loads_neutral():
    """A v2 blob with garbage learner sections degrades to neutral factors."""
    corrupt = {
        _SCHEMA_KEY: STORAGE_DATA_VERSION_V2,
        STORE_KEY_LAST_PAYLOAD: None,
        STORE_KEY_ISSUED_LOG: {},
        STORE_KEY_ACTUALS_LOG: {},
        STORE_KEY_BIAS_STATE: "not-a-dict",
        STORE_KEY_SHADEMAP_STATE: [1, 2, 3],
        STORE_KEY_DRIFT_STATE: 3.14,
        STORE_KEY_LEARNER_SNAPSHOTS: "garbage",
    }
    state = validate_state(corrupt)
    bias = BiasState.from_dict(state[STORE_KEY_BIAS_STATE])
    shade = ShademapState.from_dict(state[STORE_KEY_SHADEMAP_STATE])
    drift = DriftState.from_dict(state[STORE_KEY_DRIFT_STATE])
    assert bias.cells == {}
    assert shade.channels == {}
    assert drift.fast_loss_streak == 0 and not drift.fast_disabled
    assert state[STORE_KEY_LEARNER_SNAPSHOTS] == []


def test_corrupt_bias_cell_clamped_into_band():
    """Insane per-cell values are clamped into the day-ahead band on load."""
    blob = {
        _SCHEMA_KEY: STORAGE_DATA_VERSION_V2,
        STORE_KEY_BIAS_STATE: {
            "version": 1,
            "cells": {
                "clear|midday": {"theta": 99.0, "covariance": -5.0, "n": 10},
                "fog|morning": {"theta": float("nan"), "covariance": 1.0, "n": 4},
            },
        },
    }
    state = validate_state(blob)
    bias = BiasState.from_dict(state[STORE_KEY_BIAS_STATE])
    c1 = bias.cells["clear|midday"]
    assert c1.theta == DAY_AHEAD_BIAS_MAX  # 99 -> clamped high
    assert c1.covariance >= 0.0  # negative P repaired
    c2 = bias.cells["fog|morning"]
    assert DAY_AHEAD_BIAS_MIN <= c2.theta <= DAY_AHEAD_BIAS_MAX  # NaN -> band


def test_corrupt_shademap_tau_clamped():
    blob = {
        _SCHEMA_KEY: STORAGE_DATA_VERSION_V2,
        STORE_KEY_SHADEMAP_STATE: {
            "version": 1,
            "channels": {
                "M4": {
                    "10:15:1": {"tau": 5.0, "n": 3},  # over max
                    "9:14:0": {"tau": -2.0, "n": 100},  # under min
                }
            },
        },
    }
    state = validate_state(blob)
    shade = ShademapState.from_dict(state[STORE_KEY_SHADEMAP_STATE])
    assert shade.channels["M4"]["10:15:1"].tau == SHADEMAP_TAU_MAX
    assert shade.channels["M4"]["9:14:0"].tau == SHADEMAP_TAU_MIN


# ===========================================================================
# Ring trims
# ===========================================================================


def test_issued_ring_trimmed_on_load():
    ring = {f"2026-01-{d:02d}": {"status": "fresh"} for d in range(1, 32)}
    ring.update({f"2026-02-{d:02d}": {"status": "fresh"} for d in range(1, 29)})
    ring.update({f"2026-03-{d:02d}": {"status": "fresh"} for d in range(1, 31)})
    # 31 + 28 + 30 = 89 -> add 5 more to exceed 90.
    ring.update({f"2026-04-{d:02d}": {"status": "fresh"} for d in range(1, 6)})
    assert len(ring) == 94
    state = validate_state({_SCHEMA_KEY: STORAGE_DATA_VERSION_V2, STORE_KEY_ISSUED_LOG: ring})
    kept = state[STORE_KEY_ISSUED_LOG]
    assert len(kept) == 90
    # Newest kept, oldest dropped.
    assert "2026-04-05" in kept
    assert "2026-01-01" not in kept


def test_actuals_ring_drops_non_dict_values():
    blob = {
        _SCHEMA_KEY: STORAGE_DATA_VERSION_V2,
        STORE_KEY_ACTUALS_LOG: {
            "2026-07-01": {"M1": 1.0},
            "2026-07-02": "bogus",  # non-dict -> dropped
            123: {"M1": 2.0},  # non-str key -> dropped
        },
    }
    state = validate_state(blob)
    assert set(state[STORE_KEY_ACTUALS_LOG]) == {"2026-07-01"}


# ===========================================================================
# Rings: idempotent record + runtime trim
# ===========================================================================


def test_record_issued_idempotent_and_trims():
    store = _store()
    for d in range(1, 96):
        store.record_issued(f"2026-01-{d:03d}", {"status": "fresh", "n": d})
    assert len(store.issued_dates()) == 90
    # Overwrite is idempotent (same key replaces, no growth).
    before = len(store.issued_dates())
    store.record_issued("2026-01-095", {"status": "cached"})
    assert len(store.issued_dates()) == before
    assert store.get_issued("2026-01-095")["status"] == "cached"


def test_record_actuals_copies_input():
    store = _store()
    src = {"M1": 100.0}
    store.record_actuals("2026-07-06", src)
    src["M1"] = 999.0  # mutate caller's dict
    assert store.get_actuals("2026-07-06") == {"M1": 100.0}  # stored copy intact
    assert store.has_actuals("2026-07-06")


# ===========================================================================
# Learner getters / setters round-trip
# ===========================================================================


def test_bias_state_roundtrip():
    store = _store()
    bs = BiasState(
        cells={
            BiasState.cell_key("clear", "midday"): BiasCell(theta=1.2, covariance=5.0, n=4),
        }
    )
    store.set_bias_state(bs)
    got = store.get_bias_state()
    # n >= RLS_MIN_SAMPLES so it applies.
    assert 4 >= RLS_MIN_SAMPLES
    assert got.get_bias("clear", "midday") == pytest.approx(1.2)


def test_shademap_state_roundtrip():
    store = _store()
    ss = ShademapState(channels={"M4": {"10:15:1": ShademapBin(tau=0.3, n=25)}})
    store.set_shademap_state(ss)
    got = store.get_shademap_state()
    assert got.channels["M4"]["10:15:1"].tau == pytest.approx(0.3)
    assert got.channels["M4"]["10:15:1"].n == 25


def test_drift_state_roundtrip():
    store = _store()
    ds = DriftState(
        daily_mae={"2026-07-05": {"raw": 1.0, "corrected": 0.8, "baseline": 1.2}},
        fast_loss_streak=3,
        slow_disabled=True,
    )
    store.set_drift_state(ds)
    got = store.get_drift_state()
    assert got.fast_loss_streak == 3
    assert got.slow_disabled is True
    assert got.daily_mae["2026-07-05"]["corrected"] == pytest.approx(0.8)


def test_get_bias_state_neutral_when_absent():
    store = _store()  # empty state
    assert store.get_bias_state().cells == {}
    assert store.get_shademap_state().channels == {}
    assert store.get_drift_state().fast_loss_streak == 0


# ===========================================================================
# Snapshot rollback ring
# ===========================================================================


def _snap(tag: str) -> LearnerSnapshot:
    return LearnerSnapshot(
        taken_at=f"2026-07-{tag}T01:30:00+00:00",
        bias=BiasState(cells={"clear|midday": BiasCell(theta=1.0 + int(tag) / 100)}),
        shademap=ShademapState(),
    )


def test_snapshot_ring_trims_to_capacity():
    store = _store()
    for i in range(LEARNER_SNAPSHOT_RING + 3):
        store.push_snapshot(_snap(f"{i + 1:02d}"))
    snaps = store.get_snapshots()
    assert len(snaps) == LEARNER_SNAPSHOT_RING
    # Newest kept (last pushed), oldest dropped.
    latest = store.latest_snapshot()
    assert latest is not None
    assert latest.taken_at == snaps[-1].taken_at
    expected_last = f"2026-07-{LEARNER_SNAPSHOT_RING + 3:02d}T01:30:00+00:00"
    assert latest.taken_at == expected_last


def test_latest_snapshot_none_when_empty():
    store = _store()
    assert store.latest_snapshot() is None
    assert store.get_snapshots() == []


def test_snapshots_validated_on_read():
    """A corrupt snapshot in the on-disk ring reads back as a neutral one."""
    blob = {
        _SCHEMA_KEY: STORAGE_DATA_VERSION_V2,
        STORE_KEY_LEARNER_SNAPSHOTS: [
            {"taken_at": "2026-07-05T01:30:00+00:00", "bias": "junk", "shademap": 5},
        ],
    }
    state = validate_state(blob)
    # _coerce_snapshots already normalised it; reading yields a neutral snap.
    store = _store(state)
    store._data = state  # inject validated state directly
    snaps = store.get_snapshots()
    assert len(snaps) == 1
    assert snaps[0].bias.cells == {}
    assert snaps[0].shademap.channels == {}


def test_snapshot_ring_over_capacity_on_load_trimmed():
    ring = [
        LearnerSnapshot(
            taken_at=f"2026-07-{i:02d}T01:30:00+00:00",
            bias=BiasState(),
            shademap=ShademapState(),
        ).to_dict()
        for i in range(1, LEARNER_SNAPSHOT_RING + 5)
    ]
    state = validate_state(
        {_SCHEMA_KEY: STORAGE_DATA_VERSION_V2, STORE_KEY_LEARNER_SNAPSHOTS: ring}
    )
    assert len(state[STORE_KEY_LEARNER_SNAPSHOTS]) == LEARNER_SNAPSHOT_RING
    # Kept the newest.
    kept_dates = [s["taken_at"] for s in state[STORE_KEY_LEARNER_SNAPSHOTS]]
    assert kept_dates[-1] == f"2026-07-{LEARNER_SNAPSHOT_RING + 4:02d}T01:30:00+00:00"


# ===========================================================================
# Bootstrap ingestion
# ===========================================================================


def _bootstrap(shademap_bins=None, bias_cells=None, schema=BOOTSTRAP_SCHEMA_VERSION):
    return {
        BOOTSTRAP_KEY_SCHEMA: schema,
        BOOTSTRAP_KEY_SHADEMAP: {
            "version": 1,
            "channels": {"M4": shademap_bins or {}},
        },
        BOOTSTRAP_KEY_BIAS: {
            "version": 1,
            "cells": bias_cells or {},
        },
    }


def test_bootstrap_rejects_wrong_schema():
    with pytest.raises(ValueError):
        ingest_bootstrap(ShademapState(), BiasState(), _bootstrap(schema=999))


def test_bootstrap_rejects_non_dict():
    with pytest.raises(ValueError):
        ingest_bootstrap(ShademapState(), BiasState(), ["not", "a", "dict"])
    with pytest.raises(ValueError):
        ingest_bootstrap(ShademapState(), BiasState(), None)


def test_bootstrap_caps_bin_credit():
    payload = _bootstrap(
        shademap_bins={
            "10:15:1": {"tau": 0.4, "n": 500},  # way over the cap
            "9:14:0": {"tau": 0.9, "n": 2},  # already under cap
        }
    )
    shademap, _bias = ingest_bootstrap(ShademapState(), BiasState(), payload)
    assert shademap.channels["M4"]["10:15:1"].n == BOOTSTRAP_MAX_BIN_N
    assert shademap.channels["M4"]["9:14:0"].n == 2  # untouched
    # tau still learned (and clamped).
    assert shademap.channels["M4"]["10:15:1"].tau == pytest.approx(0.4)


def test_bootstrap_clamps_insane_values():
    payload = _bootstrap(
        shademap_bins={"5:8:0": {"tau": 42.0, "n": 3}},
        bias_cells={"overcast|afternoon": {"theta": -10.0, "covariance": 1.0, "n": 5}},
    )
    shademap, bias = ingest_bootstrap(ShademapState(), BiasState(), payload)
    assert shademap.channels["M4"]["5:8:0"].tau == SHADEMAP_TAU_MAX  # 42 -> 1.1
    # bias theta clamped into band (applied value).
    assert bias.get_bias("overcast", "afternoon") == DAY_AHEAD_BIAS_MIN


def test_import_bootstrap_replaces_and_snapshots():
    """import_bootstrap swaps learner state and pushes a rollback snapshot."""
    store = _store()
    # Prime some existing learner state so we can prove the rollback captured it.
    store.set_shademap_state(
        ShademapState(channels={"M1": {"1:1:0": ShademapBin(tau=0.7, n=30)}})
    )
    store.set_bias_state(
        BiasState(cells={"clear|morning": BiasCell(theta=1.1, covariance=1.0, n=5)})
    )
    before_snaps = len(store.get_snapshots())

    payload = _bootstrap(
        shademap_bins={"10:15:1": {"tau": 0.3, "n": 99}},
        bias_cells={"fog|midday": {"theta": 0.9, "covariance": 2.0, "n": 4}},
    )
    store.import_bootstrap(payload)

    # New state is the (n-capped) bootstrap; the old M1 channel is gone (REPLACE).
    shade = store.get_shademap_state()
    assert "M1" not in shade.channels
    assert shade.channels["M4"]["10:15:1"].n == BOOTSTRAP_MAX_BIN_N

    # Rollback snapshot captured the PRIOR state.
    assert len(store.get_snapshots()) == before_snaps + 1
    rolled = store.latest_snapshot()
    assert rolled is not None
    assert rolled.shademap.channels["M1"]["1:1:0"].tau == pytest.approx(0.7)
    assert rolled.bias.get_bias("clear", "morning") == pytest.approx(1.1)


def test_import_bootstrap_raises_on_bad_schema_no_mutation():
    store = _store()
    store.set_shademap_state(
        ShademapState(channels={"M1": {"1:1:0": ShademapBin(tau=0.7, n=30)}})
    )
    with pytest.raises(ValueError):
        store.import_bootstrap(_bootstrap(schema=7))
    # State untouched, no snapshot pushed.
    assert "M1" in store.get_shademap_state().channels
    assert store.get_snapshots() == []


def test_cap_shademap_credit_pure():
    ss = ShademapState(
        channels={
            "M4": {
                "a": ShademapBin(tau=0.5, n=100),
                "b": ShademapBin(tau=0.9, n=1),
            }
        }
    )
    capped = _cap_shademap_credit(ss, BOOTSTRAP_MAX_BIN_N)
    assert capped.channels["M4"]["a"].n == BOOTSTRAP_MAX_BIN_N
    assert capped.channels["M4"]["b"].n == 1
    # tau preserved.
    assert capped.channels["M4"]["a"].tau == pytest.approx(0.5)


# ===========================================================================
# Last-good payload write gating (unchanged from v1 behaviour)
# ===========================================================================


def test_first_payload_write_scheduled():
    store = _store()
    fake = store._store  # type: ignore[attr-defined]
    store.set_last_payload({"p": 1}, "2026-07-06T10:00:00+00:00")
    assert fake.delay_saves == 1
    assert store.get_last_payload()["payload"] == {"p": 1}


def test_payload_write_time_gated(monkeypatch):
    """A second payload within the gate updates memory but NOT the disk."""
    store = _store()
    fake = store._store  # type: ignore[attr-defined]

    fake_now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])

    store.set_last_payload({"p": 1}, "t1")
    assert fake.delay_saves == 1

    # 1 hour later — still inside the ~6 h gate: memory fresh, no new write.
    fake_now[0] += 3600.0
    store.set_last_payload({"p": 2}, "t2")
    assert fake.delay_saves == 1  # no additional disk write
    assert store.get_last_payload()["payload"] == {"p": 2}  # memory updated

    # Past the gate: a write is scheduled again.
    fake_now[0] += PAYLOAD_MIN_SAVE_INTERVAL_SECONDS + 1.0
    store.set_last_payload({"p": 3}, "t3")
    assert fake.delay_saves == 2


async def test_flush_writes_immediately_and_resets_gate(monkeypatch):
    store = _store()
    fake = store._store  # type: ignore[attr-defined]

    fake_now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])

    store.set_last_payload({"p": 1}, "t1")
    assert fake.delay_saves == 1  # first payload scheduled a delayed save
    await store.async_flush()
    assert fake.immediate_saves == 1
    assert fake.saved[STORE_KEY_LAST_PAYLOAD]["payload"] == {"p": 1}

    # After a flush the gate is reset to "now"; an immediate follow-up write at
    # the same instant is still gated (flush counts as the last disk write), so
    # no ADDITIONAL delayed save beyond the first is scheduled.
    store.set_last_payload({"p": 2}, "t2")
    assert fake.delay_saves == 1  # unchanged: still inside the gate


async def test_remove_deletes_file():
    store = _store()
    await store.async_remove()
    assert store._store.removed is True  # type: ignore[attr-defined]


# ===========================================================================
# Setters schedule bundled writes
# ===========================================================================


def test_setters_schedule_saves():
    store = _store()
    fake = store._store  # type: ignore[attr-defined]
    store.set_bias_state(BiasState())
    store.set_shademap_state(ShademapState())
    store.set_drift_state(DriftState())
    store.push_snapshot(_snap("01"))
    store.record_issued("2026-07-06", {"status": "fresh"})
    store.record_actuals("2026-07-06", {"M1": 1.0})
    assert fake.delay_saves == 6


# ===========================================================================
# DriftState option-seen round-trip (FIX-5) + safe-int load contract
# ===========================================================================


def test_drift_state_option_seen_roundtrip():
    ds = DriftState(fast_option_seen=False, slow_option_seen=True,
                    collapse_frozen_date="2026-01-16")
    got = DriftState.from_dict(ds.to_dict())
    assert got.fast_option_seen is False
    assert got.slow_option_seen is True
    assert got.collapse_frozen_date == "2026-01-16"
    # An absent key round-trips as None (legacy blob, not a transition).
    legacy = DriftState.from_dict({"slow_disabled": True})
    assert legacy.fast_option_seen is None
    assert legacy.slow_option_seen is None


def test_corrupt_scalar_does_not_crash_load():
    """A v2 blob with string/NaN garbage in int/float fields loads neutral,
    never raising (types:523)."""
    blob = {
        _SCHEMA_KEY: STORAGE_DATA_VERSION_V2,
        STORE_KEY_DRIFT_STATE: {"version": "x", "fast_loss_streak": "?"},
        STORE_KEY_BIAS_STATE: {
            "version": 1,
            "cells": {"clear|midday": {"theta": 1.0, "covariance": "abc", "n": "?"}},
        },
        STORE_KEY_SHADEMAP_STATE: {
            "version": 1,
            "channels": {"M4": {"1:1:0": {"tau": 0.5, "n": "nope"}}},
        },
    }
    # validate_state must not raise.
    state = validate_state(blob)
    drift = DriftState.from_dict(state[STORE_KEY_DRIFT_STATE])
    assert drift.version == 1 and drift.fast_loss_streak == 0
    bias = BiasState.from_dict(state[STORE_KEY_BIAS_STATE])
    assert bias.cells["clear|midday"].n == 0
    shade = ShademapState.from_dict(state[STORE_KEY_SHADEMAP_STATE])
    assert shade.channels["M4"]["1:1:0"].n == 0


# ===========================================================================
# Hourly-actuals ring + site-signature bootstrap guard
# ===========================================================================


def test_hourly_actuals_roundtrip_and_ring():
    store = _store()
    store.record_hourly_actuals(
        "2026-07-06", {"M1": {"2026-07-06T10:00:00+00:00": 120.0}}
    )
    got = store.get_hourly_actuals("2026-07-06")
    assert got["M1"]["2026-07-06T10:00:00+00:00"] == pytest.approx(120.0)
    assert store.get_hourly_actuals("2020-01-01") is None


def test_bootstrap_rejects_wrong_site_signature():
    from custom_components.balcony_solar_forecast.const import (
        BOOTSTRAP_KEY_SITE_SIGNATURE,
    )

    payload = _bootstrap()
    payload[BOOTSTRAP_KEY_SITE_SIGNATURE] = "deadbeefdeadbeef"
    with pytest.raises(ValueError):
        ingest_bootstrap(
            ShademapState(), BiasState(), payload,
            expected_signature="0000000000000000",
        )
    # Matching signature is accepted.
    payload[BOOTSTRAP_KEY_SITE_SIGNATURE] = "0000000000000000"
    ingest_bootstrap(
        ShademapState(), BiasState(), payload,
        expected_signature="0000000000000000",
    )
    # A payload with NO signature is accepted (older backfill files).
    payload.pop(BOOTSTRAP_KEY_SITE_SIGNATURE, None)
    ingest_bootstrap(
        ShademapState(), BiasState(), payload,
        expected_signature="0000000000000000",
    )
