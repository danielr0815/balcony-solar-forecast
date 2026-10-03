"""Reproduce loss of issued AC values and leakage of request coordinates."""
import traceback
from datetime import date
from types import SimpleNamespace

import aiohttp
import pytest
from balcony_solar_forecast.fetcher import FetchError, OpenMeteoFetcher
from yarl import URL

from custom_components.balcony_solar_forecast import _nightly
from tests.helpers.coordinator import _FakeStore, _make_coordinator


async def test_archive_preserves_served_ac_and_original_computation():
    store = _FakeStore()
    coord = _make_coordinator(store)
    hour = '2026-06-21T10:00:00+00:00'
    computed = '2026-06-21T00:15:00+00:00'
    coord.data = {'status': 'fresh', 'computed_at': computed,
                  'raw_hourly_wh': {hour: 100}, 'corrected_hourly_wh': {hour: 100},
                  'hourly_wh_ac': {hour: 85}}
    await _nightly.snapshot_issued(coord, date(2026, 6, 21))
    archived = store.issued['2026-06-21']
    assert archived.get('corrected_ac_hourly_wh') == {hour: 85}
    assert archived['issued_at'] == computed


async def test_failed_update_cannot_be_archived_as_fresh():
    store = _FakeStore()
    coord = _make_coordinator(store)
    coord.last_update_success = False
    coord.data = {'status': 'fresh', 'computed_at': '2026-06-21T00:15:00+00:00'}
    await _nightly.snapshot_issued(coord, date(2026, 6, 21))
    assert not store.issued


async def test_non_json_provider_error_does_not_expose_location():
    request = SimpleNamespace(real_url=URL('https://weather.example/?latitude=12.345&longitude=67.890'))
    exc = aiohttp.ContentTypeError(request, (), message='unexpected mimetype')

    class Response:
        status = 200
        async def json(self):
            raise exc
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False

    session = SimpleNamespace(get=lambda *args, **kwargs: Response())
    fetcher = OpenMeteoFetcher(session)
    with pytest.raises(FetchError) as caught:
        await fetcher._request_once(aiohttp, {})
    assert '12.345' not in str(caught.value)
    assert 'latitude' not in str(caught.value)
    # HA logs exception tracebacks as well as messages. Suppressing the
    # aiohttp cause must also keep its real request URL out of that surface.
    rendered = ''.join(traceback.format_exception(caught.value))
    assert 'latitude=12.345' not in rendered
    assert 'longitude=67.890' not in rendered
    assert caught.value.__suppress_context__


async def test_archive_roundtrip_preserves_bands_and_bounded_provenance():
    from custom_components.balcony_solar_forecast.core.types import IssuedSnapshot

    store = _FakeStore()
    coord = _make_coordinator(store)
    slot = '2026-06-21T10:15:00+00:00'
    coord.data = {'status': 'fresh', 'computed_at': '2026-06-21T00:15:00+00:00',
                  'provenance': {'model_contract': 'postclip-dc-ac-v2-positive-sky',
                                 'weather_sha256': 'a'*64, 'secret_url': 'https://private.example'},
                  'quantile_curves': {'p10': {slot: 20}, 'p90': {slot: 30}},
                  'wh_period_ac_p10': {slot: 18}, 'wh_period_ac_p90': {slot: 27}}
    await _nightly.snapshot_issued(coord, date(2026, 6, 21))
    blob = store.issued['2026-06-21']
    restored = IssuedSnapshot.from_dict(blob)
    assert restored.to_dict() == blob
    assert restored.bands == {'dc_p10_slot_wh': {slot: 20}, 'dc_p90_slot_wh': {slot: 30},
                              'ac_p10_slot_wh': {slot: 18}, 'ac_p90_slot_wh': {slot: 27}}
    assert restored.provenance == {'model_contract': 'postclip-dc-ac-v2-positive-sky',
                                   'weather_sha256': 'a'*64}


async def test_refresh_during_archive_cannot_mix_computations(monkeypatch):
    store = _FakeStore()
    coord = _make_coordinator(store)
    coord.data = {'status': 'fresh'}
    async def overtaken(day):
        coord.data = {'status': 'fresh', 'computed_at': '2026-06-21T00:30:00+00:00'}
        return {}
    monkeypatch.setattr(coord, '_slow_only_hourly', overtaken)
    await _nightly.snapshot_issued(coord, date(2026, 6, 21))
    assert not store.issued


async def test_computation_identity_is_captured_before_executor_state_change(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from custom_components.balcony_solar_forecast.core.engine import compute_forecast
    from custom_components.balcony_solar_forecast.core.types import WeatherSeries

    coord = _make_coordinator()
    weather = WeatherSeries(())
    at = datetime(2026, 6, 21, tzinfo=UTC)
    result = compute_forecast(coord._site, weather, at)
    before = coord._build_data(result, {}, at, 'fresh', timedelta())['provenance']
    monkeypatch.setattr('custom_components.balcony_solar_forecast.coordinator.compute_forecast',
                        lambda *args, **kwargs: result)
    async def executor(run):
        coord._intraday_scalar = .7
        return run()
    coord.hass.async_add_executor_job = executor
    actual = await coord._compute(weather, at)
    assert actual is result
    after = coord._build_data(result, {}, at, 'fresh', timedelta())['provenance']
    assert after['learner_sha256'] == before['learner_sha256']
    assert coord._intraday_scalar == .7


async def test_observer_reference_keeps_theta_from_its_result_generation(monkeypatch):
    from datetime import UTC, datetime

    from custom_components.balcony_solar_forecast.core.engine import (
        LearnerHooks,
        compute_forecast,
    )
    from custom_components.balcony_solar_forecast.core.types import (
        WeatherSeries,
        WeatherSlot,
    )

    coord = _make_coordinator()
    at = datetime(2026, 6, 21, 10, tzinfo=UTC)
    weather = WeatherSeries((WeatherSlot(at, 800, 850, 100, 20),))
    result = compute_forecast(coord._site, weather, at)
    def hooks(weather, now):
        coord._day_factor = {at: 1.2}
        return LearnerHooks()
    coord._build_learner_hooks = hooks
    async def executor(run):
        coord._day_factor = {at: .7}
        return result
    coord.hass.async_add_executor_job = executor
    await coord._compute(weather, at)
    reference = coord._intraday_reference(result, at)
    assert reference == {p.name: p.slow_watts[0]*1.2 for p in result.plane_results}
