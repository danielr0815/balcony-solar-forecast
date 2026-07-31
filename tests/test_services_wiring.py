"""Tests for the service REGISTRATION wiring + remaining handler error paths.

``async_register_services`` registers all ten actions once (action-setup
quality scale: services exist before any config entry, and stay after the
last one unloads — an automation then gets a clear ServiceValidationError
instead of "Service not found"). These tests drive the REGISTERED handler
closures end-to-end against fakes (registration -> invocation -> response),
plus the error branches the handler-level tests in
tests/test_services_learning.py do not reach: the no-entry resolve, an
unreadable bootstrap file, a non-JSON-object payload, the rollback guards and
the small duck-typing helpers.

Needs Home Assistant (ServiceValidationError etc.); skipped on the
plain-core path.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

from homeassistant.exceptions import ServiceValidationError  # noqa: E402

from custom_components.balcony_solar_forecast import _services as svc  # noqa: E402

# Reuse the established fakes/builders (same import-reuse pattern as
# tests/test_nightly_orchestration.py).
from tests.test_services_learning import (  # noqa: E402
    _Call,
    _FakeCoordinator,
    _FakeHass,
    _shade_site,
    _shade_state,
    _ShadeCoordinator,
    _ShadeProfileCoordinator,
    _uniform,
)


class _ServicesRegistry:
    """``hass.services`` stand-in: keeps the registered handlers invocable."""

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def has_service(self, domain: str, service: str) -> bool:
        return service in self.handlers

    def async_register(self, domain, service, handler, **kwargs) -> None:
        self.handlers[service] = handler


class _WiredHass(_FakeHass):
    def __init__(self, store, *, allowed=True) -> None:
        super().__init__(store, allowed=allowed)
        self.services = _ServicesRegistry()


def _wired(store) -> _WiredHass:
    hass = _WiredHass(store)
    svc.async_register_services(hass)
    return hass


# --------------------------------------------------------------------------
# Registration: all ten actions, exactly once.
# --------------------------------------------------------------------------


def test_register_services_registers_all_ten_idempotently():
    hass = _WiredHass({})
    svc.async_register_services(hass)
    svc.async_register_services(hass)  # second call must not re-register
    assert sorted(hass.services.handlers) == [
        "dump_shademap",
        "get_forecast",
        "get_issued_forecast",
        "get_shade_profile",
        "import_bootstrap",
        "install_dashboard",
        "reset_day_ahead_bias",
        "rollback_learners",
        "run_bootstrap",
        "suggest_shade_groups",
    ]


# --------------------------------------------------------------------------
# The registered handler closures reach their implementations.
# --------------------------------------------------------------------------


class _DataCoordinator:
    """Coordinator double carrying a flat served-forecast data dict."""

    def __init__(self, data) -> None:
        self.data = data


async def test_registered_get_forecast_returns_served_curves():
    data = {
        "slot_starts": ["2026-07-30T10:00:00+00:00"],
        "plane_watts": {"M1": [123.0]},
        "hourly_wh": {"2026-07-30T10:00": 30.0},
        "computed_at": "2026-07-30T10:00:05+00:00",
    }
    hass = _wired({"e1": _DataCoordinator(data)})
    resp = await hass.services.handlers["get_forecast"](_Call({}))
    entry = resp["entries"]["e1"]
    assert entry["planes"] == {"M1": [123.0]}
    assert entry["slot_starts"] == ["2026-07-30T10:00:00+00:00"]
    assert entry["total_hourly"] == {"2026-07-30T10:00": 30.0}
    assert entry["issued_at"] == "2026-07-30T10:00:05+00:00"


async def test_registered_import_bootstrap_forwards_payload():
    coord = _FakeCoordinator(import_result={"bias_cells": 4})
    hass = _wired({"e1": coord})
    resp = await hass.services.handlers["import_bootstrap"](
        _Call({"payload": {"a": 1}})
    )
    assert coord.imported == {"a": 1}
    assert resp == {"result": {"bias_cells": 4}}


async def test_registered_run_bootstrap_without_entry_is_a_clear_error():
    hass = _wired({})
    with pytest.raises(ServiceValidationError, match="No Balcony Solar"):
        await hass.services.handlers["run_bootstrap"](_Call({}))


async def test_registered_dump_shademap_returns_entries():
    state = {"channels": {"M1": {"1:2:0": {"tau": 0.5, "n": 3}}}}
    hass = _wired({"e1": _FakeCoordinator(shademap=state)})
    resp = await hass.services.handlers["dump_shademap"](_Call({}))
    assert resp["entries"]["e1"]["channels"]["M1"]["bins"][0]["n"] == 3


async def test_registered_reset_day_ahead_bias_forwards():
    class _Resettable:
        def __init__(self) -> None:
            self.calls = 0

        async def async_reset_day_ahead_bias(self):
            self.calls += 1
            return {"cells_cleared": 2}

    coord = _Resettable()
    hass = _wired({"e1": coord})
    resp = await hass.services.handlers["reset_day_ahead_bias"](_Call({}))
    assert coord.calls == 1
    assert resp == {"result": {"cells_cleared": 2}}


async def test_registered_install_dashboard_requires_a_config_entry():
    """A coordinator double without ``entry`` cannot resolve entity ids — the
    handler surfaces that as a user error, not a traceback."""
    hass = _wired({"e1": _FakeCoordinator()})
    with pytest.raises(ServiceValidationError, match="no config entry"):
        await hass.services.handlers["install_dashboard"](_Call({}))


async def test_registered_suggest_shade_groups_handler():
    state = _shade_state({"A": _uniform(0.3), "B": _uniform(0.3)})
    coord = _ShadeCoordinator(_shade_site(["A", "B"]), state)
    hass = _wired({"e1": coord})
    resp = await hass.services.handlers["suggest_shade_groups"](_Call({}))
    plane_sets = sorted(tuple(g["planes"]) for g in resp["result"]["groups"])
    assert plane_sets == [("A", "B")]


async def test_registered_get_shade_profile_handler():
    from datetime import date

    coord = _ShadeProfileCoordinator(
        names=["M1"], module="M1", day=date(2026, 6, 21)
    )
    hass = _wired({"e1": coord})
    resp = await hass.services.handlers["get_shade_profile"](_Call({}))
    assert resp["result"]["module"] == "M1"


async def test_registered_get_issued_forecast_unknown_day_is_not_an_error():
    class _IssuedStore:
        def get_issued(self, iso):
            return None

        def issued_dates(self):
            return []

    class _Coord:
        _store = _IssuedStore()

    hass = _wired({"e1": _Coord()})
    resp = await hass.services.handlers["get_issued_forecast"](
        _Call({"date": "2026-01-15"})
    )
    # A miss draws no forecast line on the card — and names the archive start.
    assert resp["result"] == {
        "date": "2026-01-15",
        "available": False,
        "oldest_available": None,
    }


# --------------------------------------------------------------------------
# _resolve_single_coordinator / payload-loading error branches.
# --------------------------------------------------------------------------


async def test_resolve_single_coordinator_without_any_entry():
    hass = _FakeHass({})
    with pytest.raises(ServiceValidationError, match="No Balcony Solar"):
        svc._resolve_single_coordinator(hass, None)


async def test_load_bootstrap_data_unreadable_file(tmp_path):
    """An allowed path that cannot be READ (gone between check and open) is a
    user error, not an OSError traceback."""
    hass = _FakeHass({}, allowed=True)
    with pytest.raises(ServiceValidationError, match="Could not read"):
        await svc._load_bootstrap_data(
            hass, None, str(tmp_path / "missing.json")
        )


async def test_load_bootstrap_data_rejects_non_json_object_payload():
    hass = _FakeHass({})
    with pytest.raises(ServiceValidationError, match="JSON object"):
        await svc._load_bootstrap_data(hass, 123, None)


# --------------------------------------------------------------------------
# rollback_learners handler: forwarding + guards.
# --------------------------------------------------------------------------


class _RollbackCoordinator:
    def __init__(self, *, result=None, raises=None) -> None:
        self._result = result
        self._raises = raises
        self.requested_back: list[int] = []

    async def async_rollback_learners(self, snapshots_back: int):
        self.requested_back.append(snapshots_back)
        if self._raises is not None:
            raise self._raises
        return self._result


async def test_rollback_learners_forwards_snapshots_back():
    coord = _RollbackCoordinator(result={"bias_cells": 3})
    hass = _FakeHass({"e1": coord})
    resp = await svc._handle_rollback_learners(
        hass, _Call({"snapshots_back": 2})
    )
    assert coord.requested_back == [2]
    assert resp == {"result": {"bias_cells": 3}}


async def test_rollback_learners_defaults_to_one_snapshot_back():
    coord = _RollbackCoordinator(result=None)  # legacy: no summary dict
    hass = _FakeHass({"e1": coord})
    resp = await svc._handle_rollback_learners(hass, _Call({}))
    assert coord.requested_back == [1]
    assert resp == {"result": {}}


async def test_rollback_learners_unsupported_coordinator():
    hass = _FakeHass({"e1": object()})
    with pytest.raises(ServiceValidationError, match="does not support"):
        await svc._handle_rollback_learners(hass, _Call({}))


async def test_rollback_learners_empty_ring_is_a_user_error():
    coord = _RollbackCoordinator(raises=ValueError("no snapshots"))
    hass = _FakeHass({"e1": coord})
    with pytest.raises(ServiceValidationError, match="Rollback rejected"):
        await svc._handle_rollback_learners(hass, _Call({}))


# --------------------------------------------------------------------------
# Small duck-typing helpers.
# --------------------------------------------------------------------------


def test_measured_entities_without_site_is_empty():
    assert svc._measured_entities(object()) == []


def test_tau_n_of_unconvertible_values():
    class _BadAttr:
        tau = "not-a-float"
        n = 3

    assert svc._tau_n_of(_BadAttr()) == (None, 0)
    assert svc._tau_n_of({"tau": "not-a-float", "n": 3}) == (None, 0)
    assert svc._tau_n_of({"tau": None}) == (None, 0)
    assert svc._tau_n_of(0.5) == (None, 0)
