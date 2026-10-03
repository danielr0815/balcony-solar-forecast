"""Causal offline learner scheduler with isolated state and per-variant references.

This is a research boundary, not a reconstruction of historical HA training.
Adapters must provide the original issue-time inputs and their own matching
training references. No mutable live coordinator or installed Store is used.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .replay_benchmark import number, timestamp


@dataclass(frozen=True)
class LearnerVariant:
    initial_state: Any
    initial_available_at: str
    predict: Callable[[Any, dict], float | None]
    train: Callable[[Any, dict], Any]


def replay(issues: list[dict], labels: list[dict], variants: dict[str, LearnerVariant]) -> dict:
    """Train only closed, available labels before the next issue.

    Prediction callbacks see issue inputs, never actual targets. Each training
    callback sees only its explicitly frozen reference; borrowing another
    variant's learned theta/reference would invalidate a learning ablation.
    Invalid labels are quarantined for all variants. Returned states and inputs
    are private copies, so running this scheduler cannot alter caller state.
    """
    if not variants:
        raise ValueError('At least one independently defined learner is required')
    ordered = sorted(deepcopy(issues), key=lambda row: timestamp(row['issued_at']))
    events = sorted(deepcopy(labels), key=lambda row: (timestamp(row['available_at']), row['id']))
    seen = set()
    for label in events:
        if label['id'] in seen:
            raise ValueError('Duplicate label identity cannot be trained twice')
        seen.add(label['id'])
        if not timestamp(label['target_start']) < timestamp(label['target_end']) <= timestamp(label['available_at']):
            raise ValueError('A training label must be a closed interval')
        for name in variants:
            reference = label.get('references', {}).get(name)
            if not isinstance(reference, dict):
                raise ValueError('Each learner needs its own frozen training reference')
            if not timestamp(reference['available_at']) <= timestamp(reference['issued_at']) <= timestamp(label['target_start']):
                raise ValueError('Training reference must be an originally available future forecast')
    states = {name: deepcopy(variant.initial_state) for name, variant in variants.items()}
    forecasts, trained, cursor = [], {name: [] for name in variants}, 0
    for row in ordered:
        at = timestamp(row['issued_at'])
        if any(timestamp(variant.initial_available_at) > at for variant in variants.values()):
            raise ValueError('Initial learner state was not available at the issue')
        if 'actual_wh' in row or 'references' in row:
            raise ValueError('Issue inputs must not contain future labels')
        if not at <= timestamp(row['target_start']) < timestamp(row['target_end']):
            raise ValueError('A prediction target must be fully future')
        for feature in row.get('features', []):
            if not timestamp(feature['end']) <= timestamp(feature['available_at']) <= at:
                raise ValueError('Issue feature was not yet closed and available')
        while cursor < len(events) and timestamp(events[cursor]['available_at']) <= at:
            label = events[cursor]
            cursor += 1
            if label.get('quality', 'valid') != 'valid' or number(label.get('actual_wh')) is None:
                continue
            # One common eligibility mask; no variant can discard its own bad fit.
            if any(number(label['references'][name].get('forecast_wh')) is None for name in variants):
                continue
            for name, variant in variants.items():
                own = {key: deepcopy(value) for key, value in label.items() if key != 'references'}
                own['reference'] = deepcopy(label['references'][name])
                states[name] = deepcopy(variant.train(deepcopy(states[name]), own))
                trained[name].append(label['id'])
        values = {name: variant.predict(deepcopy(states[name]), deepcopy(row)) for name, variant in variants.items()}
        if any(value is not None and number(value) is None for value in values.values()):
            raise ValueError('Learner emitted invalid power/energy')
        forecasts.append({**row, 'forecasts': values})
    return {'ablation': 'independent_causal_learning', 'records': forecasts,
            'states': states, 'trained_labels': trained,
            'pending_labels': len(events)-cursor, 'activation': 'never_automatic'}
