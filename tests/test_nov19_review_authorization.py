"""Check module-level authorization and degraded diagnostic output."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components import recorder
from homeassistant.helpers import entity_registry
from homeassistant.util import dt as dt_util

from custom_components.balcony_solar_forecast import _services
from custom_components.balcony_solar_forecast.const import DOMAIN
from custom_components.balcony_solar_forecast.core.types import (
    BiasCell,
    BiasState,
    PlaneConfig,
)
from custom_components.balcony_solar_forecast.sensor import MeasuredDcTotalSensor
from tests.helpers.weather import NOW, _coord, _om_payload


def test_available_total_exposes_no_module_bindings():
    coord = _coord()
    sensor = MeasuredDcTotalSensor(coord, ["sensor.hidden"], source_wps=[400.0])
    sensor._value = 200.0
    sensor._reporting = 1
    sensor._rejected_sources = ["sensor.other_private"]
    assert sensor.available
    assert sensor.native_value == 200.0
    assert sensor.extra_state_attributes == {
        "channels_total": 1, "channels_reporting": 1, "channels_rejected": 1,
    }


@pytest.mark.parametrize("revoke", [None, "sensor.readable", "sensor.total"])
async def test_history_checks_each_source_and_rechecks_permissions(monkeypatch, revoke):
    coord = SimpleNamespace(_site=SimpleNamespace(planes=(
        PlaneConfig("Readable", 180.0, 0.0, 400.0, actual_entity="sensor.readable"),
        PlaneConfig("Hidden", 180.0, 0.0, 500.0, actual_entity="sensor.hidden"),
    )))
    hass = SimpleNamespace(
        data={DOMAIN: {"site": coord}},
        services=SimpleNamespace(has_service=lambda *_: False, async_register=lambda *a, **kw: None),
    )
    row = SimpleNamespace(platform=DOMAIN, config_entry_id="site", unique_id="site_measured_dc_power_total", disabled_by=None)
    monkeypatch.setattr(entity_registry, "async_get", lambda _hass: SimpleNamespace(async_get=lambda _entity: row))
    grants = {"sensor.total", "sensor.readable"}
    queries, replies, errors = [], [], []

    async def executor(_read, _hass, start, end, statistic_ids, period, units, fields):
        queries.append((statistic_ids, period, units, fields))
        if revoke:
            grants.remove(revoke)
        row = {"start": NOW.timestamp(), "mean": 100.0}
        return {"sensor.readable": [row], "sensor.hidden": [row]}

    monkeypatch.setattr(recorder, "get_instance", lambda _hass: SimpleNamespace(async_add_executor_job=executor))
    connection = SimpleNamespace(
        user=SimpleNamespace(permissions=SimpleNamespace(check_entity=lambda entity, _policy: entity in grants)),
        send_result=lambda *args: replies.append(args), send_error=lambda *args: errors.append(args),
    )
    _services.async_register_services(hass)
    endpoint = hass.data["websocket_api"].get(f"{DOMAIN}/measurement_history")
    assert endpoint is not None, "Recorder history must be read through the per-source checked endpoint"
    handle, schema = endpoint
    msg = schema({
        "id": 1, "type": f"{DOMAIN}/measurement_history", "entity_id": "sensor.total", "entry_id": "site",
        "start_time": NOW.isoformat(), "end_time": (NOW + timedelta(hours=1)).isoformat(),
    })
    await handle.__wrapped__(hass, connection, msg)
    assert queries == [({"sensor.readable"}, "hour", {"power": "W"}, {"mean"})]
    if revoke == "sensor.total":
        assert errors and errors[0][:2] == (1, "unauthorized")
        assert not replies
    elif revoke == "sensor.readable":
        assert not errors
        assert replies == [(1, {"sources": [], "source_names": [], "source_limits_w": [], "statistics": {}})]
    else:
        assert not errors
        assert replies == [(1, {
            "sources": ["sensor.readable"], "source_names": ["Readable"], "source_limits_w": [500.0],
            "statistics": {"sensor.readable": [{"start": int(NOW.timestamp() * 1000), "mean": 100.0}]},
        })]


async def test_fallback_reports_only_active_corrections_from_its_generation(monkeypatch):
    coord = _coord()
    coord._weather.restore({
        "payload": _om_payload(start_iso="2026-07-05T12:15"),
        "fetched_at": (NOW - timedelta(hours=30)).isoformat(),
    })
    coord._last_attempt_at = NOW
    coord._intraday_scalar = 1.3
    coord._bias_state = BiasState(cells={"clear|midday": BiasCell(theta=1.3, covariance=1.0, n=99)})
    monkeypatch.setattr(dt_util, "utcnow", lambda: NOW)
    data = await coord._async_update_data()
    assert data["status"] == "physics_fallback"
    assert data["correction_source"] == "none"
    assert data["intraday_scalar"] == 1.0
    status = data["learner_status"]
    assert not status["fast_active"] and not status["slow_active"]
    assert status["fast"] == "frozen"
    assert status["day_ahead"] == "frozen"
    assert data["bias_cells"]["clear|midday"] == {
        "cloud_class": "clear", "day_part": "midday", "theta": 1.3, "n": 99, "applied": 1.0, "clamped": False,
    }
    assert coord._intraday_scalar == 1.3
