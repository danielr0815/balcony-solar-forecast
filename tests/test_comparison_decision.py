"""Repeated intraday targets are not a substitute for holdout day diversity."""
import pytest

from scripts.validation.comparison_decision import paired_day_decision


def decide(days, **kwargs):
    return paired_day_decision(days, baseline_available=100, candidate_available=100, **kwargs)


def test_many_targets_from_one_day_stay_insufficient():
    result = decide({'2026-09-01': [(100, 50)]*1000})
    assert result['status'] == 'insufficient_days'
    assert result['gain_ci95_wh'] is None


def test_day_cluster_reproducibility_and_availability_gate():
    days = {f'day{i:02d}': [(100, 50), (50, 25)] for i in range(30)}
    first = decide(days)
    assert first == decide(dict(reversed(list(days.items()))))
    assert first['gain_ci95_wh'] == [37.5, 37.5]
    assert first['status'] == 'promising_requires_regime_review'
    result = paired_day_decision(days, baseline_available=100, candidate_available=99)
    assert result['status'] == 'availability_regression'


def test_consistent_regression_and_small_effect_are_distinguished():
    assert decide({str(i): [(10, 20)] for i in range(30)})['status'] == 'worse'
    assert decide({str(i): [(100, 99)] for i in range(30)})['status'] == 'inconclusive'
    with pytest.raises(ValueError):
        decide({}, minimum_days=29)
