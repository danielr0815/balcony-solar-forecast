"""Shade gate ablations exclude target groups and disclose lost evidence."""
from copy import deepcopy

import pytest

from scripts.validation.shadow_gate import evaluate_shadow_gate


def record():
    frames = []
    for minute in ('00', '05'):
        stamp = f'2026-09-03T10:{minute}:00Z'
        panels = [{'source': str(i), 'group': group, 'orientation': orientation,
                   'reported_at': stamp, 'watts': 100, 'reference_watts': 100}
                  for i, group, orientation in [(0, 'target', 'east'), (1, 'A', 'east'),
                                                (2, 'A', 'east'), (3, 'B', 'south')]]
        frames.append({'end': stamp, 'available_at': stamp, 'generation': 'original', 'panels': panels})
    return {'id': 'one', 'decision_at': '2026-09-03T10:05:00Z', 'frames': frames,
            'target_source': '0', 'baseline_eligible': True, 'shadow_alarm': True,
            'independent_shade': False, 'independent_reference_role': 'synthetic_truth'}


def test_shadow_policy_counts_false_alarms_and_target_group_exclusion_without_mutation():
    row = record()
    other = deepcopy(row)
    other['id'] = 'two'
    for frame in other['frames']:
        frame['panels'][1]['group'] = 'target'
    data = {'basis': 'DC', 'timezone': 'UTC', 'shade_records': [row, other]}
    before = deepcopy(data)
    result = evaluate_shadow_gate(data)
    assert result['counts'] == {'labels': 2, 'baseline_eligible': 2, 'candidate_eligible': 1}
    assert result['models']['baseline']['false_alarms'] == 2
    assert result['models']['candidate']['false_alarms'] == 1
    assert result['models']['candidate']['false_alarm_rate'] == 1
    assert result['bias_quantile_filter'] == 'unchanged'
    assert result['predictive_improvement'] == 'not_measured'
    assert data == before


def test_unknown_targets_and_panel_derived_truth_cannot_fake_independence():
    row = record()
    row['target_source'] = 'absent'
    assert evaluate_shadow_gate({'basis': 'DC', 'timezone': 'UTC', 'shade_records': [row]})['counts']['candidate_eligible'] == 0
    row['target_source'] = '0'
    row['independent_reference_role'] = 'same_panel_indicator'
    result = evaluate_shadow_gate({'basis': 'DC', 'timezone': 'UTC', 'shade_records': [row]})
    assert result['models']['candidate']['false_alarm_rate'] is None
    assert result['models']['candidate']['unknown_reference'] == 1
    row['decision_at'] = '2026-09-03T10:10:00Z'
    result = evaluate_shadow_gate({'basis': 'DC', 'timezone': 'UTC', 'shade_records': [row]})
    assert result['counts']['candidate_eligible'] == 0
    assert result['reasons'] == {'stale_decision_frame': 1}
    row['decision_at'] = '2026-09-03T10:05:00Z'
    row['frames'][1]['available_at'] = '2026-09-03T10:06:00Z'
    with pytest.raises(ValueError, match='available'):
        evaluate_shadow_gate({'basis': 'DC', 'timezone': 'UTC', 'shade_records': [row]})


def test_shadow_geometry_overrides_declared_diversity_at_angle_boundary():
    row = record()
    for frame in row['frames']:
        for panel in frame['panels']:
            panel.update(azimuth_deg=22.4 if panel['orientation'] == 'east' else 22.6,
                         tilt_deg=70)
    result = evaluate_shadow_gate({'basis': 'DC', 'timezone': 'UTC', 'shade_records': [row]})
    assert result['counts']['candidate_eligible'] == 0
    assert result['reasons'] == {'insufficient_diversity': 1}
