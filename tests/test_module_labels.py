"""HA labels and restored selections remain bound to their stable module ID."""
from dataclasses import replace

import pytest

from custom_components.balcony_solar_forecast._site_validation import (
    SiteValidationError,
    validate_site,
)
from custom_components.balcony_solar_forecast.select import ShadeProfileModuleSelect
from custom_components.balcony_solar_forecast.sensor import _measured_sources
from tests.helpers.shade_entities import _bare, _ShadeCoordinator


@pytest.mark.parametrize('label', ['', ' ', None, True, 1, 'x'*101, 'two\nlines'])
def test_invalid_display_names_are_rejected(label):
    site = {'latitude':0,'longitude':0,'planes':[{'name':'P','azimuth_deg':180,'tilt_deg':30,'wp':400,'display_name':label}], 'groups':[]}
    with pytest.raises(SiteValidationError, match='bad_display_name'):
        validate_site(site)


def test_duplicate_labels_are_rejected_even_when_one_uses_legacy_name():
    site = {'latitude':0,'longitude':0,'planes':[{'name':'P','azimuth_deg':180,'tilt_deg':30,'wp':400},
        {'name':'Q','display_name':'P','azimuth_deg':180,'tilt_deg':30,'wp':400}], 'groups':[]}
    with pytest.raises(SiteValidationError, match='duplicate_display_name'):
        validate_site(site)


async def test_select_label_is_translated_to_id_and_restore_survives_a_rename(monkeypatch):
    from homeassistant.helpers.update_coordinator import CoordinatorEntity
    async def noop(_self):
        pass
    monkeypatch.setattr(CoordinatorEntity, 'async_added_to_hass', noop)
    coord = _ShadeCoordinator(plane_names=['M1','M2'],module='M2')
    coord.shade_profile_labels = lambda: {'M1':'East','M2':'West'}
    sel = _bare(ShadeProfileModuleSelect, coord, async_write_ha_state=lambda: None)
    assert sel.options == ['East','West'] and sel.current_option == 'West'
    await sel.async_select_option('East')
    assert coord.module_set == ['M1']
    with pytest.raises(ValueError):
        await sel.async_select_option('missing')
    from homeassistant.core import State
    async def restored():
        return State('select.panel', 'Old label', {'module_id':'M2'})
    sel.async_get_last_state = restored
    await sel.async_added_to_hass()
    assert coord.module_set[-1] == 'M2'
    assert sel.extra_state_attributes['module_labels']['M2'] == 'West'


def test_measured_source_uses_display_name_without_changing_entity():
    from types import SimpleNamespace

    from custom_components.balcony_solar_forecast.core.types import (
        PlaneConfig,
        SiteConfig,
    )
    plane = PlaneConfig('M1',180,30,400,actual_entity='sensor.p')
    coord = SimpleNamespace(_site=SiteConfig(0,0,(replace(plane,display_name='South'),),()))
    assert _measured_sources(coord) == [('sensor.p','South',400)]
