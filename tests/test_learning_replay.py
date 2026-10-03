"""Independent states cannot borrow theta or labels from the future."""
from copy import deepcopy

import pytest

from scripts.validation.learning_replay import LearnerVariant, replay


def inputs():
    issues = [{'issued_at': f'2026-09-03T{hour}:00Z', 'target_start': f'2026-09-03T{hour}:00Z',
               'target_end': f'2026-09-03T{hour}:30Z', 'point_wh': 100} for hour in ('08', '09', '10')]
    label = {'id': 'closed-08', 'target_start': '2026-09-03T08:00Z',
             'target_end': '2026-09-03T08:30Z', 'available_at': '2026-09-03T09:05Z',
             'actual_wh': 100, 'references': {
                 name: {'forecast_wh': value, 'issued_at': '2026-09-03T07:00Z',
                        'available_at': '2026-09-03T07:00Z'} for name, value in [('raw', 100), ('slow', 50)]}}
    initial = {'theta': 1}
    def predict(state, row):
        return row['point_wh']*state['theta']
    def train(state, row):
        state['theta'] = row['actual_wh']/row['reference']['forecast_wh']
        return state
    variants = {name: LearnerVariant(initial, '2026-09-01T00:00Z', predict, train) for name in ('raw', 'slow')}
    return issues, [label], variants


def test_delayed_labels_and_separate_references_preserve_causality():
    issues, labels, variants = inputs()
    before = deepcopy((issues, labels))
    result = replay(issues, labels, variants)
    assert [r['forecasts'] for r in result['records']] == [
        {'raw': 100, 'slow': 100}, {'raw': 100, 'slow': 100}, {'raw': 100, 'slow': 200}]
    assert result['trained_labels'] == {'raw': ['closed-08'], 'slow': ['closed-08']}
    assert (issues, labels) == before
    assert variants['raw'].initial_state == {'theta': 1}


@pytest.mark.parametrize('bad', ['future_feature', 'initial_hindsight', 'unclosed_label', 'duplicate', 'foreign_reference'])
def test_invalid_replay_inputs_are_rejected(bad):
    issues, labels, variants = inputs()
    if bad == 'future_feature':
        issues[0]['features'] = [{'end': '2026-09-03T08:05Z', 'available_at': '2026-09-03T08:06Z'}]
    elif bad == 'initial_hindsight':
        old = variants['raw']
        variants['raw'] = LearnerVariant(old.initial_state, '2026-09-04T00:00Z', old.predict, old.train)
    elif bad == 'unclosed_label':
        labels[0]['available_at'] = '2026-09-03T08:29Z'
    elif bad == 'duplicate':
        labels.append(deepcopy(labels[0]))
    else:
        del labels[0]['references']['slow']
    with pytest.raises(ValueError):
        replay(issues, labels, variants)


def test_quality_mask_is_common_and_late_labels_remain_pending():
    issues, labels, variants = inputs()
    labels[0]['quality'] = 'frozen'
    result = replay(issues, labels, variants)
    assert result['trained_labels'] == {'raw': [], 'slow': []}
    labels[0]['quality'] = 'valid'
    labels[0]['available_at'] = '2026-09-03T10:05Z'
    result = replay(issues, labels, variants)
    assert result['pending_labels'] == 1
    assert result['states'] == {'raw': {'theta': 1}, 'slow': {'theta': 1}}
