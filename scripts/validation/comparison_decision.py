"""Paired day-cluster uncertainty for predeclared model comparisons."""
from __future__ import annotations

import random


def paired_day_decision(days: dict[str, list[tuple[float, float]]], *,
                        baseline_available: int, candidate_available: int,
                        minimum_days: int = 30, minimum_relative_gain: float = .05) -> dict:
    """Each tuple is baseline/candidate absolute error on the SAME target.

    Resample whole local days, preserving all errors within a day. The fixed
    seed makes the report reproducible. This is a screening rule, not authority
    to enable a production model or evidence of independent weather episodes.
    """
    if minimum_days < 30 or not 0 <= minimum_relative_gain < 1:
        raise ValueError('Predeclare at least 30 days and a valid practical gain')
    clusters = [(sum(a for a, _ in pairs), sum(b for _, b in pairs), len(pairs))
                for _, pairs in sorted(days.items()) if pairs]
    result = {'distinct_local_days': len(clusters), 'minimum_days': minimum_days,
              'minimum_relative_gain': minimum_relative_gain,
              'resampling': 'paired_local_day_clusters', 'activation': 'never_automatic',
              'mae_gain_wh': None, 'gain_ci95_wh': None, 'status': 'insufficient_days'}
    if not clusters:
        return result
    baseline = sum(a for a, _, _ in clusters)
    candidate = sum(b for _, b, _ in clusters)
    count = sum(n for _, _, n in clusters)
    result['mae_gain_wh'] = (baseline-candidate)/count
    if len(clusters) < minimum_days:
        return result
    rng = random.Random(0)
    gains = []
    for _ in range(2000):
        sample = rng.choices(clusters, k=len(clusters))
        gains.append(sum(a-b for a, b, _ in sample)/sum(n for _, _, n in sample))
    gains.sort()
    interval = [gains[49], gains[1949]]
    result['gain_ci95_wh'] = interval
    if candidate_available < baseline_available:
        result['status'] = 'availability_regression'
    elif interval[1] < 0:
        result['status'] = 'worse'
    elif baseline > 0 and (baseline-candidate)/baseline >= minimum_relative_gain and interval[0] > 0:
        result['status'] = 'promising_requires_regime_review'
    else:
        result['status'] = 'inconclusive'
    return result
