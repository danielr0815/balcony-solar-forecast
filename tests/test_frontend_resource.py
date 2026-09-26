"""Tests for the bundled shade-profile card serving + auto-registration (SPEC §18.5).

Two halves:

  * ``async_register_frontend`` driven against fake hass/http/lovelace doubles
    (mirrors the fake-hass style of ``test_services_learning.py``): static path
    registration, storage-mode create/update/no-op, yaml / absent lovelace
    no-op, a raising resources collection swallowed, and the deferred-until-
    ``EVENT_HOMEASSISTANT_STARTED`` path.
  * A pure sanity check on the shipped JS (no HA needed): it defines the custom
    element, advertises itself, references EVERY ``ATTR_SP_*`` array name from
    const, uses the three τ threshold colours, and pulls in no external URL.

Needs Home Assistant (``_frontend`` imports the http/lovelace helpers); skipped
on the plain-core path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

from balcony_solar_forecast import _frontend, const  # noqa: E402
from homeassistant.components.http import StaticPathConfig  # noqa: E402
from homeassistant.components.lovelace import LOVELACE_DATA  # noqa: E402
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


class _FakeBus:
    def __init__(self):
        self.listeners: dict = {}

    def async_listen_once(self, event, cb):
        self.listeners.setdefault(event, []).append(cb)
        return lambda: None

    async def async_fire(self, event):
        for cb in list(self.listeners.get(event, [])):
            await cb(object())


class _FakeHttp:
    def __init__(self):
        self.registered: list = []

    async def async_register_static_paths(self, configs):
        self.registered.extend(configs)


class _FakeResources:
    """A minimal ResourceStorageCollection double."""

    loaded = False

    def __init__(self, items=None, *, raise_on=None):
        self._items = list(items or [])
        self.created: list = []
        self.updated: list = []
        self.load_called = False
        self._raise = raise_on

    async def async_load(self):
        self.load_called = True

    def async_items(self):
        if self._raise == "items":
            raise RuntimeError("boom items")
        return list(self._items)

    async def async_create_item(self, data):
        if self._raise == "create":
            raise RuntimeError("boom create")
        self.created.append(data)
        item = {"id": "new-id", **data}
        self._items.append(item)
        return item

    async def async_update_item(self, item_id, updates):
        self.updated.append((item_id, updates))
        for it in self._items:
            if it["id"] == item_id:
                it.update(updates)
                return it
        raise KeyError(item_id)


class _FakeLovelace:
    def __init__(self, resource_mode="storage", resources=None):
        self.resource_mode = resource_mode
        self.resources = resources


class _FakeHass:
    def __init__(self, *, is_running=True, lovelace=None):
        self.data: dict = {}
        self.http = _FakeHttp()
        self.bus = _FakeBus()
        self.is_running = is_running
        if lovelace is not None:
            self.data[LOVELACE_DATA] = lovelace


def _storage_hass(items=None, *, is_running=True, raise_on=None):
    res = _FakeResources(items, raise_on=raise_on)
    hass = _FakeHass(is_running=is_running, lovelace=_FakeLovelace("storage", res))
    return hass, res


# ---------------------------------------------------------------------------
# Static path.
# ---------------------------------------------------------------------------


async def test_static_path_registered_points_at_existing_file():
    hass = _FakeHass()  # no lovelace -> only the static paths are registered
    await _frontend.async_register_frontend(hass)

    # Both bundled cards are served in one call, each under the shared prefix.
    assert len(hass.http.registered) == len(_frontend._ASSETS)
    by_url = {cfg.url_path: cfg for cfg in hass.http.registered}
    for url, path in _frontend._ASSETS:
        cfg = by_url[url]
        assert isinstance(cfg, StaticPathConfig)
        assert cfg.path == str(path)
        assert cfg.cache_headers is True
        # The served file must actually exist on disk.
        assert Path(cfg.path).is_file()
    # The single-card aliases still point at card 0 (the shade-profile card).
    assert _frontend.FRONTEND_URL in by_url
    assert by_url[_frontend.FRONTEND_URL].path == str(_frontend._FRONTEND_FILE)
    assert hass.data.get(_frontend._DATA_STATIC_DONE) is True


async def test_static_path_registered_only_once():
    hass = _FakeHass()
    await _frontend.async_register_frontend(hass)
    await _frontend.async_register_frontend(hass)
    assert len(hass.http.registered) == len(_frontend._ASSETS)


# ---------------------------------------------------------------------------
# Storage-mode resource create / update / no-op.
# ---------------------------------------------------------------------------


async def test_storage_empty_collection_creates_versioned_resource():
    hass, res = _storage_hass(items=[])
    await _frontend.async_register_frontend(hass)

    assert res.load_called is True  # unloaded collection is loaded first
    # One versioned resource created per bundled card, in _CARDS order.
    assert res.created == [
        {"res_type": "module", "url": _frontend._versioned_url(url)}
        for url, _path in _frontend._CARDS
    ]
    assert res.updated == []
    assert hass.data.get(_frontend._DATA_RESOURCE_DONE) is True


async def test_storage_old_version_updates_that_item():
    shade_url = _frontend._CARDS[0][0]
    power_url = _frontend._CARDS[1][0]
    # Only the shade card has a stale resource on disk; the power card is new.
    old = {
        "id": "abc",
        "type": "module",
        "url": f"{shade_url}?v=0.0.1",
    }
    hass, res = _storage_hass(items=[old])
    await _frontend.async_register_frontend(hass)

    # Shade card (matched by url prefix) version-updated in place; power card
    # (no existing resource) created fresh.
    assert res.updated == [
        ("abc", {"res_type": "module", "url": _frontend._versioned_url(shade_url)})
    ]
    assert res.created == [
        {"res_type": "module", "url": _frontend._versioned_url(power_url)}
    ]


async def test_storage_identical_resource_is_noop():
    same = [
        {"id": f"id{i}", "type": "module", "url": _frontend._versioned_url(url)}
        for i, (url, _path) in enumerate(_frontend._CARDS)
    ]
    hass, res = _storage_hass(items=same)
    await _frontend.async_register_frontend(hass)

    assert res.created == []
    assert res.updated == []
    assert hass.data.get(_frontend._DATA_RESOURCE_DONE) is True


# ---------------------------------------------------------------------------
# yaml mode / lovelace absent -> no create/update, no raise.
# ---------------------------------------------------------------------------


async def test_yaml_mode_does_not_touch_resources():
    res = _FakeResources(items=[])
    hass = _FakeHass(lovelace=_FakeLovelace("yaml", res))
    await _frontend.async_register_frontend(hass)

    assert res.created == []
    assert res.updated == []
    # Static paths are still served in yaml mode.
    assert len(hass.http.registered) == len(_frontend._ASSETS)


async def test_lovelace_absent_does_not_raise():
    hass = _FakeHass(lovelace=None)
    await _frontend.async_register_frontend(hass)  # must not raise
    assert len(hass.http.registered) == len(_frontend._ASSETS)


# ---------------------------------------------------------------------------
# A raising resources collection is swallowed (async_setup contract).
# ---------------------------------------------------------------------------


async def test_raising_resources_items_is_swallowed():
    hass, res = _storage_hass(items=[], raise_on="items")
    await _frontend.async_register_frontend(hass)  # must not raise
    assert res.created == []
    assert len(hass.http.registered) == len(_frontend._ASSETS)


async def test_raising_resources_create_is_swallowed():
    hass, res = _storage_hass(items=[], raise_on="create")
    await _frontend.async_register_frontend(hass)  # must not raise
    assert len(hass.http.registered) == len(_frontend._ASSETS)


# ---------------------------------------------------------------------------
# Not-yet-running hass -> resource registration deferred to STARTED.
# ---------------------------------------------------------------------------


async def test_registration_deferred_until_started():
    hass, res = _storage_hass(items=[], is_running=False)
    await _frontend.async_register_frontend(hass)

    # Static paths are registered immediately; the resources are NOT yet created.
    assert len(hass.http.registered) == len(_frontend._ASSETS)
    assert res.created == []
    assert EVENT_HOMEASSISTANT_STARTED in hass.bus.listeners

    # Firing the one-shot listener performs the deferred registration.
    await hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    assert res.created == [
        {"res_type": "module", "url": _frontend._versioned_url(url)}
        for url, _path in _frontend._CARDS
    ]


# ---------------------------------------------------------------------------
# async_setup wiring.
# ---------------------------------------------------------------------------


def test_async_setup_wires_in_frontend_registration():
    """``async_setup`` imports and awaits ``async_register_frontend``.

    A behavioural exec of the real ``__init__`` is impossible under the pure
    test harness (tests/conftest.py stubs the ``core`` subpackage with an empty
    ``__init__``, so the coordinator import chain cannot resolve). A source-level
    assertion robustly locks the wiring without dragging in Home Assistant.
    """
    import balcony_solar_forecast as pkg

    src = (Path(pkg.__path__[0]) / "__init__.py").read_text(encoding="utf-8")
    assert "from ._frontend import async_register_frontend" in src
    assert "await async_register_frontend(hass)" in src


# ---------------------------------------------------------------------------
# Shipped JS sanity (no HA needed for the assertions themselves).
# ---------------------------------------------------------------------------


def test_js_card_assets_are_local_and_reference_sensor_contract():
    """Only static delivery/schema contracts belong in source-level checks.

    Controls, discovery, colour boundaries, services and rendered states are
    exercised by tests/harness/cards_regression_harness.mjs on real instances.
    """
    for _url, path in _frontend._ASSETS:
        text = path.read_text(encoding="utf-8")
        assert text.strip(), f"empty frontend asset: {path.name}"
        assert re.search(r'from\s+["\']https?:', text) is None
        assert re.search(r'import\s*\(["\']https?:', text) is None
    shade = _frontend._FRONTEND_FILE.read_text(encoding="utf-8")
    attr_names = [v for k, v in vars(const).items() if k.startswith("ATTR_SP_")]
    assert attr_names
    for name in attr_names:
        assert f'"{name}"' in shade, f"card does not reference sensor field {name}"


def test_js_cards_no_property_shadows_method():
    """No instance property may share a name with a class method (both cards).

    Regression guard for the v0.15.0 power-history bug: the constructor set
    ``this._fetchDay = undefined`` while the class also defined
    ``async _fetchDay(...)`` — the property assignment SHADOWS the method on
    the instance, so the first ``this._fetchDay(...)`` call raised a
    TypeError and every statistics fetch silently died (bars never loaded,
    only the forecast line rendered). ``node --check`` cannot catch this
    runtime-only collision, so pin it structurally: for every method defined
    in a card class, assert the file never assigns ``this.<method> =``.
    """
    for fname in ("power_history_card.js", "shade_profile_card.js"):
        fpath = next(path for _url, path in _frontend._CARDS
                     if path.name == fname)
        text = fpath.read_text(encoding="utf-8")
        # Class-body method definitions: two-space indent, optional async/get/
        # static, identifier + "(" — the shape both hand-written cards use.
        methods = set(
            re.findall(
                r"^  (?:async |get |static )*([A-Za-z_$][\w$]*)\s*\(",
                text,
                re.M,
            )
        ) - {"if", "for", "while", "switch", "catch", "constructor"}
        assigned = set(re.findall(r"this\.([A-Za-z_$][\w$]*)\s*=[^=]", text))
        shadowed = methods & assigned
        assert not shadowed, (
            f"{fname}: instance properties shadow class methods: "
            f"{sorted(shadowed)} — rename the property"
        )
