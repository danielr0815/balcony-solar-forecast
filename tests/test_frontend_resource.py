"""Tests for the bundled shade-profile card serving + auto-registration (SPEC §15).

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
    assert len(hass.http.registered) == len(_frontend._CARDS)
    by_url = {cfg.url_path: cfg for cfg in hass.http.registered}
    for url, path in _frontend._CARDS:
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
    assert len(hass.http.registered) == len(_frontend._CARDS)


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
    assert len(hass.http.registered) == len(_frontend._CARDS)


async def test_lovelace_absent_does_not_raise():
    hass = _FakeHass(lovelace=None)
    await _frontend.async_register_frontend(hass)  # must not raise
    assert len(hass.http.registered) == len(_frontend._CARDS)


# ---------------------------------------------------------------------------
# A raising resources collection is swallowed (async_setup contract).
# ---------------------------------------------------------------------------


async def test_raising_resources_items_is_swallowed():
    hass, res = _storage_hass(items=[], raise_on="items")
    await _frontend.async_register_frontend(hass)  # must not raise
    assert res.created == []
    assert len(hass.http.registered) == len(_frontend._CARDS)


async def test_raising_resources_create_is_swallowed():
    hass, res = _storage_hass(items=[], raise_on="create")
    await _frontend.async_register_frontend(hass)  # must not raise
    assert len(hass.http.registered) == len(_frontend._CARDS)


# ---------------------------------------------------------------------------
# Not-yet-running hass -> resource registration deferred to STARTED.
# ---------------------------------------------------------------------------


async def test_registration_deferred_until_started():
    hass, res = _storage_hass(items=[], is_running=False)
    await _frontend.async_register_frontend(hass)

    # Static paths are registered immediately; the resources are NOT yet created.
    assert len(hass.http.registered) == len(_frontend._CARDS)
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


def test_js_card_file_sanity():
    text = _frontend._FRONTEND_FILE.read_text(encoding="utf-8")
    assert text.strip(), "card JS is empty"

    assert 'customElements.define("balcony-shade-profile-card"' in text
    assert "window.customCards" in text

    # Every ATTR_SP_* array name from const must be referenced by the card
    # (no hardcoded duplicate list — iterate const).
    attr_names = [v for k, v in vars(const).items() if k.startswith("ATTR_SP_")]
    assert attr_names, "no ATTR_SP_* names found in const"
    for name in attr_names:
        assert f'"{name}"' in text, f"card JS does not reference attribute {name!r}"

    # The three τ threshold colours (identical to the ApexCharts snippet).
    for color in ("#2ecc71", "#e67e22", "#c0392b"):
        assert color in text, f"missing τ colour {color}"

    # The group/single τ-view toggle labels (SPEC §5 read-time pooling), both
    # locales — the operator compares each module's individual map vs the pool.
    for label in ("Gruppe", "Einzeln", "Group", "Single"):
        assert f'"{label}"' in text, f"card JS is missing toggle label {label!r}"

    # The hover crosshair wires a mousemove handler over the plot overlay.
    assert "mousemove" in text, "card JS has no hover crosshair (mousemove)"

    # Card-LOCAL comparison date (SPEC §15): a second sun path overlaid from the
    # read-only get_shade_profile service. Assert the compare-date input marker,
    # the two-locale "Compare" label, and the reliable service-call-with-response
    # variant (the low-level websocket call_service with return_response).
    assert "compare-input" in text, "card JS has no comparison date input"
    for label in ("Compare", "Vergleich"):
        assert f'"{label}"' in text, f"card JS is missing compare label {label!r}"
    assert "get_shade_profile" in text, "card JS does not call get_shade_profile"
    assert "return_response" in text, "card JS does not request a service response"
    assert "call_service" in text, "card JS does not use the WS call_service command"

    # No external-URL ES imports (self-contained module).
    assert re.search(r'from\s+["\']https?:', text) is None


def test_power_history_js_card_sanity():
    # The power-history card is card 1 in the _CARDS list.
    power_file = _frontend._CARDS[1][1]
    assert power_file.name == "power_history_card.js"
    text = power_file.read_text(encoding="utf-8")
    assert text.strip(), "power-history card JS is empty"

    # Registers the custom element + advertises itself to the picker.
    assert 'customElements.define("balcony-power-history-card"' in text
    assert "window.customCards" in text

    # Reads hourly LTS via the recorder websocket command, the forecast curve
    # attribute, and the module-name attribute the Python contract now exposes.
    assert "recorder/statistics_during_period" in text
    assert "statistics_during_period" in text
    assert "wh_period" in text
    assert "source_names" in text

    # The hover crosshair wires a mousemove handler over the plot overlay.
    assert "mousemove" in text, "power-history card JS has no mousemove hover"

    # Day/Week navigation (part 2b): the ◀/▶ nav glyphs, the Day|Week toggle
    # labels in both locales, and the week view's daily-statistics marker.
    assert "◀" in text and "▶" in text, "power-history card JS has no nav arrows"
    for label in ("Day", "Week", "Tag", "Woche"):
        assert f'"{label}"' in text, f"power-history card JS missing toggle label {label!r}"
    assert 'period: "day"' in text, "power-history card JS has no week (daily) statistics query"

    # Past-day dashed line = the ISSUED archived forecast, read via the read-only
    # get_issued_forecast action (the stable low-level websocket variant).
    assert "get_issued_forecast" in text, "power-history card JS does not call get_issued_forecast"
    assert "call_service" in text, "power-history card JS does not use the WS call_service command"
    assert "return_response" in text, "power-history card JS does not request a service response"

    # Self-contained module: no external-URL ES imports, and it must NOT pull in
    # the sibling shade-profile card (each card stays independent).
    assert re.search(r'from\s+["\']https?:', text) is None
    assert "shade_profile_card" not in text
