"""Per-layer drift attribution (audit #13b).

Owner: glue (drift monitor). The nightly drift monitor used to declare a
"losing" day from ONE signal — corrected daily MAE vs raw physics MAE — and
increment BOTH layer streaks on it, so an innocent layer was auto-disabled and
rolled back alongside a genuinely drifting sibling. These tests exercise the
decomposition ``corrected = slow ∘ fast``: the slow (shademap) layer is judged
on slow-only-vs-physics, the fast (day-ahead) layer on corrected-vs-slow-only,
with independent streaks — and the LEGACY single-signal fallback when a snapshot
carries no slow-only curve.

Reuses the learning-test ``_make_coordinator`` / ``_FakeStore`` harness; HA must
be installed (the whole module is skipped otherwise).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    DATA_KEY_CORRECTED_HOURLY_WH,
    DATA_KEY_RAW_HOURLY_WH,
    DRIFT_LOSS_STREAK_DAYS,
    ISSUE_FAST_LEARNER_DISABLED,
    ISSUE_SLOW_LEARNER_DISABLED,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    DriftState,
    IssuedSnapshot,
    LearnerConfig,
    ShademapBin,
    ShademapState,
)
from tests.test_coordinator_learning import _make_coordinator  # noqa: E402

_HOUR = "T11:00:00+00:00"


def _snapshot(
    iso: str, *, raw: float, corrected: float, slow: float | None = None
) -> dict:
    """A v2 issued snapshot dict with a single daylight hour per curve.

    ``slow=None`` omits the slow-only curve entirely (the legacy branch); any
    float writes a slow-only curve of that Wh (the per-layer decomposition).
    """
    h = iso + _HOUR
    kwargs: dict = {
        "issued_at": "x",
        "status": "fresh",
        "raw_hourly_wh": {h: raw},
        "corrected_hourly_wh": {h: corrected},
    }
    if slow is not None:
        kwargs["slow_only_hourly_wh"] = {h: slow}
    return IssuedSnapshot(**kwargs).to_dict()


# ---------------------------------------------------------------------------
# Per-leg attribution: only the guilty layer's streak advances
# ---------------------------------------------------------------------------


def test_slow_good_fast_bad_increments_only_fast():
    """raw == slow-only (shademap innocent), corrected materially worse
    (day-ahead guilty) -> ONLY the fast streak advances."""
    c = _make_coordinator()  # both layers enabled by default
    iso = "2026-05-01"
    # slow-only == raw == measured (slow_mae == raw_mae == 0); corrected off by
    # 200 Wh (well over the absolute floor) -> fast loses, slow does not.
    snap = _snapshot(iso, raw=1000.0, corrected=1200.0, slow=1000.0)
    c._update_drift(iso, snap, {"M1": 1000.0})
    assert c._drift_state.fast_loss_streak == 1
    assert c._drift_state.slow_loss_streak == 0
    # The daily_mae entry carries the slow leg when a slow-only curve exists.
    assert "slow" in c._drift_state.daily_mae[iso]
    assert c._drift_state.daily_mae[iso]["slow"] == pytest.approx(0.0)


def test_slow_bad_fast_neutral_increments_only_slow():
    """slow-only materially worse than physics (shademap guilty), corrected ==
    slow-only (day-ahead neutral) -> ONLY the slow streak advances."""
    c = _make_coordinator()
    iso = "2026-05-01"
    # raw perfect (raw_mae 0), slow-only off by 200 (slow loses vs physics),
    # corrected == slow-only (fast adds nothing -> does not lose).
    snap = _snapshot(iso, raw=1000.0, corrected=1200.0, slow=1200.0)
    c._update_drift(iso, snap, {"M1": 1000.0})
    assert c._drift_state.slow_loss_streak == 1
    assert c._drift_state.fast_loss_streak == 0


def test_both_bad_increments_both():
    c = _make_coordinator()
    iso = "2026-05-01"
    # raw perfect; slow-only off by 300 (slow loses); corrected off by 700
    # (corrected vs slow-only delta 400 -> fast loses too).
    snap = _snapshot(iso, raw=1000.0, corrected=1700.0, slow=1300.0)
    c._update_drift(iso, snap, {"M1": 1000.0})
    assert c._drift_state.fast_loss_streak == 1
    assert c._drift_state.slow_loss_streak == 1


def test_both_good_resets_both():
    c = _make_coordinator()
    # Pre-load both streaks; a clean day (both legs win) resets both.
    c._drift_state = DriftState(fast_loss_streak=3, slow_loss_streak=4)
    iso = "2026-05-01"
    snap = _snapshot(iso, raw=1000.0, corrected=1000.0, slow=1000.0)
    c._update_drift(iso, snap, {"M1": 1000.0})
    assert c._drift_state.fast_loss_streak == 0
    assert c._drift_state.slow_loss_streak == 0


def test_materiality_floor_applies_per_leg():
    """A sub-floor slow leg does NOT count even though the fast leg loses
    materially: the absolute floor is evaluated on each leg independently."""
    from custom_components.balcony_solar_forecast.const import DRIFT_LOSS_MIN_ABS_WH

    c = _make_coordinator()
    iso = "2026-05-01"
    # slow-only off by < floor (raw_mae 0, slow_mae = floor-10) -> slow does NOT
    # lose; corrected off by 200 vs slow-only -> fast loses.
    sub = DRIFT_LOSS_MIN_ABS_WH - 10.0
    snap = _snapshot(
        iso, raw=1000.0, corrected=1000.0 + sub + 200.0, slow=1000.0 + sub
    )
    c._update_drift(iso, snap, {"M1": 1000.0})
    assert c._drift_state.fast_loss_streak == 1
    assert c._drift_state.slow_loss_streak == 0


# ---------------------------------------------------------------------------
# Legacy fallback: no slow-only curve -> single shared corrected-vs-raw signal
# ---------------------------------------------------------------------------


def test_legacy_snapshot_drives_both_streaks_from_shared_signal():
    """A snapshot WITHOUT a slow-only curve keeps the original behaviour: one
    corrected-vs-raw signal advances BOTH streaks in lockstep."""
    c = _make_coordinator()
    iso = "2026-05-01"
    snap = _snapshot(iso, raw=1000.0, corrected=1200.0)  # no slow -> legacy
    assert "slow_only_hourly_wh" not in snap  # store trim: omitted when empty
    c._update_drift(iso, snap, {"M1": 1000.0})
    assert c._drift_state.fast_loss_streak == 1
    assert c._drift_state.slow_loss_streak == 1
    # Dict shape stays stable (no "slow" leg recorded) on a legacy day.
    assert "slow" not in c._drift_state.daily_mae[iso]


# ---------------------------------------------------------------------------
# Auto-disable fires for the guilty layer ONLY
# ---------------------------------------------------------------------------


def test_auto_disable_after_streak_only_guilty_layer(monkeypatch):
    """A slow-good / fast-bad streak of DRIFT_LOSS_STREAK_DAYS auto-disables the
    fast layer alone; the innocent slow layer stays enabled."""
    c = _make_coordinator()
    raised: list[str] = []
    monkeypatch.setattr(c, "_raise_repair_issue", lambda issue_id: raised.append(issue_id))
    base = datetime(2026, 5, 1, tzinfo=UTC).date()
    for i in range(DRIFT_LOSS_STREAK_DAYS):
        iso = (base + timedelta(days=i)).isoformat()
        # raw == slow-only == measured; corrected materially worse each day.
        snap = _snapshot(iso, raw=1000.0, corrected=1300.0, slow=1000.0)
        c._update_drift(iso, snap, {"M1": 1000.0})
    assert c._drift_state.fast_disabled is True
    assert c._drift_state.slow_disabled is False
    assert ISSUE_FAST_LEARNER_DISABLED in raised
    assert ISSUE_SLOW_LEARNER_DISABLED not in raised


# ---------------------------------------------------------------------------
# Snapshot side: _slow_only_hourly gate + snapshot_issued wiring
# ---------------------------------------------------------------------------


def test_slow_only_hourly_empty_when_slow_inactive():
    """No learned shademap channels -> slow layer inactive -> {} (callers treat
    it as slow-only == raw; the raw curve is never duplicated into the store)."""
    c = _make_coordinator()  # default: empty ShademapState -> slow inactive
    assert c._slow_only_hourly("2026-05-01") == {}


def test_slow_only_hourly_empty_when_no_weather():
    """Slow layer active but no cached weather -> {} (the snapshot never fails
    on the extra engine pass)."""
    c = _make_coordinator()
    c._learner_config = LearnerConfig(slow_enabled=True)
    c._shademap_state = ShademapState(
        channels={"M1": {"10:15:1": ShademapBin(tau=0.3, n=20)}}
    )
    # _FakeStore.get_last_payload() -> None -> _cached_weather() is None.
    assert c._slow_only_hourly("2026-05-01") == {}


async def test_snapshot_issued_stores_slow_only_curve(monkeypatch):
    """snapshot_issued persists the slow-only curve returned by
    _slow_only_hourly (the real engine pass is covered by the {}-on-no-weather
    gate above)."""
    c = _make_coordinator()
    h = "2026-07-01" + _HOUR
    c.data = {
        DATA_KEY_RAW_HOURLY_WH: {h: 1000.0},
        DATA_KEY_CORRECTED_HOURLY_WH: {h: 1000.0},
        "status": "fresh",
    }
    monkeypatch.setattr(c, "_slow_only_hourly", lambda iso: {h: 900.0})
    await c._snapshot_issued(date(2026, 7, 1))
    stored = IssuedSnapshot.from_dict(c._store.get_issued("2026-07-01"))
    assert stored.slow_only_hourly_wh == {h: 900.0}


# ---------------------------------------------------------------------------
# IssuedSnapshot round-trip with / without the slow-only curve
# ---------------------------------------------------------------------------


def test_issued_snapshot_roundtrip_with_slow_only():
    h = "2026-07-01" + _HOUR
    snap = IssuedSnapshot(
        issued_at="x", status="fresh",
        raw_hourly_wh={h: 1000.0},
        corrected_hourly_wh={h: 1100.0},
        slow_only_hourly_wh={h: 1050.0},
    )
    d = snap.to_dict()
    assert d["slow_only_hourly_wh"] == {h: 1050.0}
    assert IssuedSnapshot.from_dict(d).slow_only_hourly_wh == {h: 1050.0}


def test_issued_snapshot_roundtrip_without_slow_only():
    h = "2026-07-01" + _HOUR
    snap = IssuedSnapshot(
        issued_at="x", status="fresh",
        raw_hourly_wh={h: 1000.0},
        corrected_hourly_wh={h: 1000.0},
    )
    d = snap.to_dict()
    # Store trim: the empty curve is omitted, and reads back as {}.
    assert "slow_only_hourly_wh" not in d
    assert IssuedSnapshot.from_dict(d).slow_only_hourly_wh == {}
