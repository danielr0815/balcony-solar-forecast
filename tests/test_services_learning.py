"""Tests for the learner services (import_bootstrap / dump_shademap, SPEC §5/§6).

The pure polar-table builder is exercised directly (bin-centre math, malformed
input tolerance, deterministic ordering). The two service handlers are driven
against fake hass/coordinator doubles: payload/path resolution + single-entry
targeting for import_bootstrap, and the per-entry polar dump for dump_shademap.

Needs Home Assistant (the service handlers import ServiceValidationError etc.);
skipped on the plain-core path.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

from balcony_solar_forecast import _services as svc  # noqa: E402
from balcony_solar_forecast.const import (  # noqa: E402
    DOMAIN,
    SHADEMAP_AZ_BIN_DEG,
    SHADEMAP_EL_BIN_DEG,
)
from homeassistant.exceptions import ServiceValidationError  # noqa: E402

# --------------------------------------------------------------------------
# Pure polar-table builder.
# --------------------------------------------------------------------------


def test_build_polar_table_bin_centres_and_sorting():
    state = {
        "version": 1,
        "channels": {
            "M4": {
                # az_idx 41 -> centre (41+0.5)*5 = 207.5 ; el_idx 16 -> 41.25
                "41:16:1": {"tau": 0.42, "n": 7},
                # az_idx 20 -> 102.5 ; el_idx 4 -> 11.25 ; half 0
                "20:4:0": {"tau": 1.05, "n": 3},
            }
        },
    }
    table = svc.build_polar_table(state)
    rows = table["channels"]["M4"]["bins"]
    assert len(rows) == 2
    # Sorted by (half, sun_az, sun_el): half 0 row first.
    assert rows[0]["half"] == 0
    assert rows[0]["sun_az"] == pytest.approx(20.5 * SHADEMAP_AZ_BIN_DEG)
    assert rows[0]["sun_el"] == pytest.approx(4.5 * SHADEMAP_EL_BIN_DEG)
    assert rows[0]["tau"] == pytest.approx(1.05)
    assert rows[0]["n"] == 3
    assert rows[1]["half"] == 1
    assert rows[1]["sun_az"] == pytest.approx(41.5 * SHADEMAP_AZ_BIN_DEG)


def test_build_polar_table_skips_malformed():
    state = {
        "channels": {
            "M1": {
                "bad-key": {"tau": 0.5, "n": 1},       # not 3 parts
                "1:2:9": {"tau": 0.5, "n": 1},         # half out of range
                "1:x:0": {"tau": 0.5, "n": 1},         # non-int index
                "3:4:0": {"n": 1},                      # no tau
                "5:6:1": {"tau": 0.7, "n": 2},         # the only good row
            },
            "not-a-dict": "junk",
        }
    }
    table = svc.build_polar_table(state)
    rows = table["channels"]["M1"]["bins"]
    assert len(rows) == 1
    assert rows[0]["tau"] == pytest.approx(0.7)
    assert "not-a-dict" not in table["channels"]


def test_build_polar_table_accepts_shademap_state_object():
    from balcony_solar_forecast.core.types import ShademapBin, ShademapState

    state = ShademapState(
        channels={"M8": {"10:5:1": ShademapBin(tau=0.3, n=12)}}
    )
    table = svc.build_polar_table(state)
    rows = table["channels"]["M8"]["bins"]
    assert rows[0]["tau"] == pytest.approx(0.3)
    assert rows[0]["n"] == 12


def test_build_polar_table_empty_and_garbage():
    assert svc.build_polar_table({}) == {"channels": {}}
    assert svc.build_polar_table(None) == {"channels": {}}
    assert svc.build_polar_table("nope") == {"channels": {}}


def test_parse_bin_key_and_tau_n():
    assert svc._parse_bin_key("41:16:1") == (41, 16, 1)
    assert svc._parse_bin_key("41:16:2") is None
    assert svc._parse_bin_key("a:b:c") is None
    assert svc._parse_bin_key(123) is None
    tau, n = svc._tau_n_of({"tau": 0.5, "n": 4})
    assert tau == pytest.approx(0.5)
    assert n == 4
    assert svc._tau_n_of({"n": 4}) == (None, 0)


# --------------------------------------------------------------------------
# Fakes for the service handlers.
# --------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self, allowed=True):
        self._allowed = allowed

    def is_allowed_path(self, path):
        return self._allowed


class _FakeHass:
    def __init__(self, store, *, allowed=True):
        self.data = {DOMAIN: store}
        self.config = _FakeConfig(allowed)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _FakeCoordinator:
    def __init__(self, *, shademap=None, import_result=None):
        self._shademap = shademap
        self._import_result = import_result
        self.imported = None

    async def async_import_bootstrap(self, data):
        self.imported = data
        return self._import_result if self._import_result is not None else {}

    def get_shademap_state(self):
        return self._shademap


class _LegacyCoordinator:
    """A coordinator predating the learner build (no import method)."""


class _Call:
    def __init__(self, data):
        self.data = data


# --------------------------------------------------------------------------
# import_bootstrap: payload/path resolution and single-entry targeting.
# --------------------------------------------------------------------------


async def test_import_bootstrap_inline_dict_forwarded():
    coord = _FakeCoordinator(import_result={"bias_cells": 4})
    hass = _FakeHass({"e1": coord})
    payload = {"schema_version": 1, "bias_state": {}, "shademap_state": {}}
    resp = await svc._handle_import_bootstrap(hass, _Call({"payload": payload}))
    assert coord.imported == payload
    assert resp == {"result": {"bias_cells": 4}}


async def test_import_bootstrap_json_string_parsed():
    coord = _FakeCoordinator()
    hass = _FakeHass({"e1": coord})
    payload = {"schema_version": 1}
    resp = await svc._handle_import_bootstrap(
        hass, _Call({"payload": json.dumps(payload)})
    )
    assert coord.imported == payload
    assert resp == {"result": {}}


async def test_import_bootstrap_from_path(tmp_path):
    coord = _FakeCoordinator()
    hass = _FakeHass({"e1": coord})
    f = tmp_path / "boot.json"
    payload = {"schema_version": 1, "shademap_state": {"channels": {}}}
    f.write_text(json.dumps(payload), encoding="utf-8")
    await svc._handle_import_bootstrap(hass, _Call({"path": str(f)}))
    assert coord.imported == payload


async def test_import_bootstrap_rejects_disallowed_path(tmp_path):
    coord = _FakeCoordinator()
    hass = _FakeHass({"e1": coord}, allowed=False)
    f = tmp_path / "boot.json"
    f.write_text("{}", encoding="utf-8")
    with pytest.raises(ServiceValidationError):
        await svc._handle_import_bootstrap(hass, _Call({"path": str(f)}))


async def test_import_bootstrap_requires_exactly_one_source():
    coord = _FakeCoordinator()
    hass = _FakeHass({"e1": coord})
    # Neither.
    with pytest.raises(ServiceValidationError):
        await svc._handle_import_bootstrap(hass, _Call({}))
    # Both.
    with pytest.raises(ServiceValidationError):
        await svc._handle_import_bootstrap(
            hass, _Call({"payload": {}, "path": "/x"})
        )


async def test_import_bootstrap_bad_json_string():
    coord = _FakeCoordinator()
    hass = _FakeHass({"e1": coord})
    with pytest.raises(ServiceValidationError):
        await svc._handle_import_bootstrap(
            hass, _Call({"payload": "{not json"})
        )


async def test_import_bootstrap_json_not_object():
    coord = _FakeCoordinator()
    hass = _FakeHass({"e1": coord})
    with pytest.raises(ServiceValidationError):
        await svc._handle_import_bootstrap(hass, _Call({"payload": "[1,2,3]"}))


async def test_import_bootstrap_multiple_entries_needs_id():
    coord1 = _FakeCoordinator()
    coord2 = _FakeCoordinator()
    hass = _FakeHass({"e1": coord1, "e2": coord2})
    with pytest.raises(ServiceValidationError):
        await svc._handle_import_bootstrap(hass, _Call({"payload": {}}))
    # With an explicit id it resolves.
    await svc._handle_import_bootstrap(
        hass, _Call({"payload": {"schema_version": 1}, "entry_id": "e2"})
    )
    assert coord2.imported == {"schema_version": 1}
    assert coord1.imported is None


async def test_import_bootstrap_unknown_entry():
    hass = _FakeHass({"e1": _FakeCoordinator()})
    with pytest.raises(ServiceValidationError):
        await svc._handle_import_bootstrap(
            hass, _Call({"payload": {}, "entry_id": "nope"})
        )


async def test_import_bootstrap_unsupported_coordinator():
    hass = _FakeHass({"e1": _LegacyCoordinator()})
    with pytest.raises(ServiceValidationError):
        await svc._handle_import_bootstrap(
            hass, _Call({"payload": {"schema_version": 1}})
        )


# --------------------------------------------------------------------------
# dump_shademap handler.
# --------------------------------------------------------------------------


def test_dump_shademap_all_entries():
    state = {"channels": {"M4": {"41:16:1": {"tau": 0.42, "n": 7}}}}
    coord = _FakeCoordinator(shademap=state)
    hass = _FakeHass({"e1": coord})
    resp = svc._handle_dump_shademap(hass, _Call({}))
    assert set(resp) == {"entries"}
    rows = resp["entries"]["e1"]["channels"]["M4"]["bins"]
    assert rows[0]["tau"] == pytest.approx(0.42)


def test_dump_shademap_entry_filter():
    coordA = _FakeCoordinator(shademap={"channels": {}})
    coordB = _FakeCoordinator(shademap={"channels": {}})
    hass = _FakeHass({"A": coordA, "B": coordB})
    resp = svc._handle_dump_shademap(hass, _Call({"entry_id": "B"}))
    assert list(resp["entries"]) == ["B"]


def test_dump_shademap_unsupported_coordinator():
    class _Bare:
        pass

    hass = _FakeHass({"e1": _Bare()})
    resp = svc._handle_dump_shademap(hass, _Call({}))
    assert resp["entries"]["e1"] == {"channels": {}, "available": False}


def test_dump_shademap_getter_raises_is_caught():
    class _Boom:
        def get_shademap_state(self):
            raise RuntimeError("kaboom")

    hass = _FakeHass({"e1": _Boom()})
    resp = svc._handle_dump_shademap(hass, _Call({}))
    assert "error" in resp["entries"]["e1"]


# --------------------------------------------------------------------------
# FIX-4: real-coordinator integration (import_bootstrap / dump_shademap must
# work against the ACTUAL BalconySolarCoordinator, not just the fake).
# --------------------------------------------------------------------------


class _FakeHAStore:
    """Minimal HA Store stand-in (pattern from tests/test_store_v2.py)."""

    def __init__(self, initial=None):
        self._initial = initial
        self.saved = None

    async def async_load(self):
        return self._initial

    def async_delay_save(self, data_func, delay):
        self.saved = data_func()

    async def async_save(self, data):
        self.saved = data


def _real_coordinator():
    """A real BalconySolarCoordinator over a real ForecastStore (fake HA Store).

    Built via __new__ with only the attributes the import/dump path touches.
    """
    from collections import deque

    from custom_components.balcony_solar_forecast.coordinator import (
        BalconySolarCoordinator,
    )
    from custom_components.balcony_solar_forecast.core.types import (
        BiasState,
        DriftState,
        LearnerConfig,
        PlaneConfig,
        ShademapState,
        SiteConfig,
    )
    from custom_components.balcony_solar_forecast.store import ForecastStore

    store = ForecastStore(None, "e1", store=_FakeHAStore())  # type: ignore[arg-type]

    c = BalconySolarCoordinator.__new__(BalconySolarCoordinator)
    c._store = store
    c._site = SiteConfig(
        latitude=48.5, longitude=12.2,
        planes=(PlaneConfig(name="M1", azimuth_deg=115.0, tilt_deg=70.0,
                            wp=370.0, actual_entity="sensor.m1"),),
        groups=(),
    )
    c._bias_state = BiasState()
    c._shademap_state = ShademapState()
    c._drift_state = DriftState()
    c._learner_config = LearnerConfig()
    c._learner_states_loaded = True
    c._intraday_samples = deque()

    # async_request_refresh is a no-op for the import path here.
    async def _noop_refresh():
        return None

    c.async_request_refresh = _noop_refresh  # type: ignore[method-assign]
    return c, store


def _valid_bootstrap(sig):
    from balcony_solar_forecast.const import (
        BOOTSTRAP_KEY_BIAS,
        BOOTSTRAP_KEY_SCHEMA,
        BOOTSTRAP_KEY_SHADEMAP,
        BOOTSTRAP_KEY_SITE_SIGNATURE,
        BOOTSTRAP_SCHEMA_VERSION,
    )

    return {
        BOOTSTRAP_KEY_SCHEMA: BOOTSTRAP_SCHEMA_VERSION,
        BOOTSTRAP_KEY_SITE_SIGNATURE: sig,
        BOOTSTRAP_KEY_SHADEMAP: {
            "version": 1,
            "channels": {"M1": {"10:15:1": {"tau": 0.3, "n": 999}}},
        },
        BOOTSTRAP_KEY_BIAS: {
            "version": 1,
            "cells": {"clear|midday": {"theta": 0.9, "covariance": 1.0, "n": 5}},
        },
    }


async def test_import_bootstrap_end_to_end():
    from balcony_solar_forecast.const import BOOTSTRAP_MAX_BIN_N

    coord, store = _real_coordinator()
    payload = _valid_bootstrap(coord._site_signature())
    call = _Call({"payload": payload})
    fake_hass = _FakeHass({"e1": coord})
    resp = await svc._handle_import_bootstrap(fake_hass, call)

    assert resp["result"]["shademap_bins"] == 1
    # Cap applied on the backfilled bin.
    bin_ = store.get_shademap_state().channels["M1"]["10:15:1"]
    assert bin_.n == BOOTSTRAP_MAX_BIN_N
    # In-memory state re-synced from the store (stale-memory regression).
    assert coord._shademap_state.channels["M1"]["10:15:1"].n == BOOTSTRAP_MAX_BIN_N
    # A rollback snapshot was pushed.
    assert len(store.get_snapshots()) == 1


async def test_import_bootstrap_schema_mismatch_is_validation_error():
    coord, store = _real_coordinator()
    payload = _valid_bootstrap(coord._site_signature())
    payload["schema_version"] = 999
    fake_hass = _FakeHass({"e1": coord})
    with pytest.raises(ServiceValidationError):
        await svc._handle_import_bootstrap(fake_hass, _Call({"payload": payload}))
    # Store + memory unchanged.
    assert store.get_shademap_state().channels == {}
    assert coord._shademap_state.channels == {}


async def test_import_bootstrap_wrong_site_is_validation_error():
    coord, store = _real_coordinator()
    payload = _valid_bootstrap("deadbeefdeadbeef")  # wrong signature
    fake_hass = _FakeHass({"e1": coord})
    with pytest.raises(ServiceValidationError):
        await svc._handle_import_bootstrap(fake_hass, _Call({"payload": payload}))
    assert coord._shademap_state.channels == {}


async def test_dump_shademap_returns_imported_bins():
    coord, _store = _real_coordinator()
    payload = _valid_bootstrap(coord._site_signature())
    fake_hass = _FakeHass({"e1": coord})
    await svc._handle_import_bootstrap(fake_hass, _Call({"payload": payload}))
    resp = svc._handle_dump_shademap(fake_hass, _Call({}))
    bins = resp["entries"]["e1"]["channels"]["M1"]["bins"]
    assert bins and bins[0]["tau"] == pytest.approx(0.3)


# --------------------------------------------------------------------------
# suggest_shade_groups handler.
# --------------------------------------------------------------------------


def _shade_site(names, *, shade_groups=None):
    from balcony_solar_forecast.core.types import PlaneConfig, SiteConfig

    shade_groups = shade_groups or {}
    planes = tuple(
        PlaneConfig(
            name=n, azimuth_deg=115.0, tilt_deg=70.0, wp=370.0,
            shade_group=shade_groups.get(n),
        )
        for n in names
    )
    return SiteConfig(latitude=48.5, longitude=12.2, planes=planes, groups=())


class _ShadeCoordinator:
    """Fake coordinator exposing a site + a live shademap state."""

    def __init__(self, site, state):
        self._site = site
        self._state = state

    def get_shademap_state(self):
        return self._state


def _shade_state(channels):
    """{channel: {bin_key: (tau, n)}} -> ShademapState."""
    from balcony_solar_forecast.core.types import ShademapBin, ShademapState

    return ShademapState(channels={
        ch: {k: ShademapBin(tau=t, n=n) for k, (t, n) in bins.items()}
        for ch, bins in channels.items()
    })


def _uniform(tau, *, n=10, count=30):
    return {f"{i}:0:0": (tau, n) for i in range(count)}


def test_suggest_shade_groups_response_shape_and_defaults():
    from balcony_solar_forecast.const import (
        SHADE_SIM_MAX_MEAN_DIFF,
        SHADE_SIM_MIN_COMMON_BINS,
    )

    # A == B over 30 bins (share shade); C deviates by 0.4 -> different.
    state = _shade_state({
        "A": _uniform(0.3), "B": _uniform(0.3), "C": _uniform(0.7),
    })
    coord = _ShadeCoordinator(_shade_site(["A", "B", "C"]), state)
    hass = _FakeHass({"e1": coord})
    resp = svc._handle_suggest_shade_groups(hass, _Call({}))
    result = resp["result"]
    # thresholds echo the const defaults when the fields are omitted.
    assert result["thresholds"] == {
        "max_diff": SHADE_SIM_MAX_MEAN_DIFF,
        "min_common_bins": SHADE_SIM_MIN_COMMON_BINS,
    }
    # current_groups reflects each plane's shade_channel (its own name here).
    assert result["current_groups"] == {"A": "A", "B": "B", "C": "C"}
    # The suggestion groups A with B and leaves C out.
    plane_sets = sorted(tuple(g["planes"]) for g in result["groups"])
    assert plane_sets == [("A", "B"), ("C",)]
    ab = next(g for g in result["groups"] if g["planes"] == ["A", "B"])
    assert ab["suggested_group"] == "A"
    # pairs carry the full similarity record.
    assert {"a", "b", "common_bins", "mean_abs_diff", "max_abs_diff", "verdict"} <= set(
        result["pairs"][0]
    )


def test_suggest_shade_groups_custom_thresholds_honoured():
    # A vs B differ by 0.1 over only 4 bins: below the default bar on both axes,
    # but a loosened max_diff + lowered min_common_bins should merge them.
    state = _shade_state({
        "A": _uniform(0.3, count=4), "B": _uniform(0.4, count=4),
    })
    coord = _ShadeCoordinator(_shade_site(["A", "B"]), state)
    hass = _FakeHass({"e1": coord})
    resp = svc._handle_suggest_shade_groups(
        hass, _Call({"max_diff": 0.15, "min_common_bins": 4})
    )
    result = resp["result"]
    assert result["thresholds"] == {"max_diff": 0.15, "min_common_bins": 4}
    assert sorted(tuple(g["planes"]) for g in result["groups"]) == [("A", "B")]
    # Under the DEFAULT thresholds the same state does NOT group (diff 0.1 > 0.06
    # AND 4 bins < 30) — proving the custom values actually drove the merge.
    resp_default = svc._handle_suggest_shade_groups(hass, _Call({}))
    assert sorted(
        tuple(g["planes"]) for g in resp_default["result"]["groups"]
    ) == [("A",), ("B",)]


def test_suggest_shade_groups_current_groups_uses_shade_channel():
    state = _shade_state({})  # empty is fine; only current_groups is asserted
    site = _shade_site(["M1", "M2"], shade_groups={"M1": "south"})
    coord = _ShadeCoordinator(site, state)
    hass = _FakeHass({"e1": coord})
    resp = svc._handle_suggest_shade_groups(hass, _Call({}))
    assert resp["result"]["current_groups"] == {"M1": "south", "M2": "M2"}


def test_suggest_shade_groups_no_planes_raises():
    from balcony_solar_forecast.core.types import ShademapState

    coord = _ShadeCoordinator(_shade_site([]), ShademapState())
    hass = _FakeHass({"e1": coord})
    with pytest.raises(ServiceValidationError):
        svc._handle_suggest_shade_groups(hass, _Call({}))


def test_suggest_shade_groups_unsupported_coordinator_raises():
    class _Bare:
        _site = None

    hass = _FakeHass({"e1": _Bare()})
    with pytest.raises(ServiceValidationError):
        svc._handle_suggest_shade_groups(hass, _Call({}))
