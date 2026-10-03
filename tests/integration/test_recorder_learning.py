"""Actual recorder unit conversion, DST bounds, nightly persistence and reload."""
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics

from tests.integration.helpers import DOMAIN, start_advanced, user_data


@pytest.mark.parametrize('has_gap', [False, True])
@pytest.mark.parametrize('day, hours', [(date(2025, 3, 30), 23), (date(2025, 10, 26), 25)])
async def test_recorder_kw_local_day_nightly_store_reload(recorder_hass, day, hours, has_gap):
    from custom_components.balcony_solar_forecast.core.types import IssuedSnapshot

    hass = recorder_hass
    flow = await start_advanced(hass)
    created = await hass.config_entries.flow.async_configure(flow['flow_id'], user_data())
    await hass.async_block_till_done()
    entry = created['result']
    coord = hass.data[DOMAIN][entry.entry_id]
    tz = ZoneInfo('Europe/Berlin')
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    records = [{'start': start+timedelta(hours=i), 'mean': .1+.001*i,
                'min': .1+.001*i, 'max': .1+.001*i} for i in range(hours)
               if not has_gap or i not in range(8, 14)]
    async_import_statistics(hass, {'statistic_id': 'sensor.module_dc', 'source': 'recorder',
                                   'name': 'Synthetic DC', 'unit_of_measurement': 'kW',
                                   'unit_class': 'power', 'mean_type': StatisticMeanType.ARITHMETIC,
                                   'has_sum': False}, records)
    await get_instance(hass).async_block_till_done()
    daily, hourly = await coord._async_read_actuals(day)
    if has_gap:
        assert daily == hourly == {}
        before = coord._bias_state.to_dict()
        await coord._train_and_guard(day)
        assert coord._bias_state.to_dict() == before
        return
    expected = {row['start'].isoformat(): 100+i for i, row in enumerate(records)}
    assert hourly['M1'] == pytest.approx(expected)
    assert daily['M1'] == sum(expected.values())
    iso = day.isoformat()
    coord._store.record_actuals(iso, daily)
    coord._store.record_hourly_actuals(iso, hourly)
    forecast = {stamp: value*1.2 for stamp, value in expected.items()}
    coord._store.record_issued(iso, IssuedSnapshot(
        issued_at=start.isoformat(), status='fresh', corrected_hourly_wh=forecast,
        raw_hourly_wh=forecast, cloud_class_by_hour={stamp: 'clear' for stamp in forecast}).to_dict())
    await coord._train_and_guard(day)
    await coord._score_scoreboard_day(day)
    learned = coord._store.get_bias_state().to_dict()
    assert coord._store.get_scoreboard_state().days[iso].measured_kwh == pytest.approx(sum(expected.values())/1000)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restored = hass.data[DOMAIN][entry.entry_id]
    assert restored._store.get_actuals(iso) == daily
    assert restored._store.get_hourly_actuals(iso) == hourly
    assert restored._store.get_bias_state().to_dict() == learned
