"""Independent regression cases from the October review, no live data."""
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from balcony_solar_forecast.core import bootstrap_build as bootstrap
from balcony_solar_forecast.core import horizon, openmeteo_backfill
from balcony_solar_forecast.core.types import HorizonRow, PlaneConfig, SiteConfig


@pytest.mark.parametrize("suffix", ["", "_previous_day1"])
def test_provider_end_stamp_joins_preceding_recorder_hour(suffix):
    payload = {"hourly": {"time": ["2026-10-01T00:00"],
                           **{key + suffix: [value] for key, value in {
                               "shortwave_radiation": 500,
                               "direct_normal_irradiance": 700,
                               "diffuse_radiation": 100, "temperature_2m": 20,
                           }.items()}}}
    record = openmeteo_backfill.parse_hourly_payload(payload, var_suffix=suffix)[0]
    assert record.start == datetime(2026, 9, 30, 23, tzinfo=UTC)


@pytest.mark.parametrize("value", [0.0, 200.0, -1.0, float("nan")])
def test_rejected_bootstrap_day_mutates_no_learner(value):
    plane = PlaneConfig("P", 180, 30, 400, actual_entity="sensor.p")
    site = SiteConfig(51, 10, (plane,), ())
    hours = [bootstrap.HourlyWeather(datetime(2026, 6, 21, h, tzinfo=UTC),
                                    800, 850, 100, 20) for h in range(8, 16)]
    acc = bootstrap.BootstrapAccumulator()
    before = bootstrap.build_bootstrap_json(acc, site, generated_at=hours[0].start)
    used = bootstrap.process_day_hourly(
        acc, site, hours, {"P": {w.start.isoformat(): value for w in hours}},
        svf_by_plane={}, tz=UTC,
    )
    assert not used
    assert bootstrap.build_bootstrap_json(acc, site, generated_at=hours[0].start) == before


def test_bootstrap_evidence_uses_one_local_day_across_utc_midnight():
    plane = PlaneConfig("P", 180, 30, 400, actual_entity="sensor.p")
    site = SiteConfig(34, -118, (plane,), ())
    hours = [bootstrap.HourlyWeather(t, 500, 600, 100, 20) for t in (
        datetime(2026, 6, 21, 20, tzinfo=UTC), datetime(2026, 6, 22, 2, tzinfo=UTC))]
    acc = bootstrap.accumulate_days(
        site, hours, {"P": {hours[0].start.isoformat(): 250,
                            hours[1].start.isoformat(): 100}},
        svf_by_plane={}, tz=ZoneInfo("America/Los_Angeles"),
    )
    assert acc.days_used == 1
    assert acc.last_iso_date == "2026-06-21"


def test_more_rear_sky_transparency_cannot_reduce_visible_diffuse():
    els = (0, 50, 55, 60, 65, 70, 75, 80, 90)
    rows = tuple(HorizonRow(float(az), 90, .1,
                           tau_points=tuple((float(el), .1) for el in els))
                 for az in range(0, 360, 10))
    plane = PlaneConfig("P", 0, 70, 400, horizon=rows)
    opened = tuple(replace(row, tau_points=tuple(
        (float(el), .9 if el == 60 and 160 <= row.azimuth_deg <= 200 else .1)
        for el in els)) for row in rows)
    assert horizon.sky_view_factor(replace(plane, horizon=opened)) >= horizon.sky_view_factor(plane)


def test_constant_thirty_degree_horizon_matches_positive_ray_integral():
    # Independent hemispherical quadrature integrates only incoming rays.
    import math
    tilt = math.radians(30)
    full = observed = 0.0
    for az in range(360):
        for el in range(90):
            angle = math.radians(el + .5)
            incoming = max(0, math.sin(tilt) * math.cos(math.radians(az + .5))
                           * math.cos(angle) + math.cos(tilt) * math.sin(angle))
            weight = incoming * math.cos(angle)
            full += weight
            if el >= 30:
                observed += weight
    plane = PlaneConfig("P", 0, 30, 400, horizon=(HorizonRow(0, 30, 0),))
    assert horizon.sky_view_factor(plane) == pytest.approx(observed / full, abs=3e-5)


