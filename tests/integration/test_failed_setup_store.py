"""A retried setup must release the same Store that scheduled the migration."""
from pathlib import Path

from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from custom_components.balcony_solar_forecast import BalconySolarCoordinator
from custom_components.balcony_solar_forecast.const import STORAGE_VERSION
from tests.integration.helpers import DOMAIN, start_advanced, user_data


async def test_removal_after_failed_migration_refresh_stays_deleted(real_hass, monkeypatch):
    hass = real_hass
    flow = await start_advanced(hass)
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], user_data())
    entry = result["result"]
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)

    key = f"{DOMAIN}.{entry.entry_id}"
    disk = Store(hass, STORAGE_VERSION, key)
    await disk.async_save({"schema_version": 2})
    writers = []

    async def fail_refresh(coordinator):
        writers.append(coordinator._store._store)
        raise ConfigEntryNotReady("synthetic refresh failure")

    monkeypatch.setattr(BalconySolarCoordinator, "async_config_entry_first_refresh", fail_refresh)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not Path(disk.path).exists()
    (writer,) = writers
    # Even an already dispatched delayed callback must have no data left to write.
    await writer._async_callback_delayed_write()
    assert not Path(disk.path).exists()


async def test_clean_failed_retries_do_not_rewrite_current_store(real_hass, monkeypatch):
    from custom_components.balcony_solar_forecast import async_setup_entry

    hass = real_hass
    flow = await start_advanced(hass)
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], user_data())
    entry = result["result"]
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    disk_writes = []
    async_save = Store.async_save

    async def count_store(store, data):
        if store.key == f"{DOMAIN}.{entry.entry_id}":
            disk_writes.append(store.key)
        await async_save(store, data)

    async def fail_refresh(coordinator):
        raise ConfigEntryNotReady("weather unavailable during retry")

    monkeypatch.setattr(Store, "async_save", count_store)
    monkeypatch.setattr(BalconySolarCoordinator, "async_config_entry_first_refresh", fail_refresh)
    import pytest
    for _ in range(3):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)
    assert disk_writes == []
