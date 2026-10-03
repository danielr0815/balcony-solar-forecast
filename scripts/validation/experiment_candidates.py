"""Offline research candidates. No production forecast/learner imports or writes."""
from __future__ import annotations

from datetime import datetime
from statistics import median


def persistence_candidate(*, slow_theta_wh: float, ratio: float, horizon_minutes: float,
                          feature_end: datetime, feature_available_at: datetime,
                          issued_at: datetime, weather_class: str | None = None) -> float:
    """One residual correction with bounded horizon; never stack onto intraday."""
    times = (feature_end, feature_available_at, issued_at)
    if any(t.tzinfo is None for t in times) or not feature_end <= feature_available_at <= issued_at:
        raise ValueError('Feature must be closed and actually available at issue')
    import math
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
           for v in (slow_theta_wh, ratio, horizon_minutes)):
        raise ValueError('Invalid candidate input')
    bounded = max(.5, min(1.5, ratio))
    strength = max(0, 1-horizon_minutes/120)
    # A forecast prior reduces confidence in disagreement; it never declares
    # observed clouds or overrides panel evidence by itself.
    if weather_class == 'clear' and ratio < .8:
        strength *= .5
    return slow_theta_wh * (1+strength*(bounded-1))


def daily_band_candidate(history: list[dict], *, issue: datetime, point_wh: float,
                         minimum_days: int = 20) -> dict:
    """Day-level residual paths preserve intra-day dependence; cold is unknown.

    Each row is ONE complete local day, with the original forecast and the
    availability time of that day's final actual. Partial days never seed a
    daily band; the candidate cannot use its own future day or target labels.
    """
    import math

    from .replay_benchmark import timestamp
    if (issue.tzinfo is None or isinstance(point_wh, bool) or not isinstance(point_wh, (int, float))
            or not math.isfinite(point_wh) or point_wh < 0
            or isinstance(minimum_days, bool) or not isinstance(minimum_days, int) or minimum_days < 1):
        raise ValueError('Invalid issued day forecast')
    by_day = {}
    for row in history:
        if not row.get('complete') or row.get('quality', 'valid') != 'valid' or timestamp(row['actual_available_at']) > issue:
            continue
        if timestamp(row['available_at']) > timestamp(row['issued_at']):
            raise ValueError('Original forecast was unavailable at its issue')
        if timestamp(row['target_end']) > timestamp(row['actual_available_at']):
            raise ValueError('Actual day cannot be available before it ends')
        if timestamp(row['issued_at']) > timestamp(row['target_start']):
            continue
        forecast, actual = row['forecast_wh'], row['actual_wh']
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               or v < 0 for v in (forecast, actual)) or forecast <= 0:
            continue
        day = row['local_day']
        if day in by_day:
            raise ValueError('One residual per local day; duplicate labels are not new evidence')
        by_day[day] = actual/forecast
    ratios = sorted(by_day[day] for day in sorted(by_day)[-90:])
    if len(ratios) < minimum_days:
        return {'status': 'cold', 'days': len(ratios), 'p10': None, 'p50': None, 'p90': None}
    def percentile(p):
        position = p*(len(ratios)-1)
        low = int(position)
        high = min(low+1, len(ratios)-1)
        return ratios[low]+(position-low)*(ratios[high]-ratios[low])
    return {'status': 'experimental', 'days': len(ratios), 'p10': point_wh*percentile(.1),
            'p50': point_wh*median(ratios), 'p90': point_wh*percentile(.9)}


def reference_mask(rows: list[dict], *, max_reference_span: float = .1) -> list[bool]:
    """Freeze geometry-experiment eligibility before choosing a candidate.

    The same resulting mask is supplied to every candidate, never reselected
    from its fitted residuals. Verified reference roles must be explicit.
    """
    from .replay_benchmark import number

    if number(max_reference_span) is None:
        raise ValueError('Reference span limit must be finite and nonnegative')
    return [row.get('reference_verified') is True and number(row.get('reference_span')) is not None
            and row['reference_span'] <= max_reference_span and row.get('quality') == 'valid' for row in rows]


