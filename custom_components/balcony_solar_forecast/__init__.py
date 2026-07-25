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
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from ._frontend import async_register_frontend
from ._services import async_register_services
from .const import DOMAIN
from .coordinator import BalconySolarCoordinator
from .fetcher import OpenMeteoFetcher
from .store import ForecastStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.DATE,
    Platform.SELECT,
    Platform.SENSOR,
]

# Config-entry-only integration: no YAML config beyond the entry system.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type BalconySolarConfigEntry = ConfigEntry[BalconySolarCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration services (quality-scale ``action-setup``).

    All 10 services (get_forecast / import_bootstrap / run_bootstrap /
    rollback_learners / reset_day_ahead_bias / dump_shademap / install_dashboard
    / suggest_shade_groups / get_shade_profile / get_issued_forecast) are
    registered here — once, independent of any config entry — and stay
    registered, so an automation firing while no entry is loaded gets a clear
    ServiceValidationError instead of "Service not found". The handlers resolve
    their coordinators dynamically from ``hass.data``.

    The count and the list are machine-guarded against drift by
    ``tests/test_spec_integrity.py`` (this docstring said "six" for four
    releases while services.yaml grew to ten).

    Also serves + auto-registers the two bundled Lovelace cards (the
    shade-profile diagram, SPEC §15, and the power-history card) so they appear
    in the card picker with no HACS install: static paths now, the storage-mode
    resources once HA is running. This step never raises into setup — the cards
    are an enhancement, not a dependency.
    """
    async_register_services(hass)
    await async_register_frontend(hass)
    return True


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

    coordinator.async_start_nightly_job()

    # Learning visibility (0.23.1, SPEC §5): verify every configured
    # ``actual_entity`` actually exists in this HA. The nightly gates discard
    # the WHOLE day as soon as one channel is unusable, so an install that
    # adopted the shipped reference site (which carries the reference plant's
    # eight Hoymiles entity ids) would never learn anything while looking
    # healthy. Deferred to EVENT_HOMEASSISTANT_STARTED during a cold boot so
    # the inverter integration has had its turn.
    coordinator.async_schedule_channel_health_check()

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
        # Services stay registered (action-setup): with no loaded entry each
        # handler raises a clear ServiceValidationError instead of vanishing.
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
