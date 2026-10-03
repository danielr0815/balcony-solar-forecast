"""Guided setup validates generic sites before its first persistent write."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.helpers.setup_flow import make_setup_flow


def panel(**changes):
    return {'name': 'East', 'azimuth_deg': 90, 'tilt_deg': 70, 'wp': 400,
            'group_name': 'WR', 'ac_limit_w': 800, 'inverter_efficiency': .96,
            'add_panel': False, **changes}


async def start(flow):
    return await flow.async_step_guided({'name': 'Example', 'latitude': 35, 'longitude': 139})


async def test_guided_site_commits_only_after_review_and_preserves_group_defaults(monkeypatch):
    flow, writes = make_setup_flow(monkeypatch)
    assert (await flow.async_step_user())['menu_options'] == ['guided', 'advanced']
    await start(flow)
    second = await flow.async_step_guided_panel(panel(add_panel=True))
    defaults = {key.schema: key.default() for key in second['data_schema'].schema if callable(key.default)}
    assert defaults['group_name'] == 'WR' and defaults['inverter_efficiency'] == .96
    assert 'actual_entity' not in flow._guided_panel_defaults
    review = await flow.async_step_guided_panel(panel(name='West', azimuth_deg=270))
    assert review['step_id'] == 'guided_review'
    assert not writes
    result = await flow.async_step_guided_review({'advanced': False})
    assert result['type'] == 'create_entry'
    assert len(writes) == 1
    site = writes[0]['data']['site']
    assert site['latitude'] == 35 and site['longitude'] == 139
    assert [p['azimuth_deg'] for p in site['planes']] == [90, 270]
    assert site['groups'][0]['plane_names'] == ['East', 'West']
    assert 'fetch_interval_seconds' in writes[0]['data']


@pytest.mark.parametrize('unit', ['W', 'kW', 'kWh'])
async def test_sensor_metadata_is_checked_before_panel_acceptance(monkeypatch, unit):
    state = SimpleNamespace(attributes={'unit_of_measurement': unit})
    flow, writes = make_setup_flow(monkeypatch, {'sensor.port': state})
    await start(flow)
    result = await flow.async_step_guided_panel(panel(actual_entity='sensor.port'))
    if unit == 'kWh':
        assert result['errors']['base'] == 'invalid_power_source'
        assert flow._setup_draft['site']['planes'] == []
    else:
        assert result['step_id'] == 'guided_review'
    assert not writes


async def test_invalid_panel_and_group_conflict_leave_draft_unchanged(monkeypatch):
    flow, writes = make_setup_flow(monkeypatch)
    assert (await flow.async_step_guided({'name': ' ', 'latitude': 35, 'longitude': 139}))['errors']['name'] == 'name_required'
    await start(flow)
    blank = await flow.async_step_guided_panel(panel(group_name=' '))
    assert blank['errors']['group_name'] == 'name_required'
    await flow.async_step_guided_panel(panel(add_panel=True, actual_entity='sensor.port'))
    before = deepcopy(flow._setup_draft)
    conflict = await flow.async_step_guided_panel(panel(name='West', ac_limit_w=600))
    assert conflict['errors']['group_name'] == 'guided_group_conflict'
    duplicate = await flow.async_step_guided_panel(panel(name='West', actual_entity='sensor.port'))
    assert duplicate['errors']['base'] == 'duplicate_actual_entity'
    assert flow._setup_draft == before
    assert not writes


async def test_review_advanced_editor_retains_setup_and_missing_drafts_restart(monkeypatch):
    flow, writes = make_setup_flow(monkeypatch)
    assert (await flow.async_step_guided_panel())['step_id'] == 'guided'
    assert (await flow.async_step_guided_review())['step_id'] == 'guided'
    await start(flow)
    assert (await flow.async_step_guided_review())['step_id'] == 'guided'
    await flow.async_step_guided_panel(panel())
    result = await flow.async_step_guided_review({'advanced': True})
    assert result['step_id'] == 'user'
    defaults = {key.schema: key.default() for key in result['data_schema'].schema if callable(key.default)}
    assert defaults['site']['planes'][0]['name'] == 'East'
    assert not writes


async def test_panel_limit_opens_review_instead_of_trapping_setup(monkeypatch):
    import custom_components.balcony_solar_forecast.config_flow as flows

    monkeypatch.setattr(flows, 'SITE_MAX_PLANES', 2)
    flow, writes = make_setup_flow(monkeypatch)
    await start(flow)
    await flow.async_step_guided_panel(panel(add_panel=True))
    result = await flow.async_step_guided_panel(panel(name='West', azimuth_deg=270, add_panel=True))
    assert result['step_id'] == 'guided_review'
    assert len(flow._setup_draft['site']['planes']) == 2
    assert not writes
