"""Serve + auto-register the bundled Lovelace cards (SPEC §15 + power history).

Owner: glue/frontend. End users no longer need any HACS frontend resource for
the integration's two bundled cards:

  * ``frontend/shade_profile_card.js`` — the sun-path-vs-learned-shade diagram
    (SPEC §15), replacing the opt-in ``apexcharts-card`` snippet;
  * ``frontend/power_history_card.js`` — the energy-dashboard-style stacked
    hourly production-per-module bars + forecast line.

The integration ships both files, serves them as static paths under the shared
``/balcony_solar_forecast/frontend/`` prefix, and — in storage-mode Lovelace —
auto-registers each as a dashboard resource so they turn up in the "Add card"
picker with zero manual YAML. Each resource URL is version-busted
(``?v=<INTEGRATION_VERSION>``), which is the ONLY cache-busting mechanism (the JS
files themselves carry no version string).

The cards are an ENHANCEMENT, never a setup blocker: every step here is wrapped
so no exception escapes into ``async_setup``. On any failure the operator can
still add a card manually as a JavaScript-module resource pointing at its URL.
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

_FRONTEND_DIR = Path(__file__).parent / "frontend"

# The bundled cards: ``(served URL, on-disk file)``. Both live under the SAME
# ``/balcony_solar_forecast/frontend/`` prefix; each URL is distinct per file so
# a Lovelace resource can be matched to its card by url prefix.
_CARDS: tuple[tuple[str, Path], ...] = (
    (
        "/balcony_solar_forecast/frontend/shade_profile_card.js",
        _FRONTEND_DIR / "shade_profile_card.js",
    ),
    (
        "/balcony_solar_forecast/frontend/power_history_card.js",
        _FRONTEND_DIR / "power_history_card.js",
    ),
)

# Backwards-compatible single-card aliases (the shade-profile card is card 0);
# kept so existing callers/tests importing these names keep working.
FRONTEND_URL = _CARDS[0][0]
_FRONTEND_FILE = _CARDS[0][1]

# hass.data idempotency flags (async_setup runs once, but be defensive).
_DATA_STATIC_DONE = f"{DOMAIN}_frontend_static_registered"
_DATA_RESOURCE_DONE = f"{DOMAIN}_frontend_resource_registered"

# LovelaceData.resource_mode value that owns a mutable storage collection.
_RESOURCE_MODE_STORAGE = "storage"


def _versioned_url(url: str) -> str:
    """A card's desired Lovelace resource URL, busted by the integration version."""
    return f"{url}?v={INTEGRATION_VERSION}"


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the cards and (storage mode) register their Lovelace resources.

    Called once from ``async_setup``. The static paths are registered
    immediately either way. The Lovelace resource registration needs the
    lovelace component set up, which may not have happened yet during
    ``async_setup``: if HA is already running we do it now, otherwise we defer
    it to a one-shot ``EVENT_HOMEASSISTANT_STARTED`` listener.
    """
    await _async_register_static_paths(hass)

    if hass.is_running:
        await _async_register_resources(hass)
        return

    async def _on_started(_event: Event) -> None:
        await _async_register_resources(hass)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)


async def _async_register_static_paths(hass: HomeAssistant) -> None:
    """Register every card's JS file as a cache-headed static path (idempotent).

    All cards are registered in ONE ``async_register_static_paths`` call.
    """
    if hass.data.get(_DATA_STATIC_DONE):
        return
    try:
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(url, str(path), cache_headers=True)
                for url, path in _CARDS
            ]
        )
    except Exception:  # noqa: BLE001 -- the cards are an enhancement, never a blocker
        _LOGGER.warning(
            "Could not serve the bundled Lovelace cards under %s",
            _FRONTEND_DIR,
            exc_info=True,
        )
        return
    hass.data[_DATA_STATIC_DONE] = True


async def _async_register_resources(hass: HomeAssistant) -> None:
    """Create/update the Lovelace resources, swallowing every failure.

    The whole body is wrapped so NO exception ever escapes (the async_setup
    contract): a failure here just means a card must be added manually.
    """
    if hass.data.get(_DATA_RESOURCE_DONE):
        return
    try:
        await _async_register_resources_impl(hass)
    except Exception:  # noqa: BLE001 -- never let a resource hiccup block setup
        _LOGGER.warning(
            "Could not auto-register the bundled Lovelace resources; add each "
            "manually as a JavaScript-module dashboard resource pointing at %s",
            ", ".join(_versioned_url(url) for url, _path in _CARDS),
            exc_info=True,
        )


async def _async_register_resources_impl(hass: HomeAssistant) -> None:
    """Storage-mode resource create/update per card (the raising inner worker)."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if (
        lovelace is None
        or getattr(lovelace, "resource_mode", None) != _RESOURCE_MODE_STORAGE
    ):
        # YAML mode, recovery mode, or lovelace not set up: we cannot mutate a
        # storage collection. Tell the operator how to add them by hand and stop.
        _LOGGER.info(
            "Lovelace is not in storage mode; add the bundled cards manually as "
            "JavaScript-module dashboard resources pointing at %s",
            ", ".join(_versioned_url(url) for url, _path in _CARDS),
        )
        return

    resources = lovelace.resources
    # Ensure the storage collection has loaded its items before we inspect them.
    if not getattr(resources, "loaded", True):
        await resources.async_load()
        resources.loaded = True

    # One create/update pass per card, matched to an existing resource by url
    # prefix (each card's filename makes its prefix unique).
    for url, _path in _CARDS:
        await _sync_one_resource(resources, url)

    hass.data[_DATA_RESOURCE_DONE] = True


async def _sync_one_resource(resources: object, url: str) -> None:
    """Create or version-update ONE card's storage-mode Lovelace resource."""
    desired = _versioned_url(url)
    existing = None
    for item in resources.async_items():
        item_url = item.get("url", "")
        if isinstance(item_url, str) and item_url.startswith(url):
            existing = item
            break

    if existing is None:
        await resources.async_create_item({"res_type": "module", "url": desired})
        _LOGGER.info("Registered bundled card Lovelace resource: %s", desired)
    elif existing.get("url") != desired:
        await resources.async_update_item(
            existing["id"], {"res_type": "module", "url": desired}
        )
        _LOGGER.info("Updated bundled card Lovelace resource to %s", desired)
    # else: an identical resource already exists — nothing to do.
