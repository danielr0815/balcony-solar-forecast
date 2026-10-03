#!/usr/bin/env python3
"""Offline, chronological forecast comparison with immutable hashed inputs.

Only frozen forecasts are scored. This does not reconstruct old issues using
current weather/learners, nor turn application ablations into learning trials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError('Expected an explicit interval timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Naive timestamps cannot establish causal availability')
    return parsed.astimezone(UTC)


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def metrics(pairs: list[tuple[float, float]]) -> dict:
    if not pairs:
        return {'n': 0, 'mae_wh': None, 'rmse_wh': None, 'bias_wh': None, 'wape': None}
    errors = [forecast - actual for forecast, actual in pairs]
    actual_energy = sum(actual for _, actual in pairs)
    return {'n': len(pairs), 'mae_wh': sum(map(abs, errors))/len(errors),
            'rmse_wh': math.sqrt(sum(e*e for e in errors)/len(errors)),
            'bias_wh': sum(errors)/len(errors),
            'wape': sum(map(abs, errors))/actual_energy if actual_energy > 0 else None}


def interval_score(actual: float, low: float, high: float, alpha: float = .2) -> float:
    if not 0 < alpha < 1 or low > high:
        raise ValueError('Invalid interval or nominal coverage')
    return high-low + (2/alpha)*(max(low-actual, 0)+max(actual-high, 0))


def strata(row: dict, *, issue: datetime, start: datetime, tz: ZoneInfo) -> dict[str, str]:
    """Fixed issue-time axes; observed weather remains a separate audit axis.

    Missing metadata is explicit. Never infer clouds, clipping or maturity
    from a candidate's error, which would let it select its own easy subset.
    """
    lead = (start-issue).total_seconds()/60
    hour = start.astimezone(tz).hour
    metadata = row.get('regime', {})
    def category(key, allowed):
        value = metadata.get(key)
        return value if isinstance(value, str) and value in allowed else 'unknown'
    elevation = metadata.get('sun_elevation_deg')
    angle = ('unknown' if isinstance(elevation, bool) or not isinstance(elevation, (int, float))
             or not math.isfinite(elevation) else
             'below_10' if elevation < 10 else '10_to_30' if elevation < 30 else 'above_30')
    return {
        'lead_minutes': '0_to_30' if lead < 30 else '30_to_120' if lead < 120 else '120_plus',
        'local_period': 'before_08' if hour < 8 else '08_to_12' if hour < 12 else
                        '12_to_16' if hour < 16 else '16_plus',
        'sun_elevation': angle,
        'forecast_weather': category('forecast_weather', ('clear', 'mixed', 'overcast')),
        'observed_weather': category('observed_weather', ('clear', 'mixed', 'overcast')),
        'electrical_state': category('electrical_state', ('saturated', 'unsaturated')),
        'learning_state': category('learning_state', ('cold', 'partial', 'trained')),
    }


def compare(bundle: dict) -> dict:
    if bundle.get('schema_version') != 1 or bundle.get('basis') not in ('DC', 'AC'):
        raise ValueError('Unsupported schema or missing AC/DC basis')
    tz = ZoneInfo(bundle['timezone'])
    models = bundle['models']
    if not isinstance(models, list) or not models or len(set(models)) != len(models):
        raise ValueError('Unique model names are required')
    development_end = date.fromisoformat(bundle['development_end']).isoformat()
    selection_end = date.fromisoformat(bundle['selection_end']).isoformat()
    if development_end >= selection_end:
        raise ValueError('Chronological split boundaries must be ordered')
    pairs = defaultdict(list)
    paired_days = defaultdict(lambda: defaultdict(list))
    daily = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    availability = defaultdict(int)
    omitted = defaultdict(int)
    bands = defaultdict(list)
    eligible = defaultdict(int)
    stratified = defaultdict(list)
    seen = set()
    for row in bundle['records']:
        issue, available = timestamp(row['issued_at']), timestamp(row['available_at'])
        start, end = timestamp(row['target_start']), timestamp(row['target_end'])
        if available > issue or end <= start:
            raise ValueError('Forecast was unavailable at issue, or invalid target interval')
        identity = (issue, start, end)
        if identity in seen:
            raise ValueError('Duplicate issue/target interval')
        seen.add(identity)
        if start < issue:
            omitted['target_not_fully_future'] += 1
            continue
        actual_available = timestamp(row['actual_available_at'])
        if actual_available < end:
            raise ValueError('Closed-interval actuals cannot predate the target end')
        for feature in row.get('features', []):
            if not timestamp(feature['end']) <= timestamp(feature['available_at']) <= issue:
                raise ValueError('Future/unavailable feature would leak into its own forecast')
        day = start.astimezone(tz).date().isoformat()
        split = 'development' if day <= development_end else 'selection' if day <= selection_end else 'test'
        actual = number(row.get('actual_wh'))
        if actual is None or row.get('quality', 'valid') != 'valid':
            omitted['invalid_or_missing_actual'] += 1
            continue
        eligible[split] += 1
        forecasts = {model: number(row.get('forecasts', {}).get(model)) for model in models}
        for model, forecast in forecasts.items():
            if forecast is not None:
                availability[(split, model)] += 1
        if any(value is None for value in forecasts.values()):
            omitted['unpaired_forecast'] += 1
            continue  # one common quality/availability mask for every ablation
        baseline = forecasts[models[0]]
        axes = strata(row, issue=issue, start=start, tz=tz)
        for model, forecast in forecasts.items():
            for axis, category in axes.items():
                stratified[(split, axis, category, model)].append((forecast, actual))
            paired_days[(split, model)][day].append((abs(baseline-actual), abs(forecast-actual)))
            pairs[(split, model)].append((forecast, actual))
            daily[(split, model)][day][0] += forecast
            daily[(split, model)][day][1] += actual
            interval = row.get('intervals', {}).get(model)
            if isinstance(interval, list) and len(interval) == 2:
                low, high = map(number, interval)
                if low is None or high is None:
                    raise ValueError('Invalid uncertainty interval')
                score = interval_score(actual, low, high)
                bands[(split, model)].append((low <= actual <= high, high-low, score,
                                             actual < low, actual > high))
    report = {'schema_version': 1, 'basis': bundle['basis'], 'timezone': bundle['timezone'],
              'ablation': 'frozen_application', 'omitted': dict(omitted), 'splits': {}}
    for split in ('development', 'selection', 'test'):
        report['splits'][split] = {}
        for model in models:
            values = bands[(split, model)]
            report['splits'][split][model] = {
                **metrics(pairs[(split, model)]),
                'eligible_targets': eligible[split], 'available_targets': availability[(split, model)],
                'covered_day_totals': metrics([tuple(v) for v in daily[(split, model)].values()]),
                'distinct_local_days': len(daily[(split, model)]),
                'intervals': ({'n': len(values), 'coverage': sum(v[0] for v in values)/len(values),
                               'mean_width_wh': sum(v[1] for v in values)/len(values),
                               'mean_interval_score': sum(v[2] for v in values)/len(values),
                               'lower_misses': sum(v[3] for v in values), 'upper_misses': sum(v[4] for v in values)}
                              if values else None),
            }
    if __package__:
        from .comparison_decision import paired_day_decision
    else:
        from comparison_decision import paired_day_decision
    report['test_comparisons'] = {
        model: paired_day_decision(paired_days[('test', model)],
                                  baseline_available=availability[('test', models[0])],
                                  candidate_available=availability[('test', model)],
                                  minimum_days=bundle.get('minimum_test_days', 30),
                                  minimum_relative_gain=bundle.get('minimum_relative_gain', .05))
        for model in models[1:]
    }
    report['comparison_baseline'] = models[0]
    report['strata'] = {}
    for (split, axis, category, model), values in sorted(stratified.items()):
        report['strata'].setdefault(split, {}).setdefault(axis, {}).setdefault(category, {})[model] = metrics(values)
    return report


def load_verified(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    source = manifest['input']
    relative = Path(source['path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Manifest input must remain inside its bundle directory')
    raw = (manifest_path.parent / relative).read_bytes()
    if hashlib.sha256(raw).hexdigest() != source['sha256']:
        raise ValueError('Input hash mismatch; source changed since capture')
    if not manifest.get('captured_at') or not manifest.get('licence') or not manifest.get('source_role'):
        raise ValueError('Capture time, licence and source role are required')
    timestamp(manifest['captured_at'])
    bundle = json.loads(raw)
    if manifest.get('basis') != bundle.get('basis') or manifest.get('timezone') != bundle.get('timezone'):
        raise ValueError('Manifest basis/timezone disagrees with data')
    return bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--report', type=Path, help='Readable Markdown report')
    args = parser.parse_args()
    source = args.manifest.parent / json.loads(args.manifest.read_text())['input']['path']
    for target in (args.output, args.report):
        if target and target.resolve() in (args.manifest.resolve(), source.resolve()):
            parser.error('Output cannot overwrite immutable inputs')
    if args.output and args.report and args.output.resolve() == args.report.resolve():
        parser.error('JSON and Markdown outputs need different files')
    report = compare(load_verified(args.manifest))
    report['evaluator_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
        if args.output.resolve() in (args.manifest.resolve(),
                                     (args.manifest.parent / json.loads(args.manifest.read_text())['input']['path']).resolve()):
            parser.error('Output cannot overwrite immutable inputs')
        args.output.write_text(text+'\n', encoding='utf-8')
    else:
        print(text)
    if args.report:
        lines = [f"# Frozen forecast comparison ({report['basis']})", "",
                 "Synthetic examples prove scoring arithmetic, not predictive improvement.", "",
                 "| Split | Model | Targets | Days | MAE Wh | WAPE | Available / eligible |",
                 "|---|---|---:|---:|---:|---:|---:|"]
        for split, models in report['splits'].items():
            for model, values in models.items():
                lines.append(f"| {split} | {model} | {values['n']} | {values['distinct_local_days']} | "
                             f"{values['mae_wh']} | {values['wape']} | "
                             f"{values['available_targets']} / {values['eligible_targets']} |")
        lines.extend(["", "## Test comparisons", ""])
        for model, decision in report['test_comparisons'].items():
            lines.append(f"- {model}: {decision['status']}; day-cluster 95% gain interval: {decision['gain_ci95_wh']} Wh.")
        args.report.write_text('\n'.join(lines)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
