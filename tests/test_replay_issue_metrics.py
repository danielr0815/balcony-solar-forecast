"""Issue selection, real engine bands and input immutability regression cases."""

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from scripts.validation import physical_learning_replay, replay_benchmark
from tests.helpers.physical_replay import data


def _record(issue, hour, *, forecast=120, actual=100):
    return {
        'issued_at': issue, 'available_at': issue,
        'target_start': f'2026-09-03T{hour:02}:00Z',
        'target_end': f'2026-09-03T{hour+1:02}:00Z',
        'actual_available_at': f'2026-09-03T{hour+1:02}:05Z',
        'actual_wh': actual, 'forecasts': {'raw': 100, 'candidate': forecast},
    }


def _bundle(records):
    return {'schema_version': 1, 'basis': 'DC', 'timezone': 'UTC',
            'development_end': '2026-09-01', 'selection_end': '2026-09-02',
            'models': ['raw', 'candidate'], 'records': records}


def test_two_issues_for_one_interval_are_two_errors_but_only_one_day_total():
    records = [
        _record('2026-09-02T20:00Z', 10),
        _record('2026-09-03T09:00Z', 10),
    ]
    report = replay_benchmark.compare(_bundle(records))['splits']['test']['candidate']
    assert report['n'] == 2
    assert report['covered_day_totals']['n'] == 1
    assert report['covered_day_totals']['mae_wh'] == 20
    assert report['day_coverage'] == {
        'planned_hours': 1, 'eligible_hours': 1, 'paired_hours': 1, 'calendar_hours': 24,
    }
    for period in ('previous_evening', 'intraday'):
        assert report['day_totals_by_issue_period'][period]['covered_day_totals']['mae_wh'] == 20


def test_daily_selection_uses_planned_axis_even_if_other_issue_has_more_usable_values():
    records = [
        _record('2026-09-03T04:00Z', 9, forecast=130, actual=150),
        _record('2026-09-03T04:00Z', 10, forecast=None),
        _record('2026-09-03T04:00Z', 11, actual=None),
        _record('2026-09-02T20:00Z', 9),
        _record('2026-09-02T20:00Z', 10),
    ]
    report = replay_benchmark.compare(_bundle(records))['splits']['test']['candidate']
    assert report['n'] == 3
    assert report['covered_day_totals']['bias_wh'] == -20
    assert report['day_coverage'] == {
        'planned_hours': 3, 'eligible_hours': 2, 'paired_hours': 1, 'calendar_hours': 24,
    }
    assert report['covered_day_peaks'] == {
        'n': 1, 'mae_w': 20, 'timing_n': 1, 'timing_mae_minutes': 0,
    }
    reversed_report = replay_benchmark.compare(_bundle(list(reversed(records))))
    assert reversed_report['splits']['test']['candidate'] == report


def test_overlapping_intervals_at_the_same_issue_cannot_enter_daily_energy():
    row = _record('2026-09-02T20:00Z', 9)
    short = dict(row, target_start='2026-09-03T09:30Z')
    with pytest.raises(ValueError, match='non-overlapping target intervals'):
        replay_benchmark.compare(_bundle([row, short]))


def _physical_bundle():
    bundle = data(tuple(f'2026-09-{day:02}' for day in range(3, 19)))
    bundle['development_end'] = '2026-09-01'
    bundle['selection_end'] = '2026-09-02'
    bundle['variants'] = ['raw', 'full']
    return bundle


