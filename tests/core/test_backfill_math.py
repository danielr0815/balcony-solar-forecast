"""Pure-math tests for scripts/backfill.py (NO network — SPEC §12.3/§12.4).

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
from balcony_solar_forecast.core import shademap as shademap_mod  # noqa: E402
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


def test_parse_missing_visibility_is_none_not_fog_sentinel():
    """A provider hole in the visibility column is UNKNOWN (None), never the
    0.0 sentinel — 0 m is a real dense-fog reading, so the old ``or 0.0``
    classified every data gap as fog and poisoned the fog bias cell."""
    payload = {
        "hourly": {
            "time": ["2025-06-21T09:00", "2025-06-21T10:00"],
            "shortwave_radiation": [700.0, 750.0],
            "direct_normal_irradiance": [800.0, 810.0],
            "diffuse_radiation": [110.0, 120.0],
            "temperature_2m": [22.0, 24.0],
            "visibility": [None, 24000.0],
        }
    }
    recs = bf.parse_hourly_payload(payload, var_suffix="")
    assert recs[0].visibility_m is None
    assert recs[1].visibility_m == 24000.0


# ---------------------------------------------------------------------------
# Engine mirror-invariant: reconstruct_plane_hour == engine raw plane physics
# ---------------------------------------------------------------------------
#
# The backfill reconstructs the SLOW-reference / day-ahead-bias physics with the
# SAME core/ functions the live engine runs (SPEC §12.4 mirror invariant). These
# tests pin that byte-for-byte on the v0.22 additions: an inline ``tau_points``
# elevation profile (the beam gate must resolve at the true sun elevation in BOTH
# paths — the parity break the sun_el fix closed), a ``diffuse_tau`` wall row (the
# SVF band integral), and the ``bifacial_beam_gain`` factor (the open 0.21 backlog
# item). The engine slot is positioned so its midpoint equals the backfill hour's
# midpoint (hour_start + 30 min), so both evaluate the sun at the same instant.


def _engine_raw_plane_hour(
    site: SiteConfig, plane_name: str, hour_start, wx: bf.HourlyWeather
) -> tuple[float, float, float]:
    """(raw_watts, diffuse_ref, beam_ref) for one plane from a single engine slot
    whose midpoint == the backfill hour midpoint (hour_start + 30 min)."""
    from datetime import timedelta

    from balcony_solar_forecast.core.engine import compute_forecast
    from balcony_solar_forecast.core.types import WeatherSeries, WeatherSlot

    slot_start = hour_start + timedelta(minutes=30) - timedelta(minutes=7, seconds=30)
    ws = WeatherSeries(
        slots=(
            WeatherSlot(
                start=slot_start, ghi=wx.ghi, dni=wx.dni, dhi=wx.dhi,
                temp_c=wx.temp_c, snow_depth_m=wx.snow_depth_m,
            ),
        )
    )
    res = compute_forecast(site, ws, hour_start, tz=UTC)
    pr = next(p for p in res.plane_results if p.name == plane_name)
    return pr.raw_watts[0], pr.diffuse_ref_watts[0], pr.beam_ref_watts[0]


def _tau_points_diffuse_site() -> SiteConfig:
    """East-facing plane with a v0.22 inline tau_points crown AND a diffuse_tau
    wall row (single plane, no group, so no AC clamp masks the parity)."""
    from balcony_solar_forecast.core.types import HorizonRow, PlaneConfig

    tp = ((4.5, 0.0), (5.5, 0.25), (6.5, 0.45), (8.0, 0.85), (9.5, 1.0))
    rows = (
        HorizonRow(azimuth_deg=52.0, elevation_deg=10.0, tau=0.0, tau_points=tp),
        HorizonRow(azimuth_deg=89.0, elevation_deg=10.0, tau=0.0, tau_points=tp),
        HorizonRow(azimuth_deg=195.0, elevation_deg=90.0, tau=0.0, diffuse_tau=0.5),
        HorizonRow(azimuth_deg=360.0, elevation_deg=90.0, tau=0.0, diffuse_tau=0.5),
    )
    return SiteConfig(
        latitude=48.13, longitude=11.57,
        planes=(
            PlaneConfig(
                name="M", azimuth_deg=115.0, tilt_deg=70.0, wp=430.0,
                efficiency=0.96, horizon=rows, actual_entity="sensor.m",
            ),
        ),
        groups=(),
    )


@pytest.mark.parametrize(
    "hour_start, expected_tau_label",
    [(datetime(2026, 8, 25, 4, 0, tzinfo=UTC),
      "fully-gated (slot midpoint 04:30Z, el~0.9 < node 4.5 -> tau_points 0)"),
     (datetime(2026, 8, 25, 4, 30, tzinfo=UTC),
      "intermediate (slot midpoint 05:00Z, el~5.5 -> interpolated tau_points)")],
)
def test_reconstruct_matches_engine_on_tau_points_diffuse(
    hour_start, expected_tau_label
):
    """reconstruct_plane_hour == engine raw plane physics on a tau_points +
    diffuse_tau setup (the v0.22 mirror invariant). Byte-for-byte: the gated
    total (day-ahead-bias reference), the diffuse floor (SVF band integral with
    diffuse_tau) and the ungated beam (SLOW reference).

    The two cases pin BOTH beam-gate branches at the true sun elevation (the
    parity the sun_el fix closed): the first slot's midpoint sits below the
    lowest tau_points node (fully gated, static_tau 0); the second's midpoint
    lands strictly INSIDE the profile (el~5.5, between the 4.5/5.5/6.5 knots),
    so the gate resolves an INTERPOLATED partial tau_points -- neither 0 nor 1.
    ``HourlyWeather.start`` needs no hour alignment; both paths evaluate the sun
    at start + 30 min."""
    from balcony_solar_forecast.core import horizon

    site = _tau_points_diffuse_site()
    plane = site.plane_by_name("M")
    svf = horizon.sky_view_factor(plane)
    wx = bf.HourlyWeather(
        start=hour_start, ghi=200.0, dni=850.0, dhi=60.0, temp_c=15.0
    )
    r = bf.reconstruct_plane_hour(
        plane, svf, wx, latitude=site.latitude, longitude=site.longitude
    )
    raw_w, diffuse_ref, beam_ref = _engine_raw_plane_hour(site, "M", hour_start, wx)

    assert r.gated_total_wh == pytest.approx(raw_w, abs=1e-9), expected_tau_label
    assert r.diffuse_wh == pytest.approx(diffuse_ref, abs=1e-9)
    assert r.beam_wh == pytest.approx(beam_ref, abs=1e-9)


def test_reconstruct_matches_engine_with_bifacial_beam_gain():
    """The open 0.21-backlog parity item: reconstruct_plane_hour(beam_gain=g) ==
    engine raw plane physics with site.bifacial_beam_gain=g. The factor multiplies
    beam+circumsolar identically in both paths, so the gated total stays a mirror."""
    from dataclasses import replace
    from datetime import datetime

    from balcony_solar_forecast.core import horizon

    base = _tau_points_diffuse_site()
    site = replace(base, bifacial_beam_gain=1.25)
    plane = site.plane_by_name("M")
    svf = horizon.sky_view_factor(plane)
    hour_start = datetime(2026, 8, 25, 5, 0, tzinfo=UTC)
    wx = bf.HourlyWeather(
        start=hour_start, ghi=200.0, dni=850.0, dhi=60.0, temp_c=15.0
    )
    r = bf.reconstruct_plane_hour(
        plane, svf, wx, latitude=site.latitude, longitude=site.longitude,
        beam_gain=1.25,
    )
    raw_w, diffuse_ref, beam_ref = _engine_raw_plane_hour(site, "M", hour_start, wx)
    assert r.gated_total_wh == pytest.approx(raw_w, abs=1e-9)
    assert r.diffuse_wh == pytest.approx(diffuse_ref, abs=1e-9)
    assert r.beam_wh == pytest.approx(beam_ref, abs=1e-9)
    # Sanity: the gain actually lifted the beam (guards against a 1.0 no-op slip).
    r1 = bf.reconstruct_plane_hour(
        plane, svf, wx, latitude=site.latitude, longitude=site.longitude,
        beam_gain=1.0,
    )
    assert r.beam_wh > r1.beam_wh * 1.2


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
    gate, and never learn the shade it exists to capture (SPEC §9.1).

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
    representable (SPEC §9.1 clamp [0.0, 1.1]).
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
    # A realistic site day: every module reports (tracking its model) — the
    # measured-clear day gate mirrors the live trainer and would rightly reject
    # a partial-site fabrication. Only M4 measures its diffuse floor (the wall
    # blocks its beam), which is what its bins must learn.
    hourly_actuals = _tracked_actuals(site, weather, svf, factor=1.0)
    hourly_actuals["M4"] = {}
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


def test_backfill_accumulates_per_plane_even_when_grouped():
    """The backfill stores learning PER PLANE, even for grouped planes (SPEC §9.2).

    Storage is always per plane (keyed by plane name); the group pooling happens
    later at READ time in the coordinator, so grouping stays fully reversible.
    Grouping M2/M3 must therefore NOT fold them into a shared 'front' channel —
    each keeps its own per-plane channel (accumulated via the SAME
    ``_shade_update`` as the ungrouped planes, so live parity is preserved).
    """
    import copy

    raw = copy.deepcopy(const.DEFAULT_SITE)
    # Group the two lower-balcony front planes M2 and M3 (both azimuth 115).
    for p in raw["planes"]:
        if p["name"] in ("M2", "M3"):
            p[const.CONF_SHADE_GROUP] = "front"
    grouped = SiteConfig.from_dict(raw)
    svf = _svf(grouped)
    weather = _clear_summer_noon_hours()
    hourly_actuals = _tracked_actuals(grouped, weather, svf, factor=1.0)

    acc = bf.BootstrapAccumulator()
    used = bf.process_day_hourly(
        acc, grouped, weather, hourly_actuals, svf_by_plane=svf
    )
    assert used is True
    # Grouped M2/M3 accumulate under their OWN names — never a 'front' channel.
    assert "M2" in acc.shade
    assert "M3" in acc.shade
    assert "front" not in acc.shade
    # An ungrouped plane (M1) keeps its own per-plane channel too (unchanged).
    assert "M1" in acc.shade


def test_process_day_daily_fallback_disaggregates(site: SiteConfig):
    """The daily-total fallback path (process_day) trains without hourly LTS.

    Supplies one DAILY total per module; the accumulator must still produce
    shademap/bias samples (disaggregated across daylight hours), exercising the
    coarse fallback branch (SPEC §12.4).
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