def evaluate_persistence(bundle: dict) -> dict:
    """Compare frozen served forecasts to three independently applied candidates.

    candidate_inputs must carry the original closed-feature availability. The
    caller freezes source roles, aggregation and quality before this evaluator;
    no candidate gets to relabel its own errors or refit the learner state.
    """
    import copy

    from .replay_benchmark import compare, timestamp
    result = copy.deepcopy(bundle)
    candidates = ('persistence_sum', 'persistence_panel', 'persistence_panel_weather')
    result['models'] = ['served', *candidates]
    for row in result['records']:
        inputs = row.get('candidate_inputs')
        point = row.get('forecasts', {}).get('theta')
        for name in candidates:
            row.setdefault('forecasts', {})[name] = None
        if not isinstance(inputs, dict) or point is None:
            continue
        issue = timestamp(row['issued_at'])
        end, available = timestamp(inputs['feature_end']), timestamp(inputs['feature_available_at'])
        row.setdefault('features', []).append({'end': end.isoformat(), 'available_at': available.isoformat()})
        for name in candidates:
            ratio = inputs.get('sum_ratio' if name == 'persistence_sum' else 'panel_ratio')
            if ratio is None:
                continue
            row['forecasts'][name] = persistence_candidate(
                slow_theta_wh=point, ratio=ratio,
                horizon_minutes=(timestamp(row['target_start'])-issue).total_seconds()/60,
                feature_end=end, feature_available_at=available, issued_at=issue,
                weather_class=inputs.get('forecast_class') if name.endswith('_weather') else None)
    report = compare(result)
    report['experiment'] = 'bounded_residual_persistence'
    report['learning_ablation'] = False
    return report


def evaluate_daily_bands(bundle: dict) -> dict:
    """A separate complete-calendar-day band comparison, with causal history."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from .replay_benchmark import interval_score, number, timestamp
    tz = ZoneInfo(bundle['timezone'])
    history = bundle.get('daily_records', [])
    rows = []
    cold, eligible = 0, 0
    for row in sorted(history, key=lambda r: r['issued_at']):
        issue = timestamp(row['issued_at'])
        if timestamp(row['available_at']) > issue:
            raise ValueError('Original forecast was unavailable at its issue')
        start, end = timestamp(row['target_start']), timestamp(row['target_end'])
        local = start.astimezone(tz)
        if local.hour or local.minute or local.second or local.microsecond:
            raise ValueError('Day targets must start at local midnight')
        expected_end = datetime.combine(local.date()+timedelta(days=1), datetime.min.time(), tzinfo=tz)
        if end != expected_end or row['local_day'] != local.date().isoformat():
            raise ValueError('Day targets must cover exactly one local calendar day')
        if issue > start or not row.get('complete') or row.get('quality', 'valid') != 'valid':
            continue
        actual, point = number(row.get('actual_wh')), number(row.get('forecast_wh'))
        if actual is None or point is None:
            continue
        if timestamp(row['actual_available_at']) < end:
            raise ValueError('Final day actual is not yet closed')
        day = local.date().isoformat()
        if day <= bundle['selection_end']:
            continue
        eligible += 1
        band = daily_band_candidate(history, issue=issue, point_wh=point)
        baseline = row.get('issued_interval')
        if band['status'] == 'cold':
            cold += 1
            continue
        if not isinstance(baseline, list) or len(baseline) != 2:
            continue
        low, high = map(number, baseline)
        if low is None or high is None:
            raise ValueError('Invalid issued day interval')
        intervals = {'issued': (low, high), 'day_residual': (band['p10'], band['p90'])}
        rows.append((day, actual, intervals))
    if len({day for day, _, _ in rows}) != len(rows):
        raise ValueError('One fixed issue per local day is required')
    report = {'experiment': 'calendar_day_residual_bands', 'basis': bundle['basis'],
              'eligible_days': eligible, 'cold_days': cold, 'paired_days': len(rows),
              'activation': 'never_automatic', 'models': {}}
    for model in ('issued', 'day_residual'):
        values = [(actual, intervals[model]) for _, actual, intervals in rows]
        report['models'][model] = {
            'coverage': sum(low <= actual <= high for actual, (low, high) in values)/len(values) if values else None,
            'mean_width_wh': sum(high-low for _, (low, high) in values)/len(values) if values else None,
            'mean_interval_score': sum(interval_score(actual, low, high) for actual, (low, high) in values)/len(values) if values else None,
            'lower_misses': sum(actual < low for actual, (low, _) in values),
            'upper_misses': sum(actual > high for actual, (_, high) in values),
        }
    return report


def main() -> None:
    import argparse
    import hashlib
    import json
    from pathlib import Path

    from .replay_benchmark import load_verified
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--experiment', choices=('persistence', 'daily-bands', 'shade-gate'), required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    bundle = load_verified(args.manifest)
    source = args.manifest.parent / json.loads(args.manifest.read_text())['input']['path']
    if args.output and args.output.resolve() in (source.resolve(), args.manifest.resolve()):
        parser.error('Output cannot overwrite immutable input')
    if args.experiment == 'shade-gate':
        from .shadow_gate import evaluate_shadow_gate
        report = evaluate_shadow_gate(bundle)
    else:
        report = evaluate_persistence(bundle) if args.experiment == 'persistence' else evaluate_daily_bands(bundle)
    report['evaluator_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)+'\n'
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
