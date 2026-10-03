"""Selected-entry identity must survive renames and exclude foreign sources."""
import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def fetch(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'/'validation'))
    yield importlib.import_module('bsf_fetch')
    sys.modules.pop('bsf_fetch', None)
    sys.modules.pop('bsf_data', None)


def test_renamed_sources_are_resolved_only_from_selected_entry(fetch):
    entries = [{'domain': fetch.DOMAIN, 'entry_id': e} for e in ('one', 'two')]
    registry = [{'config_entry_id': e, 'platform': fetch.DOMAIN,
                 'unique_id': f'{e}_measured_dc_power_total', 'entity_id': f'sensor.{e}'}
                for e in ('one', 'two')]
    states = [{'entity_id': f'sensor.{e}', 'attributes': {'sources': [f'sensor.{e}_port']}}
              for e in ('one', 'two')]
    assert fetch.resolve_sources(entries, registry, states, 'two') == (
        'two', {'measured_dc_power_total': 'sensor.two'}, ['sensor.two_port'])
    with pytest.raises(fetch.FetchError, match='unique'):
        fetch.resolve_sources(entries, registry, states)
    with pytest.raises(fetch.FetchError, match='belong'):
        fetch.resolve_sources(entries, registry, states, 'foreign')


def test_no_disabled_source_and_no_entity_name_membership(fetch):
    entries = [{'domain': fetch.DOMAIN, 'entry_id': 'one'}]
    registry = [{'config_entry_id': 'one', 'platform': fetch.DOMAIN,
                 'unique_id': 'one_measured_dc_power_total', 'entity_id': 'sensor.disabled',
                 'disabled_by': 'user'}]
    states = [{'entity_id': 'sensor.balcony_solar_forecast_fake', 'attributes': {'sources': ['secret']}}]
    assert fetch.resolve_sources(entries, registry, states) == ('one', {}, [])
