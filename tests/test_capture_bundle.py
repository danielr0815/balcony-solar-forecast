"""Hashed operator captures retain renamed roles and local calendar boundaries."""
import hashlib
import json
from datetime import UTC, date, datetime

import pytest

from scripts.validation.bsf_data import SID_ACT_DC, SID_SCALAR, load_bundle


def test_renamed_capture_uses_site_timezone_and_rejects_changed_bytes(tmp_path):
    at = datetime(2026, 9, 3, 23, tzinfo=UTC)
    files = {
        'actuals_hourly_stats.json': {'stats': {'sensor.renamed_port_total': [{'start': at.timestamp()*1000, 'mean': 125}]}},
        'fiveminute_stats.json': {'stats': {'sensor.renamed_scalar': [{'start': at.timestamp()*1000, 'mean': .8}]}},
    }
    hashes = {}
    for name, payload in files.items():
        raw = json.dumps(payload).encode()
        (tmp_path/name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    manifest = {'timezone': 'Asia/Tokyo', 'files_sha256': hashes,
                'roles': {'measured_dc_power_total': 'sensor.renamed_port_total',
                          'intraday_scalar': 'sensor.renamed_scalar'}}
    (tmp_path/'capture_manifest.json').write_text(json.dumps(manifest))
    result = load_bundle(str(tmp_path))
    assert result.days == [date(2026, 9, 4)]
    assert result.day_sum_wh(SID_ACT_DC, date(2026, 9, 4)) == 125
    assert result.day_sum_wh(SID_ACT_DC, date(2026, 9, 3)) == 0
    assert result.five[SID_SCALAR] == [(at, .8)]
    assert result.features['capture_entry_scoped']
    (tmp_path/'actuals_hourly_stats.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='hash'):
        load_bundle(str(tmp_path))


@pytest.mark.parametrize("bad", [True, False, "NaN", "Infinity", "", -1])
def test_corrupt_statistic_is_not_a_measurement(tmp_path, bad):
    payload = {'stats': {SID_ACT_DC: [{'start': '2026-09-04T10:00:00+00:00', 'mean': bad}]}}
    # Recorder uses epoch milliseconds in both supported statistic resolutions.
    payload['stats'][SID_ACT_DC][0]['start'] = 1788516000000
    for name in ['actuals_hourly_stats.json', 'fiveminute_stats.json']:
        (tmp_path/name).write_text(json.dumps(payload))
    result = load_bundle(str(tmp_path))
    assert not result.hourly.get(SID_ACT_DC)
    assert not result.five.get(SID_ACT_DC)


@pytest.mark.parametrize('day,hours', [(date(2026,3,29),23), (date(2026,10,25),25)])
def test_calendar_coverage_includes_dst_night_hours(day, hours):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from scripts.validation.bsf_data import Bundle, _derive
    tz = ZoneInfo('Europe/Berlin')
    start = datetime.combine(day, datetime.min.time(), tz).astimezone(UTC)
    series = {start + timedelta(hours=i): 0.0 for i in range(hours)}
    result = Bundle(data_dir='', timezone=tz, hourly={SID_ACT_DC:series})
    _derive(result)
    assert not result.partial_days
    series.pop(start)
    _derive(result)
    assert result.partial_days == {day}
