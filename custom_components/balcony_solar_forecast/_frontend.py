"""Serve + auto-register the bundled Lovelace shade-profile card (SPEC §15).

Owner: glue/frontend. End users no longer need the HACS ``apexcharts-card`` for
the sun-path-vs-learned-shade diagram: the integration ships
``frontend/shade_profile_card.js``, serves it as a static path under
:data:`FRONTEND_URL`, and — in storage-mode Lovelace — auto-registers it as a
dashboard resource so it turns up in the "Add card" picker with zero manual
YAML. The resource URL is version-busted (``?v=<INTEGRATION_VERSION>``), which
is the ONLY cache-busting mechanism (the JS file itself carries no version).

The card is an ENHANCEMENT, never a setup blocker: every step here is wrapped so
no exception escapes into ``async_setup``. On any failure the operator can still
add the card manually as a JavaScript-module resource pointing at the URL below.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace import LOVELACE_DATA
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant

from .const import DOMAIN, INTEGRATION_VERSION

_LOGGER = logging.getLogger(__name__)

# Public: the URL the card is served under (also the manual-resource hint).
FRONTEND_URL = "/balcony_solar_forecast/frontend/shade_profile_card.js"
# The on-disk file backing that URL.
_FRONTEND_FILE = Path(__file__).parent / "frontend" / "shade_profile_card.js"

# hass.data idempotency flags (async_setup runs once, but be defensive).
_DATA_STATIC_DONE = f"{DOMAIN}_frontend_static_registered"
_DATA_RESOURCE_DONE = f"{DOMAIN}_frontend_resource_registered"

# LovelaceData.resource_mode value that owns a mutable storage collection.
_RESOURCE_MODE_STORAGE = "storage"


def _versioned_url() -> str:
    """The desired Lovelace resource URL, busted by the integration version."""
    return f"{FRONTEND_URL}?v={INTEGRATION_VERSION}"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the card and (storage mode) register its Lovelace resource.

    Called once from ``async_setup``. The static path is registered
    immediately either way. The Lovelace resource registration needs the
    lovelace component set up, which may not have happened yet during
    ``async_setup``: if HA is already running we do it now, otherwise we defer
    it to a one-shot ``EVENT_HOMEASSISTANT_STARTED`` listener.
    """
    await _async_register_static_path(hass)

    if hass.is_running:
        await _async_register_resource(hass)
        return

    async def _on_started(_event: Event) -> None:
        await _async_register_resource(hass)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)


async def _async_register_static_path(hass: HomeAssistant) -> None:
    """Register the JS file as a cache-headed static path (idempotent)."""
    if hass.data.get(_DATA_STATIC_DONE):
        return
    try:
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    FRONTEND_URL, str(_FRONTEND_FILE), cache_headers=True
                )
            ]
        )
    except Exception:  # noqa: BLE001 -- the card is an enhancement, never a blocker
        _LOGGER.warning(
            "Could not serve the shade-profile card at %s",
            FRONTEND_URL,
            exc_info=True,
        )
        return
    hass.data[_DATA_STATIC_DONE] = True


async def _async_register_resource(hass: HomeAssistant) -> None:
    """Create/update the Lovelace resource, swallowing every failure.

    The whole body is wrapped so NO exception ever escapes (the async_setup
    contract): a failure here just means the card must be added manually.
    """
    if hass.data.get(_DATA_RESOURCE_DONE):
        return
    try:
        await _async_register_resource_impl(hass)
    except Exception:  # noqa: BLE001 -- never let a resource hiccup block setup
        _LOGGER.warning(
            "Could not auto-register the shade-profile Lovelace resource; add it "
            "manually as a JavaScript-module dashboard resource pointing at %s",
            _versioned_url(),
            exc_info=True,
        )


async def _async_register_resource_impl(hass: HomeAssistant) -> None:
    """Storage-mode resource create/update (the raising inner worker)."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if (
        lovelace is None
        or getattr(lovelace, "resource_mode", None) != _RESOURCE_MODE_STORAGE
    ):
        # YAML mode, recovery mode, or lovelace not set up: we cannot mutate a
        # storage collection. Tell the operator how to add it by hand and stop.
        _LOGGER.info(
            "Lovelace is not in storage mode; add the shade-profile card manually "
            "as a JavaScript-module dashboard resource pointing at %s",
            _versioned_url(),
        )
        return

    resources = lovelace.resources
    # Ensure the storage collection has loaded its items before we inspect them.
    if not getattr(resources, "loaded", True):
        await resources.async_load()
        resources.loaded = True

    desired = _versioned_url()
    existing = None
    for item in resources.async_items():
        url = item.get("url", "")
        if isinstance(url, str) and url.startswith(FRONTEND_URL):
            existing = item
            break

    if existing is None:
        await resources.async_create_item({"res_type": "module", "url": desired})
        _LOGGER.info("Registered shade-profile card Lovelace resource: %s", desired)
    elif existing.get("url") != desired:
        await resources.async_update_item(
            existing["id"], {"res_type": "module", "url": desired}
        )
        _LOGGER.info(
            "Updated shade-profile card Lovelace resource to %s", desired
        )
    # else: an identical resource already exists — nothing to do.

    hass.data[_DATA_RESOURCE_DONE] = True