def test_physical_replay_scores_issued_hourly_band_triplets_on_matched_dc_targets():
    bundle = _physical_bundle()
    replay = physical_learning_replay.replay_physical_days(bundle)
    scored = physical_learning_replay.score_physical_replay(bundle, replay)
    stats = scored['splits']['test']['full']['intervals']
    assert stats is not None
    assert stats['mean_width_wh'] > 0
    assert scored['splits']['test']['raw']['intervals'] is None
    assert scored['interval_scope']['full'] == 'hourly_site_dc_sum_of_15min_bounds'
    assert scored['interval_evaluation'] == {
        'basis': 'DC', 'target_minutes': 60, 'source_minutes': 15,
        'bound_aggregation': 'sum_of_15min_bounds',
    }
    intervals = []
    measured = {row['local_day']: row['actuals_hourly']['P'] for row in bundle['days']}
    for row in replay['records']:
        assert row['forecasts']['full']['issued_at'] == row['issued_at']
        hourly = row['dc_hourly_bands'].get('full')
        if hourly:
            slot_bands = row['forecasts']['full']['bands']
            assert len(slot_bands['dc_p50_slot_wh']) == 96
            for hour, p10 in hourly['p10'].items():
                start = datetime.fromisoformat(hour)
                slots = [(start + timedelta(minutes=15*i)).isoformat() for i in range(4)]
                for name in ('p10', 'p50', 'p90'):
                    assert hourly[name][hour] == pytest.approx(
                        sum(slot_bands[f'dc_{name}_slot_wh'][slot] for slot in slots)
                    )
                actual = measured[row['local_day']][hour]
                intervals.append((actual, p10, hourly['p50'][hour], hourly['p90'][hour]))
    assert stats['n'] == len(intervals)
    assert stats['coverage'] == pytest.approx(sum(lo <= actual <= hi for actual, lo, _, hi in intervals) / len(intervals))
    assert stats['p50']['mae_wh'] == pytest.approx(sum(abs(p50 - actual) for actual, _, p50, _ in intervals) / len(intervals))
    assert stats['evaluated_fraction'] == pytest.approx(len(intervals) / scored['splits']['test']['full']['n'])


def test_partial_metering_scores_exact_metered_point_curve_but_no_site_interval():
    bundle = _physical_bundle()
    partial = deepcopy(bundle['site'])
    partial['planes'].append({'name':'extra','azimuth_deg':180,'tilt_deg':30,'wp':400})
    bundle['variants'] = {'raw': {'preset':'raw'}, 'full': {'preset':'full','site':partial}}
    replay = physical_learning_replay.replay_physical_days(bundle)
    first = replay['records'][0]['forecasts']
    assert sum(first['full']['corrected_hourly_wh'].values()) > sum(first['raw']['corrected_hourly_wh'].values())
    # Score the first issued day alone, with neutral learner point curves.
    # The additional unmetered plane must not appear in either error series.
    first_only = dict(replay, records=replay['records'][:1])
    scored = physical_learning_replay.score_physical_replay(bundle, first_only)
    assert scored['splits']['test']['full']['mae_wh'] == pytest.approx(scored['splits']['test']['raw']['mae_wh'], abs=.01)
    scored = physical_learning_replay.score_physical_replay(bundle, replay)
    assert any(row['dc_hourly_bands'].get('full') for row in replay['records'])
    assert scored['splits']['test']['full']['intervals'] is None
    assert scored['interval_scope']['full'] == 'unavailable_for_metered_subset'


@pytest.mark.parametrize("cli,option", [
    (physical_learning_replay, '--output'),
    (replay_benchmark, '--report'),
])
def test_cli_preserves_immutable_input_even_via_hardlink(tmp_path, monkeypatch, cli, option):
    bundle = (
        data(('2026-09-03',)) if cli is physical_learning_replay else
        _bundle([_record('2026-09-02T20:00Z', 9)])
    )
    source = tmp_path/'data.json'
    original = json.dumps(bundle).encode()
    source.write_bytes(original)
    manifest = tmp_path/'manifest.json'
    manifest.write_text(json.dumps({
        'basis': 'DC', 'timezone': 'UTC', 'captured_at': '2026-09-04T00:30Z',
        'licence': 'CC0 synthetic', 'source_role': 'frozen_forecast_and_closed_actual',
        'input': {'path': source.name, 'sha256': hashlib.sha256(original).hexdigest()},
    }))
    alias = tmp_path/'output'
    alias.hardlink_to(source)
    monkeypatch.setattr('sys.argv', ['replay', str(manifest), option, str(alias)])
    with pytest.raises(SystemExit):
        cli.main()
    assert source.read_bytes() == original


@pytest.mark.parametrize("timezone,start,end", [
    ('UTC', '2026-09-03T23:30Z', '2026-09-04T01:00Z'),
    ('Asia/Kathmandu', '2026-09-03T18:00Z', '2026-09-03T19:00Z'),
])
def test_target_interval_must_belong_to_one_local_day(timezone, start, end):
    row = _record('2026-09-02T20:00Z', 9)
    row.update(target_start=start, target_end=end, actual_available_at=end)
    bundle = _bundle([row])
    bundle['timezone'] = timezone
    with pytest.raises(ValueError, match='local calendar day boundary'):
        replay_benchmark.compare(bundle)


