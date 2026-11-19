"""Plan completeness and prediction inputs remain independent of target outcomes."""
from datetime import UTC, datetime, timedelta

from scripts.validation.learning_replay import LearnerVariant, replay
from scripts.validation.replay_benchmark import compare


def test_missing_phases_do_not_qualify_a_thirty_day_candidate():
    records = []
    missing = {p: [] for p in ('previous_evening', 'early_morning', 'intraday')}
    for offset in range(30):
        day = datetime(2026, 9, 3, 9, tzinfo=UTC) + timedelta(days=offset)
        for h in range(12):
            start = day + timedelta(hours=h)
            for phase in missing:
                if phase != 'intraday' or h != 0:
                    missing[phase].append(start.isoformat())
        issue = (day - timedelta(minutes=15)).isoformat()
        end = (day + timedelta(hours=1)).isoformat()
        records.append(dict(issued_at=issue, available_at=issue, target_start=day.isoformat(),
                            target_end=end, actual_available_at=end, actual_wh=120,
                            forecasts={'raw': 80, 'served': 120}))
    report = compare(dict(schema_version=1, basis='DC', timezone='UTC',
                          development_end='2026-09-01', selection_end='2026-09-02',
                          models=['raw', 'served'], records=records, missing_forecast_captures=missing))
    assert report['test_comparisons']['served']['status'] == 'insufficient_planned_coverage'
    scores = report['splits']['test']['served']
    assert (scores['planned_targets'], scores['captured_targets'], scores['n']) == (1080, 30, 30)
    assert scores['complete_calendar_day_totals']['n'] == 0
    counts = report['capture_plan']['test']
    assert sum(v['planned_targets'] for v in counts.values()) == 1080
    assert counts['previous_evening']['captured_targets'] == 0
    assert counts['intraday'] == dict(planned_targets=360, captured_targets=30,
                                     paired_targets=30, complete_planned_days=0)
    assert all(pair['planned_targets'] == 360 and pair['paired_targets'] == 0
               for pair in report['phase_comparisons']['test'])
    assert len(report['phase_comparisons']['test']) == 3


def test_variants_cannot_read_observations_or_evaluation_only_fields():
    start = '2026-09-03T12:00Z'
    row = dict(issued_at=start, target_start=start, target_end='2026-09-03T13:00Z',
               point_wh=90, regime={'forecast_weather': 'clear', 'observed_weather': 'fog'},
               missing_modules=['A'], actual_available_at='2026-09-03T13:05Z')
    def predict(state, issue):
        assert issue['regime'] == {'forecast_weather': 'clear'}
        assert 'actual_available_at' not in issue and 'missing_modules' not in issue
        return issue['point_wh']
    variant = LearnerVariant({}, start, predict, lambda state, label: state)
    result = replay([row], [], {'test': variant})['records'][0]
    assert result['regime']['observed_weather'] == 'fog' and result['forecasts']['test'] == 90


def test_missing_phases_cannot_reverse_the_decision_on_complete_test_days(tmp_path, monkeypatch):
    import json
    import sys

    from scripts.validation import replay_benchmark as benchmark

    records = []
    missing = {phase: [] for phase in ('previous_evening', 'early_morning', 'intraday')}
    for day in range(90):
        start = datetime(2026, 9, 3, 12, tzinfo=UTC) + timedelta(days=day)
        end = (start + timedelta(hours=1)).isoformat()
        for phase, lead in [('previous_evening', 17), ('early_morning', 9), ('intraday', .25)]:
            if day >= 30 and phase != 'intraday':
                missing[phase].append(start.isoformat())
                continue
            issue = (start - timedelta(hours=lead)).isoformat()
            values = {'raw': 110, 'served': 120} if day < 30 else {'raw': 400, 'served': 100}
            records.append(dict(issued_at=issue, available_at=issue, target_start=start.isoformat(),
                                target_end=end, actual_available_at=end, actual_wh=100, forecasts=values))
    bundle = dict(schema_version=1, basis='DC', timezone='UTC', models=['raw', 'served'],
                  development_end='2026-09-01', selection_end='2026-09-02', records=records,
                  missing_forecast_captures=missing)
    decision = compare(bundle)['test_comparisons']['served']
    assert decision['status'] == 'worse'
    assert decision['distinct_local_days'] == 30
    assert decision['gain_ci95_wh'] == [-10, -10]

    from hashlib import sha256
    contents = json.dumps(bundle).encode()
    (tmp_path / 'bundle.json').write_bytes(contents)
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps(dict(input=dict(path='bundle.json', sha256=sha256(contents).hexdigest()),
                                       timezone='UTC', basis='DC', captured_at='2026-12-20T00:00Z',
                                       licence='synthetic', source_role='frozen_forecast_and_closed_actual')))
    report = tmp_path / 'report.md'
    monkeypatch.setattr(sys, 'argv', ['benchmark', str(manifest), '--report', str(report),
                                      '--output', str(tmp_path / 'result.json')])
    benchmark.main()
    assert '| test | previous_evening | 90 | 30 | 30 |' in report.read_text()
    assert '| test | intraday | 90 | 90 | 90 |' in report.read_text()