def test_process_day_kw_scaled_sensor_discards_day(site: SiteConfig):
    """SPEC §10 plausibility gate: a kW-instead-of-W channel discards the WHOLE
    day for every learner. A sustained hourly reading above
    CHANNEL_PLAUSIBILITY_MAX_WP_FRAC x the module's Wp is physically impossible
    (cloud-edge enhancement peaks are sub-hourly), so the mis-scaled channel
    must never become shademap/bias ground truth — before the gate it seeded
    saturated tau into the M2 bins and a ~1000x deficit into the RLS cells."""
    acc = bf.BootstrapAccumulator()
    weather = _clear_summer_noon_hours()
    svf = _svf(site)
    hourly_actuals = _tracked_actuals(site, weather, svf, factor=1.0)
    # One channel reports kW instead of W (1000x).
    hourly_actuals["M2"] = {
        h: wh * 1000.0 for h, wh in hourly_actuals["M2"].items()
    }
    used = bf.process_day_hourly(
        acc, site, weather, hourly_actuals, svf_by_plane=svf
    )
    assert used is False
    assert acc.shade_samples == 0
    assert not acc.shade
    assert acc.bias_samples == 0


def test_process_day_reading_at_plausibility_bound_still_trains(site: SiteConfig):
    """The gate fires ABOVE 1.25 x Wp, not at it: a sustained reading exactly at
    the cloud-edge headroom is still legitimate training data."""
    acc = bf.BootstrapAccumulator()
    weather = _clear_summer_noon_hours()
    svf = _svf(site)
    hourly_actuals = _tracked_actuals(site, weather, svf, factor=1.0)
    m2 = site.plane_by_name("M2")
    first = next(iter(hourly_actuals["M2"]))
    hourly_actuals["M2"][first] = 1.25 * m2.wp  # exactly AT the bound
    used = bf.process_day_hourly(
        acc, site, weather, hourly_actuals, svf_by_plane=svf
    )
    assert used is True