def test_dst_fallback_keeps_both_repeated_hours_on_the_same_local_day():
    rows = []
    for start, end in [('00:00', '01:00'), ('01:00', '02:00')]:
        row = _record('2026-10-24T20:00Z', 9)
        row.update(target_start=f'2026-10-25T{start}Z', target_end=f'2026-10-25T{end}Z',
                   actual_available_at=f'2026-10-25T{end}Z')
        rows.append(row)
    bundle = _bundle(rows)
    bundle['timezone'] = 'Europe/Berlin'
    result = replay_benchmark.compare(bundle)['splits']['test']['candidate']
    assert result['covered_day_totals']['mae_wh'] == 40
    assert result['day_coverage']['calendar_hours'] == 25
    assert result['day_coverage']['paired_hours'] == 2


def test_phase_band_metrics_report_width_coverage_status_and_p50_separately():
    evening = _record('2026-09-02T20:00Z', 10)
    evening['intervals'] = {'candidate': [90, 100, 110]}
    evening['band_status'] = {'candidate': 'trained'}
    intraday = _record('2026-09-03T09:45Z', 10)
    intraday['intervals'] = {'candidate': [120, 120]}
    intraday['band_status'] = {'candidate': 'neutral'}
    report = replay_benchmark.compare(_bundle([evening, intraday]))
    strata = report['strata']['test']
    by_period = strata['issue_period']
    stats = by_period['previous_evening']['candidate']['intervals']
    assert (stats['coverage'], stats['mean_width_wh']) == (1, 20)
    assert stats['p50_status'] == 'complete'
    assert stats['by_band_status']['trained']['p50']['mae_wh'] == 0
    stats = by_period['intraday']['candidate']['intervals']
    assert (stats['coverage'], stats['mean_width_wh']) == (0, 0)
    assert stats['p50_status'] == 'unavailable'
    assert stats['by_band_status']['neutral']['upper_misses'] == 0
    assert stats['by_band_status']['neutral']['lower_misses'] == 1
    assert strata['lead_minutes']['0_to_30']['candidate']['intervals'] == stats


def test_phase_pair_uses_matching_targets_for_daily_peaks_points_and_bands():
    evening_at = '2026-09-02T20:00Z'
    morning_at = '2026-09-03T04:00Z'
    shared_evening = _record(evening_at, 10, forecast=120)
    shared_morning = _record(morning_at, 10, forecast=90)
    shared_evening['intervals'] = {'candidate': [90, 100, 130]}
    shared_morning['intervals'] = {'candidate': [80, 90, 95]}
    records = [_record(evening_at, 9, forecast=1000), shared_evening,
               shared_morning, _record(morning_at, 11, forecast=700)]
    phase, = replay_benchmark.compare(_bundle(records))['phase_comparisons']['test']
    assert (phase['planned_targets'], phase['eligible_targets'], phase['paired_targets']) == (1, 1, 1)
    for name, mae, bias, coverage in [('previous_evening', 20, 20, 1), ('early_morning', 10, -10, 0)]:
        result = phase['periods'][name]['candidate']
        assert result['n'] == 1
        assert result['mae_wh'] == mae
        assert result['covered_day_totals']['bias_wh'] == bias
        assert result['covered_day_peaks']['mae_w'] == mae
        assert result['intervals']['coverage'] == coverage
        assert result['day_coverage']['paired_hours'] == 1

    shared_morning['actual_wh'] = 95
    phase, = replay_benchmark.compare(_bundle(records))['phase_comparisons']['test']
    assert phase['paired_targets'] == 0
    assert phase['conflicting_actual_targets'] == 1


def test_physical_replay_rejects_duplicate_day_even_with_different_iso_spelling():
    bundle = data(('2026-09-03',))
    original = bundle['days'][0]
    duplicate = deepcopy(original)
    duplicate['local_day'] = '20260903'
    duplicate['issued_at'] = '2026-09-02T23:30Z'
    bundle['days'].append(duplicate)
    with pytest.raises(ValueError, match='Duplicate local day identity'):
        physical_learning_replay.replay_physical_days(bundle)