@pytest.mark.parametrize(('day', 'count'), [('2026-03-29', 23), ('2026-10-25', 25)])
def test_local_dst_days_keep_every_distinct_utc_hour(day, count):
    from datetime import timedelta
    tz = ZoneInfo('Europe/Berlin')
    midnight = datetime.fromisoformat(day).replace(tzinfo=tz)
    end = (midnight + timedelta(days=1)).astimezone(UTC)
    t = midnight.astimezone(UTC)
    weather = []
    while t < end:
        weather.append(bootstrap.HourlyWeather(t, 0, 0, 0, 20))
        t += timedelta(hours=1)
    grouped = bootstrap._group_by_day(weather, tz)
    assert list(grouped) == [day]
    assert len(grouped[day]) == count
    actuals = {'P': {w.start.isoformat(): 0 for w in weather}}
    assert len(bootstrap._filter_actuals_for_day(actuals, day, tz)['P']) == count


def test_auckland_day_does_not_lose_utc_previous_evening():
    t = datetime(2026, 6, 20, 22, tzinfo=UTC)
    weather = [bootstrap.HourlyWeather(t, 500, 600, 100, 20)]
    tz = ZoneInfo('Pacific/Auckland')
    assert list(bootstrap._group_by_day(weather, tz)) == ['2026-06-21']
    assert bootstrap._filter_actuals_for_day({'P': {t.isoformat(): 100}}, '2026-06-21', tz)


def test_healthy_weather_error_remains_a_learnable_label():
    plane = PlaneConfig('P', 180, 30, 400, actual_entity='sensor.p')
    site = SiteConfig(51, 10, (plane,), ())
    hours = [bootstrap.HourlyWeather(datetime(2026, 6, 21, h, tzinfo=UTC),
                                    800, 850, 100, 20) for h in range(8, 16)]
    acc = bootstrap.BootstrapAccumulator()
    assert bootstrap.process_day_hourly(
        acc, site, hours, {'P': {w.start.isoformat(): 100 + i for i, w in enumerate(hours)}},
        svf_by_plane={}, tz=UTC,
    )
    assert acc.bias_samples > 0 and acc.quantile_samples > 0


def test_repeated_accepted_bootstrap_day_is_a_noop():
    plane = PlaneConfig('P', 180, 30, 400, actual_entity='sensor.p')
    site = SiteConfig(51, 10, (plane,), ())
    weather = [bootstrap.HourlyWeather(datetime(2026, 6, 21, h, tzinfo=UTC),
                                       700, 750, 100, 20) for h in range(8, 16)]
    actual = {'P': {wx.start.isoformat(): 150+i for i, wx in enumerate(weather)}}
    acc = bootstrap.BootstrapAccumulator()
    assert bootstrap.process_day_hourly(acc, site, weather, actual, svf_by_plane={}, tz=UTC)
    before = bootstrap.build_bootstrap_json(acc, site, generated_at=weather[0].start)
    assert not bootstrap.process_day_hourly(acc, site, weather, actual, svf_by_plane={}, tz=UTC)
    assert bootstrap.build_bootstrap_json(acc, site, generated_at=weather[0].start) == before


def test_missing_daylight_bootstrap_labels_quarantine_whole_day():
    plane = PlaneConfig('P', 180, 30, 400, actual_entity='sensor.p')
    site = SiteConfig(51, 10, (plane,), ())
    weather = [bootstrap.HourlyWeather(datetime(2026, 6, 21, h, tzinfo=UTC),
                                       700, 750, 100, 20) for h in range(8, 16)]
    actual = {'P': {wx.start.isoformat(): 150+i for i, wx in enumerate(weather[:5])}}
    acc = bootstrap.BootstrapAccumulator()
    before = bootstrap.build_bootstrap_json(acc, site, generated_at=weather[0].start)
    assert not bootstrap.process_day_hourly(acc, site, weather, actual, svf_by_plane={}, tz=UTC)
    assert bootstrap.build_bootstrap_json(acc, site, generated_at=weather[0].start) == before
