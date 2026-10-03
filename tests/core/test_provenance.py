"""Calculation identity is deterministic, additive and excludes unbounded data."""
import pytest
from balcony_solar_forecast.core.provenance import bounded_provenance, digest
from balcony_solar_forecast.core.types import IssuedSnapshot


def test_canonical_hash_is_order_independent_and_rejects_invalid_numbers():
    assert digest({'x': 1, 'y': 2}) == digest({'y': 2, 'x': 1})
    assert digest({'x': 1}) != digest({'x': 2})
    with pytest.raises(ValueError):
        digest(float('nan'))
    with pytest.raises(TypeError):
        digest(object())


def test_metadata_accepts_only_bounded_model_identity_and_aware_time():
    assert bounded_provenance({'config_sha256': 'a'*64, 'basis': 'DC',
                               'weather_fetched_at': '2026-06-21T00:00+00:00',
                               'location': 'private', 'model_contract': 'bad?latitude=12',
                               'learner_sha256': 'wrong', 'integration_version': 10}) == {
        'config_sha256': 'a'*64, 'basis': 'DC',
        'weather_fetched_at': '2026-06-21T00:00:00+00:00'}
    assert bounded_provenance({'weather_fetched_at': 'bad'}) is None
    assert bounded_provenance({'weather_fetched_at': '2026-06-21T00:00'}) is None
    assert bounded_provenance(None) is None


def test_legacy_snapshot_does_not_acquire_new_optional_keys():
    legacy = IssuedSnapshot('', '').to_dict()
    assert not {'bands', 'provenance', 'computed_at', 'archived_at', 'corrected_ac_hourly_wh'} & legacy.keys()
    assert IssuedSnapshot.from_dict(legacy).to_dict() == legacy


def test_corrupt_archived_values_cannot_become_energy_or_metadata():
    snap = IssuedSnapshot.from_dict({'corrected_ac_hourly_wh': {'zero': 0, 'boolean': True,
                                    'negative': -1, 'nan': float('nan')},
                                    'bands': {'ac_p10_slot_wh': {'zero': 0, 'bad': float('inf')},
                                              'private': {'location': 12}}})
    assert snap.corrected_ac_hourly_wh == {'zero': 0}
    assert snap.bands == {'ac_p10_slot_wh': {'zero': 0}}
