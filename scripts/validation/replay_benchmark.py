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
from datetime import UTC, date, datetime, time, timedelta
from itertools import combinations
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


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Return UTC bounds for one local calendar day."""
    start = datetime.combine(day, time.min, tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC)
    return start, end


def issue_period(issue: datetime, target: datetime, tz: ZoneInfo) -> str:
    local_issue, local_target = issue.astimezone(tz), target.astimezone(tz)
    days_ahead = (local_target.date() - local_issue.date()).days
    if days_ahead == 0:
        return 'early_morning' if local_issue.hour < 6 else 'intraday'
    if days_ahead == 1 and local_issue.hour >= 18:
        return 'previous_evening'
    return 'earlier'


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
        'issue_period': issue_period(issue, start, tz),
        'lead_minutes': '0_to_30' if lead < 30 else '30_to_120' if lead < 120 else '120_plus',
        'local_period': 'before_08' if hour < 8 else '08_to_12' if hour < 12 else
                        '12_to_16' if hour < 16 else '16_plus',
        'sun_elevation': angle,
        'forecast_weather': category('forecast_weather', ('clear', 'mixed', 'overcast')),
        'observed_weather': category('observed_weather', ('clear', 'mixed', 'overcast')),
        'electrical_state': category('electrical_state', ('saturated', 'unsaturated')),
        'learning_state': category('learning_state', ('cold', 'partial', 'trained')),
    }


def _axis_seconds(rows):
    ordered = sorted((row[0], row[1]) for row in rows)
    if any(start < previous_end for (_, previous_end), (start, _) in zip(ordered, ordered[1:], strict=False)):
        raise ValueError('Daily evaluation requires non-overlapping target intervals per issue')
    return sum((end - start).total_seconds() for start, end in ordered)


def _select_day_issues(planned, tz):
    """One issue per day and period, selected using only the planned target axis."""
    selected = {}
    for (split, day, issue), targets in planned.items():
        seconds = _axis_seconds(targets)
        period = issue_period(issue, targets[0][0], tz)
        for category in ('all', period):
            key = (split, day, category)
            if key not in selected or (seconds, issue) > selected[key]:
                selected[key] = (seconds, issue)
    return selected


def _selected_day_axes(selected, eligible_axes, paired_axes, *, split, period):
    for (day_split, day, category), (seconds, issue) in sorted(selected.items()):
        if day_split == split and category == period:
            key = (split, day, issue)
            yield day, seconds, eligible_axes.get(key, ()), paired_axes.get(key, ())


def _day_metrics(axes, *, model, tz):
    totals, peaks, timings = [], [], []
    paired_seconds = eligible_seconds = planned_seconds = calendar_seconds = 0.0
    for day, seconds, eligible_targets, rows in axes:
        planned_seconds += seconds
        eligible_seconds += _axis_seconds(eligible_targets)
        paired_seconds += _axis_seconds(rows)
        start, end = local_day_bounds(date.fromisoformat(day), tz)
        calendar_seconds += (end-start).total_seconds()
        if not rows:
            continue
        totals.append((sum(row[2][model] for row in rows), sum(row[3] for row in rows)))
        # Convert interval energies to mean W so different target durations are
        # comparable. Tied peaks choose the earliest covered interval.
        power = sorted(
            (start, forecasts[model] * 3600 / (end-start).total_seconds(),
             actual * 3600 / (end-start).total_seconds())
            for start, end, forecasts, actual, _ in rows
        )
        forecast_peak, actual_peak = max(power, key=lambda r: r[1]), max(power, key=lambda r: r[2])
        if actual_peak[2] > 0:
            peaks.append(abs(forecast_peak[1] - actual_peak[2]))
            if forecast_peak[1] > 0:
                timings.append(abs((forecast_peak[0] - actual_peak[0]).total_seconds()) / 60)
    return {
        'covered_day_totals': metrics(totals),
        'day_coverage': {'paired_hours': paired_seconds/3600, 'eligible_hours': eligible_seconds/3600,
                         'planned_hours': planned_seconds/3600, 'calendar_hours': calendar_seconds/3600},
        'covered_day_peaks': {'n': len(peaks), 'mae_w': sum(peaks)/len(peaks) if peaks else None,
                              'timing_n': len(timings), 'timing_mae_minutes': sum(timings)/len(timings) if timings else None},
    }


def _interval_summary(values, paired_targets, *, status_breakdown=True):
    if not values:
        return None
    n = len(values)
    p50 = metrics([v[5] for v in values if v[5] is not None])
    summary = {
        'n': n, 'paired_targets': paired_targets, 'evaluated_fraction': n/paired_targets,
        'coverage': sum(v[0] for v in values)/n,
        'mean_width_wh': sum(v[1] for v in values)/n,
        'mean_interval_score': sum(v[2] for v in values)/n,
        'lower_misses': sum(v[3] for v in values), 'upper_misses': sum(v[4] for v in values),
        'zero_width_targets': sum(v[1] == 0.0 for v in values),
        'p50': p50, 'p50_status': 'unavailable' if p50['n'] == 0 else
                                  'complete' if p50['n'] == n else 'partial',
    }
    if status_breakdown:
        groups = defaultdict(list)
        for observation in values:
            groups[observation[6]].append(observation)
        summary['by_band_status'] = {
            status: _interval_summary(rows, paired_targets, status_breakdown=False)
            for status, rows in sorted(groups.items())
        }
    return summary


def _interval_observation(row, model, actual):
    interval = row.get('intervals', {}).get(model)
    if interval is None:
        return None
    if not isinstance(interval, list) or len(interval) not in (2, 3):
        raise ValueError('Expected P10/P90 or P10/P50/P90 uncertainty interval')
    ordered = [number(value) for value in interval]
    if any(value is None for value in ordered) or ordered != sorted(ordered):
        raise ValueError('Invalid uncertainty interval')
    low, high = ordered[0], ordered[-1]
    median = (ordered[1], actual) if len(ordered) == 3 else None
    status = row.get('band_status', {}).get(model)
    if status is None and model == 'served':
        status = row.get('regime', {}).get('band_learning_state')
    if status not in ('neutral', 'mixed', 'trained', 'ensemble'):
        status = 'unknown'
    return low <= actual <= high, high-low, interval_score(actual, low, high), actual < low, actual > high, median, status


def _previous_day_reference(row, issue, start, end, tz):
    reference = row.get('references', {}).get('previous_day')
    if reference is None:
        return None
    source_start = timestamp(reference['target_start'])
    source_end = timestamp(reference['target_end'])
    measured_available = timestamp(reference['available_at'])
    local_source, local_target = source_start.astimezone(tz), start.astimezone(tz)
    previous_day = local_target.date() - timedelta(days=1)
    _, previous_end = local_day_bounds(previous_day, tz)
    wh = number(reference.get('wh'))
    if (
        wh is None or not source_start < source_end <= measured_available <= issue
        or source_end > previous_end or source_end-source_start != end-start
        or local_source.date() != previous_day or local_source.time() != local_target.time()
    ):
        raise ValueError('Previous-day reference must match the local interval and be known at issue')
    return wh


def _phase_comparisons(planned_axes, eligible_axes, paired_axes, models, tz):
    """Compare issue periods on the same targets, including rolling issues."""
    planned_by_phase = defaultdict(lambda: defaultdict(dict))
    for (split, day, issue), targets in planned_axes.items():
        phase = issue_period(issue, targets[0][0], tz)
        by_target = planned_by_phase[(split, day)][phase]
        for target in targets:
            if target not in by_target or issue > by_target[target]:
                by_target[target] = issue
    eligible = {key: set(targets) for key, targets in eligible_axes.items()}
    paired = {key: {row[:2]: row for row in rows} for key, rows in paired_axes.items()}
    output = {}
    for split in ('development', 'selection', 'test'):
        period_days = {day: axes for (s, day), axes in planned_by_phase.items() if s == split}
        present = {period for axes in period_days.values() for period in axes}
        periods = [period for period in ('earlier', 'previous_evening', 'early_morning', 'intraday') if period in present]
        comparisons = []
        for phases in combinations(periods, 2):
            day_axes = {phase: [] for phase in phases}
            all_rows = {phase: [] for phase in phases}
            planned_n = eligible_n = paired_n = conflicting_n = overlapping_days = 0
            for day, phase_targets in sorted(period_days.items()):
                if any(phase not in phase_targets for phase in phases):
                    continue
                first, second = (phase_targets[phase] for phase in phases)
                planned = sorted(first.keys() & second.keys())
                if not planned:
                    continue
                try:
                    duration = _axis_seconds(planned)
                except ValueError:
                    # Valid issues may use different overlapping resolutions.
                    # A rolling phase cannot sum those into one energy curve.
                    overlapping_days += 1
                    continue
                planned_n += len(planned)
                eligible_targets, left, right = [], [], []
                for target in planned:
                    first_key, second_key = (split, day, first[target]), (split, day, second[target])
                    if target not in eligible.get(first_key, ()) or target not in eligible.get(second_key, ()):
                        continue
                    eligible_targets.append(target)
                    first_row = paired.get(first_key, {}).get(target)
                    second_row = paired.get(second_key, {}).get(target)
                    if first_row is None or second_row is None:
                        continue
                    if first_row[3] != second_row[3]:
                        conflicting_n += 1
                        continue
                    left.append(first_row)
                    right.append(second_row)
                eligible_n += len(eligible_targets)
                paired_n += len(left)
                for phase, rows in zip(phases, (left, right), strict=True):
                    day_axes[phase].append((day, duration, eligible_targets, rows))
                    all_rows[phase].extend(rows)
            summaries = {}
            for phase, other_phase in (phases, phases[::-1]):
                summaries[phase] = {}
                for model in models:
                    rows, intervals = all_rows[phase], []
                    for row, other in zip(rows, all_rows[other_phase], strict=True):
                        band, other_band = row[4][model], other[4][model]
                        if band is not None and other_band is not None:
                            if other_band[5] is None:
                                band = (*band[:5], None, band[6])
                            intervals.append(band)
                    summaries[phase][model] = {
                        **metrics([(row[2][model], row[3]) for row in rows]),
                        **_day_metrics(day_axes[phase], model=model, tz=tz),
                        'intervals': _interval_summary(intervals, len(rows)),
                    }
            comparisons.append({
                'periods': summaries, 'planned_targets': planned_n,
                'eligible_targets': eligible_n, 'paired_targets': paired_n,
                'conflicting_actual_targets': conflicting_n,
                'overlapping_target_days': overlapping_days,
            })
        output[split] = comparisons
    return output


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
    planned_axes = defaultdict(list)
    paired_axes = defaultdict(list)
    eligible_axes = defaultdict(list)
    availability = defaultdict(int)
    omitted = defaultdict(int)
    bands = defaultdict(list)
    eligible = defaultdict(int)
    stratified = defaultdict(list)
    stratified_bands = defaultdict(list)
    reference_pairs = defaultdict(list)
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
        local_day = start.astimezone(tz).date()
        _, local_end = local_day_bounds(local_day, tz)
        if end > local_end:
            raise ValueError('Target interval crosses its local calendar day boundary')
        day = local_day.isoformat()
        split = 'development' if day <= development_end else 'selection' if day <= selection_end else 'test'
        planned_axes[(split, day, issue)].append((start, end))
        previous_day = _previous_day_reference(row, issue, start, end, tz)
        actual = number(row.get('actual_wh'))
        if actual is None or row.get('quality', 'valid') != 'valid':
            omitted['invalid_or_missing_actual'] += 1
            continue
        eligible[split] += 1
        eligible_axes[(split, day, issue)].append((start, end))
        forecasts = {model: number(row.get('forecasts', {}).get(model)) for model in models}
        for model, forecast in forecasts.items():
            if forecast is not None:
                availability[(split, model)] += 1
        if any(value is None for value in forecasts.values()):
            omitted['unpaired_forecast'] += 1
            continue  # one common quality/availability mask for every ablation
        observations = {model: _interval_observation(row, model, actual) for model in models}
        paired_axes[(split, day, issue)].append((start, end, forecasts, actual, observations))
        baseline = forecasts[models[0]]
        axes = strata(row, issue=issue, start=start, tz=tz)
        if previous_day is not None:
            for period in ('all', axes['issue_period']):
                reference_pairs[(split, period)].append((previous_day, forecasts, actual))
        for model, forecast in forecasts.items():
            interval = observations[model]
            for axis, category in axes.items():
                key = (split, axis, category, model)
                stratified[key].append((forecast, actual))
                if interval is not None and axis in ('issue_period', 'lead_minutes'):
                    stratified_bands[key].append(interval)
            paired_days[(split, model)][day].append((abs(baseline-actual), abs(forecast-actual)))
            pairs[(split, model)].append((forecast, actual))
            if interval is not None:
                bands[(split, model)].append(interval)
    selected = _select_day_issues(planned_axes, tz)
    report = {'schema_version': 1, 'basis': bundle['basis'], 'timezone': bundle['timezone'],
              'ablation': 'frozen_application', 'omitted': dict(omitted), 'splits': {},
              'day_issue_selection': 'greatest planned target duration, then latest issue',
              'phase_issue_selection': 'latest planned issue per target and period'}
    for split in ('development', 'selection', 'test'):
        report['splits'][split] = {}
        periods = sorted({period for (s, _, period) in selected if s == split and period != 'all'})
        for model in models:
            values = pairs[(split, model)]
            daily = _day_metrics(
                _selected_day_axes(selected, eligible_axes, paired_axes, split=split, period='all'),
                model=model, tz=tz,
            )
            report['splits'][split][model] = {
                **metrics(values), **daily,
                'eligible_targets': eligible[split], 'available_targets': availability[(split, model)],
                'distinct_local_days': daily['covered_day_totals']['n'],
                'intervals': _interval_summary(bands[(split, model)], len(values)),
                'day_totals_by_issue_period': {
                    period: _day_metrics(
                        _selected_day_axes(selected, eligible_axes, paired_axes, split=split, period=period),
                        model=model, tz=tz,
                    )
                    for period in periods
                },
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
    for key, values in sorted(stratified.items()):
        split, axis, category, model = key
        result = metrics(values)
        if axis in ('issue_period', 'lead_minutes'):
            result['intervals'] = _interval_summary(stratified_bands[key], len(values))
        report['strata'].setdefault(split, {}).setdefault(axis, {}).setdefault(category, {})[model] = result
    if reference_pairs:
        report['previous_day_reference'] = {}
        for (split, period), rows in sorted(reference_pairs.items()):
            paired_targets = (
                len(pairs[(split, models[0])]) if period == 'all' else
                len(stratified[(split, 'issue_period', period, models[0])])
            )
            report['previous_day_reference'].setdefault(split, {})[period] = {
                'paired_targets': paired_targets,
                'reference_targets': len(rows),
                'reference': metrics([(previous, actual) for previous, _, actual in rows]),
                'models': {model: metrics([(forecasts[model], actual) for _, forecasts, actual in rows])
                           for model in models},
            }
    report['phase_comparisons'] = _phase_comparisons(
        planned_axes, eligible_axes, paired_axes, models, tz
    )
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


def validate_output_paths(manifest_path: Path, *outputs: Path | None) -> None:
    source = manifest_path.parent / json.loads(manifest_path.read_text(encoding='utf-8'))['input']['path']

    def aliases(left, right):
        return left.resolve() == right.resolve() or (
            left.exists() and right.exists() and left.samefile(right)
        )

    targets = [output for output in outputs if output is not None]
    for index, output in enumerate(targets):
        if any(aliases(output, item) for item in (manifest_path, source)):
            raise ValueError('Output cannot overwrite immutable inputs')
        if any(aliases(output, item) for item in targets[:index]):
            raise ValueError('JSON and Markdown outputs need different files')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--report', type=Path, help='Readable Markdown report')
    args = parser.parse_args()
    try:
        validate_output_paths(args.manifest, args.output, args.report)
    except ValueError as error:
        parser.error(str(error))
    report = compare(load_verified(args.manifest))
    report['evaluator_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
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
