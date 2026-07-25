"""Tests for the in-process ``run_bootstrap`` action (SPEC §6).

The handler is driven against fake hass/coordinator doubles with the network
(Open-Meteo) and recorder reads monkeypatched out, so the tests exercise the
service contract — dry_run default + summary, the import path on
``dry_run: false``, the concurrency lock, the range defaults, the seconds-epoch
recorder reduce and every ServiceValidationError picture — WITHOUT a real
provider, recorder or a multi-minute reconstruction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

import voluptuous as vol  # noqa: E402
from homeassistant.core import SupportsResponse  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402

# Import via the REAL HA-importing package (``custom_components.…``) so the
# coordinator/_actuals chain — which does ``from .core import SiteConfig`` —
# resolves against the real ``core/__init__`` (the synthetic test shim only
# shadows the bare ``balcony_solar_forecast`` name, with an empty core).
from custom_components.balcony_solar_forecast import _bootstrap as bs  # noqa: E402
from custom_components.balcony_solar_forecast import _services as svc  # noqa: E402
from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    BOOTSTRAP_DEFAULT_MAX_DAYS,
    BOOTSTRAP_KEY_SHADEMAP,
    DEFAULT_SITE,
    DOMAIN,
    SERVICE_RUN_BOOTSTRAP,
)
from custom_components.balcony_solar_forecast.core import SiteConfig  # noqa: E402
from custom_components.balcony_solar_forecast.core.bootstrap_build import (  # noqa: E402
    BootstrapAccumulator,
)
from custom_components.balcony_solar_forecast.core.types import BiasCell  # noqa: E402

# --------------------------------------------------------------------------
# Doubles.
# --------------------------------------------------------------------------


class _FakeConfig:
    time_zone = "Europe/Berlin"


class _FakeHass:
    def __init__(self, store):
        self.data = {DOMAIN: store}
        self.config = _FakeConfig()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _FakeCoordinator:
    def __init__(self, site=None):
        self._site = site if site is not None else SiteConfig.from_dict(DEFAULT_SITE)
        self.imported = None

    async def async_import_bootstrap(self, data):
        self.imported = data
        return {"bias_cells": len(data.get("bias_state", {}).get("cells", {}))}


class _Call:
    def __init__(self, data):
        self.data = data


def _canned_acc() -> BootstrapAccumulator:
    acc = BootstrapAccumulator()
    acc.days_used = 5
    acc.days_skipped = 2
    acc.shade = {"M2": {"20:4:0": [0.55, 3]}}
    acc.bias = {"clear|midday": BiasCell(theta=1.1, covariance=0.5, n=4)}
    acc.shade_samples = 9
    acc.quantile_samples = 12
    acc.last_iso_date = "2026-07-01"
    return acc


def _patch_pipeline(monkeypatch, *, weather=("wx",), actuals=None, acc=None):
    """Stub the network + recorder + heavy reconstruction seams."""
    if actuals is None:
        actuals = {"M2": {"2026-06-01T10:00:00+00:00": 100.0}}
    if acc is None:
        acc = _canned_acc()

    async def _fake_weather(hass, site, start, end):
        return list(weather), True

    async def _fake_actuals(hass, site, start, end):
        return dict(actuals)

    def _fake_build(site, w, ha, svf, tz):
        return acc

    monkeypatch.setattr(bs, "_fetch_weather", _fake_weather)
    monkeypatch.setattr(bs, "_read_hourly_actuals", _fake_actuals)
    monkeypatch.setattr(bs, "_build_accumulator", _fake_build)
    return acc


# --------------------------------------------------------------------------
# Registration + schema.
# --------------------------------------------------------------------------


def test_run_bootstrap_registered_response_only():
    registered = {}

    class _Services:
        def has_service(self, domain, service):
            return False

        def async_register(self, domain, service, handler, *, schema, supports_response):
            registered[service] = supports_response

    class _Hass:
        def __init__(self):
            self.services = _Services()

    svc.async_register_services(_Hass())
    assert registered[SERVICE_RUN_BOOTSTRAP] is SupportsResponse.ONLY


def test_schema_dry_run_defaults_true_and_rejects_unknown():
    assert svc.RUN_BOOTSTRAP_SCHEMA({}) == {"dry_run": True}
    got = svc.RUN_BOOTSTRAP_SCHEMA({"dry_run": False, "start_date": "2025-01-01"})
    assert got["dry_run"] is False
    assert got["start_date"] == "2025-01-01"
    with pytest.raises(vol.Invalid):
        svc.RUN_BOOTSTRAP_SCHEMA({"bogus": 1})


# --------------------------------------------------------------------------
# dry_run behaviour + import path.
# --------------------------------------------------------------------------


async def test_dry_run_true_returns_summary_without_importing(monkeypatch):
    _patch_pipeline(monkeypatch)
    coord = _FakeCoordinator()
    hass = _FakeHass({"e1": coord})
    resp = await bs.async_run_bootstrap(hass, _Call({}))  # dry_run defaults True

    assert coord.imported is None  # store untouched
    assert resp["imported"] is False
    assert resp["days_used"] == 5
    assert resp["days_skipped"] == 2
    assert resp["bias_cells"] == 1
    assert resp["shademap_channels"] == 1
    assert resp["shademap_bins"] == 1
    assert resp["quantile_samples"] == 12
    assert "hint" in resp
    assert "duration_s" in resp
    assert set(resp["date_range"]) == {"start", "end"}


async def test_dry_run_false_imports_built_payload(monkeypatch):
    _patch_pipeline(monkeypatch)
    coord = _FakeCoordinator()
    hass = _FakeHass({"e1": coord})
    resp = await bs.async_run_bootstrap(hass, _Call({"dry_run": False}))

    assert resp["imported"] is True
    # The EXACT rebuilt payload reached the coordinator's import path.
    assert coord.imported is not None
    channels = coord.imported[BOOTSTRAP_KEY_SHADEMAP]["channels"]
    assert "M2" in channels and "20:4:0" in channels["M2"]


# --------------------------------------------------------------------------
# Concurrency lock.
# --------------------------------------------------------------------------


async def test_second_run_while_locked_is_rejected(monkeypatch):
    _patch_pipeline(monkeypatch)
    coord = _FakeCoordinator()
    hass = _FakeHass({"e1": coord})
    lock = bs._bootstrap_lock(coord)
    await lock.acquire()
    try:
        with pytest.raises(ServiceValidationError, match="already running"):
            await bs.async_run_bootstrap(hass, _Call({}))
    finally:
        lock.release()
    # coordinator untouched by the rejected call
    assert coord.imported is None


# --------------------------------------------------------------------------
# Error pictures.
# --------------------------------------------------------------------------


async def test_no_weather_raises(monkeypatch):
    _patch_pipeline(monkeypatch, weather=())
    hass = _FakeHass({"e1": _FakeCoordinator()})
    with pytest.raises(ServiceValidationError, match="no usable weather"):
        await bs.async_run_bootstrap(hass, _Call({}))


async def test_no_actuals_raises(monkeypatch):
    _patch_pipeline(monkeypatch, actuals={})
    hass = _FakeHass({"e1": _FakeCoordinator()})
    with pytest.raises(ServiceValidationError, match="No measured module energy"):
        await bs.async_run_bootstrap(hass, _Call({}))


async def test_no_usable_days_raises(monkeypatch):
    empty = BootstrapAccumulator()  # days_used == 0
    _patch_pipeline(monkeypatch, acc=empty)
    hass = _FakeHass({"e1": _FakeCoordinator()})
    with pytest.raises(ServiceValidationError, match="No usable days"):
        await bs.async_run_bootstrap(hass, _Call({}))


async def test_inverted_range_raises(monkeypatch):
    _patch_pipeline(monkeypatch)
    hass = _FakeHass({"e1": _FakeCoordinator()})
    with pytest.raises(ServiceValidationError, match="before start_date"):
        await bs.async_run_bootstrap(
            hass, _Call({"start_date": "2026-07-10", "end_date": "2026-07-01"})
        )


async def test_bad_date_raises(monkeypatch):
    _patch_pipeline(monkeypatch)
    hass = _FakeHass({"e1": _FakeCoordinator()})
    with pytest.raises(ServiceValidationError, match="Invalid start_date"):
        await bs.async_run_bootstrap(hass, _Call({"start_date": "not-a-date"}))


async def test_no_actual_entity_raises(monkeypatch):
    _patch_pipeline(monkeypatch)
    # A site whose planes carry no actual_entity has no measured history.
    site_dict = {**DEFAULT_SITE}
    site_dict["planes"] = [
        {**p, "actual_entity": None} for p in DEFAULT_SITE["planes"]
    ]
    coord = _FakeCoordinator(site=SiteConfig.from_dict(site_dict))
    hass = _FakeHass({"e1": coord})
    with pytest.raises(ServiceValidationError, match="no measured history"):
        await bs.async_run_bootstrap(hass, _Call({}))


# --------------------------------------------------------------------------
# Range defaults.
# --------------------------------------------------------------------------


def test_range_defaults_end_yesterday_start_capped():
    start, end = bs._resolve_range(None, None)
    today = dt_util.now().date()
    assert end == today - timedelta(days=1)
    assert start == today - timedelta(days=BOOTSTRAP_DEFAULT_MAX_DAYS)


def test_range_explicit_dates_parsed():
    start, end = bs._resolve_range("2025-03-01", "2025-06-30")
    assert start.isoformat() == "2025-03-01"
    assert end.isoformat() == "2025-06-30"


def test_date_windows_chunks_inclusive():
    from datetime import date

    wins = bs._date_windows(date(2025, 1, 1), date(2025, 4, 11), 90)
    assert wins[0] == (date(2025, 1, 1), date(2025, 3, 31))
    assert wins[-1][1] == date(2025, 4, 11)
    # windows are contiguous and non-overlapping
    for (_a_start, a_end), (b_start, _b_end) in zip(wins, wins[1:], strict=False):
        assert b_start == a_end + timedelta(days=1)


# --------------------------------------------------------------------------
# Seconds-epoch recorder reduce (the historical bug's regression guard).
# --------------------------------------------------------------------------


def test_reduce_stats_seconds_epoch():
    # In-process recorder returns ``start`` as epoch SECONDS floats.
    h10 = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    h11 = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
    stats = {
        "sensor.p2": [
            {"start": h10.timestamp(), "mean": 120.0},
            {"start": h10.timestamp(), "mean": 30.0},   # same hour -> summed
            {"start": h11.timestamp(), "mean": 80.0},
            {"start": h11.timestamp(), "mean": None},   # skipped
        ],
        "sensor.absent": [],
    }
    out = bs._reduce_stats(stats, {"M2": "sensor.p2", "M9": "sensor.absent"})
    assert "M9" not in out  # no rows -> omitted
    assert out["M2"][h10.isoformat()] == pytest.approx(150.0)
    assert out["M2"][h11.isoformat()] == pytest.approx(80.0)


def test_reduce_stats_milliseconds_epoch_also_resolves():
    # Defense in depth: a ms-epoch row (WS wire format) still lands on the hour.
    h = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    stats = {"sensor.p2": [{"start": h.timestamp() * 1000.0, "mean": 50.0}]}
    out = bs._reduce_stats(stats, {"M2": "sensor.p2"})
    assert out["M2"][h.isoformat()] == pytest.approx(50.0)
