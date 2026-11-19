"""Historical inputs keep their forecast lead, interval and measurement quality."""
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.balcony_solar_forecast import _bootstrap as ha
from custom_components.balcony_solar_forecast.core import bootstrap_build as core
from custom_components.balcony_solar_forecast.core import openmeteo_backfill as provider
from custom_components.balcony_solar_forecast.core.types import PlaneConfig, SiteConfig
from scripts import backfill as cli


def test_bootstrap_snow_and_visibility_use_issue_context_at_interval_start():
    hourly = {'time': ['2026-11-16T12:00', '2026-11-16T13:00', '2026-11-16T14:00']}
    for name in provider._RADIATION_VARS:
        hourly[name + '_previous_day1'] = [100, 120, 130]
    for name in provider._CONTEXT_VARS:
        hourly[name + '_previous_day1'] = [0, 2, 4]
        hourly[name] = [100, 101, 102]
    hourly['visibility_previous_day1'] = [20000, 300, 400]
    records = provider.parse_hourly_payload({'hourly': hourly}, var_suffix='_previous_day1')
    noon, one = records[-2:]
    assert (noon.start.hour, noon.ghi, noon.snow_depth_m, noon.visibility_m) == (12, 120, 0, 20000)
    assert (one.start.hour, one.ghi, one.snow_depth_m, one.visibility_m) == (13, 130, 2, 300)


@pytest.mark.parametrize('bad', [True, float('nan'), 'broken'])
@pytest.mark.parametrize('adapter', ['homeassistant', 'cli'])
def test_invalid_lts_row_quarantines_entire_day_but_zero_remains_measurable(bad, adapter):
    start = datetime(2026, 6, 21, 8, tzinfo=UTC)
    hours = [core.HourlyWeather(start + timedelta(hours=i), 750, 800, 100, 20) for i in range(8)]
    site = SiteConfig(52, 13, (PlaneConfig('A', 180, 30, 400, actual_entity='sensor.a'),), ())
    def used(value):
        rows = [{'start': h.start.timestamp(), 'mean': 160 + i*7} for i, h in enumerate(hours)]
        rows[1]['mean'] = value
        if adapter == 'homeassistant':
            actual = ha._reduce_stats({'sensor.a': rows}, {'A': 'sensor.a'})['A']
        else:
            buckets = {'sensor.a': {}}
            cli._parse_lts_result({'sensor.a': rows}, buckets)
            actual = buckets['sensor.a']
        return core.process_day_hourly(core.BootstrapAccumulator(), site, hours, {'A': actual},
                                       svf_by_plane={}, tz=UTC)
    assert used(0)
    assert not used(bad)


def test_export_provenance_only_counts_weather_from_contributing_days():
    start = datetime(2026, 6, 21, 8, tzinfo=UTC)
    site = SiteConfig(52, 13, (PlaneConfig('A', 180, 30, 400, actual_entity='sensor.a'),), ())
    weather = [core.HourlyWeather(start + timedelta(days=day, hours=hour), 750, 800, 100, 20,
                                  weather_source=source)
               for day, source in enumerate(('as_issued', 'analysis_fallback'))
               for hour in range(8)]
    for day, source in enumerate(('as_issued', 'analysis_fallback')):
        actuals = {'A': {wx.start.isoformat(): 160+i*7 for i, wx in enumerate(weather[8*day:8*day+8])}}
        acc = core.accumulate_days(site, weather, actuals, svf_by_plane={}, tz=UTC)
        assert acc.days_used == acc.days_skipped == 1
        provenance = core.build_bootstrap_json(acc, site)['weather_provenance']
        assert provenance['weather_source'] == source
        assert provenance['hours_by_weather_source'] == {source: 8}
