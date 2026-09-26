"""Unloading and concurrent actions must not leave or interleave store writers."""

import asyncio
from types import SimpleNamespace

from custom_components.balcony_solar_forecast import _nightly, async_unload_entry
from custom_components.balcony_solar_forecast.const import DOMAIN
from tests.helpers.coordinator import _make_coordinator


async def test_reset_waits_until_nightly_mutation_is_complete(monkeypatch):
    coord = _make_coordinator()
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    async def nightly(*_):
        entered.set()
        await release.wait()
        events.append("nightly")

    async def refresh():
        events.append("reset")

    monkeypatch.setattr(_nightly, "async_nightly_job", nightly)
    monkeypatch.setattr(coord, "async_request_refresh", refresh)
    task = asyncio.create_task(coord._async_nightly_job())
    await entered.wait()
    reset = asyncio.create_task(coord.async_reset_day_ahead_bias())
    try:
        await asyncio.sleep(0)
        assert not reset.done(), "reset must wait for the in-flight learner transaction"
    finally:
        release.set()
        await asyncio.gather(task, reset)
    assert events == ["nightly", "reset"]


async def test_unload_joins_nightly_before_final_flush(monkeypatch):
    coord = _make_coordinator()
    coord._unsub_nightly = lambda: None
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    async def nightly(*_):
        entered.set()
        try:
            await release.wait()
            events.append("late write")
        finally:
            events.append("nightly finished")

    async def flush():
        events.append("flush")

    async def unload_platforms(*_):
        return True

    monkeypatch.setattr(_nightly, "async_nightly_job", nightly)
    coord._store.async_flush = flush
    hass = SimpleNamespace(data={DOMAIN: {"entry": coord}},
                           config_entries=SimpleNamespace(async_unload_platforms=unload_platforms))
    task = asyncio.create_task(coord._async_nightly_job())
    await entered.wait()
    try:
        assert await async_unload_entry(hass, SimpleNamespace(entry_id="entry"))
        assert task.done(), "the unloaded coordinator still owns a running writer"
        assert events == ["nightly finished", "flush"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert "late write" not in events


async def test_unload_cancels_in_process_bootstrap_before_import(monkeypatch):
    from custom_components.balcony_solar_forecast import _bootstrap

    coord = _make_coordinator()
    coord._unsub_nightly = None
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def importer(_):
        writes.append("import")

    async def reconstruction(hass, coordinator, import_fn, *args):
        entered.set()
        await release.wait()
        await import_fn({})

    async def flush():
        writes.append("flush")

    async def unload_platforms(*_):
        return True

    coord.async_import_bootstrap = importer
    coord._store.async_flush = flush
    monkeypatch.setattr(_bootstrap, "_run_locked", reconstruction)
    hass = SimpleNamespace(data={DOMAIN: {"entry": coord}},
                           config_entries=SimpleNamespace(async_unload_platforms=unload_platforms))
    call = SimpleNamespace(data={"entry_id": "entry", "start_date": "2026-07-01",
                                 "end_date": "2026-07-02", "dry_run": False})
    task = asyncio.create_task(_bootstrap.async_run_bootstrap(hass, call))
    await entered.wait()
    try:
        assert await async_unload_entry(hass, SimpleNamespace(entry_id="entry"))
        assert task.done(), "bootstrap must be joined before the store is flushed"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert writes == ["flush"]