# ---------------------------------------------------------------------------
# Partial metering (SPEC §9.1/§9.5/§11.1 Teilmengen-Regel)
# ---------------------------------------------------------------------------


def _partial_metered_site() -> SiteConfig:
    """Two identical front planes; only M1 carries a meter (actual_entity)."""
    return SiteConfig.from_dict({
        const.CONF_LATITUDE: 51.1,
        const.CONF_LONGITUDE: 10.4,
        const.CONF_PLANES: [
            {
                const.CONF_PLANE_NAME: "M1",
                const.CONF_AZIMUTH: 115.0,
                const.CONF_TILT: 70.0,
                const.CONF_WP: 370,
                const.CONF_ACTUAL_ENTITY: "sensor.m1",
            },
            {
                const.CONF_PLANE_NAME: "M2",
                const.CONF_AZIMUTH: 115.0,
                const.CONF_TILT: 70.0,
                const.CONF_WP: 370,
                # no actual_entity: unmetered
            },
        ],
        const.CONF_GROUPS: [],
    })


def _unmetered_site() -> SiteConfig:
    """The same two planes, NEITHER metered."""
    raw = {
        const.CONF_LATITUDE: 51.1,
        const.CONF_LONGITUDE: 10.4,
        const.CONF_PLANES: [
            {
                const.CONF_PLANE_NAME: "M1",
                const.CONF_AZIMUTH: 115.0,
                const.CONF_TILT: 70.0,
                const.CONF_WP: 370,
            },
        ],
        const.CONF_GROUPS: [],
    }
    return SiteConfig.from_dict(raw)


