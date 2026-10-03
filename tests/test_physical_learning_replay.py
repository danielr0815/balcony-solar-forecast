"""Real-engine learner references remain causal and variant-specific offline."""
from copy import deepcopy

import pytest

from scripts.validation.physical_learning_replay import replay_physical_days
from tests.helpers.physical_replay import data


def test_original_references_and_delayed_labels_keep_separate_real_states():
    bundle = data()
    before = deepcopy(bundle)
    result = replay_physical_days(bundle)
    assert bundle == before
    assert result['initial_state'] == 'cold'
    assert result['pending_days'] == 2
    assert all(days == ['2026-09-03'] for days in result['trained_days'].values())
    records = result['records']
    assert records[0]['forecasts']['raw']['corrected_hourly_wh'] == records[0]['forecasts']['bias']['corrected_hourly_wh']
    assert records[1]['forecasts']['raw']['corrected_hourly_wh'] == records[1]['forecasts']['bias']['corrected_hourly_wh']
    assert records[2]['forecasts']['raw']['corrected_hourly_wh'] == records[2]['forecasts']['bias']['corrected_hourly_wh']
    assert all(cell['n'] == 1 for cell in result['states']['bias']['bias']['cells'].values())
    assert result['states']['raw']['bias']['cells'] == {}
    assert result['states']['bias']['bias']['cells']
    # The full learner's quantile residual uses its OWN corrected issued curve.
    issued = records[0]['forecasts']['full']['corrected_hourly_wh']
    samples = [sample for ring in result['states']['full']['quantile']['bins'].values() for sample in ring]
    possible = [min(5, value/issued[h]) for h,value in before['days'][0]['actuals_hourly']['P'].items() if issued.get(h,0) > 5]
    assert samples and all(any(abs(sample[1]-value)<1e-9 for value in possible) for sample in samples)
    assert result['activation'] == 'never_automatic'


@pytest.mark.parametrize('timezone,day,count',[('Europe/Berlin','2026-03-29',92),('Europe/Berlin','2026-10-25',100),('Asia/Tokyo','2026-09-03',96)])
def test_full_calendar_axis_uses_site_timezone(timezone, day, count):
    bundle=data((day,),timezone)
    assert len(bundle['days'][0]['weather_slots']) == count
    result=replay_physical_days(bundle)
    assert result['pending_days']==1 and result['timezone']==timezone


@pytest.mark.parametrize('bad',['weather_future','observed_weather','gap','duplicate_day','invalid_number','early_label','unknown_variant'])
def test_noncausal_or_corrupt_inputs_are_rejected(bad):
    bundle=data()
    row=bundle['days'][0]
    if bad=='weather_future':
        row['weather_available_at']='2026-09-03T00:00Z'
    elif bad=='observed_weather':
        row['weather_role']='observed_weather'
    elif bad=='gap':
        row['weather_slots'].pop(40)
    elif bad=='duplicate_day':
        bundle['days'].append(deepcopy(row))
    elif bad=='invalid_number':
        row['weather_slots'][40]['ghi']=True
    elif bad=='early_label':
        row['actuals_available_at']='2026-09-03T12:00Z'
    else:
        bundle['variants']=['fictional']
    with pytest.raises(ValueError):
        replay_physical_days(bundle)


@pytest.mark.parametrize('bad',['missing','invalid','frozen','collapsed','declared'])
def test_bad_label_days_do_not_mutate_any_persisted_learner(bad):
    bundle=data()
    row=bundle['days'][0]
    hours=row['actuals_hourly']['P']
    if bad=='missing':
        row['actuals_hourly']={}
    elif bad=='invalid':
        hours['2026-09-03T10:00:00+00:00']=True
    elif bad=='frozen':
        row['actuals_hourly']['P']={h:100 for h in hours}
    elif bad=='collapsed':
        row['actuals_hourly']['P']={h:0 for h in hours}
    else:
        row['quality']='quarantined'
    result=replay_physical_days(bundle)
    assert all(not days for days in result['trained_days'].values())
    assert sum(result['skipped_days'].values())==1
    assert not result['states']['full']['bias']['cells']
    assert not result['states']['full']['shade']['channels']
    assert not result['states']['full']['quantile']['bins']


