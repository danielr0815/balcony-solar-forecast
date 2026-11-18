"""The η repair counts only days measured under its active meter binding."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    ISSUE_ETA_OUT_OF_BAND,
    STORE_KEY_LEARNING_HEALTH,
)
from custom_components.balcony_solar_forecast.store import ForecastStore  # noqa: E402
from tests.helpers.coordinator import _make_coordinator  # noqa: E402
from tests.helpers.store import FakeStore  # noqa: E402


def _coordinator(store=None):
    coord = _make_coordinator(store)
    coord._site = replace(coord._site, ac_actual_entity="sensor.original")
    coord.entry.created_at = coord.entry.modified_at = datetime(2026, 6, 1, tzinfo=UTC)
    repairs = set()
    coord._raise_repair_issue = lambda issue, _: repairs.add(issue)
    coord._delete_repair_issue = repairs.discard
    return coord, repairs


async def _out_of_band(coord, day):
    iso = day.isoformat()
    hour = f"{iso}T11:00:00+00:00"
    coord._store.record_issued(iso, {"status": "fresh"})
    coord._store.record_hourly_actuals(iso, {"M1": {hour: 200}, "M2": {hour: 200}})

    async def read_ac(_day):
        return {hour: 300}

    coord._async_read_ac_actuals = read_ac
    await coord._train_inverter_cal(day)


@pytest.mark.parametrize("change", [
    {"ac_actual_entity": "sensor.replacement"}, {"ac_actual_invert": True},
])
async def test_replacement_ignores_old_watchdog_streak_and_old_catchup_days(change):
    coord, repairs = _coordinator()
    first = date(2026, 7, 1)
    second = date(2026, 7, 2)
    third = date(2026, 7, 3)
    await _out_of_band(coord, first)
    await _out_of_band(coord, second)
    repaired = coord._store.get_learning_health()
    assert repaired["eta_oob_streak"] == 2
    assert repairs == set()

    coord._site = replace(coord._site, **change)
    coord.entry.modified_at = datetime(2026, 7, 2, 14, tzinfo=UTC)
    for day in (first, second, third, third):
        await _out_of_band(coord, day)
    assert repairs == set()
    assert coord._store.get_learning_health()["eta_oob_streak"] == 1
    assert coord.learning_health_summary()["eta_oob_last_day"] == third.isoformat()


async def test_remove_meter_clears_old_repair_even_without_any_more_measurements():
    coord, repairs = _coordinator()
    first = date(2026, 7, 1)
    for offset in range(3):
        await _out_of_band(coord, first + timedelta(days=offset))
    assert repairs == {ISSUE_ETA_OUT_OF_BAND}

    coord._site = replace(coord._site, ac_actual_entity=None)
    coord.entry.modified_at = datetime(2026, 7, 4, tzinfo=UTC)
    await coord.async_prime_from_store()

    assert repairs == set()
    assert coord._store.get_learning_health()["eta_oob_streak"] == 0

    coord._site = replace(coord._site, ac_actual_entity="sensor.original")
    coord.entry.modified_at = datetime(2026, 7, 5, tzinfo=UTC)
    await coord.async_prime_from_store()
    for day in (date(2026, 7, 3), date(2026, 7, 5), date(2026, 7, 6)):
        await _out_of_band(coord, day)
    assert repairs == set()
    assert coord._store.get_learning_health()["eta_oob_streak"] == 1


async def test_watchdog_reloads_same_bound_source_without_recounting():
    backend = FakeStore()
    store = ForecastStore(None, "entry", store=backend)
    coord, _ = _coordinator(store)
    first, second = date(2026, 7, 1), date(2026, 7, 2)
    await _out_of_band(coord, first)
    await _out_of_band(coord, second)
    await store.async_flush()

    restored = ForecastStore(None, "entry", store=FakeStore(backend.saved))
    await restored.async_load()
    again, issues = _coordinator(restored)
    await again.async_prime_from_store()
    await _out_of_band(again, second)
    assert restored.get_learning_health()["eta_oob_streak"] == 2
    await _out_of_band(again, date(2026, 7, 3))
    assert issues == {ISSUE_ETA_OUT_OF_BAND}


@pytest.mark.parametrize("unchanged", [True, False])
async def test_legacy_watchdog_is_kept_only_when_the_entry_has_not_changed(unchanged):
    boot = FakeStore({
        "schema_version": 3,
        STORE_KEY_LEARNING_HEALTH: {
            "eta_oob_streak": 3,
            "eta_oob_last_day": "2026-07-03",
            "eta_oob_last_median": .75,
            "eta_last_checked_day": "2026-07-03",
        },
    })
    store = ForecastStore(None, "entry", store=boot)
    await store.async_load()
    coord, repairs = _coordinator(store)
    repairs.add(ISSUE_ETA_OUT_OF_BAND)
    if not unchanged:
        coord.entry.modified_at = datetime(2026, 7, 2, tzinfo=UTC)
    await coord.async_prime_from_store()

    public = coord.learning_health_summary()
    assert public["eta_oob_streak"] == (3 if unchanged else 0)
    assert "eta_source" not in public
    assert "eta_source_changed_day" not in public
    assert repairs == ({ISSUE_ETA_OUT_OF_BAND} if unchanged else set())


async def test_unverified_legacy_watchdog_waits_for_fresh_days_without_history(monkeypatch):
    from homeassistant.util import dt as dt_util

    clock = datetime(2026, 7, 5, 12, tzinfo=UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: clock)
    coord, repairs = _coordinator()
    coord.entry.created_at = coord.entry.modified_at = datetime(1970, 1, 1, tzinfo=UTC)
    coord._store.set_learning_health({
        "eta_oob_streak": 3, "eta_oob_last_day": "2026-07-03",
        "eta_oob_last_median": .75, "eta_last_checked_day": "2026-07-03",
    })
    repairs.add(ISSUE_ETA_OUT_OF_BAND)
    await coord.async_prime_from_store()
    for day in (date(2026, 7, 3), date(2026, 7, 4), date(2026, 7, 5)):
        await _out_of_band(coord, day)
    assert repairs == set()
    assert coord._store.get_learning_health()["eta_oob_streak"] == 0

    clock += timedelta(days=2)
    await _out_of_band(coord, date(2026, 7, 6))
    assert coord._store.get_learning_health()["eta_oob_streak"] == 1