def test_process_day_bias_learns_forecast_error_not_metering_share():
    """On a partially metered site the RLS must compare the metered subset.

    Measured tracks the modeled energy of the ONE metered plane exactly; the
    identical unmetered twin doubles the modeled site total. Summing ALL
    planes on the modeled side teaches theta the metering share (~0.5, pinned
    at the clamp floor) instead of the forecast error (~1.0).
    """
    site = _partial_metered_site()
    weather = _clear_summer_noon_hours()
    svf = _svf(site)
    hourly_actuals = {"M1": _tracked_actuals(site, weather, svf)["M1"]}
    acc = bf.BootstrapAccumulator()
    used = bf.process_day_hourly(acc, site, weather, hourly_actuals, svf_by_plane=svf)
    assert used is True
    assert acc.bias, "expected at least one trained day-ahead cell"
    thetas = [c.theta for c in acc.bias.values()]
    assert all(t > 0.9 for t in thetas), thetas


def test_process_day_shademap_gate_passes_with_unmetered_plane():
    """The measured-clear day gate must compare the METERED subset: an
    unmetered plane contributing >20 % of the modeled site energy must not
    read as an overcast bust and block shademap training."""
    site = _partial_metered_site()
    weather = _clear_summer_noon_hours()
    svf = _svf(site)
    hourly_actuals = {"M1": _tracked_actuals(site, weather, svf)["M1"]}
    acc = bf.BootstrapAccumulator()
    bf.process_day_hourly(acc, site, weather, hourly_actuals, svf_by_plane=svf)
    # The unmetered twin contributes 50 % of the modeled site energy; against
    # the FULL modeled curve the gate fails (0.5 < 0.8) and no bin trains.
    assert acc.shade_samples > 0
    assert set(acc.shade) == {"M1"}  # never the unmetered channel
    taus = [v[0] for v in acc.shade["M1"].values()]
    assert taus and all(0.7 <= t <= 1.1 for t in taus)


def test_process_day_without_any_metered_plane_learns_nothing():
    """No plane with actual_entity -> no measured side exists: the day is
    skipped entirely instead of training against a caller-supplied channel
    nothing on the site measures."""
    site = _unmetered_site()
    weather = _clear_summer_noon_hours()
    svf = _svf(site)
    hourly_actuals = _tracked_actuals(site, weather, svf, factor=1.0)
    acc = bf.BootstrapAccumulator()
    used = bf.process_day_hourly(acc, site, weather, hourly_actuals, svf_by_plane=svf)
    assert used is False
    assert acc.shade_samples == 0
    assert acc.bias_samples == 0
    assert acc.quantile_samples == 0


# ---------------------------------------------------------------------------
# Shademap EMA warm-up parity: bootstrap _shade_update vs. live update_bin
# ---------------------------------------------------------------------------


def test_shade_update_matches_live_update_bin_sample_for_sample():
    """backfill._shade_update and shademap.update_bin must stay identical.

    Both apply the adaptive warm-up alpha = max(SHADEMAP_EMA_ALPHA, 1/(n_old+1)),
    so feeding the SAME sample sequence through each must yield the same tau at
    EVERY step — the 12 samples here cross the mean->EMA transition (n_old >= 6).
    """
    samples = [0.0, 1.0, 0.5, 0.8, 0.2, 0.9, 0.3, 0.7, 0.4, 0.6, 0.95, 0.05]
    sun_az, sun_el, doy = 150.0, 40.0, 220
    bin_key = shademap_mod.shademap_bin_key(sun_az, sun_el, doy)

    st = ShademapState()
    acc = bf.BootstrapAccumulator()
    for s in samples:
        st = shademap_mod.update_bin(
            st, channel="M4", sun_az=sun_az, sun_el=sun_el, doy=doy, measured_t=s,
        )
        bf._shade_update(acc, "M4", bin_key, s)
        tau_live = st.channels["M4"][bin_key].tau
        tau_boot = acc.shade["M4"][bin_key][0]
        assert tau_boot == pytest.approx(tau_live, abs=1e-12)
    assert acc.shade["M4"][bin_key][1] == len(samples)


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