def test_bias_activates_after_three_available_days_and_each_quantile_uses_its_original_curve():
    bundle=data(tuple(f'2026-09-{day:02}' for day in range(3,10)))
    result=replay_physical_days(bundle)
    final=result['records'][-1]['forecasts']
    assert final['raw']['corrected_hourly_wh'] != final['bias']['corrected_hourly_wh']
    full_state=result['states']['full']['quantile']['bins']
    for ring in full_state.values():
        for day,ratio in ring:
            original=next(row['forecasts']['full']['corrected_hourly_wh'] for row in result['records'] if row['local_day']==day)
            actual=next(row['actuals_hourly']['P'] for row in bundle['days'] if row['local_day']==day)
            expected=[min(5,wh/original[h]) for h,wh in actual.items() if original.get(h,0)>5]
            assert any(abs(ratio-value)<1e-9 for value in expected)


def test_geometry_and_warm_start_variants_keep_same_measurements_and_distinct_references():
    bundle=data()
    before=deepcopy(bundle)
    candidate=deepcopy(bundle['site'])
    candidate['planes'][0]['tilt_deg']=70
    bundle['variants']={'baseline':{'preset':'full'},'geometry':{'preset':'full','site':candidate}}
    result=replay_physical_days(bundle)
    assert result['variant_config_sha256']['baseline'] != result['variant_config_sha256']['geometry']
    assert result['records'][0]['forecasts']['baseline']['raw_hourly_wh'] != result['records'][0]['forecasts']['geometry']['raw_hourly_wh']
    assert result['states']['baseline']['quantile'] != result['states']['geometry']['quantile']
    assert bundle['days']==before['days']
    bundle['initial_states']={'baseline': {'available_at':'2026-09-01T00:00Z',
        'bias':{'cells':{'clear|morning':{'theta':1.2,'covariance':1,'n':10}}}}}
    warm=replay_physical_days(bundle)
    assert warm['initial_state']=='provided_or_cold'
    assert warm['state_origins']['geometry']=={'source':'cold'}
    bundle['initial_states']['baseline']['available_at']='2026-09-04T00:00Z'
    with pytest.raises(ValueError,match='Initial state'):
        replay_physical_days(bundle)
    del bundle['initial_states']
    candidate['planes'][0]['actual_entity']='sensor.foreign'
    with pytest.raises(ValueError,match='measured identities'):
        replay_physical_days(bundle)


def test_weather_variants_keep_original_inputs_and_reject_late_weather():
    bundle = data()
    bundle['variants'] = {'baseline':{'preset':'full'},'weather':{'preset':'full'}}
    original = deepcopy(bundle['days'][0])
    alternate = {key:deepcopy(original[key]) for key in ('weather_role','weather_available_at','weather_slots')}
    for slot in alternate['weather_slots']:
        slot['ghi'] *= .8
        slot['dni'] *= .8
    bundle['days'][0]['variant_weather'] = {'weather':alternate}
    result = replay_physical_days(bundle)
    issued = result['records'][0]
    assert issued['weather_sha256']['baseline'] != issued['weather_sha256']['weather']
    assert issued['forecasts']['baseline']['corrected_hourly_wh'] != issued['forecasts']['weather']['corrected_hourly_wh']
    assert bundle['days'][0]['weather_slots'] == original['weather_slots']
    alternate['weather_available_at'] = '2026-09-03T00:00Z'
    with pytest.raises(ValueError,match='available forecast'):
        replay_physical_days(bundle)


def test_physical_cli_verifies_hashes_and_scores_common_dc_targets(monkeypatch, tmp_path):
    import json
    import sys
    from pathlib import Path

    from scripts.validation.physical_learning_replay import main
    manifest = Path(__file__).parent/'fixtures/replay/physical-manifest.json'
    output = tmp_path/'report.json'
    monkeypatch.setattr(sys,'argv',['physical_replay',str(manifest),'--output',str(output)])
    main()
    report = json.loads(output.read_text())
    assert report['comparison']['ablation'] == 'independent_physical_learning'
    assert report['comparison']['splits']['test']['raw']['n'] == 96
    assert report['comparison']['splits']['test']['full']['n'] == 96
    assert report['comparison']['splits']['test']['raw']['distinct_local_days'] == 4
    assert report['input_sha256'] == json.loads(manifest.read_text())['input']['sha256']
    # An honest verified bundle is immutable, even when it still parses as JSON.
    altered = tmp_path/'manifest.json'
    data = json.loads(manifest.read_text())
    data['input']['path'] = 'changed.json'
    (tmp_path/'changed.json').write_text('{}')
    altered.write_text(json.dumps(data))
    monkeypatch.setattr(sys,'argv',['physical_replay',str(altered),'--output',str(output)])
    with pytest.raises(ValueError,match='hash mismatch'):
        main()
