"""Independent scoring arithmetic, common masks and no-leakage boundaries."""
import copy
import hashlib
import json

import pytest

from scripts.validation.replay_benchmark import compare, interval_score, load_verified


def bundle():
    return {'schema_version': 1, 'basis': 'DC', 'timezone': 'UTC',
            'development_end': '2026-09-01', 'selection_end': '2026-09-02',
            'models': ['raw', 'candidate'], 'records': [
                {'issued_at': '2026-09-03T08:00Z', 'available_at': '2026-09-03T08:00Z',
                 'target_start': f'2026-09-03T{hour}:00Z', 'target_end': f'2026-09-03T{hour}:30Z',
                 'actual_available_at': f'2026-09-03T{hour}:35Z', 'actual_wh': 100,
                 'forecasts': {'raw': raw, 'candidate': candidate}}
                for hour, raw, candidate in [('09', 100, 110), ('10', 150, 90)]]}


def test_independent_errors_do_not_hide_opposite_hourly_misses():
    report = compare(bundle())['splits']['test']
    assert report['raw']['mae_wh'] == 25
    assert report['raw']['bias_wh'] == 25
    assert report['raw']['wape'] == .25
    assert report['candidate']['mae_wh'] == 10
    assert report['candidate']['bias_wh'] == 0
    assert report['candidate']['covered_day_totals']['mae_wh'] == 0


def test_missing_candidate_cannot_choose_its_own_easier_targets():
    data = bundle()
    del data['records'][1]['forecasts']['candidate']
    report = compare(data)
    assert report['splits']['test']['raw']['n'] == 1
    assert report['splits']['test']['candidate']['n'] == 1
    assert report['splits']['test']['candidate']['available_targets'] == 1
    assert report['splits']['test']['raw']['available_targets'] == 2
    assert report['omitted']['unpaired_forecast'] == 1


def test_future_feature_and_forecast_availability_are_rejected():
    for field in ('available_at', 'feature'):
        data = bundle()
        if field == 'available_at':
            data['records'][0]['available_at'] = '2026-09-03T08:01Z'
        else:
            data['records'][0]['features'] = [{'end': '2026-09-03T08:30Z',
                                               'available_at': '2026-09-03T08:35Z'}]
        with pytest.raises(ValueError):
            compare(data)


def test_invalid_actual_mask_is_shared_and_prior_targets_are_not_forecasts():
    data = bundle()
    data['records'][0]['actual_wh'] = float('nan')
    data['records'][1]['target_start'] = '2026-09-03T07:00Z'
    result = compare(data)
    assert result['splits']['test']['raw']['n'] == 0
    assert result['omitted'] == {'invalid_or_missing_actual': 1, 'target_not_fully_future': 1}


def test_wide_intervals_pay_for_width_and_misses_pay_for_distance():
    assert interval_score(100, 90, 110) == 20
    assert interval_score(130, 90, 110) == 220
    assert interval_score(100, 0, 200) == 200
    with pytest.raises(ValueError):
        interval_score(100, 110, 90)


def test_manifest_checks_hash_role_units_and_never_rewrites_input(tmp_path):
    data = json.dumps(bundle()).encode()
    source = tmp_path/'forecast.json'
    source.write_bytes(data)
    manifest = {'input': {'path': source.name, 'sha256': hashlib.sha256(data).hexdigest()},
                'basis': 'DC', 'timezone': 'UTC', 'captured_at': '2026-09-04T00:00Z',
                'licence': 'CC0 synthetic', 'source_role': 'frozen_forecast_and_closed_actual'}
    path = tmp_path/'manifest.json'
    path.write_text(json.dumps(manifest))
    assert load_verified(path) == bundle()
    assert source.read_bytes() == data
    bad = copy.deepcopy(manifest)
    bad['timezone'] = 'Asia/Tokyo'
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match='basis/timezone'):
        load_verified(path)
    source.write_bytes(data+b' ')
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='hash'):
        load_verified(path)


def test_strata_expose_regression_and_do_not_infer_observed_clouds():
    data = bundle()
    data['records'][0]['forecasts'] = {'raw': 100, 'candidate': 120}
    data['records'][0]['regime'] = {'forecast_weather': 'clear', 'sun_elevation_deg': 5,
                                   'learning_state': 'cold', 'electrical_state': 'unsaturated'}
    data['records'][1]['regime'] = {'forecast_weather': 'overcast', 'observed_weather': 'clear',
                                   'sun_elevation_deg': 40, 'learning_state': 'trained'}
    report = compare(data)
    assert report['splits']['test']['candidate']['mae_wh'] < report['splits']['test']['raw']['mae_wh']
    clear = report['strata']['test']['forecast_weather']['clear']
    assert clear['candidate']['mae_wh'] == 20
    assert clear['raw']['mae_wh'] == 0
    assert report['strata']['test']['observed_weather']['unknown']['candidate']['n'] == 1
    assert report['strata']['test']['sun_elevation']['below_10']['candidate']['mae_wh'] == 20
    data['records'][1]['forecasts']['candidate'] = None
    strata = compare(data)['strata']['test']
    assert 'overcast' not in strata['forecast_weather']
    assert strata['forecast_weather']['clear']['raw']['n'] == 1
