"""Offline leave-group-out shade eligibility; no production learner mutation."""
from __future__ import annotations

import importlib
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

from .replay_benchmark import timestamp


def _panel_core():
    name = '_bsf_shade_research'
    if name not in sys.modules:
        directory = Path(__file__).resolve().parents[2]/'custom_components'/'balcony_solar_forecast'
        package = ModuleType(name)
        package.__path__ = [str(directory)]
        sys.modules[name] = package
        core = ModuleType(name+'.core')
        core.__path__ = [str(directory/'core')]
        sys.modules[name+'.core'] = core
    return importlib.import_module(name+'.core.panel_weather')


def evaluate_shadow_gate(bundle: dict) -> dict:
    """Measure a deliberately conservative shadow gate against frozen baseline.

    This experiment retains only steady, diverse leave-group-out evidence.
    Ambiguous/common-damping frames are excluded from the geometric candidate,
    never from bias or quantile labels. False alarms require separately sourced
    shade references, not the panels used by the indicator. Future predictive
    effects need an independently trained learning replay, not this mask alone.
    """
    from zoneinfo import ZoneInfo

    if bundle.get('basis') != 'DC':
        raise ValueError('Panel shade evidence requires an explicit DC basis')
    core = _panel_core()
    tz = ZoneInfo(bundle['timezone'])
    counts, reasons = Counter(), Counter()
    days = {'baseline': set(), 'candidate': set()}
    alarms = {name: Counter() for name in days}
    seen = set()
    for row in bundle.get('shade_records', []):
        if row['id'] in seen:
            raise ValueError('Duplicate shade label identity')
        seen.add(row['id'])
        decision = timestamp(row['decision_at'])
        monitor = core.PanelWeatherMonitor()
        evidence = None
        previous = None
        for item in row['frames']:
            end, available = timestamp(item['end']), timestamp(item['available_at'])
            if not end <= available <= decision or previous is not None and end <= previous:
                raise ValueError('Panel frames must be ordered, closed and available before the decision')
            previous = end
            panels = tuple(core.PanelObservation(
                panel['source'], panel['group'], panel['orientation'], timestamp(panel['reported_at']),
                panel['watts'], panel['reference_watts'], panel.get('clipped', False),
                panel.get('eligible', True), azimuth_deg=panel.get('azimuth_deg'),
                tilt_deg=panel.get('tilt_deg')) for panel in item['panels'])
            if not any(p.source == row['target_source'] for p in panels):
                evidence = core.WeatherEvidence('unknown', 'target_unidentified')
                continue
            frame = core.PanelFrame(end, available, item['generation'], panels, item.get('forecast_class', 'unknown'))
            frame = core.without_target(frame, row['target_source'], whole_group=True)
            evidence = monitor.observe(frame, now=available)
        if previous is not None and (decision-previous).total_seconds() > 90:
            evidence = core.WeatherEvidence('unknown', 'stale_decision_frame')
        state = evidence.state if evidence else 'unknown'
        reasons[evidence.reason if evidence else 'no_frames'] += 1
        baseline = row.get('baseline_eligible') is True
        candidate = baseline and state == 'steady'
        counts['labels'] += 1
        counts['baseline_eligible'] += baseline
        counts['candidate_eligible'] += candidate
        day = decision.astimezone(tz).date().isoformat()
        for name, accepted in (('baseline', baseline), ('candidate', candidate)):
            if not accepted:
                continue
            days[name].add(day)
            if row.get('independent_reference_role') not in ('external_shade_reference', 'camera', 'synthetic_truth'):
                alarms[name]['unknown_reference'] += 1
                continue
            truth, alarm = row.get('independent_shade'), row.get('shadow_alarm')
            if not isinstance(truth, bool) or not isinstance(alarm, bool):
                alarms[name]['unknown_reference'] += 1
                continue
            alarms[name]['referenced_labels'] += 1
            if not truth:
                alarms[name]['unshaded_labels'] += 1
                alarms[name]['false_alarms'] += alarm
    return {'experiment': 'leave_group_out_shade_gate', 'basis': 'DC',
            'activation': 'never_automatic', 'predictive_improvement': 'not_measured',
            'bias_quantile_filter': 'unchanged', 'counts': dict(counts), 'reasons': dict(reasons),
            'models': {name: {'distinct_local_days': len(days[name]), **dict(alarms[name]),
                             'false_alarm_rate': (alarms[name]['false_alarms']/alarms[name]['unshaded_labels']
                                                  if alarms[name]['unshaded_labels'] else None)} for name in days}}
