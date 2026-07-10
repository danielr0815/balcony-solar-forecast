"""Tests for the nightly orchestration + catch-up date math (audit #17).

Owner: glue (nightly trainer, ``_nightly.async_nightly_job`` /
``_nightly.catchup_days`` reached through the coordinator's thin delegates).
The nightly sweep's *shape* — snapshot-today-first, then a bounded catch-up over
closed days that reads actuals only once, trains and scores each day, and never
lets one day's failure abort the rest — had no direct coverage. These drive it
against the learning-test's ``_make_coordinator`` / ``_FakeStore`` with the
per-step coordinator hooks monkeypatched to record call order + args, plus the
pure ``_catchup_days`` date math (window bound, resume-after-newest, calendar
boundaries).

Import is via ``custom_components.balcony_solar_forecast`` (the real HA-importing
package), so HA must be installed; the whole module is skipped otherwise.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    NIGHTLY_CATCHUP_MAX_DAYS,
)
from tests.test_coordinator_learning import (  # noqa: E402
    _FakeStore,
    _make_coordinator,
)

# ---------------------------------------------------------------------------
# _catchup_days: pure, bounded, resume-after-newest, calendar-safe date math
# ---------------------------------------------------------------------------


def test_catchup_days_empty_store_is_full_window_oldest_first():
    c = _make_coordinator()  # empty store -> no recorded actuals
    latest = date(2026, 3, 11)
    days = c._catchup_days(latest)
    assert len(days) == NIGHTLY_CATCHUP_MAX_DAYS
    # Oldest-first, ending exactly at latest.
    assert days == [date(2026, 3, 9), date(2026, 3, 10), date(2026, 3, 11)]
    assert days == sorted(days)


def test_catchup_days_resumes_the_day_after_newest_recorded():
    store = _FakeStore()
    # Newest recorded actuals sit INSIDE the window (not at either edge).
    store.record_actuals("2026-03-10", {"M1": 1.0})
    c = _make_coordinator(store)
    latest = date(2026, 3, 12)
    days = c._catchup_days(latest)
    # Sweep starts the day AFTER 2026-03-10, i.e. 03-11, up to latest.
    assert days == [date(2026, 3, 11), date(2026, 3, 12)]


def test_catchup_days_newest_equals_latest_clamps_to_single_day():
    store = _FakeStore()
    store.record_actuals("2026-03-11", {"M1": 1.0})
    c = _make_coordinator(store)
    latest = date(2026, 3, 11)
    days = c._catchup_days(latest)
    # candidate (day after newest) overshoots latest -> start clamped to latest.
    assert days == [date(2026, 3, 11)]


def test_catchup_days_crosses_month_and_year_boundary():
    c = _make_coordinator()  # empty store -> full window
    latest = date(2026, 1, 1)
    days = c._catchup_days(latest)
    # Full window back across both the month AND year boundary.
    assert days == [date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 1)]


def test_catchup_days_old_recorded_actuals_do_not_extend_window():
    store = _FakeStore()
    # A very old recorded day (candidate day-after is before the window start):
    # the window bound wins, so we never fan out past NIGHTLY_CATCHUP_MAX_DAYS.
    store.record_actuals("2025-01-01", {"M1": 1.0})
    c = _make_coordinator(store)
    latest = date(2026, 3, 11)
    days = c._catchup_days(latest)
    assert len(days) == NIGHTLY_CATCHUP_MAX_DAYS
    assert days[-1] == latest


# ---------------------------------------------------------------------------
# Orchestration: order, actuals-read gating, per-day resilience, idempotence
# ---------------------------------------------------------------------------


def _instrument(monkeypatch, c, *, sweep, read_return=None,
                train_raises_on=None, score_raises_on=None):
    """Monkeypatch the coordinator's per-step hooks to record call order/args.

    Returns the shared ``calls`` list of ``(step, arg)`` tuples. ``sweep`` fixes
    the catch-up day list so the orchestration order is deterministic.
    """
    calls: list[tuple[str, object]] = []

    async def _snap(today):
        calls.append(("snapshot", today))

    async def _read(day):
        calls.append(("read", day))
        return read_return

    async def _train(day):
        calls.append(("train", day))
        if train_raises_on is not None and day == train_raises_on:
            raise RuntimeError("train blew up")

    async def _score(day):
        calls.append(("score", day))
        if score_raises_on is not None and day == score_raises_on:
            raise RuntimeError("score blew up")

    monkeypatch.setattr(c, "_snapshot_issued", _snap)
    monkeypatch.setattr(c, "_read_actuals_safe", _read)
    monkeypatch.setattr(c, "_train_and_guard", _train)
    monkeypatch.setattr(c, "_score_scoreboard_day", _score)
    monkeypatch.setattr(c, "_catchup_days", lambda latest: list(sweep))
    return calls


async def test_nightly_snapshot_first_then_per_day_read_train_score(monkeypatch):
    c = _make_coordinator()
    d1, d2 = date(2026, 3, 10), date(2026, 3, 11)
    calls = _instrument(monkeypatch, c, sweep=[d1, d2])
    now = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
    await c._async_nightly_job(now=now)
    # Snapshot runs first, keyed on TODAY (the served day).
    assert calls[0] == ("snapshot", date(2026, 3, 12))
    # Then each catch-up day in order: read -> train -> score.
    assert calls[1:] == [
        ("read", d1), ("train", d1), ("score", d1),
        ("read", d2), ("train", d2), ("score", d2),
    ]


async def test_nightly_catchup_latest_is_yesterday(monkeypatch):
    """The sweep window is computed for YESTERDAY (last closed day), not today."""
    c = _make_coordinator()
    seen: list[date] = []
    monkeypatch.setattr(c, "_catchup_days", lambda latest: seen.append(latest) or [])

    async def _snap(today):
        return None

    monkeypatch.setattr(c, "_snapshot_issued", _snap)
    now = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
    await c._async_nightly_job(now=now)
    assert seen == [date(2026, 3, 11)]


async def test_nightly_reads_actuals_only_when_absent(monkeypatch):
    store = _FakeStore()
    d1, d2 = date(2026, 3, 10), date(2026, 3, 11)
    # d1 already has actuals recorded -> must NOT be re-read.
    store.record_actuals(d1.isoformat(), {"M1": 1.0})
    c = _make_coordinator(store)
    calls = _instrument(monkeypatch, c, sweep=[d1, d2])
    await c._async_nightly_job(now=datetime(2026, 3, 12, 12, 0, tzinfo=UTC))
    reads = [d for step, d in calls if step == "read"]
    trains = [d for step, d in calls if step == "train"]
    scores = [d for step, d in calls if step == "score"]
    assert reads == [d2]            # only the day without actuals is read
    assert trains == [d1, d2]       # both days still trained
    assert scores == [d1, d2]       # both days still scored


async def test_nightly_idempotent_day_trained_and_scored_not_reread(monkeypatch):
    """The date-keyed design: a day already in store.actuals is not re-read but
    IS still trained + scored (re-running is a no-op inside the guards)."""
    store = _FakeStore()
    d1 = date(2026, 3, 11)
    store.record_actuals(d1.isoformat(), {"M1": 2.0})
    c = _make_coordinator(store)
    calls = _instrument(monkeypatch, c, sweep=[d1])
    await c._async_nightly_job(now=datetime(2026, 3, 12, 12, 0, tzinfo=UTC))
    assert ("read", d1) not in calls
    assert ("train", d1) in calls
    assert ("score", d1) in calls


async def test_nightly_records_read_actuals_into_store(monkeypatch):
    """A successful actuals read is persisted for the day (daily + hourly)."""
    store = _FakeStore()
    d1 = date(2026, 3, 11)
    c = _make_coordinator(store)
    daily = {"M1": 500.0, "M2": 400.0}
    hourly = {"M1": {"2026-03-11T11:00:00+00:00": 500.0}}
    _instrument(monkeypatch, c, sweep=[d1], read_return=(daily, hourly))
    await c._async_nightly_job(now=datetime(2026, 3, 12, 12, 0, tzinfo=UTC))
    assert store.get_actuals(d1.isoformat()) == daily
    assert store.get_hourly_actuals(d1.isoformat()) == hourly


async def test_nightly_train_failure_does_not_abort_sweep(monkeypatch):
    """A _train_and_guard that raises still scores its own day AND processes the
    remaining days (the try/except guards each leg)."""
    c = _make_coordinator()
    d1, d2 = date(2026, 3, 10), date(2026, 3, 11)
    calls = _instrument(monkeypatch, c, sweep=[d1, d2], train_raises_on=d1)
    await c._async_nightly_job(now=datetime(2026, 3, 12, 12, 0, tzinfo=UTC))
    trains = [d for step, d in calls if step == "train"]
    scores = [d for step, d in calls if step == "score"]
    assert trains == [d1, d2]          # d2 still trained after d1 raised
    assert scores == [d1, d2]          # score still runs for d1 (same day) + d2


async def test_nightly_score_failure_does_not_abort_later_days(monkeypatch):
    c = _make_coordinator()
    d1, d2 = date(2026, 3, 10), date(2026, 3, 11)
    calls = _instrument(monkeypatch, c, sweep=[d1, d2], score_raises_on=d1)
    await c._async_nightly_job(now=datetime(2026, 3, 12, 12, 0, tzinfo=UTC))
    trains = [d for step, d in calls if step == "train"]
    scores = [d for step, d in calls if step == "score"]
    assert scores == [d1, d2]          # d2 still scored after d1 raised
    assert trains == [d1, d2]


# ---------------------------------------------------------------------------
# Startup catch-up: never fatal
# ---------------------------------------------------------------------------


async def test_startup_catchup_swallows_nightly_failure(monkeypatch):
    c = _make_coordinator()

    async def _boom(now=None):
        raise RuntimeError("nightly blew up")

    monkeypatch.setattr(c, "_async_nightly_job", _boom)
    # Must not raise (startup best-effort).
    await c.async_startup_catchup()
