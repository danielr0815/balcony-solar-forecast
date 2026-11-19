"""Actual recorder unit conversion, DST bounds, nightly persistence and reload."""
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType

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
    expected = {(start + timedelta(hours=i)).isoformat(): 100 + i for i in range(hours)}
    records = [
        {'start': start + timedelta(hours=i, minutes=5 * slot), 'mean': .1 + .001 * i,
         'min': .1 + .001 * i, 'max': .1 + .001 * i}
        for i in range(hours)
        # Thin hours have one plausible mean instead of twelve.
        for slot in range(1 if has_gap and 8 <= i < 14 else 12)
    ]
    recorder = get_instance(hass)
    recorder.async_import_statistics(
        {'statistic_id': 'sensor.module_dc', 'source': 'recorder',
         'name': 'Synthetic DC', 'unit_of_measurement': 'kW',
         'unit_class': 'power', 'mean_type': StatisticMeanType.ARITHMETIC, 'has_sum': False},
        records, StatisticsShortTerm,
    )
    await recorder.async_block_till_done()
    daily, hourly = await coord._async_read_actuals(day)
    if has_gap:
        assert daily == hourly == {}
        before = coord._bias_state.to_dict()
        await coord._train_and_guard(day)
        assert coord._bias_state.to_dict() == before
        return
    assert hourly['M1'] == pytest.approx(expected)
    assert daily['M1'] == sum(expected.values())
    iso = day.isoformat()
    forecast = {stamp: value*1.2 for stamp, value in expected.items()}
    coord._store.record_issued(iso, IssuedSnapshot(
        issued_at=start.isoformat(), status='fresh', corrected_hourly_wh=forecast,
        raw_hourly_wh=forecast, cloud_class_by_hour={stamp: 'clear' for stamp in forecast},
        complete_weather_hours=tuple(forecast)).to_dict())
    await coord._async_nightly_job(start + timedelta(hours=hours + 2))
    learned = coord._store.get_bias_state().to_dict()
    assert coord._store.get_scoreboard_state().days[iso].measured_kwh == pytest.approx(sum(expected.values())/1000)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restored = hass.data[DOMAIN][entry.entry_id]
    assert restored._store.has_verified_actuals(iso)
    assert restored._store.get_actuals(iso) == daily
    assert restored._store.get_hourly_actuals(iso) == hourly
    assert restored._store.get_bias_state().to_dict() == learned
