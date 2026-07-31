"""Setup-path tests: the REAL coordinator constructor + ``__init__`` lifecycle.

Every other coordinator test builds the instance via ``__new__`` and injects
only the attributes a glue method reads — so the constructor itself (config
merge ``{**data, **options}``, ``SiteConfig.from_dict``,
``LearnerConfig.from_dict``, option resolution) and the setup wiring in
``__init__.py`` (prime-from-store, nightly-job scheduling, stop-flush,
update-listener, unload bookkeeping) had no coverage. These tests drive the
real ``async_setup_entry`` / ``async_unload_entry`` against fakes: a fake
hass (bus / config_entries / services / http), the real ``ForecastStore``
over the in-memory HA-Store fake from tests/test_store_v2.py, and the real
``BalconySolarCoordinator`` class — only HA's own first-refresh machinery and
the time-change scheduler are patched out (both are HA-side, covered by HA).

Needs Home Assistant; skipped on the plain-core path.
"""

from __future__ import annotations

import copy
from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")

from homeassistant.const import EVENT_HOMEASSISTANT_STOP  # noqa: E402
from homeassistant.core import CoreState  # noqa: E402

import custom_components.balcony_solar_forecast as init_mod  # noqa: E402
from custom_components.balcony_solar_forecast import (  # noqa: E402
    coordinator as coordinator_mod,
)
from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    CONF_FAST_LEARNER_ENABLED,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SITE,
    DEFAULT_SITE,
    DOMAIN,
)
from custom_components.balcony_solar_forecast.coordinator import (  # noqa: E402
    BalconySolarCoordinator,
)
from custom_components.balcony_solar_forecast.store import (  # noqa: E402
    ForecastStore,
)
from tests.test_store_v2 import FakeStore  # noqa: E402

_ENTRY_ID = "setup1"


class _FakeBus:
    """Records one-shot listeners so tests can fire them later."""

    def __init__(self) -> None:
        self.listeners: list[tuple[str, object]] = []

    def async_listen_once(self, event: str, callback):
        self.listeners.append((event, callback))

        def _unsub() -> None:
            pass

        return _unsub

    async def fire(self, event: str) -> None:
        for registered, callback in list(self.listeners):
            if registered == event:
                ret = callback(None)
                if hasattr(ret, "__await__"):
                    await ret


class _FakeConfigEntries:
    def __init__(self) -> None:
        self.forwarded: list[tuple[object, list]] = []
        self.unloaded: list[tuple[object, list]] = []
        self.reloaded: list[str] = []
        self.unload_ok = True

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded.append((entry, list(platforms)))

    async def async_unload_platforms(self, entry, platforms):
        self.unloaded.append((entry, list(platforms)))
        return self.unload_ok

    async def async_reload(self, entry_id: str):
        self.reloaded.append(entry_id)


class _FakeServices:
    def __init__(self) -> None:
        self.registered: list[str] = []

    def has_service(self, domain: str, service: str) -> bool:
        return service in self.registered

    def async_register(self, domain, service, handler, **kwargs):
        self.registered.append(service)


class _FakeHttp:
    def __init__(self) -> None:
        self.static_paths: list = []

    async def async_register_static_paths(self, configs):
        self.static_paths.extend(configs)