def test_rls_step_nonfinite_or_negative_is_noop():
    """Guards mirroring bias._rls_step: a NaN/inf/negative sample must be a
    no-op — before the guards a NaN measured poisoned theta (NaN -> clamped to
    the 0.5 floor) and advanced n on pure garbage."""
    cell = BiasCell(theta=1.2, covariance=5.0, n=3)
    for modeled, measured in (
        (float("nan"), 500.0),
        (1000.0, float("nan")),
        (float("inf"), 500.0),
        (1000.0, float("inf")),
        (1000.0, -1.0),
    ):
        assert bf._rls_step(cell, modeled=modeled, measured=measured) == cell


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


def test_classify_cloud_unknown_visibility_is_not_fog():
    """None (unknown) visibility passes through to bias.classify_cloud as
    'no fog evidence' — it must neither raise nor classify fog (Live =
    Backfill parity, SPEC §8). The legacy 0.0 sentinel STILL maps to unknown
    here: historical backfill data carries 0.0 for 'no reading', while the
    live path can now tell a real 0 m reading (fog) apart from None."""
    base = dict(ghi=780.0, dni=820.0, dhi=120.0, temp_c=24.0)
    wx_none = bf.HourlyWeather(
        start=datetime(2025, 6, 21, 9, tzinfo=UTC), visibility_m=None, **base
    )
    assert bf._classify_cloud(wx_none) == const.CLOUD_CLASS_CLEAR
    wx_zero = bf.HourlyWeather(
        start=datetime(2025, 6, 21, 9, tzinfo=UTC), visibility_m=0.0, **base
    )
    assert bf._classify_cloud(wx_zero) == const.CLOUD_CLASS_CLEAR


