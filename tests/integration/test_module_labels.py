"""Real reconfigure/reload preserves learned channels when only a label changes."""
from copy import deepcopy

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from tests.integration.helpers import DOMAIN, start_advanced, user_data


async def test_label_change_preserves_registry_learning_and_archives(real_hass):
    from custom_components.balcony_solar_forecast.core.types import (
        BiasCell,
        BiasState,
        IssuedSnapshot,
        QuantileState,
        ShademapBin,
        ShademapState,
    )
    hass = real_hass
    flow = await start_advanced(hass)
    created = await hass.config_entries.flow.async_configure(flow['flow_id'], user_data('Label test'))
    await hass.async_block_till_done()
    entry = created['result']
    old = hass.data[DOMAIN][entry.entry_id]
    registry = er.async_get(hass)
    ids = {e.unique_id.removeprefix(entry.entry_id+'_'):e.entity_id
           for e in er.async_entries_for_config_entry(registry,entry.entry_id)}
    before = set(ids.values())
    old._bias_state = BiasState(cells={'clear|midday':BiasCell(1.1,3,9)})
    old._shademap_state = ShademapState(channels={'M1':{'180|30|1':ShademapBin(.7,12)}})
    old._quantile_state = QuantileState(bins={'clear|midday':[['2026-01-01',1.1]]})
    old._store.set_bias_state(old._bias_state)
    old._store.set_shademap_state(old._shademap_state)
    old._store.set_quantile_state(old._quantile_state)
    archive=IssuedSnapshot(issued_at='2026-01-01T00:00Z',status='label-test',raw_hourly_wh={'2026-01-01T12:00:00+00:00':100}).to_dict()
    old._store.record_issued('2026-01-01',archive)
    expected = [old._bias_state.to_dict(),old._shademap_state.to_dict(),old._quantile_state.to_dict()]
    fingerprint=old._config_fingerprint()
    signature=old._site_signature()
    reconfigure=await hass.config_entries.flow.async_init(DOMAIN,context={'source':'reconfigure','entry_id':entry.entry_id})
    payload=user_data('Label test')
    payload.pop('name')
    payload['site']=deepcopy(entry.data['site'])
    payload['site']['planes'][0]['display_name']='South balcony'
    preview=await hass.config_entries.flow.async_configure(reconfigure['flow_id'],payload)
    assert preview['step_id']=='confirm_changes'
    assert 'display_name' in preview['description_placeholders']['fields']
    assert preview['description_placeholders']['modules']=='M1 → South balcony'
    assert entry.data['site']['planes'][0].get('display_name') is None
    saved=await hass.config_entries.flow.async_configure(preview['flow_id'],{})
    assert saved['type']==FlowResultType.ABORT
    await hass.async_block_till_done()
    restored=hass.data[DOMAIN][entry.entry_id]
    assert restored is not old
    assert restored._config_fingerprint()==fingerprint and restored._site_signature()==signature
    assert [restored._bias_state.to_dict(),restored._shademap_state.to_dict(),restored._quantile_state.to_dict()]==expected
    assert restored._store.get_issued('2026-01-01')==archive
    assert set(e.entity_id for e in er.async_entries_for_config_entry(registry,entry.entry_id))==before
    selected=hass.states.get(ids['shade_profile_module'])
    assert selected.state=='South balcony' and selected.attributes['module_id']=='M1'
    profile=await hass.services.async_call(DOMAIN,'get_shade_profile',{'entry_id':entry.entry_id,'module':'M1'},blocking=True,return_response=True)
    assert profile['result']['module']=='M1' and profile['result']['display_name']=='South balcony'
