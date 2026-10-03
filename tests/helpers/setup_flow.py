"""Shared HA boundary for initial setup tests; captures writes without a manager."""
from types import SimpleNamespace

from custom_components.balcony_solar_forecast.config_flow import (
    BalconySolarForecastConfigFlow,
)


def make_setup_flow(monkeypatch, states=None):
    flow = BalconySolarForecastConfigFlow.__new__(BalconySolarForecastConfigFlow)
    flow.hass = SimpleNamespace(config=SimpleNamespace(latitude=52, longitude=13),
                                states=SimpleNamespace(get=(states or {}).get))
    writes = []
    async def unique_id(name):
        return None
    def create_entry(**kwargs):
        writes.append(kwargs)
        return {'type': 'create_entry', **kwargs}
    monkeypatch.setattr(flow, 'async_set_unique_id', unique_id)
    monkeypatch.setattr(flow, '_abort_if_unique_id_configured', lambda: None)
    monkeypatch.setattr(flow, 'async_create_entry', create_entry)
    monkeypatch.setattr(flow, 'async_show_form', lambda **kwargs: {'type': 'form', **kwargs})
    monkeypatch.setattr(flow, 'async_show_menu', lambda **kwargs: {'type': 'menu', **kwargs})
    return flow, writes