class _FakeHass:
    """Hass double with the attributes the setup/lifecycle path touches."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.bus = _FakeBus()
        self.config_entries = _FakeConfigEntries()
        self.services = _FakeServices()
        self.http = _FakeHttp()
        # NOT running: the channel-health check + frontend resource sync defer
        # to EVENT_HOMEASSISTANT_STARTED instead of firing inline.
        self.state = CoreState.starting
        self.is_running = False


class _FakeEntry:
    """ConfigEntry double capturing the unload/listener registrations."""

    def __init__(self, data, options) -> None:
        self.entry_id = _ENTRY_ID
        self.title = "Balkon"
        self.data = data
        self.options = options
        self.on_unload: list = []
        self.update_listeners: list = []
        self.background_tasks: list[str] = []

    def async_on_unload(self, unsub) -> None:
        self.on_unload.append(unsub)

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)

        def _unsub() -> None:
            pass

        return _unsub

    def async_create_background_task(self, hass, coro, *, name: str) -> None:
        self.background_tasks.append(name)
        # The catch-up coroutine is driven (and covered) elsewhere; close it so
        # the test does not leak a never-awaited coroutine.
        coro.close()


def _entry(**options) -> _FakeEntry:
    return _FakeEntry(data={CONF_SITE: copy.deepcopy(DEFAULT_SITE)}, options=options)


def _real_store(hass, backend: FakeStore | None = None) -> tuple[ForecastStore, FakeStore]:
    backend = backend or FakeStore(None)
    return ForecastStore(hass, _ENTRY_ID, store=backend), backend  # type: ignore[arg-type]


@pytest.fixture
def patched_setup(monkeypatch):
    """Patch the two HA-side seams of ``async_setup_entry``.

    Returns the ``nightly`` list the fake time-change scheduler appends its
    kwargs to. ``async_config_entry_first_refresh`` is HA's own refresh
    machinery (its failure ladder is covered by the coordinator tests via
    ``_async_update_data`` directly); ``async_track_time_change`` is HA's
    event helper. Both are patched so the REAL constructor and wiring run.
    """
    nightly: list[dict] = []

    def _fake_track(hass, action, **kwargs):
        nightly.append(kwargs)
        return lambda: None

    async def _noop_first_refresh(self):
        return None

    monkeypatch.setattr(init_mod, "async_get_clientsession", lambda hass: object())
    monkeypatch.setattr(coordinator_mod, "async_track_time_change", _fake_track)
    monkeypatch.setattr(
        BalconySolarCoordinator,
        "async_config_entry_first_refresh",
        _noop_first_refresh,
    )
    return nightly


async def test_setup_entry_runs_real_constructor_and_wiring(patched_setup, monkeypatch):
    """The full setup path: real constructor (config merge + SiteConfig /
    LearnerConfig resolution), prime-from-store, platform forward, nightly
    scheduling, background catch-up, stop-flush + update-listener wiring."""
    hass = _FakeHass()
    entry = _entry(**{CONF_RECOMPUTE_INTERVAL: 1200, CONF_FAST_LEARNER_ENABLED: False})
    backend = FakeStore(None)
    store = ForecastStore(hass, entry.entry_id, store=backend)  # type: ignore[arg-type]

    # ForecastStore is constructed inside async_setup_entry; inject ours so the
    # SAME instance the coordinator uses is observable (flush assertions below).
    monkeypatch.setattr(init_mod, "ForecastStore", lambda hass_, entry_id: store)

    ok = await init_mod.async_setup_entry(hass, entry)

    assert ok is True
    coordinator = hass.data[DOMAIN][_ENTRY_ID]
    assert isinstance(coordinator, BalconySolarCoordinator)
    # The REAL constructor ran: options won the {**data, **options} merge …
    assert coordinator.update_interval == timedelta(seconds=1200)
    # … SiteConfig.from_dict parsed the shipped default site …
    assert coordinator._site.latitude == pytest.approx(51.1)
    assert coordinator._site.longitude == pytest.approx(10.4)
    assert [p.name for p in coordinator._site.planes] == [
        "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8",
    ]
    # … and LearnerConfig.from_dict resolved the kill switch from options.
    assert coordinator._learner_config.fast_enabled is False
    # Prime-from-store ran against the fresh store: learner states loaded,
    # config fingerprint recorded, no weather image yet.
    assert coordinator._learner_states_loaded is True
    assert store.get_config_fingerprint() is not None
    assert coordinator.weather_age_seconds_live is None
    # Wiring: platforms forwarded, nightly job scheduled at 01:30 local,
    # catch-up backgrounded, stop-flush + update listener registered.
    assert hass.config_entries.forwarded == [(entry, init_mod.PLATFORMS)]
    assert patched_setup == [{"hour": 1, "minute": 30, "second": 0}]
    assert entry.background_tasks == [f"{DOMAIN}_startup_catchup"]
    assert len(entry.on_unload) >= 2
    assert entry.update_listeners == [init_mod._async_reload_entry]


async def test_stop_event_flushes_store_and_update_listener_reloads(
    patched_setup, monkeypatch
):
    """EVENT_HOMEASSISTANT_STOP awaits a store flush (hard-shutdown last-good
    cache); the update listener reloads the entry (options-flow path)."""
    hass = _FakeHass()
    entry = _entry()
    store, backend = _real_store(hass)
    monkeypatch.setattr(init_mod, "ForecastStore", lambda hass_, entry_id: store)

    await init_mod.async_setup_entry(hass, entry)

    # Fire HA stop: the once-listener awaits store.async_flush().
    assert backend.immediate_saves == 0
    await hass.bus.fire(EVENT_HOMEASSISTANT_STOP)
    assert backend.immediate_saves == 1
    assert backend.saved is not None

    # The update listener drives exactly one reload of THIS entry.
    await entry.update_listeners[0](hass, entry)
    assert hass.config_entries.reloaded == [_ENTRY_ID]


async def test_unload_entry_tears_down_and_flushes(patched_setup, monkeypatch):
    """Unload: platforms unloaded, nightly listener cancelled, pending store
    write flushed (a reload fires no HA-stop event), hass.data cleaned."""
    hass = _FakeHass()
    entry = _entry()
    store, backend = _real_store(hass)
    monkeypatch.setattr(init_mod, "ForecastStore", lambda hass_, entry_id: store)

    await init_mod.async_setup_entry(hass, entry)

    coordinator = hass.data[DOMAIN][_ENTRY_ID]
    nightly_unsub = coordinator._unsub_nightly
    assert nightly_unsub is not None

    ok = await init_mod.async_unload_entry(hass, entry)

    assert ok is True
    assert hass.config_entries.unloaded == [(entry, init_mod.PLATFORMS)]
    # async_shutdown_extra ran: nightly listener consumed, scalar state reset.
    assert coordinator._unsub_nightly is None
    # The pending write was flushed even without an HA-stop event.
    assert backend.immediate_saves == 1
    # Bookkeeping: entry gone, and the empty domain dict is popped too.
    assert DOMAIN not in hass.data


async def test_unload_entry_keeps_bookkeeping_when_platforms_refuse(
    patched_setup, monkeypatch
):
    """A failed platform unload must not drop the coordinator from hass.data."""
    hass = _FakeHass()
    entry = _entry()
    store, _backend = _real_store(hass)
    monkeypatch.setattr(init_mod, "ForecastStore", lambda hass_, entry_id: store)

    await init_mod.async_setup_entry(hass, entry)

    hass.config_entries.unload_ok = False
    ok = await init_mod.async_unload_entry(hass, entry)

    assert ok is False
    assert hass.data[DOMAIN][_ENTRY_ID] is not None


async def test_component_setup_registers_services_and_frontend():
    """``async_setup`` registers all ten services + serves the bundled cards
    (action-setup quality scale: services exist before any config entry)."""
    hass = _FakeHass()
    ok = await init_mod.async_setup(hass, {})
    assert ok is True
    assert len(hass.services.registered) == 10
    assert "get_forecast" in hass.services.registered
    assert "run_bootstrap" in hass.services.registered
    # Both bundled cards are served under the shared static prefix.
    urls = [c.url_path for c in hass.http.static_paths]
    assert urls == [
        "/balcony_solar_forecast/frontend/shade_profile_card.js",
        "/balcony_solar_forecast/frontend/power_history_card.js",
    ]
    assert hass.data[f"{DOMAIN}_frontend_static_registered"] is True


async def test_prime_from_warm_store_restores_fetch_provenance():
    """A stored last-good payload primes the degradation ladder's age anchor
    (real constructor + real store, no full setup)."""
    hass = _FakeHass()
    entry = _entry()
    store1, backend1 = _real_store(hass)
    await store1.async_load()
    store1.set_last_payload({"minutely_15": {}}, "2026-07-30T08:00:00+00:00")
    on_disk = backend1.pending_snapshot()

    # "Restart": a fresh store loads the flushed blob; a fresh coordinator
    # (REAL constructor) primes from it.
    store2 = ForecastStore(hass, entry.entry_id, store=FakeStore(on_disk))  # type: ignore[arg-type]
    await store2.async_load()
    coordinator = BalconySolarCoordinator(
        hass, entry, fetcher=object(), store=store2  # type: ignore[arg-type]
    )
    await coordinator.async_prime_from_store()

    assert coordinator._last_fetched_at is not None
    assert coordinator._last_fetched_at.isoformat() == "2026-07-30T08:00:00+00:00"
    # The live age is a real, non-negative number of seconds now.
    assert coordinator.weather_age_seconds_live >= 0.0

    # A corrupt fetched_at must NOT prime the ladder (stays cold).
    store3, _ = _real_store(hass)
    await store3.async_load()
    store3.set_last_payload({"minutely_15": {}}, "not-a-date")
    coordinator3 = BalconySolarCoordinator(
        hass, entry, fetcher=object(), store=store3  # type: ignore[arg-type]
    )
    await coordinator3.async_prime_from_store()
    assert coordinator3._last_fetched_at is None
    assert coordinator3.weather_age_seconds_live is None


async def test_load_learner_states_tolerates_store_without_getters():
    """A store missing the v2/v3 getters (older schema in flight) leaves the
    neutral in-memory states — setup must never crash on it (SPEC §16.4)."""
    hass = _FakeHass()
    entry = _entry()
    store, _ = _real_store(hass)
    coordinator = BalconySolarCoordinator(
        hass, entry, fetcher=object(), store=store  # type: ignore[arg-type]
    )
    # Simulate the older-schema store: no learner getters at all.
    coordinator._store = object()  # type: ignore[assignment]
    coordinator._learner_states_loaded = False

    coordinator._load_learner_states()

    assert coordinator._learner_states_loaded is True
    assert coordinator._bias_state.cells == {}
    assert coordinator._shademap_state.channels == {}
    assert coordinator._scoreboard_state.days == {}