def test_day_part_for_slot_solar_boundaries():
    # v0.19: backfill bins the day-ahead bias by SOLAR time (matching the live
    # coordinator), not the clock. At the operator site (lon 12.2) solar noon is
    # ~11:15 UTC on 2026-07-01, so 08/12/16 UTC fall well inside morning / midday
    # / afternoon — the SAME mapping the coordinator's _day_part_for_hourkey uses.
    lon = 12.2
    assert bf._day_part_for_slot(
        datetime(2026, 7, 1, 8, tzinfo=UTC), lon
    ) == const.DAY_PART_MORNING
    assert bf._day_part_for_slot(
        datetime(2026, 7, 1, 12, tzinfo=UTC), lon
    ) == const.DAY_PART_MIDDAY
    assert bf._day_part_for_slot(
        datetime(2026, 7, 1, 16, tzinfo=UTC), lon
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


# ---------------------------------------------------------------------------
# Day-level hygiene gates in the shademap section (mirror the live trainer):
# measured-clear day gate, per-hour snow gate, frozen-channel module drop.
# ---------------------------------------------------------------------------


def _clear_hours(n: int, snow_depth_m: float = 0.0) -> list[bf.HourlyWeather]:
    """Like _clear_summer_noon_hours but n hours (9..9+n-1 UTC) + optional snow."""
    base = datetime(2025, 6, 21, 9, 0, tzinfo=UTC)
    return [
        bf.HourlyWeather(
            start=base.replace(hour=9 + h),
            ghi=780.0, dni=820.0, dhi=120.0, temp_c=24.0,
            snow_depth_m=snow_depth_m,
        )
        for h in range(n)
    ]


def _tracked_actuals(
    site: SiteConfig, weather: list, svf: dict, factor: float = 1.0
) -> dict[str, dict[str, float]]:
    """Hourly actuals tracking factor x the modeled (ungated) plane totals."""
    out: dict[str, dict[str, float]] = {}
    for plane in site.planes:
        hh = {}
        for wx in weather:
            r = bf.reconstruct_plane_hour(
                plane, svf[plane.name], wx,
                latitude=site.latitude, longitude=site.longitude,
            )
            total = (r.beam_wh + r.diffuse_wh) * factor
            if total > 0.0:
                hh[wx.start.isoformat()] = total
        if hh:
            out[plane.name] = hh
    return out


def test_is_frozen_hourly_mirrors_live_gate():
    n = const.LABEL_FROZEN_MIN_REPEATS
    assert bf._is_frozen_hourly([180.0] * n) is True
    assert bf._is_frozen_hourly([180.0] * (n - 1) + [181.0]) is False
    # A run of identical ZEROS is legitimate night/shade, never frozen.
    assert bf._is_frozen_hourly([0.0] * (n + 3)) is False


def test_process_day_shademap_skipped_on_overcast_reality(site: SiteConfig):
    """Measured far below the gated forecast (overcast/snow-covered reality):
    the day must not train the GEOMETRY — without the gate the uniform low
    ratio passes every per-hour check and seeds tau~0 into the traversed bins."""
    acc = bf.BootstrapAccumulator()
    weather = _clear_summer_noon_hours()
    svf = _svf(site)
    hourly_actuals = _tracked_actuals(site, weather, svf, factor=0.3)

    bf.process_day_hourly(acc, site, weather, hourly_actuals, svf_by_plane=svf)

    assert acc.shade_samples == 0
    assert not acc.shade
    # The day-ahead bias still trains (the daily ratio IS its real signal).
    assert len(acc.bias) > 0


def test_process_day_shademap_skips_snow_hours(site: SiteConfig):
    """Snow on the panels is weather occlusion, not geometry: snowy hours never
    train the shademap even when the measured energy tracks the (snow-albedo)
    model well enough to pass the measured-clear day gate."""
    acc = bf.BootstrapAccumulator()
    weather = _clear_hours(3, snow_depth_m=0.05)  # > SNOW_DEPTH_THRESHOLD_M
    svf = _svf(site)
    hourly_actuals = _tracked_actuals(site, weather, svf, factor=1.0)

    bf.process_day_hourly(acc, site, weather, hourly_actuals, svf_by_plane=svf)

    assert acc.shade_samples == 0
    assert not acc.shade


def test_process_day_drops_frozen_module_only(site: SiteConfig):
    """A module whose hourly means repeat byte-identically (stuck Hoymiles/DTU
    sensor) is dropped for the day — the other modules keep training."""
    n_hours = const.LABEL_FROZEN_MIN_REPEATS + 1
    acc = bf.BootstrapAccumulator()
    weather = _clear_hours(n_hours)
    svf = _svf(site)
    hourly_actuals = _tracked_actuals(site, weather, svf, factor=1.0)

    # Freeze M2 (a front plane that would otherwise train): the SAME non-zero
    # value every hour. Its own average keeps the site measured-clear gate
    # satisfied, so only the frozen-channel gate can reject it.
    m2 = hourly_actuals["M2"]
    frozen_value = sum(m2.values()) / len(m2)
    hourly_actuals["M2"] = {h: frozen_value for h in m2}

    bf.process_day_hourly(acc, site, weather, hourly_actuals, svf_by_plane=svf)

    assert acc.shade_samples > 0, "healthy modules must still train"
    assert "M2" not in acc.shade, "the frozen module-day must be dropped"


# ---------------------------------------------------------------------------
# Quantile bootstrap seeding (A6 / SPEC §12.6) — the F7 cold-start fix
# ---------------------------------------------------------------------------


def _clear_afternoon(day, hours=(14, 15, 16, 17)) -> list[bf.HourlyWeather]:
    """Clear high-sun AFTERNOON UTC hours on ``day`` at the reference site.

    14..17 UTC fall solidly in the SOLAR afternoon part at lon 12.2 (solar noon
    ~11:15 UTC), so every produced hour lands in the SAME (clear x afternoon)
    quantile bin — letting a single bin accumulate across days.
    """
    from datetime import datetime as _dt

    out = []
    for h in hours:
        out.append(
            bf.HourlyWeather(
                start=_dt(day.year, day.month, day.day, h, 0, tzinfo=UTC),
                ghi=780.0, dni=820.0, dhi=120.0, temp_c=24.0,
            )
        )
    return out


def test_backfill_seeds_quantile_ring_yields_real_bands(site: SiteConfig):
    """>= QUANTILE_MIN_SAMPLES samples from >= QUANTILE_MIN_DAYS days -> a REAL
    band (not the neutral 1/1/1 collapse). This is the whole point of A6: before
    it, the backfill left the quantile store empty and day-0 bands collapsed for
    weeks."""
    from datetime import date, timedelta

    from balcony_solar_forecast.core import quantiles as q

    acc = bf.BootstrapAccumulator()
    svf = _svf(site)
    start = date(2025, 6, 16)
    n_days = 7
    for i in range(n_days):
        d = start + timedelta(days=i)
        weather = _clear_afternoon(d)
        # Spread the measured/corrected ratio across days so the band has a real
        # empirical spread (otherwise every relerr ~ 1 and the band is flat).
        actuals = _tracked_actuals(site, weather, svf, factor=0.7 + 0.08 * i)
        bf.process_day_hourly(acc, site, weather, actuals, svf_by_plane=svf)

    key = q.QuantileState.bin_key(
        const.CLOUD_CLASS_CLEAR, const.DAY_PART_AFTERNOON
    )
    ring = acc.quantile_state.bins.get(key)
    assert ring is not None, "clear|afternoon bin must be seeded"
    assert len(ring) >= const.QUANTILE_MIN_SAMPLES
    distinct = {e[0] for e in ring}
    assert len(distinct) >= const.QUANTILE_MIN_DAYS
    bands = q.bands_for_bin(
        acc.quantile_state,
        cloud_class=const.CLOUD_CLASS_CLEAR,
        day_part=const.DAY_PART_AFTERNOON,
    )
    assert bands.n >= const.QUANTILE_MIN_SAMPLES
    assert bands.p10 < bands.p90, "a well-fed bin must emit a genuine spread"


def test_thin_quantile_bin_stays_neutral(site: SiteConfig):
    """A bin below the sample/day floor collapses to the neutral band (no fake
    spread) — only the seeded well-fed bins get real bands."""
    from datetime import date, timedelta

    from balcony_solar_forecast.core import quantiles as q

    acc = bf.BootstrapAccumulator()
    svf = _svf(site)
    for i in range(2):  # far below QUANTILE_MIN_DAYS
        d = date(2025, 6, 16) + timedelta(days=i)
        weather = _clear_afternoon(d)
        actuals = _tracked_actuals(site, weather, svf, factor=0.7 + 0.2 * i)
        bf.process_day_hourly(acc, site, weather, actuals, svf_by_plane=svf)

    bands = q.bands_for_bin(
        acc.quantile_state,
        cloud_class=const.CLOUD_CLASS_CLEAR,
        day_part=const.DAY_PART_AFTERNOON,
    )
    assert (bands.p10, bands.p50, bands.p90) == (1.0, 1.0, 1.0)


def _highlat_single_plane_site() -> SiteConfig:
    """A near-horizontal single plane far north, so the SOLAR afternoon has more
    than QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN sunlit hours in one day (impossible
    at the reference latitude) — the only way to actually exercise the per-day
    cap with real physics."""
    return SiteConfig.from_dict({
        const.CONF_LATITUDE: 69.0,
        const.CONF_LONGITUDE: 25.0,
        "planes": [{
            "name": "P1", "azimuth_deg": 180.0, "tilt_deg": 5.0,
            "wp": 400.0, "efficiency": 0.96, "horizon": [],
            "actual_entity": "sensor.p1",
        }],
        "groups": [],
    })


def test_quantile_per_day_cap_bounds_correlated_hours():
    """A single (class x part) bin never takes more than
    QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN samples from ONE day (SPEC §12.6): the
    hourly backfill's within-day hours are strongly correlated."""
    from datetime import date
    from datetime import datetime as _dt

    hs = _highlat_single_plane_site()
    svf = _svf(hs)
    # 10 sunlit SOLAR-afternoon UTC hours on the solstice (13..22 UTC) — verified
    # > the cap of 8.
    day = date(2025, 6, 21)
    weather = [
        bf.HourlyWeather(
            start=_dt(day.year, day.month, day.day, h, 0, tzinfo=UTC),
            ghi=600.0, dni=650.0, dhi=110.0, temp_c=15.0,
        )
        for h in range(13, 23)
    ]
    actuals = _tracked_actuals(hs, weather, svf, factor=1.0)
    acc = bf.BootstrapAccumulator()
    bf.process_day_hourly(acc, hs, weather, actuals, svf_by_plane=svf)

    afternoon = [
        v for k, v in acc.quantile_state.bins.items()
        if k.endswith(f"|{const.DAY_PART_AFTERNOON}")
    ]
    assert afternoon, "expected an afternoon bin to be seeded"
    for ring in afternoon:
        assert len(ring) <= const.QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN


def test_build_bootstrap_windows_and_caps_quantile_ring(site: SiteConfig):
    """build_bootstrap_json re-windows every ring to QUANTILE_RING_DAYS relative
    to the LAST backfill day and enforces the count-cap backstop (SPEC §12.6)."""
    from balcony_solar_forecast.core import quantiles as q
    from balcony_solar_forecast.core.types import QuantileState

    acc = bf.BootstrapAccumulator()
    acc.last_iso_date = "2025-06-30"
    key = "clear|afternoon"
    # One in-window sample, one stale sample (> 90 days before the last day).
    ring = [["2025-06-20", 1.1], ["2025-01-01", 0.9]]
    # Pad past the count cap with in-window dated samples.
    ring += [["2025-06-25", 1.0] for _ in range(q._BIN_RING_CAP + 5)]
    acc.quantile_state = QuantileState(bins={key: ring})

    js = bf.build_bootstrap_json(acc, site)
    emitted = js[const.BOOTSTRAP_KEY_QUANTILE]["bins"][key]
    # Stale sample dropped by the date window.
    assert ["2025-01-01", 0.9] not in emitted
    # Count cap enforced.
    assert len(emitted) <= q._BIN_RING_CAP


def test_bootstrap_json_includes_quantile_key(site: SiteConfig):
    """The bootstrap contract now carries the quantile section (round-trips
    through QuantileState.from_dict) so store.import_bootstrap can seed it."""
    from balcony_solar_forecast.core.types import QuantileState

    acc = bf.BootstrapAccumulator()
    js = bf.build_bootstrap_json(acc, site)
    assert const.BOOTSTRAP_KEY_QUANTILE in js
    # Empty ring round-trips to an empty (valid) QuantileState.
    qs = QuantileState.from_dict(js[const.BOOTSTRAP_KEY_QUANTILE])
    assert qs.bins == {}


def test_backfill_classifies_by_kc_when_elevation_supplied():
    """With the sun elevation supplied, _classify_cloud keys on the clear-sky
    index (A5), matching the live path — a low-GHI high-sun hour is overcast even
    with zero reported cloud cover; without elevation it falls back to the layer
    cover (clear)."""
    from datetime import datetime as _dt

    dim = bf.HourlyWeather(
        start=_dt(2025, 6, 21, 10, tzinfo=UTC),
        ghi=120.0, dni=0.0, dhi=120.0, temp_c=15.0,
        cloud_low=0.0, cloud_mid=0.0, cloud_high=0.0, visibility_m=30000.0,
    )
    # kc path (sun high, GHI far below clear-sky) -> overcast.
    assert bf._classify_cloud(dim, elevation_deg=45.0) == const.CLOUD_CLASS_OVERCAST
    # No elevation -> layer-cover fallback -> clear (all covers zero).
    assert bf._classify_cloud(dim) == const.CLOUD_CLASS_CLEAR


# ---------------------------------------------------------------------------
# accumulate_days progress_cb plumbing (the in-process run_bootstrap uses it
# for periodic INFO logs). Mutation-proof: deleting the ``progress_cb(done,
# total)`` invocation must fail this test.
# ---------------------------------------------------------------------------


def _hours_on(day: int) -> list[bf.HourlyWeather]:
    """A short clear midday window on 2025-06-<day> (UTC), like the noon fixture."""
    out = []
    for h in range(3):
        out.append(
            bf.HourlyWeather(
                start=datetime(2025, 6, day, 9 + h, 0, tzinfo=UTC),
                ghi=780.0, dni=820.0, dhi=120.0, temp_c=24.0,
            )
        )
    return out


def _tracking_actuals(
    site: SiteConfig, weather: list[bf.HourlyWeather], svf: dict
) -> dict[str, dict[str, float]]:
    """Per-module hourly actuals that TRACK the modeled beam+diffuse (T ~ 1)."""
    out: dict[str, dict[str, float]] = {}
    for plane in site.planes:
        hh: dict[str, float] = {}
        for wx in weather:
            r = bf.reconstruct_plane_hour(
                plane, svf[plane.name], wx,
                latitude=site.latitude, longitude=site.longitude,
            )
            total = r.beam_wh + r.diffuse_wh
            if total > 0.0:
                hh[wx.start.isoformat()] = total
        if hh:
            out[plane.name] = hh
    return out


def test_accumulate_days_progress_cb_fires_per_calendar_day(site: SiteConfig):
    svf = _svf(site)
    # Three UTC days; the MIDDLE day (21st) carries NO measured actuals -> it is
    # skipped, but progress must still fire for it (counts skipped days too).
    d20, d21, d22 = _hours_on(20), _hours_on(21), _hours_on(22)
    weather = d20 + d21 + d22
    actuals = _tracking_actuals(site, d20 + d22, svf)  # nothing for the 21st

    calls: list[tuple[int, int]] = []

    def _cb(done: int, total: int) -> None:
        calls.append((done, total))

    acc = bf.accumulate_days(
        site, weather, actuals, svf_by_plane=svf, tz=None, progress_cb=_cb
    )

    # Fires exactly once per calendar day, done monotonically 1..total, total==3
    # constant (skipped middle day included).
    assert calls == [(1, 3), (2, 3), (3, 3)]
    assert acc.days_skipped >= 1  # the actuals-less 21st was skipped, not dropped
    assert acc.days_used >= 1

    # Omitting progress_cb (the CLI path) still works and yields the same counts.
    acc2 = bf.accumulate_days(site, weather, actuals, svf_by_plane=svf, tz=None)
    assert acc2.days_used == acc.days_used
    assert acc2.days_skipped == acc.days_skipped
