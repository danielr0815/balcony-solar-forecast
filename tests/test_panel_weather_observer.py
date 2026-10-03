"""HA adapter uses only configured DC sources, preserves forecasts and unloads."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.balcony_solar_forecast import _panel_weather
from custom_components.balcony_solar_forecast.core.types import (
    InverterGroup,
    PlaneConfig,
    SiteConfig,
)


def observer():
    at = datetime(2026, 6, 21, 10, tzinfo=UTC)
    planes = tuple(PlaneConfig(str(i), 90 if i < 2 else 180, 30, 400,
                              actual_entity=f"sensor.p{i}") for i in range(3))
    site = SiteConfig(0, 0, planes, (InverterGroup("A", ("0", "1"), 800),
                                    InverterGroup("B", ("2",), 800)))
    states = {p.actual_entity: SimpleNamespace(state="100", last_reported=at,
                                               attributes={"unit_of_measurement": "W"}) for p in planes}
    result = SimpleNamespace(slot_starts=tuple(at + timedelta(minutes=i) for i in (-30, -15, 0, 15, 30)))
    coord = SimpleNamespace(hass=SimpleNamespace(states=SimpleNamespace(get=states.get)),
                            _site=site, _last_result=result, last_update_success=True,
                            _intraday_reference=lambda result, at: {str(i): 100 for i in range(3)},
                            _cached_weather=lambda: None)
    return _panel_weather.PanelWeatherObserver(coord), coord, states, at


def test_common_change_with_kw_source_is_observational():
    monitor, coord, states, at = observer()
    states["sensor.p0"].state = ".1"
    states["sensor.p0"].attributes["unit_of_measurement"] = "kW"
    monitor.sample(at)
    assert monitor.summary()["state"] == "warming_up"
    at += timedelta(minutes=5)
    for key, state in states.items():
        state.last_reported = at
        state.state = ".06" if key == "sensor.p0" else "60"
    original = coord._last_result
    monitor.sample(at)
    assert monitor.summary()["state"] == "common_change"
    assert monitor.summary()["mode"] == "observational"
    assert coord._last_result is original


def test_failed_forecast_and_stale_ports_are_unknown():
    monitor, coord, states, at = observer()
    coord.last_update_success = False
    monitor.sample(at)
    assert monitor.summary()["reason"] == "forecast_unavailable"
    coord.last_update_success = True
    monitor.sample(at + timedelta(minutes=3))
    assert monitor.summary()["state"] == "unknown"


def test_start_is_idempotent_and_stop_unsubscribes(monkeypatch):
    monitor, coord, states, at = observer()
    subscriptions, removals = [], []
    def track(hass, action, **kwargs):
        subscriptions.append(kwargs)
        return lambda: removals.append(True)
    monkeypatch.setattr(_panel_weather, "async_track_time_change", track)
    monitor.start()
    monitor.start()
    monitor.stop()
    monitor.stop()
    assert subscriptions == [{"second": 0}]
    assert removals == [True]


@pytest.mark.parametrize('condition', ['clipped', 'ungrouped', 'horizontal', 'invalid_eta'])
def test_non_independent_or_electrically_limited_panels_are_unknown(condition):
    monitor, coord, states, at = observer()
    if condition == 'clipped':
        coord._site = replace(coord._site, groups=tuple(replace(g, ac_limit_w=100) for g in coord._site.groups))
    elif condition == 'ungrouped':
        coord._site = replace(coord._site, groups=())
    elif condition == 'horizontal':
        coord._site = replace(coord._site, planes=tuple(replace(p, tilt_deg=0) for p in coord._site.planes))
    else:
        coord._site = replace(coord._site, groups=tuple(replace(g, inverter_efficiency=0) for g in coord._site.groups))
    monitor.sample(at)
    assert monitor.summary()['state'] == 'unknown'
    assert monitor.summary()['reason'] == 'insufficient_diversity'


def test_weather_prior_uses_current_interval_and_model_change_restarts_history(monkeypatch):
    monitor, coord, states, at = observer()
    slot = SimpleNamespace(start=at-timedelta(minutes=15), cloud_low=0, cloud_mid=0,
                           cloud_high=0, visibility_m=10000, ghi=1)
    current = SimpleNamespace(start=at, cloud_low=100, cloud_mid=100,
                              cloud_high=100, visibility_m=10000, ghi=2)
    coord._cached_weather = lambda: SimpleNamespace(slots=(slot, current))
    monkeypatch.setattr(_panel_weather.bias, 'classify_cloud',
                        lambda **kwargs: 'overcast' if kwargs['ghi'] == 2 else 'clear')
    monitor.sample(at)
    assert monitor.summary()['forecast_class'] == 'overcast'
    coord._result_reference_context = (coord._last_result, {}, SimpleNamespace(slots=(
        SimpleNamespace(**{**vars(slot), 'start': at}),)))
    monitor.sample(at+timedelta(seconds=1))
    assert monitor.summary()['forecast_class'] == 'clear'
    at += timedelta(minutes=5)
    for state in states.values():
        state.last_reported = at
        state.state = '60'
    coord._last_result = SimpleNamespace(slot_starts=coord._last_result.slot_starts)
    monitor.sample(at)
    assert monitor.summary()['state'] == 'warming_up'
    assert monitor.summary()['common_ramp'] is None


def test_adjacent_azimuth_bins_are_not_independent_weather_evidence(monkeypatch):
    monitor,coord,states,at=observer()
    coord._site=replace(coord._site,planes=tuple(replace(p,azimuth_deg=22.4 if i<2 else 22.6,tilt_deg=70) for i,p in enumerate(coord._site.planes)))
    monkeypatch.setattr(_panel_weather.solpos,'sun_position',lambda *args:(22.5,45))
    monitor.sample(at)
    assert monitor.summary()['reason']=='insufficient_diversity'
