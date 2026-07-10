"""Regression tests for the two verify-pass findings (2026-07-06).

1. Per-day training idempotence: the restart-time catch-up re-sweeps the last
   processed day on EVERY HA start / options reload, and neither the RLS
   update nor the drift-streak counters are internally idempotent — without
   the persisted trained-day marker each restart double-counts the same
   training sample and double-increments the loss streak (a bad-weather week
   plus a few restarts would spuriously auto-disable a healthy learner).

2. Rollback restore path: the ring was write-only — an auto-disabled layer
   kept its poisoned state, so a later manual re-enable resumed from exactly
   the state that caused the disable. Now the auto-disable rolls the layer
   back to its pre-streak snapshot, and ``rollback_learners`` exposes a
   manual restore.

Same bare-coordinator harness as ``test_coordinator_learning`` (built via
``__new__``; HA required for the import, module skipped otherwise).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    DRIFT_LOSS_STREAK_DAYS,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasCell,
    BiasState,
    IssuedSnapshot,
    LearnerConfig,
    LearnerSnapshot,
    ShademapBin,
    ShademapState,
)
from tests.test_coordinator_learning import (  # noqa: E402
    _FakeStore,
    _make_coordinator,
)


def _losing_issued(iso: str) -> dict:
    """A day whose corrected curve loses badly against raw (drift 'losing')."""
    return IssuedSnapshot(
        issued_at="x",
        status="fresh",
        raw_hourly_wh={f"{iso}T11:00:00+00:00": 1000.0},
        corrected_hourly_wh={f"{iso}T11:00:00+00:00": 2000.0},
    ).to_dict()


# ---------------------------------------------------------------------------
# 1) Per-day training idempotence
# ---------------------------------------------------------------------------


async def test_train_and_guard_second_run_is_noop(monkeypatch):
    """Re-running the same day (restart catch-up) must not re-train."""
    c = _make_coordinator()
    c._learner_config = LearnerConfig(fast_enabled=True, slow_enabled=False)
    iso = "2026-05-01"
    day = datetime.fromisoformat(iso + "T00:00:00+00:00").date()
    c._store.issued[iso] = _losing_issued(iso)
    c._store.actuals[iso] = {"M1": 1000.0}

    drift_calls: list[str] = []
    orig = c._update_drift

    def _spy(iso_arg, issued, actuals):
        drift_calls.append(iso_arg)
        return orig(iso_arg, issued, actuals)

    monkeypatch.setattr(c, "_update_drift", _spy)

    await c._train_and_guard(day)
    assert c._store.is_day_trained(iso) is True
    assert c._drift_state.fast_loss_streak == 1

    # Second sweep (simulated restart catch-up): full no-op.
    await c._train_and_guard(day)
    assert drift_calls == [iso]  # drift ran exactly once
    assert c._drift_state.fast_loss_streak == 1  # not double-incremented


async def test_day_without_actuals_is_retried_later():
    """A day whose actuals arrive late must NOT be marked trained early."""
    c = _make_coordinator()
    c._learner_config = LearnerConfig(fast_enabled=True, slow_enabled=False)
    iso = "2026-05-02"
    day = datetime.fromisoformat(iso + "T00:00:00+00:00").date()
    c._store.issued[iso] = _losing_issued(iso)
    # No actuals yet (LTS lag / recorder gap).
    await c._train_and_guard(day)
    assert c._store.is_day_trained(iso) is False
    assert c._drift_state.fast_loss_streak == 0

    # Actuals arrive; the retry now trains and marks the day.
    c._store.actuals[iso] = {"M1": 1000.0}
    await c._train_and_guard(day)
    assert c._store.is_day_trained(iso) is True
    assert c._drift_state.fast_loss_streak == 1


# ---------------------------------------------------------------------------
# 2) Rollback restore path
# ---------------------------------------------------------------------------


def _distinct_snapshot(tag: float) -> LearnerSnapshot:
    return LearnerSnapshot(
        taken_at=f"2026-04-0{int(tag)}T01:30:00+00:00",
        bias=BiasState(cells={"clear|midday": BiasCell(theta=1.0 + tag / 100)}),
        shademap=ShademapState(
            channels={"M1": {"10:5:0": ShademapBin(tau=tag / 10.0, n=int(tag))}}
        ),
    )


def test_auto_disable_restores_pre_streak_state(monkeypatch):
    """Auto-disable must roll the losing layer back to its ring snapshot."""
    c = _make_coordinator()
    monkeypatch.setattr(c, "_raise_repair_issue", lambda issue_id: None)
    c._learner_config = LearnerConfig(fast_enabled=True, slow_enabled=False)

    snap = _distinct_snapshot(3.0)
    c._store.push_snapshot(snap)
    # Poisoned live state, distinct from the snapshot.
    c._bias_state = BiasState(
        cells={"clear|midday": BiasCell(theta=0.55, covariance=1.0, n=99)}
    )

    base = datetime(2026, 5, 1, tzinfo=UTC).date()
    for i in range(DRIFT_LOSS_STREAK_DAYS):
        iso = (base + timedelta(days=i)).isoformat()
        c._update_drift(iso, _losing_issued(iso), {"M1": 1000.0})

    assert c._drift_state.fast_disabled is True
    # The live bias state was rolled back to the ring snapshot...
    assert c._bias_state.cells["clear|midday"].theta == pytest.approx(1.03)
    # ...and persisted.
    assert (
        BiasState.from_dict(c._store.bias).cells["clear|midday"].theta
        == pytest.approx(1.03)
    )


async def test_rollback_service_backend(monkeypatch):
    c = _make_coordinator()

    async def _noop_refresh():
        return None

    monkeypatch.setattr(c, "async_request_refresh", _noop_refresh)
    c._store.push_snapshot(_distinct_snapshot(1.0))
    c._store.push_snapshot(_distinct_snapshot(2.0))

    result = await c.async_rollback_learners(1)
    assert result["restored_taken_at"].startswith("2026-04-02")
    assert result["ring_size"] == 2
    assert c._bias_state.cells["clear|midday"].theta == pytest.approx(1.02)
    assert c._shademap_state.channels["M1"]["10:5:0"].tau == pytest.approx(0.2)

    # Going further back restores the older snapshot.
    result = await c.async_rollback_learners(2)
    assert result["restored_taken_at"].startswith("2026-04-01")
    assert c._bias_state.cells["clear|midday"].theta == pytest.approx(1.01)

    # snapshots_back beyond the ring is capped at the oldest entry.
    result = await c.async_rollback_learners(50)
    assert result["snapshots_back"] == 2


async def test_rollback_service_empty_ring_raises():
    c = _make_coordinator()
    with pytest.raises(ValueError):
        await c.async_rollback_learners(1)


def test_fake_store_matches_real_store_marker_api():
    """The fake used here must mirror the real ForecastStore surface."""
    from custom_components.balcony_solar_forecast.store import ForecastStore

    for method in ("is_day_trained", "mark_day_trained"):
        assert hasattr(ForecastStore, method)
        assert hasattr(_FakeStore, method)