def test_physical_replay_excludes_hour_that_spills_past_fractional_local_midnight():
    from datetime import UTC

    bundle = data(('2026-09-03', '2026-09-04'), 'Asia/Kathmandu')
    for row in bundle['days']:
        date_id = row['local_day']
        labels = {
            datetime.fromisoformat(date_id).replace(hour=hour, tzinfo=UTC).isoformat():
                140 + hour if 6 <= hour < 18 else 0
            for hour in range(19)
        }
        # UTC 18:00–19:00 crosses midnight at 18:15Z and is not a target
        # of this local day. Its implausible value must not quarantine the day.
        labels[f'{date_id}T18:00:00+00:00'] = 5000
        row['actuals_hourly'] = {'P': labels}
    bundle['days'][0]['actuals_available_at'] = '2026-09-03T18:15Z'
    bundle['days'][1]['issued_at'] = '2026-09-03T18:15Z'
    result = physical_learning_replay.replay_physical_days(bundle)
    assert result['trained_days']['full'] == ['2026-09-03']
    assert result['skipped_days'] == {}


def test_previous_day_reference_is_causal_and_scores_only_same_paired_targets():
    matched = _record('2026-09-03T04:00Z', 10, forecast=110)
    missing = _record('2026-09-03T04:00Z', 11, forecast=160)
    previous = {'target_start': '2026-09-02T10:00Z', 'target_end': '2026-09-02T11:00Z',
                'available_at': '2026-09-03T03:00Z', 'wh': 75}
    matched['references'] = {'previous_day': previous}
    report = replay_benchmark.compare(_bundle([matched, missing]))
    assert report['splits']['test']['candidate']['mae_wh'] == 35
    reference = report['previous_day_reference']['test']['early_morning']
    assert (reference['paired_targets'], reference['reference_targets']) == (2, 1)
    assert reference['reference']['mae_wh'] == 25
    assert reference['models']['candidate']['mae_wh'] == 10
    previous['available_at'] = '2026-09-03T04:01Z'
    with pytest.raises(ValueError, match='known at issue'):
        replay_benchmark.compare(_bundle([matched, missing]))


def test_phase_pair_keeps_every_target_from_separate_intraday_issues():
    records = [
        _record('2026-09-02T20:00Z', hour, forecast=forecast)
        for hour, forecast in [(9, 140), (10, 160), (11, 180)]
    ]
    records.extend(
        _record(f'2026-09-03T{hour-1:02}:45Z', hour, forecast=forecast)
        for hour, forecast in [(9, 105), (10, 95), (11, 110)]
    )
    report = replay_benchmark.compare(_bundle(records))
    phase, = report['phase_comparisons']['test']
    assert (phase['planned_targets'], phase['eligible_targets'], phase['paired_targets']) == (3, 3, 3)
    rolling = phase['periods']['intraday']['candidate']
    assert rolling['n'] == 3
    assert rolling['covered_day_totals']['bias_wh'] == 10
    assert rolling['covered_day_peaks']['mae_w'] == 10
    assert rolling['day_coverage']['paired_hours'] == 3
    single = report['splits']['test']['candidate']['day_totals_by_issue_period']['intraday']
    assert single['day_coverage']['paired_hours'] == 1

    incomplete_latest = _record('2026-09-03T08:55Z', 9, forecast=None)
    phase, = replay_benchmark.compare(_bundle([*records, incomplete_latest]))['phase_comparisons']['test']
    assert (phase['planned_targets'], phase['eligible_targets'], phase['paired_targets']) == (3, 3, 2)


def test_phase_pair_does_not_sum_overlapping_targets_from_separate_issues():
    records = []
    for earlier, later in [('2026-09-02T18:00Z', '2026-09-02T20:00Z'),
                           ('2026-09-03T08:45Z', '2026-09-03T09:15Z')]:
        records.append(_record(earlier, 9))
        shifted = _record(later, 9)
        shifted.update(target_start='2026-09-03T09:30Z', target_end='2026-09-03T10:30Z',
                       actual_available_at='2026-09-03T10:35Z')
        records.append(shifted)
    report = replay_benchmark.compare(_bundle(records))
    phase, = report['phase_comparisons']['test']
    assert phase['overlapping_target_days'] == 1
    assert phase['paired_targets'] == 0
    assert report['splits']['test']['candidate']['n'] == 4
    assert report['splits']['test']['candidate']['day_coverage']['paired_hours'] == 1
