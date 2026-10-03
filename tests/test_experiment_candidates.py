"""Research candidates stay causal, bounded and independent of production."""
from datetime import UTC, datetime, timedelta

import pytest

from scripts.validation.experiment_candidates import (
    daily_band_candidate,
    persistence_candidate,
    reference_mask,
)

ISSUE = datetime(2026, 9, 30, tzinfo=UTC)


def test_short_horizon_uses_one_bounded_residual_and_fades():
    args = {'slow_theta_wh': 100, 'ratio': .6, 'feature_end': ISSUE-timedelta(minutes=5),
            'feature_available_at': ISSUE, 'issued_at': ISSUE}
    assert persistence_candidate(**args, horizon_minutes=0) == 60
    assert persistence_candidate(**args, horizon_minutes=120) == 100
    assert persistence_candidate(**{**args, 'ratio': 0}, horizon_minutes=0) == 50
    with pytest.raises(ValueError):
        persistence_candidate(**{**args, 'feature_available_at': ISSUE+timedelta(seconds=1)}, horizon_minutes=0)


def history():
    return [{'local_day': f'2026-09-{day:02}', 'complete': True, 'forecast_wh': 100,
             'actual_wh': actual, 'issued_at': f'2026-09-{day:02}T00:00Z',
             'available_at': f'2026-09-{day:02}T00:00Z',
             'target_start': f'2026-09-{day:02}T00:00Z', 'target_end': f'2026-09-{day:02}T23:00Z',
             'actual_available_at': f'2026-09-{day:02}T23:30Z'} for day, actual in [(1, 50), (2, 150)]]


def test_daily_residual_candidate_never_uses_its_future_or_partial_day():
    rows = history()
    assert daily_band_candidate(rows, issue=ISSUE, point_wh=200)['status'] == 'cold'
    band = daily_band_candidate(rows, issue=ISSUE, point_wh=200, minimum_days=2)
    assert (band['p10'], band['p50'], band['p90']) == (120, 200, 280)
    rows[1]['complete'] = False
    assert daily_band_candidate(rows, issue=ISSUE, point_wh=200, minimum_days=2)['status'] == 'cold'
    rows[1]['complete'] = True
    rows[1]['actual_available_at'] = '2026-10-01T00:00Z'
    assert daily_band_candidate(rows, issue=ISSUE, point_wh=200, minimum_days=2)['days'] == 1


def test_geometry_eligibility_is_frozen_from_reference_not_candidate_error():
    rows = [{'reference_verified': True, 'reference_span': .05, 'quality': 'valid'},
            {'reference_verified': False, 'reference_span': .01, 'quality': 'valid'},
            {'reference_verified': True, 'reference_span': .2, 'quality': 'valid'}]
    assert reference_mask(rows) == [True, False, False]


def test_invalid_reference_metadata_and_boolean_energy_are_not_evidence():
    rows = [{'reference_verified': verified, 'reference_span': span, 'quality': 'valid'}
            for verified, span in [('false', .01), (True, -.1), (True, False),
                                   (True, float('nan')), (True, 'unknown')]]
    assert reference_mask(rows) == [False]*5
    with pytest.raises(ValueError):
        reference_mask([], max_reference_span=float('inf'))
    with pytest.raises(ValueError):
        daily_band_candidate([], issue=ISSUE, point_wh=True)
    with pytest.raises(ValueError):
        daily_band_candidate([], issue=ISSUE, point_wh=100, minimum_days=0)
    with pytest.raises(ValueError):
        persistence_candidate(slow_theta_wh=100, ratio=True, horizon_minutes=0,
                              feature_end=ISSUE, feature_available_at=ISSUE, issued_at=ISSUE)


def test_executable_persistence_comparison_uses_theta_once_and_preserves_source():
    import copy
    from pathlib import Path

    from scripts.validation.experiment_candidates import evaluate_persistence
    from scripts.validation.replay_benchmark import load_verified
    data = load_verified(Path('tests/fixtures/replay/manifest.json'))
    for row in data['records']:
        row['actual_wh'] = 100
        row['forecasts']['theta'] = row['forecasts']['served'] = 100
        row['candidate_inputs']['sum_ratio'] = row['candidate_inputs']['panel_ratio'] = .6
    before = copy.deepcopy(data)
    result = evaluate_persistence(data)['splits']['test']
    assert result['served']['mae_wh'] == 0
    assert result['persistence_sum']['mae_wh'] == 10
    assert result['persistence_panel']['mae_wh'] == 10
    assert result['persistence_panel_weather']['mae_wh'] == 5
    assert data == before


def test_daily_comparison_uses_complete_days_and_penalizes_overconfident_band():
    from scripts.validation.experiment_candidates import evaluate_daily_bands
    start = datetime(2026, 9, 1, tzinfo=UTC)
    rows = []
    for i in range(21):
        at, end = start+timedelta(days=i), start+timedelta(days=i+1)
        rows.append({'local_day': at.date().isoformat(), 'complete': True,
                     'issued_at': at.isoformat(), 'available_at': at.isoformat(), 'target_start': at.isoformat(), 'target_end': end.isoformat(),
                     'actual_available_at': (end+timedelta(minutes=5)).isoformat(),
                     'forecast_wh': 100, 'actual_wh': 100 if i < 20 else 120,
                     'issued_interval': [90, 110]})
    # At 00:00 on day 21, day 20's actual is not yet available: only 19 days.
    data = {'basis': 'DC', 'timezone': 'UTC', 'selection_end': '2026-09-20', 'daily_records': rows}
    cold = evaluate_daily_bands(data)
    assert cold['cold_days'] == 1 and cold['paired_days'] == 0
    rows[-1]['issued_at'] = (start+timedelta(days=20)-timedelta(seconds=1)).isoformat()
    rows[-1]['available_at'] = rows[-1]['issued_at']
    # Move only the already-closed day 20 availability to midnight; remains
    # unavailable to this earlier issue and must not create new evidence.
    rows[-2]['actual_available_at'] = rows[-2]['target_end']
    assert evaluate_daily_bands(data)['cold_days'] == 1
    rows[-1]['issued_at'] = rows[-1]['target_start']
    rows[-1]['available_at'] = rows[-1]['issued_at']
    result = evaluate_daily_bands(data)
    assert result['paired_days'] == 1
    assert result['models']['issued']['mean_interval_score'] == 120
    assert result['models']['day_residual']['mean_interval_score'] == 200
    assert result['models']['day_residual']['upper_misses'] == 1
