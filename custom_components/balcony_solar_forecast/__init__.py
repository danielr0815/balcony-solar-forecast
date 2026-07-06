"""Balcony Solar Forecast integration for Home Assistant.

Owner: glue. Wires the injected aiohttp session, the validating Store and
the coordinator, forwards the sensor / binary_sensor platforms, and tears
everything down cleanly on unload. All heavy lifting lives in the
coordinator; this module is pure plumbing.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ._services import async_register_services, async_remove_services
from .const import DOMAIN, SERVICE_GET_FORECAST
from .coordinator import BalconySolarCoordinator
from .fetcher import OpenMeteoFetcher
from .store import ForecastStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

type BalconySolarConfigEntry = ConfigEntry[BalconySolarCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: BalconySolarConfigEntry
) -> bool:
    """Set up Balcony Solar Forecast from a config entry."""
    session = async_get_clientsession(hass)
    fetcher = OpenMeteoFetcher(session)

    store = ForecastStore(hass, entry.entry_id)
    await store.async_load()

    coordinator = BalconySolarCoordinator(
        hass, entry, fetcher=fetcher, store=store
    )
    await coordinator.async_prime_from_store()

    # First refresh: the degradation ladder serves a cached/physics curve
    # from a warm store even when the live fetch fails, so this only raises
    # ConfigEntryNotReady (retry later) when there is truly nothing to serve.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Integration-wide learner services (import_bootstrap / dump_shademap).
    # Idempotent: registered once, shared across entries (SPEC §5/§6).
    async_register_services(hass)

    coordinator.async_start_nightly_job()

    # Catch up any nightly job missed while HA was down (idempotent/date-keyed).
    # Background task tied to the entry: tracked and auto-cancelled on unload.
    entry.async_create_background_task(
        hass,
        coordinator.async_startup_catchup(),
        name=f"{DOMAIN}_startup_catchup",
    )

    # Flush the Store on HA stop so a hard shutdown keeps the last-good cache.
    # Must AWAIT the flush directly: EVENT_HOMEASSISTANT_STOP is a blocking
    # event that HA awaits, whereas scheduling hass.async_create_task during
    # shutdown creates an orphaned task whose exception is never retrieved
    # ("calls async_create_task from a thread other than the event loop").
    async def _async_flush_on_stop(_event: Event) -> None:
        await store.async_flush()

    entry.async_on_unload(
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _async_flush_on_stop
        )
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: BalconySolarConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: BalconySolarCoordinator = entry.runtime_data
        coordinator.async_shutdown_extra()
        # Flush pending delayed save: a reload does not fire the HA-stop
        # event, so the last-good cache could otherwise be lost.
        await coordinator._store.async_flush()  # noqa: SLF001 (owned wrapper)
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.data.pop(DOMAIN, None)
            # The get_forecast service is registered once (from the sensor
            # platform) and shared across entries; remove it when the last
            # entry unloads so it does not linger returning empty results.
            if hass.services.has_service(DOMAIN, SERVICE_GET_FORECAST):
                hass.services.async_remove(DOMAIN, SERVICE_GET_FORECAST)
            # Same lifecycle for the learner services (import_bootstrap /
            # dump_shademap), registered from async_setup_entry.
            async_remove_services(hass)
    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant, entry: BalconySolarConfigEntry
) -> None:
    """Delete this entry's persisted store."""
    store = ForecastStore(hass, entry.entry_id)
    await store.async_remove()


async def _async_reload_entry(
    hass: HomeAssistant, entry: BalconySolarConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
