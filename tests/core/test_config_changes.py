"""Preview effects follow the same model identity as persisted learning."""
from dataclasses import replace

from balcony_solar_forecast.core.config_changes import site_changes
from balcony_solar_forecast.core.types import InverterGroup, PlaneConfig, SiteConfig


def site():
    return SiteConfig(0, 0, (PlaneConfig('P', 180, 30, 400, actual_entity='sensor.p'),),
                      (InverterGroup('G', ('P',), 800),))


def test_source_and_group_label_changes_preserve_model_identity():
    before = site()
    after = replace(before, planes=(replace(before.planes[0], actual_entity='sensor.renamed'),),
                    groups=(replace(before.groups[0], name='Display label'),))
    assert site_changes(before, after) == {'model_changed': False,
        'modules': {'P': ['actual_entity']}, 'site_fields': ['groups']}


def test_raw_change_reopens_learning_and_module_add_remove_is_explicit():
    before = site()
    after = replace(before, planes=(replace(before.planes[0], tilt_deg=60),))
    result = site_changes(before, after)
    assert result['model_changed'] and result['modules'] == {'P': ['tilt_deg']}
    renamed = replace(before, planes=(replace(before.planes[0], name='New identity'),),
                      groups=(replace(before.groups[0], plane_names=('New identity',)),))
    assert site_changes(before, renamed)['modules'] == {'New identity': ['added'], 'P': ['removed']}
