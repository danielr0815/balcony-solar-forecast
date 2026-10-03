"""Real HA wizard creates generic multi-orientation sites only after review."""
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType

from tests.integration.helpers import DOMAIN


async def test_real_guided_setup_two_orientations_and_review(real_hass):
    hass = real_hass
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={'source': 'user'})
    assert flow['type'] == FlowResultType.MENU
    assert flow['menu_options'] == ['guided', 'advanced']
    flow = await hass.config_entries.flow.async_configure(flow['flow_id'], {'next_step_id': 'guided'})
    assert flow['step_id'] == 'guided'
    flow = await hass.config_entries.flow.async_configure(flow['flow_id'], {'name': 'Generic balcony', 'latitude': 35, 'longitude': 139})
    assert flow['step_id'] == 'guided_panel'
    east = {'name': 'East', 'azimuth_deg': 90, 'tilt_deg': 70, 'wp': 400,
            'group_name': 'Common inverter', 'ac_limit_w': 600, 'inverter_efficiency': .96, 'add_panel': True}
    flow = await hass.config_entries.flow.async_configure(flow['flow_id'], east)
    assert flow['step_id'] == 'guided_panel'
    assert not hass.config_entries.async_entries(DOMAIN)
    west = {**east, 'name': 'West', 'azimuth_deg': 270, 'add_panel': False}
    conflicting = {**west, 'ac_limit_w': 800}
    error = await hass.config_entries.flow.async_configure(flow['flow_id'], conflicting)
    assert error['errors'] == {'group_name': 'guided_group_conflict'}
    review = await hass.config_entries.flow.async_configure(error['flow_id'], west)
    assert review['step_id'] == 'guided_review'
    assert 'East' in review['description_placeholders']['panels']
    assert 'West' in review['description_placeholders']['groups']
    assert not hass.config_entries.async_entries(DOMAIN)
    created = await hass.config_entries.flow.async_configure(review['flow_id'], {'advanced': False})
    assert created['type'] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = created['result']
    assert entry.state is ConfigEntryState.LOADED
    site = entry.data['site']
    assert (site['latitude'], site['longitude']) == (35, 139)
    assert [p['azimuth_deg'] for p in site['planes']] == [90, 270]
    assert all(not p.get('actual_entity') for p in site['planes'])
    assert site['groups'][0]['plane_names'] == ['East', 'West']
    assert len(site['groups']) == 1


async def test_guided_review_can_open_advanced_editor_without_losing_panels(real_hass):
    hass = real_hass
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={'source': 'user'})
    flow = await hass.config_entries.flow.async_configure(flow['flow_id'], {'next_step_id': 'guided'})
    flow = await hass.config_entries.flow.async_configure(flow['flow_id'], {'name': 'One panel', 'latitude': 52, 'longitude': 13})
    flow = await hass.config_entries.flow.async_configure(flow['flow_id'], {
        'name': 'Panel A', 'azimuth_deg': 180, 'tilt_deg': 30, 'wp': 450,
        'group_name': 'Inverter A', 'ac_limit_w': 800, 'inverter_efficiency': .96, 'add_panel': False})
    advanced = await hass.config_entries.flow.async_configure(flow['flow_id'], {'advanced': True})
    assert advanced['type'] == FlowResultType.FORM
    assert not hass.config_entries.async_entries(DOMAIN)
    values = {key.schema: key.default() for key in advanced['data_schema'].schema if callable(key.default)}
    assert values['site']['planes'][0]['name'] == 'Panel A'
    assert values['site']['planes'][0]['wp'] == 450
