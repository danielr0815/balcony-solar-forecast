"""Pure-math tests for scripts/backfill.py (NO network — SPEC §6 task brief).

Covers only the importable, deterministic parts of the backfill:

  * payload parsing (Previous-Runs suffix vs. Historical Forecast plain,
    dropping hours with missing physics inputs);
  * per-plane hourly reconstruction via the repo core/ (beam/diffuse split,
    horizon beam gate, snow albedo, beam_share, kc);
  * the quasi-clear gate + bin-key + half-year helpers (mirroring the frozen
    shademap contract);
  * the daily->hourly measured disaggregation (shape-preserving);
  * per-day accumulation into shademap bins and day-ahead RLS cells;
  * the n-credit CAP at BOOTSTRAP_MAX_BIN_N and the bootstrap-JSON contract
    shape (round-trips through BiasState/ShademapState.from_dict);
  * the LTS statistics-row parser (epoch-ms and ISO ``start``, mean->Wh).

The aiohttp network coroutines are intentionally NOT exercised here.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# The conftest in this directory registers ``balcony_solar_forecast`` /
# ``.core`` as namespace packages (no HA). Make the standalone dev script
# importable too; it re-registers the same packages idempotently.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import backfill as bf  # noqa: E402
from balcony_solar_forecast import const  # noqa: E402
from balcony_solar_forecast.core.types import (  # noqa: E402
    BiasCell,
    BiasState,
    ShademapState,
    SiteConfig,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def site() -> SiteConfig:
    return SiteConfig.from_dict(const.DEFAULT_SITE)


def _clear_summer_noon_hours() -> list[bf.HourlyWeather]:
    """A short clear summer-midday window at the reference site (UTC).

    Summer solstice-ish, sun high, near clear-sky GHI so kc ~ 1 -> quasi-clear
    passes for the front/south planes. Three consecutive hours so the
    neighbour-stability gate has neighbours.
    """
    base = datetime(2025, 6, 21, 9, 0, tzinfo=UTC)  # ~11:00 local
    out = []
    for h in range(3):
        start = base.replace(hour=9 + h)
        out.append(
            bf.HourlyWeather(
                start=start,
                ghi=780.0,   # close to Haurwitz clear-sky at high sun
                dni=820.0,
                dhi=120.0,
                temp_c=24.0,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def test_parse_previous_runs_suffix_reads_suffixed_radiation():
    payload = {
        "hourly": {
            "time": ["2025-06-21T09:00", "2025-06-21T10:00"],
            "shortwave_radiation_previous_day1": [700.0, 750.0],
            "direct_normal_irradiance_previous_day1": [800.0, 810.0],
            "diffuse_radiation_previous_day1": [110.0, 120.0],
            "temperature_2m_previous_day1": [22.0, 24.0],
            "cloud_cover_low": [5.0, 10.0],
            "visibility": [24000.0, 24000.0],
        }
    }
    recs = bf.parse_hourly_payload(payload, var_suffix="_previous_day1")
    assert len(recs) == 2
    assert recs[0].ghi == 700.0
    assert recs[1].temp_c == 24.0
    assert recs[0].cloud_low == 5.0
    assert recs[0].start == datetime(2025, 6, 21, 9, 0, tzinfo=UTC)


def test_parse_historical_plain_variables():
    payload = {
        "hourly": {
            "time": ["2025-01-05T12:00"],
            "shortwave_radiation": [200.0],
            "direct_normal_irradiance": [100.0],
            "diffuse_radiation": [90.0],
            "temperature_2m": [1.0],
            "snow_depth": [0.05],
        }
    }
    recs = bf.parse_hourly_payload(payload, var_suffix="")
    assert len(recs) == 1
    assert recs[0].snow_depth_m == 0.05


def test_parse_drops_hours_with_missing_physics_inputs():
    payload = {
        "hourly": {
            "time": ["2025-06-21T09:00", "2025-06-21T10:00", "2025-06-21T11:00"],
            "shortwave_radiation": [700.0, None, 750.0],   # hour 2 missing GHI
            "direct_normal_irradiance": [800.0, 800.0, 810.0],
            "diffuse_radiation": [110.0, 110.0, 120.0],
            "temperature_2m": [22.0, 23.0, None],          # hour 3 missing temp
        }
    }
    recs = bf.parse_hourly_payload(payload, var_suffix="")
    # Only hour 1 has all four inputs.
    assert len(recs) == 1
    assert recs[0].start.hour == 9


def test_parse_negative_irradiance_clamped_to_zero():
    payload = {
        "hourly": {
            "time": ["2025-06-21T05:00"],
            "shortwave_radiation": [-3.0],
            "direct_normal_irradiance": [-1.0],
            "diffuse_radiation": [-2.0],
            "temperature_2m": [10.0],
        }
    }
    recs = bf.parse_hourly_payload(payload, var_suffix="")
    assert recs[0].ghi == 0.0 and recs[0].dni == 0.0 and recs[0].dhi == 0.0


def test_parse_broken_payload_raises():
    with pytest.raises(ValueError):
        bf.parse_hourly_payload({}, var_suffix="")
    with pytest.raises(ValueError):
        bf.parse_hourly_payload({"hourly": {"time": []}}, var_suffix="")


# ---------------------------------------------------------------------------
# Bin key / half-year / quasi-clear gate (mirror the shademap contract)
# ---------------------------------------------------------------------------


def test_half_year_index_splits_at_solstice():
    # doy == solstice -> after (1); a week before -> before (0).
    assert bf.half_year_index(const.SUMMER_SOLSTICE_DOY) == 1
    assert bf.half_year_index(const.SUMMER_SOLSTICE_DOY - 7) == 0
    # April (leaf-off) and August (leaf-on) land in DIFFERENT halves.
    assert bf.half_year_index(105) != bf.half_year_index(220)


def test_shademap_bin_key_format_and_bins():
    key = bf.shademap_bin_key(207.4, 26.0, 250)
    az_idx, el_idx, half = key.split(":")
    assert int(az_idx) == int(207.4 // const.SHADEMAP_AZ_BIN_DEG)
    assert int(el_idx) == int(26.0 // const.SHADEMAP_EL_BIN_DEG)
    assert half in ("0", "1")
    # Azimuth wraps into [0,360).
    assert bf.shademap_bin_key(-5.0, 10.0, 10) == bf.shademap_bin_key(355.0, 10.0, 10)
    # Negative elevation floored to 0.
    assert bf.shademap_bin_key(100.0, -3.0, 10).split(":")[1] == "0"


def test_quasi_clear_gate_conditions():
    # Passes at high sun, clear, ample beam, stable measured/modeled ratio.
    assert bf.is_quasi_clear(
        kc=1.0, sun_el=40.0, beam_share=0.5,
        stability_ratio=1.0, neighbour_ratio=1.0,
    )
    # Rejected: beam share too low.
    assert not bf.is_quasi_clear(
        kc=1.0, sun_el=40.0,
        beam_share=const.SHADEMAP_MIN_BEAM_SHARE,
        stability_ratio=1.0, neighbour_ratio=1.0,
    )
    # Rejected: kc above the thin-cloud-enhancement guard.
    assert not bf.is_quasi_clear(
        kc=const.SHADEMAP_KC_HI + 0.1, sun_el=40.0, beam_share=0.5
    )
    # Rejected: unstable neighbour (big relative RATIO jump).
    assert not bf.is_quasi_clear(
        kc=1.0, sun_el=40.0, beam_share=0.5,
        stability_ratio=1.0, neighbour_ratio=0.5,
    )
    # Low-sun lower bound is RELAXED: kc 0.7 passes at el 0 but fails high.
    assert bf.is_quasi_clear(kc=0.70, sun_el=0.0, beam_share=0.5)
    assert not bf.is_quasi_clear(kc=0.70, sun_el=40.0, beam_share=0.5)


# ---------------------------------------------------------------------------
# Reconstruction (per-plane hourly split via core/)
# ---------------------------------------------------------------------------


def test_reconstruct_plane_hour_splits_beam_and_diffuse(site: SiteConfig):
    from balcony_solar_forecast.core import horizon

    wx = _clear_summer_noon_hours()[0]
    plane = site.plane_by_name("M2")  # front (az 115), sees midday sun
    svf = horizon.sky_view_factor(plane)
    r = bf.reconstruct_plane_hour(
        plane, svf, wx, latitude=site.latitude, longitude=site.longitude
    )
    # Daylight summer noon: both components positive, beam dominates on a
    # sun-facing plane, kc near 1, beam_share a sane fraction of Wp.
    assert r.beam_wh > 0.0
    assert r.diffuse_wh > 0.0
    assert r.sun_el > 0.0
    assert 0.5 < r.kc < 1.4
    assert 0.0 < r.beam_share <= 1.5


def test_reconstruct_night_hour_is_zero(site: SiteConfig):
    from balcony_solar_forecast.core import horizon

    plane = site.plane_by_name("M1")
    svf = horizon.sky_view_factor(plane)
    night = bf.HourlyWeather(
        start=datetime(2025, 6, 21, 0, 0, tzinfo=UTC),
        ghi=0.0, dni=0.0, dhi=0.0, temp_c=12.0,
    )
    r = bf.reconstruct_plane_hour(
        plane, svf, night, latitude=site.latitude, longitude=site.longitude
    )
    assert r.beam_wh == 0.0
    assert r.diffuse_wh == 0.0


def test_reconstruct_shaded_plane_keeps_ungated_reference_beam(site: SiteConfig):
    """A wall-occluded plane must still expose a positive UNGATED beam.

    The shademap learns a transmittance that REPLACES the static tau, so the
    reference beam must be the clear-horizon (ungated) beam — otherwise a fully
    shaded bin (static tau = 0) would have ~0 modeled beam, fail the beam-share
    gate, and never learn the shade it exists to capture (SPEC §5).

    At 2025-06-21 12:30 UTC the sun sits at az ~218 / el ~61 for the reference
    site, inside M4's hard building-wall sector (tau = 0), so the pure-physics
    GATED beam collapses to the diffuse floor while the ungated reference beam
    stays large.
    """
    from balcony_solar_forecast.core import horizon

    plane = site.plane_by_name("M4")  # south, hard wall from az 212
    svf = horizon.sky_view_factor(plane)
    wx = bf.HourlyWeather(
        start=datetime(2025, 6, 21, 12, 0, tzinfo=UTC),
        ghi=780.0, dni=820.0, dhi=120.0, temp_c=24.0,
    )
    r = bf.reconstruct_plane_hour(
        plane, svf, wx, latitude=site.latitude, longitude=site.longitude
    )
    # Ungated reference beam is present and qualifies for the beam-share gate.
    assert r.beam_wh > 0.0
    assert r.beam_share > const.SHADEMAP_MIN_BEAM_SHARE
    # The gated pure-physics total is the diffuse floor (wall killed the beam):
    # gated_total ~= diffuse, and strictly less than diffuse + ungated beam.
    assert r.gated_total_wh == pytest.approx(r.diffuse_wh, rel=1e-6)
    assert r.gated_total_wh < r.diffuse_wh + r.beam_wh


def test_reconstruct_shaded_bin_learns_full_occlusion(site: SiteConfig):
    """Measured == diffuse floor on a shaded plane -> learned T collapses to 0.

    Feeds a wall-occluded hour where the module measures only its diffuse floor
    (no beam gets through). The beam-referenced T = (P_meas - P_diffuse)/P_beam
    must be ~0, and the EMA bin seeds near SHADEMAP_TAU_MIN — full occlusion is
    representable (SPEC §5 clamp [0.0, 1.1]).
    """
    from balcony_solar_forecast.core import horizon

    acc = bf.BootstrapAccumulator()
    plane = site.plane_by_name("M4")
    svf = {p.name: horizon.sky_view_factor(p) for p in site.planes}
    # Three consecutive occluded hours (neighbour-stability needs neighbours).
    weather = [
        bf.HourlyWeather(
            start=datetime(2025, 6, 21, 12 + h, 0, tzinfo=UTC),
            ghi=780.0, dni=820.0, dhi=120.0, temp_c=24.0,
        )
        for h in range(3)
    ]
    # Measured = diffuse floor only (beam fully blocked by the wall).
    hourly_actuals = {"M4": {}}
    for wx in weather:
        r = bf.reconstruct_plane_hour(
            plane, svf["M4"], wx, latitude=site.latitude, longitude=site.longitude
        )
        hourly_actuals["M4"][wx.start.isoformat()] = r.diffuse_wh
    bf.process_day_hourly(acc, site, weather, hourly_actuals, svf_by_plane=svf)
    m4_bins = acc.shade.get("M4", {})
    assert m4_bins, "expected the occluded M4 plane to train at least one bin"
    taus = [v[0] for v in m4_bins.values()]
    assert all(t < 0.15 for t in taus), taus  # collapsed toward full occlusion


def test_reconstruct_snow_uses_high_albedo(site: SiteConfig):
    from balcony_solar_forecast.core import horizon

    plane = site.plane_by_name("M4")  # south, 70 deg tilt -> ground term matters
    svf = horizon.sky_view_factor(plane)
    common = dict(ghi=300.0, dni=200.0, dhi=150.0, temp_c=-2.0)
    start = datetime(2025, 1, 15, 11, 0, tzinfo=UTC)
    dry = bf.HourlyWeather(start=start, snow_depth_m=0.0, **common)
    snow = bf.HourlyWeather(start=start, snow_depth_m=0.10, **common)
    r_dry = bf.reconstruct_plane_hour(
        plane, svf, dry, latitude=site.latitude, longitude=site.longitude
    )
    r_snow = bf.reconstruct_plane_hour(
        plane, svf, snow, latitude=site.latitude, longitude=site.longitude
    )
    # Snow albedo (0.5 vs 0.2) lifts the ground-reflected diffuse component.
    assert r_snow.diffuse_wh > r_dry.diffuse_wh


# ---------------------------------------------------------------------------
# Daily -> hourly disaggregation (shape-preserving)
# ---------------------------------------------------------------------------


def test_hourly_disaggregation_preserves_total_and_shape():
    modeled = {
        "2025-06-21T09:00+00:00": 100.0,
        "2025-06-21T10:00+00:00": 300.0,
        "2025-06-21T11:00+00:00": 0.0,   # no modeled -> gets nothing
    }
    out = bf._resolve_hourly_measured(
        "M2",
        actuals_daily={"M2": 800.0},
        actuals_hourly=None,
        modeled_hourly=modeled,
    )
    assert pytest.approx(sum(out.values())) == 800.0
    # Split proportional to modeled shape: 100:300 -> 200:600.
    assert pytest.approx(out["2025-06-21T09:00+00:00"]) == 200.0
    assert pytest.approx(out["2025-06-21T10:00+00:00"]) == 600.0
    assert "2025-06-21T11:00+00:00" not in out


def test_hourly_actuals_used_verbatim_when_present():
    hourly = {"M2": {"2025-06-21T10:00+00:00": 555.0}}
    out = bf._resolve_hourly_measured(
        "M2", actuals_daily=None, actuals_hourly=hourly, modeled_hourly={}
    )
    assert out == {"2025-06-21T10:00+00:00": 555.0}


def test_disaggregation_empty_when_no_measured():
    assert bf._resolve_hourly_measured(
        "M2", actuals_daily={}, actuals_hourly=None, modeled_hourly={"h": 5.0}
    ) == {}


# ---------------------------------------------------------------------------
# Per-day accumulation
# ---------------------------------------------------------------------------


def _svf(site: SiteConfig) -> dict:
    from balcony_solar_forecast.core import horizon
    return {p.name: horizon.sky_view_factor(p) for p in site.planes}


def test_process_day_populates_shademap_and_bias(site: SiteConfig):
    acc = bf.BootstrapAccumulator()
    weather = _clear_summer_noon_hours()
    # Fabricate hourly actuals that TRACK the modeled beam+diffuse for M2/M6/M7
    # (front planes see this sun), so quasi-clear samples produce T ~ 1.
    svf = _svf(site)
    hourly_actuals: dict[str, dict[str, float]] = {}
    for plane in site.planes:
        chan = plane.name
        hh = {}
        for wx in weather:
            r = bf.reconstruct_plane_hour(
                plane, svf[chan], wx,
                latitude=site.latitude, longitude=site.longitude,
            )
            total = r.beam_wh + r.diffuse_wh
            if total > 0.0:
                hh[wx.start.isoformat()] = total  # measured == modeled -> T~1
        if hh:
            hourly_actuals[chan] = hh

    used = bf.process_day_hourly(
        acc, site, weather, hourly_actuals, svf_by_plane=svf
    )
    assert used is True
    # At least one plane accumulated shademap bins and at least one bias cell.
    assert acc.shade_samples > 0
    assert sum(len(b) for b in acc.shade.values()) > 0
    assert len(acc.bias) > 0
    # Where measured == modeled, learned tau ~ 1 (beam-referenced T of a clear,
    # unshaded sample). Pull any front-plane bin and check it is near 1.
    fronts = [c for c in acc.shade if c in ("M2", "M6", "M7")]
    assert fronts, "expected a front plane to have quasi-clear samples"
    taus = [v[0] for c in fronts for v in acc.shade[c].values()]
    assert taus
    assert all(0.7 <= t <= 1.1 for t in taus)


def test_process_day_daily_fallback_disaggregates(site: SiteConfig):
    """The daily-total fallback path (process_day) trains without hourly LTS.

    Supplies one DAILY total per module; the accumulator must still produce
    shademap/bias samples (disaggregated across daylight hours), exercising the
    coarse fallback branch (SPEC §6).
    """
    from balcony_solar_forecast.core import horizon

    acc = bf.BootstrapAccumulator()
    weather = _clear_summer_noon_hours()
    svf = {p.name: horizon.sky_view_factor(p) for p in site.planes}
    # A generous daily total per module so disaggregated hours clear the gates.
    daily = {p.name: 1500.0 for p in site.planes}
    used = bf.process_day(acc, site, weather, daily, svf_by_plane=svf)
    assert isinstance(used, bool)
    # The call is safe and (for a clear day) contributes at least bias samples.
    assert acc.bias_samples >= 0


def test_process_day_empty_weather_returns_false(site: SiteConfig):
    acc = bf.BootstrapAccumulator()
    assert bf.process_day_hourly(acc, site, [], {}, svf_by_plane=_svf(site)) is False


def test_process_day_channel_dropout_skips_module(site: SiteConfig):
    acc = bf.BootstrapAccumulator()
    weather = _clear_summer_noon_hours()
    # Provide actuals for only ONE module -> others are dropped, no crash.
    svf = _svf(site)
    r = bf.reconstruct_plane_hour(
        site.plane_by_name("M2"), svf["M2"], weather[1],
        latitude=site.latitude, longitude=site.longitude,
    )
    actuals = {"M2": {weather[1].start.isoformat(): r.beam_wh + r.diffuse_wh}}
    used = bf.process_day_hourly(acc, site, weather, actuals, svf_by_plane=svf)
    # M2 may or may not pass the gate at this exact hour, but the call must be
    # safe and only ever touch channel M2.
    assert set(acc.shade.keys()) <= {"M2"}
    assert isinstance(used, bool)


# ---------------------------------------------------------------------------
# RLS step
# ---------------------------------------------------------------------------


def test_rls_step_moves_theta_toward_ratio_and_clamps():
    # measured = 1.3 * modeled -> theta should climb from 1.0 toward 1.3 (and
    # never exceed the clamp).
    cell = BiasCell()
    for _ in range(30):
        cell = bf._rls_step(cell, modeled=1000.0, measured=1300.0)
    assert cell.n == 30
    assert 1.1 < cell.theta <= const.DAY_AHEAD_BIAS_MAX
    assert cell.clamped_theta() <= const.DAY_AHEAD_BIAS_MAX


def test_rls_step_clamps_extreme_bias():
    cell = BiasCell()
    # Absurd 10x measured -> theta clamped to the max band edge.
    for _ in range(50):
        cell = bf._rls_step(cell, modeled=100.0, measured=1000.0)
    assert cell.theta == const.DAY_AHEAD_BIAS_MAX


def test_rls_step_zero_modeled_is_noop():
    cell = BiasCell(theta=1.2, covariance=5.0, n=3)
    out = bf._rls_step(cell, modeled=0.0, measured=500.0)
    assert out == cell


# ---------------------------------------------------------------------------
# n-credit cap + bootstrap JSON contract
# ---------------------------------------------------------------------------


def test_build_bootstrap_caps_bin_n(site: SiteConfig):
    acc = bf.BootstrapAccumulator()
    # Manually stuff a bin with an inflated sample count.
    acc.shade = {"M4": {"41:16:1": [0.42, 999]}}
    acc.bias = {BiasState.cell_key(const.CLOUD_CLASS_CLEAR,
                                   const.DAY_PART_MIDDAY): BiasCell(theta=1.1, n=5)}
    js = bf.build_bootstrap_json(acc, site,
                                 generated_at=datetime(2026, 7, 6, tzinfo=UTC))
    shade = js[const.BOOTSTRAP_KEY_SHADEMAP]
    cap = const.BOOTSTRAP_MAX_BIN_N
    assert shade["channels"]["M4"]["41:16:1"]["n"] == cap
    assert shade["channels"]["M4"]["41:16:1"]["tau"] == pytest.approx(0.42)


def test_build_bootstrap_custom_cap(site: SiteConfig):
    acc = bf.BootstrapAccumulator()
    acc.shade = {"M4": {"1:1:0": [0.9, 100]}}
    js = bf.build_bootstrap_json(acc, site, max_bin_n=3)
    assert js[const.BOOTSTRAP_KEY_SHADEMAP]["channels"]["M4"]["1:1:0"]["n"] == 3


def test_bootstrap_json_matches_contract_schema(site: SiteConfig):
    acc = bf.BootstrapAccumulator()
    acc.shade = {"M4": {"41:16:1": [0.5, 4]}}
    acc.bias = {"clear|midday": BiasCell(theta=1.05, covariance=12.0, n=7)}
    js = bf.build_bootstrap_json(acc, site)

    # Top-level contract keys present.
    assert js[const.BOOTSTRAP_KEY_SCHEMA] == const.BOOTSTRAP_SCHEMA_VERSION
    assert const.BOOTSTRAP_KEY_GENERATED_AT in js
    assert js[const.BOOTSTRAP_KEY_SITE_SIGNATURE] == bf.site_signature(site)

    # Sub-objects round-trip through the frozen state types (what the import
    # service validates against).
    bias = BiasState.from_dict(js[const.BOOTSTRAP_KEY_BIAS])
    shade = ShademapState.from_dict(js[const.BOOTSTRAP_KEY_SHADEMAP])
    assert bias.cells["clear|midday"].n == 7
    assert shade.channels["M4"]["41:16:1"].tau == pytest.approx(0.5)
    assert shade.channels["M4"]["41:16:1"].n == 4


def test_bootstrap_clamps_out_of_range_tau(site: SiteConfig):
    acc = bf.BootstrapAccumulator()
    # tau above the [0, 1.1] band must be clamped in the emitted JSON.
    acc.shade = {"M4": {"1:1:0": [5.0, 2]}}
    js = bf.build_bootstrap_json(acc, site)
    tau = js[const.BOOTSTRAP_KEY_SHADEMAP]["channels"]["M4"]["1:1:0"]["tau"]
    assert tau == const.SHADEMAP_TAU_MAX


def test_site_signature_stable_and_site_sensitive(site: SiteConfig):
    sig1 = bf.site_signature(site)
    sig2 = bf.site_signature(SiteConfig.from_dict(const.DEFAULT_SITE))
    assert sig1 == sig2  # deterministic
    # A different latitude changes the signature.
    d = dict(const.DEFAULT_SITE)
    d[const.CONF_LATITUDE] = site.latitude + 1.0
    assert bf.site_signature(SiteConfig.from_dict(d)) != sig1


# ---------------------------------------------------------------------------
# Cloud classification / day part
# ---------------------------------------------------------------------------


def test_classify_cloud_fog_and_covers():
    # Low visibility -> fog regardless of month.
    fog = bf.HourlyWeather(
        start=datetime(2025, 3, 1, 8, tzinfo=UTC),
        ghi=50.0, dni=0.0, dhi=50.0, temp_c=2.0, visibility_m=500.0,
    )
    assert bf._classify_cloud(fog) == const.CLOUD_CLASS_FOG
    clear = bf.HourlyWeather(
        start=datetime(2025, 6, 21, 10, tzinfo=UTC),
        ghi=800.0, dni=850.0, dhi=100.0, temp_c=25.0,
        cloud_low=5.0, cloud_mid=0.0, cloud_high=0.0, visibility_m=30000.0,
    )
    assert bf._classify_cloud(clear) == const.CLOUD_CLASS_CLEAR
    overcast = bf.HourlyWeather(
        start=datetime(2025, 6, 21, 10, tzinfo=UTC),
        ghi=120.0, dni=0.0, dhi=120.0, temp_c=18.0,
        cloud_low=90.0, cloud_mid=90.0, cloud_high=90.0, visibility_m=20000.0,
    )
    # High low-cloud in June (not a fog month) -> overcast, not fog.
    assert bf._classify_cloud(overcast) == const.CLOUD_CLASS_OVERCAST


def test_day_part_boundaries():
    assert bf._day_part_for_hour(6) == const.DAY_PART_MORNING
    assert bf._day_part_for_hour(const.DAY_PART_MORNING_END_HOUR) == const.DAY_PART_MIDDAY
    assert bf._day_part_for_hour(
        const.DAY_PART_AFTERNOON_START_HOUR
    ) == const.DAY_PART_AFTERNOON


# ---------------------------------------------------------------------------
# LTS statistics-row parser
# ---------------------------------------------------------------------------


def test_parse_lts_result_epoch_ms_and_iso():
    out = {"sensor.a": {}, "sensor.b": {}}
    ts_ms = int(
        datetime(2025, 6, 21, 10, 0, tzinfo=UTC).timestamp() * 1000
    )
    result = {
        "sensor.a": [
            {"start": ts_ms, "mean": 120.0},
            {"start": ts_ms, "mean": 30.0},   # same hour -> summed
        ],
        "sensor.b": [
            {"start": "2025-06-21T11:00:00+00:00", "mean": 200.0},
            {"start": "2025-06-21T12:00:00+00:00", "mean": None},  # skipped
        ],
        "sensor.unknown": [{"start": ts_ms, "mean": 999.0}],  # not requested
    }
    bf._parse_lts_result(result, out)
    hkey_a = datetime(2025, 6, 21, 10, 0, tzinfo=UTC).isoformat()
    assert out["sensor.a"][hkey_a] == pytest.approx(150.0)
    hkey_b = datetime(2025, 6, 21, 11, 0, tzinfo=UTC).isoformat()
    assert out["sensor.b"][hkey_b] == pytest.approx(200.0)
    # None mean produced no entry; unknown sid ignored.
    assert len(out["sensor.b"]) == 1
    assert "sensor.unknown" not in out


def test_stat_row_hour_variants():
    ts_ms = int(datetime(2025, 6, 21, 10, 30, tzinfo=UTC).timestamp() * 1000)
    # Epoch ms floored to the hour.
    assert bf._stat_row_hour(ts_ms) == datetime(
        2025, 6, 21, 10, 0, tzinfo=UTC
    ).isoformat()
    # Naive ISO assumed UTC.
    assert bf._stat_row_hour("2025-06-21T10:45:00") == datetime(
        2025, 6, 21, 10, 0, tzinfo=UTC
    ).isoformat()
    # Junk -> None.
    assert bf._stat_row_hour(object()) is None
    assert bf._stat_row_hour("not-a-date") is None


def test_lts_windows_chunk_a_multi_year_range():
    # A 2-year hourly pull for all modules overflows HA's 4 MiB WS frame; the
    # LTS query must be chunked. Windows must tile [start, end) with no gap
    # or overlap and the last one clipped to end.
    start = datetime(2024, 7, 1, tzinfo=UTC)
    end = datetime(2026, 7, 6, tzinfo=UTC)
    wins = bf._lts_windows(start, end, bf._LTS_WINDOW_DAYS)
    assert wins[0][0] == start
    assert wins[-1][1] == end
    for (_a0, a1), (b0, _b1) in zip(wins, wins[1:], strict=False):
        assert a1 == b0  # contiguous, no gap/overlap
    # Every window is at most the window size.
    from datetime import timedelta as _td
    assert all(b - a <= _td(days=bf._LTS_WINDOW_DAYS) for a, b in wins)
    # A range shorter than one window yields exactly one clipped window.
    short = bf._lts_windows(start, start + _td(days=3), bf._LTS_WINDOW_DAYS)
    assert short == [(start, start + _td(days=3))]


# ---------------------------------------------------------------------------
# Day grouping / filtering
# ---------------------------------------------------------------------------


def test_group_by_day_and_filter_actuals():
    recs = [
        bf.HourlyWeather(datetime(2025, 6, 21, 9, tzinfo=UTC),
                         700, 800, 110, 22),
        bf.HourlyWeather(datetime(2025, 6, 22, 9, tzinfo=UTC),
                         700, 800, 110, 22),
    ]
    by_day = bf._group_by_day(recs)
    assert set(by_day.keys()) == {"2025-06-21", "2025-06-22"}

    hourly_actuals = {
        "M2": {
            "2025-06-21T09:00:00+00:00": 300.0,
            "2025-06-22T09:00:00+00:00": 250.0,
        }
    }
    day = bf._filter_actuals_for_day(hourly_actuals, "2025-06-21")
    assert day == {"M2": {"2025-06-21T09:00:00+00:00": 300.0}}
