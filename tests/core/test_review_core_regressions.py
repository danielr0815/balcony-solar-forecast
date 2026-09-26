"""Review regressions at the physical/learning boundary, using real physics."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from balcony_solar_forecast.const import (
    CLOUD_CLASS_CLEAR,
    DAY_PART_MIDDAY,
    QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN,
)
from balcony_solar_forecast.core import bootstrap_build as bb
from balcony_solar_forecast.core import engine, horizon, quantiles, shademap
from balcony_solar_forecast.core.types import (
    BiasCell,
    BiasState,
    HorizonRow,
    InverterGroup,
    PlaneConfig,
    QuantileBands,
    QuantileState,
    ShademapBin,
    ShademapState,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)

_START = datetime(2026, 6, 21, 11, tzinfo=UTC)


def _hour_and_slot(temp_c: float = 20.0):
    wx = bb.HourlyWeather(
        start=_START, ghi=800.0, dni=800.0, dhi=100.0, temp_c=temp_c,
    )
    # Equal solar midpoint isolates physics from the intended hourly vs 15-min
    # approximation. Energy for the modeled hour is the corresponding mean W.
    slot = WeatherSlot(
        start=_START + timedelta(minutes=22, seconds=30),
        ghi=wx.ghi, dni=wx.dni, dhi=wx.dhi, temp_c=wx.temp_c,
    )
    return wx, slot


def _plane(name: str, *, metered: bool = True) -> PlaneConfig:
    return PlaneConfig(
        name=name, azimuth_deg=180.0, tilt_deg=30.0, wp=400.0,
        actual_entity=f"sensor.{name.lower()}" if metered else None,
    )


def _seed_bias(wx, site, theta):
    r = bb.reconstruct_plane_hour(
        site.planes[0], 1.0, wx, latitude=site.latitude, longitude=site.longitude,
    )
    cloud = bb._classify_cloud(wx, UTC, elevation_deg=r.sun_el)
    part = bb._day_part_for_slot(wx.start, site.longitude)
    return {BiasState.cell_key(cloud, part): BiasCell(theta=theta, n=20)}


def test_bootstrap_quantiles_use_reclamped_metered_share():
    """Group clipping precedes the metered subset, even with a second group."""
    site = SiteConfig(
        latitude=51.0, longitude=10.0,
        planes=(_plane("P1"), _plane("P2", metered=False), _plane("P3")),
        groups=(
            InverterGroup("pair", ("P1", "P2"), 90.0, 0.9),
            InverterGroup("single", ("P3",), 90.0, 0.9),
        ),
    )
    wx, slot = _hour_and_slot()
    served = engine.compute_forecast(
        site, WeatherSeries((slot,)), slot.start,
        hooks=engine.LearnerHooks(slot_factor=lambda _at: 1.5),
    )
    actuals = {
        p.name: {_START.isoformat(): p.watts[0]}
        for p in served.plane_results if p.name != "P2"
    }
    assert sum(rows[_START.isoformat()] for rows in actuals.values()) == pytest.approx(150.0)
    acc = bb.BootstrapAccumulator(bias=_seed_bias(wx, site, 1.5))

    bb.process_day_hourly(acc, site, [wx], actuals, svf_by_plane={}, tz=UTC)

    ratios = [entry[1] for ring in acc.quantile_state.bins.values() for entry in ring]
    assert ratios == pytest.approx([1.0])


@pytest.mark.parametrize("temp_c,ross", [(-5.0, 0.02), (20.0, 0.056), (35.0, 0.08)])
def test_bootstrap_slow_curve_recomputes_learned_temperature(temp_c, ross):
    """The RAW label conversion cannot render a changed slow-layer POA."""
    plane = replace(
        _plane("P1"), ross_coeff=ross,
        horizon=(HorizonRow(0.0, 90.0, 0.2),),
    )
    site = SiteConfig(51.0, 10.0, (plane,), ())
    wx, slot = _hour_and_slot(temp_c)
    doy = slot.midpoint.timetuple().tm_yday
    r = bb.reconstruct_plane_hour(
        plane, horizon.sky_view_factor(plane, doy=doy), wx,
        latitude=site.latitude, longitude=site.longitude,
    )
    key = shademap.shademap_bin_key(r.sun_az, r.sun_el, doy)
    state = ShademapState(channels={plane.name: {key: ShademapBin(0.9, 100)}})

    def beam_tau(channel, az, el, day, prior):
        return shademap.effective_tau(
            state, channel=channel, sun_az=az, sun_el=el,
            doy=day, static_prior=prior,
        )

    served = engine.compute_forecast(
        site, WeatherSeries((slot,)), slot.start,
        hooks=engine.LearnerHooks(beam_tau=beam_tau),
    )
    acc = bb.BootstrapAccumulator(shade={plane.name: {key: [0.9, 100]}})
    bb.process_day_hourly(
        acc, site, [wx], {plane.name: {_START.isoformat(): served.total_watts[0]}},
        svf_by_plane={}, tz=UTC,
    )

    ratios = [entry[1] for ring in acc.quantile_state.bins.values() for entry in ring]
    assert ratios == pytest.approx([1.0], abs=1e-12)
    assert next(iter(acc.bias.values())).theta == pytest.approx(1.0, abs=1e-12)


def test_quantile_cap_counts_existing_samples_by_date_and_bin():
    key = QuantileState.bin_key(CLOUD_CLASS_CLEAR, DAY_PART_MIDDAY)
    sample = quantiles.QuantileSample(CLOUD_CLASS_CLEAR, DAY_PART_MIDDAY, 80.0, 100.0)
    state = QuantileState(bins={key: [["2026-06-20", 1.0], ["2026-06-21", 0.9]]})
    trained = quantiles.train_quantiles(
        state, [sample] * 12, training_date="2026-06-21",
    )
    assert len([e for e in trained.bins[key] if e[0] == "2026-06-21"]) == QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN
    assert trained.bins[key][0] == ["2026-06-20", 1.0]
    retrained = quantiles.train_quantiles(trained, [sample], training_date="2026-06-21")
    assert retrained.bins == trained.bins
    next_day = quantiles.train_quantiles(trained, [sample], training_date="2026-06-22")
    assert len(next_day.bins[key]) == len(trained.bins[key]) + 1
    assert state.bins[key] == [["2026-06-20", 1.0], ["2026-06-21", 0.9]]


@pytest.mark.parametrize("cloud,part", [("totally-invalid", DAY_PART_MIDDAY), (CLOUD_CLASS_CLEAR, "noon")])
def test_quantile_trainer_rejects_unknown_nonempty_taxonomy(cloud, part):
    trained = quantiles.train_quantiles(
        QuantileState(), [quantiles.QuantileSample(cloud, part, 80.0, 100.0)],
        training_date="2026-06-21",
    )
    assert trained.bins == {}


def test_ac_point_forecast_stays_distinct_from_empirical_median():
    """Naming the AC point correctly must not silently change the served curve."""
    site = SiteConfig(51.0, 10.0, (_plane("P"),), ())
    _wx, slot = _hour_and_slot()
    weather = WeatherSeries((slot,))
    base = engine.compute_forecast(site, weather, slot.start)
    band = QuantileBands(p10=0.5, p50=0.6, p90=0.7, n=40)
    result = engine.compute_forecast(
        site, weather, slot.start,
        hooks=engine.LearnerHooks(band_by_slot={slot.start: band}),
    )
    assert result.ac_watts == base.ac_watts
    assert result.ac_p10_watts[0] == pytest.approx(base.ac_watts[0] * band.p10)
    assert result.ac_p90_watts[0] == pytest.approx(base.ac_watts[0] * band.p90)
    assert result.ac_p90_watts[0] < result.ac_watts[0]
    assert result.p50_watts[0] == pytest.approx(result.total_watts[0] * band.p50)
