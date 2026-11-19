"""The bundled history card can read its configured data through HA's actual WS API."""
from datetime import UTC, datetime, timedelta

from aiohttp import ClientSession
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.helpers import entity_registry
from homeassistant.setup import async_setup_component

from tests.integration.helpers import DOMAIN, start_advanced, user_data


async def test_read_only_card_history_uses_recorder_units_and_timestamps(recorder_hass, unused_tcp_port):
    hass = recorder_hass
    assert await async_setup_component(hass, 'websocket_api', {})
    flow = await start_advanced(hass)
    created = await hass.config_entries.flow.async_configure(flow['flow_id'], user_data())
    await hass.async_block_till_done()
    entry = created['result']
    total = entity_registry.async_get(hass).async_get_entity_id(
        'sensor', DOMAIN, f'{entry.entry_id}_measured_dc_power_total',
    )
    assert total
    start = datetime(2025, 10, 26, 12, tzinfo=UTC)
    recorder = get_instance(hass)
    recorder.async_import_statistics(
        {'statistic_id': 'sensor.module_dc', 'source': 'recorder', 'name': 'Test DC',
         'unit_of_measurement': 'kW', 'unit_class': 'power',
         'mean_type': StatisticMeanType.ARITHMETIC, 'has_sum': False},
        [{'start': start, 'mean': .125, 'min': .110, 'max': .140}], Statistics,
    )
    await recorder.async_block_till_done()

    # Exercise the registered commands on an isolated loopback HA with a normal
    # read-only identity. Both the group permissions and the recorder are real.
    reader = await hass.auth.async_create_system_user('Card reader', group_ids=[GROUP_ID_READ_ONLY])
    refresh = await hass.auth.async_create_refresh_token(reader)
    async with ClientSession() as client, client.ws_connect(f'http://127.0.0.1:{unused_tcp_port}/api/websocket') as ws:
        assert (await ws.receive_json())['type'] == 'auth_required'
        await ws.send_json({'type': 'auth', 'access_token': hass.auth.async_create_access_token(refresh)})
        assert (await ws.receive_json())['type'] == 'auth_ok'
        args = {'entity_id': total, 'entry_id': entry.entry_id}
        await ws.send_json({'id': 1, 'type': f'{DOMAIN}/measurement_sources', **args})
        bindings = await ws.receive_json()
        assert bindings['success'] and bindings['result']['sources'] == ['sensor.module_dc']
        await ws.send_json({'id': 2, 'type': f'{DOMAIN}/measurement_history', **args,
                            'start_time': start.isoformat(), 'end_time': (start + timedelta(hours=1)).isoformat()})
        history = await ws.receive_json()
        assert history['success']
        assert history['result']['sources'] == bindings['result']['sources']
        assert history['result']['statistics']['sensor.module_dc'] == [
            {'start': int(start.timestamp() * 1000), 'mean': 125.0},
        ]
