#!/usr/bin/env python3
"""One-shot Previous-Runs backfill -> learner bootstrap JSON (SPEC §6).

DEV-MACHINE script (NOT run on Home Assistant). It reconstructs a warm start
for the two learning layers from ~2 years of history so the system does not
face its first live winter cold:

  1. Fetch Open-Meteo **Previous-Runs** day-1-lead forecasts-as-issued for the
     site (endpoint ``previous-runs-api.open-meteo.com``, archived since
     01/2024). If the forecast-as-issued variables are unavailable it degrades
     to the plain **Historical Forecast API** with a loud WARNING that the data
     is analysis-not-forecast (still useful for the geometric shademap, less so
     for the weather-error day-ahead bias).
  2. Reconstruct per-plane HOURLY modeled curves (beam / diffuse / ghi / kc) by
     importing the repo's ``core/`` package (the SAME physics the live engine
     runs) — pure Python, no numpy.
  3. Pull measured hourly per-module energy from the operator's HA long-term
     statistics via the **WebSocket API** (``recorder/statistics_during_period``;
     ``--ha-url`` + ``--token`` CLI args).
  4. Compute:
       * day-ahead RLS bias states per (cloud class x day part), and
       * shademap bins (beam-referenced transmittance EMA), with the backfilled
         sample count **n capped at BOOTSTRAP_MAX_BIN_N** — hourly smearing
         makes backfilled bins less trustworthy, so live 15-min data overrides
         quickly (SPEC §6).
  5. Emit a bootstrap JSON matching the frozen contract schema; the
     ``balcony_solar_forecast.import_bootstrap`` service ingests it
     (validate + clamp, rejects unknown schema).

Robust to gaps: any day missing weather or actuals is skipped with a warning.

The reconstruction / bootstrap MATH is pure and importable; the network
(aiohttp) layer is imported lazily inside the async functions so the unit
tests (``tests/core/test_backfill_math.py``) exercise the math with fixture
weather and NO network.

Usage (see docs/BACKFILL.md for the full operator runbook):

    python scripts/backfill.py \\
        --ha-url http://homeassistant.local:8123 \\
        --token "$HA_LONG_LIVED_TOKEN" \\
        --start 2024-07-01 --end 2026-07-01 \\
        --out bootstrap.json

Add ``--dry-run`` to fetch + reconstruct + summarise WITHOUT writing the file.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import types
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Make the HA-free core importable WITHOUT running the package __init__ (which
# imports Home Assistant). Mirrors tests/core/conftest.py: register the package
# roots as namespace packages pointing at the real dirs, so
# ``import balcony_solar_forecast.core.solpos`` resolves straight to the file.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_CUSTOM_COMPONENTS = _REPO_ROOT / "custom_components"
_PKG_DIR = _CUSTOM_COMPONENTS / "balcony_solar_forecast"


def _register_namespace_package(name: str, path: Path) -> None:
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)]
    mod.__package__ = name
    sys.modules[name] = mod


if str(_CUSTOM_COMPONENTS) not in sys.path:
    sys.path.insert(0, str(_CUSTOM_COMPONENTS))
_register_namespace_package("balcony_solar_forecast", _PKG_DIR)
_register_namespace_package("balcony_solar_forecast.core", _PKG_DIR / "core")

# Now safe to import the pure core + const (no HA).
from balcony_solar_forecast import const  # noqa: E402
from balcony_solar_forecast.core import bias as bias_mod  # noqa: E402
from balcony_solar_forecast.core import (  # noqa: E402
    clearsky,
    electrical,
    horizon,
    solpos,
    transpose,
)
from balcony_solar_forecast.core import (  # noqa: E402
    shademap as shademap_mod,
)
from balcony_solar_forecast.core.types import (  # noqa: E402
    BiasCell,
    BiasState,
    PlaneConfig,
    ShademapBin,
    ShademapState,
    SiteConfig,
)

_LOGGER = logging.getLogger("balcony_solar_forecast.backfill")

# --- Open-Meteo endpoints (SPEC §6, verified 2026-07-06) -------------------
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
# Day-1 lead: value predicted ~24 h before valid time (SPEC §2/§9: as-issued).
PREVIOUS_RUN_LEAD_DAY = 1
# The radiation + temperature variables we transpose locally. On the
# Previous-Runs API these carry a "_previous_dayN" suffix (as-issued); on the
# Historical Forecast API they are plain.
_RADIATION_VARS = (
    "shortwave_radiation",       # GHI
    "direct_normal_irradiance",  # DNI
    "diffuse_radiation",         # DHI
    "temperature_2m",
)
# Cloud / visibility / snow context for the cloud classifier. Best-effort:
# absent variables degrade the cloud class to "mixed" for that hour rather than
# aborting (the shademap does not need them; only the day-ahead bias does).
_CONTEXT_VARS = (
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "visibility",
    "snow_depth",
)
_HTTP_TIMEOUT_SECONDS = 120.0


# ===========================================================================
# Pure data structures for the reconstruction (HA-free, testable).
# ===========================================================================


@dataclass(frozen=True)
class HourlyWeather:
    """One reconstructed hourly weather record (as-issued or analysis).

    All irradiance in W/m^2 (hourly mean), temperature in deg C. ``start`` is
    the tz-aware UTC hour start. Context fields default to neutral so a partial
    provider payload still reconstructs the physics.
    """

    start: datetime
    ghi: float
    dni: float
    dhi: float
    temp_c: float
    cloud_low: float = 0.0
    cloud_mid: float = 0.0
    cloud_high: float = 0.0
    visibility_m: float = 0.0
    snow_depth_m: float = 0.0


@dataclass(frozen=True)
class PlaneHourReconstruction:
    """Per-plane modeled split for one hour (mirrors PlaneHourlyModeled cells).

    ``beam_wh`` is the **UNGATED** modeled DC energy (Wh) from beam+circumsolar
    POA — i.e. the beam that would arrive with a CLEAR horizon (static tau = 1).
    This is deliberate: the shademap learns a beam-referenced transmittance
    ``T = (P_measured − P_diffuse) / P_beam`` that **replaces** the static
    horizon tau of the bin (SPEC §5), so the reference beam must be the
    un-attenuated geometric beam — otherwise a shaded bin (static tau ~ 0)
    would have ~0 modeled beam, fail the beam-share gate, and never learn the
    very shade it exists to capture.

    ``diffuse_wh`` is the SVF-gated diffuse(iso)+ground DC energy — the real
    diffuse floor the panel sees (the diffuse is not what the shademap learns).

    ``gated_total_wh`` is the modeled DC energy WITH the static horizon beam
    gate applied (what the live pure-physics engine issues); it feeds the
    day-ahead bias aggregation so that layer trains against the forecast the
    engine actually serves. ``ghi`` is the horizontal GHI, ``kc`` the clear-sky
    index at the hour's sun position; sun az/el + ``beam_share`` (ungated) drive
    the quasi-clear gate + bin key.
    """

    beam_wh: float          # UNGATED beam+circumsolar DC (shademap reference)
    diffuse_wh: float       # SVF-gated diffuse+ground DC (the shade floor)
    gated_total_wh: float   # static-horizon-gated total DC (day-ahead bias)
    ghi: float
    kc: float
    sun_az: float
    sun_el: float
    beam_share: float       # ungated beam DC / Wp (>5% quasi-clear gate)


@dataclass
class BootstrapAccumulator:
    """Accumulates the two learner bootstraps across all processed days."""

    # Shademap: {channel: {bin_key: [running_tau, n]}}. EMA over quasi-clear
    # hourly samples; n is capped at emit time.
    shade: dict[str, dict[str, list]] = field(default_factory=dict)
    # Day-ahead RLS: {cell_key: BiasCell}. Trained by a scalar RLS step per
    # (day x cell) aggregated Wh pair.
    bias: dict[str, BiasCell] = field(default_factory=dict)
    days_used: int = 0
    days_skipped: int = 0
    shade_samples: int = 0
    bias_samples: int = 0


# ===========================================================================
# Weather-payload parsing (pure) — Previous-Runs and Historical Forecast.
# ===========================================================================


def _as_utc_hour(value: str) -> datetime:
    """Parse an Open-Meteo (UTC, no suffix) hourly ISO stamp to aware UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def parse_hourly_payload(
    payload: dict,
    *,
    var_suffix: str = "",
) -> list[HourlyWeather]:
    """Parse an Open-Meteo hourly payload into HourlyWeather records (pure).

    ``var_suffix`` is appended to each radiation variable name when reading the
    Previous-Runs API (e.g. "_previous_day1"); the Historical Forecast API uses
    an empty suffix. Context variables (cloud/visibility/snow) are read
    UNSUFFIXED best-effort — the Previous-Runs API exposes forecast-as-issued
    radiation, while cloud context is taken from the same call's plain hourly
    block when present. Hours missing any of GHI/DNI/DHI/temp are dropped
    (the engine cannot transpose them); the caller sees a shorter list.

    Raises ``ValueError`` on a structurally broken payload (missing hourly
    block or time array) — the network layer maps that to a skipped range.
    """
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise ValueError("payload missing 'hourly' block")
    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        raise ValueError("payload 'hourly.time' missing or empty")

    def _col(name: str) -> list:
        col = hourly.get(name)
        return col if isinstance(col, list) else []

    ghi_c = _col(f"{_RADIATION_VARS[0]}{var_suffix}")
    dni_c = _col(f"{_RADIATION_VARS[1]}{var_suffix}")
    dhi_c = _col(f"{_RADIATION_VARS[2]}{var_suffix}")
    temp_c = _col(f"{_RADIATION_VARS[3]}{var_suffix}")

    cl_low = _col("cloud_cover_low")
    cl_mid = _col("cloud_cover_mid")
    cl_high = _col("cloud_cover_high")
    vis = _col("visibility")
    snow = _col("snow_depth")

    def _at(col: list, i: int) -> float | None:
        return _num(col[i]) if i < len(col) else None

    out: list[HourlyWeather] = []
    for i, stamp in enumerate(times):
        if not isinstance(stamp, str):
            continue
        ghi = _at(ghi_c, i)
        dni = _at(dni_c, i)
        dhi = _at(dhi_c, i)
        temp = _at(temp_c, i)
        # Physics inputs are mandatory; drop the hour if any is missing.
        if ghi is None or dni is None or dhi is None or temp is None:
            continue
        out.append(
            HourlyWeather(
                start=_as_utc_hour(stamp),
                ghi=max(0.0, ghi),
                dni=max(0.0, dni),
                dhi=max(0.0, dhi),
                temp_c=temp,
                cloud_low=_at(cl_low, i) or 0.0,
                cloud_mid=_at(cl_mid, i) or 0.0,
                cloud_high=_at(cl_high, i) or 0.0,
                visibility_m=_at(vis, i) or 0.0,
                snow_depth_m=_at(snow, i) or 0.0,
            )
        )
    return out


# ===========================================================================
# Reconstruction (pure) — per-plane hourly modeled curves via core/.
# ===========================================================================
#
# The live engine works on 15-min interval-mean slots; the backfill only has
# HOURLY weather. We evaluate the SAME physics (solpos -> hay_davies_poa ->
# horizon gate -> electrical.dc_power) at the hour MIDPOINT and treat the
# resulting instantaneous DC power as the hour's mean (hourly-mean semantics),
# so hour energy Wh == mean power W * 1 h. This is exactly the smearing the
# SPEC calls out as the reason to cap the backfilled bin n (§6): sub-hour
# geometry is lost, so a backfilled bin is worth less than a live 15-min one.


def _hour_midpoint(hour_start: datetime) -> datetime:
    return hour_start + timedelta(minutes=30)


def _slot_albedo(snow_depth_m: float) -> float:
    if snow_depth_m is not None and snow_depth_m > const.SNOW_DEPTH_THRESHOLD_M:
        return const.ALBEDO_SNOW
    return const.ALBEDO_DEFAULT


def reconstruct_plane_hour(
    plane: PlaneConfig,
    svf: float,
    wx: HourlyWeather,
    *,
    latitude: float,
    longitude: float,
) -> PlaneHourReconstruction:
    """Reconstruct one plane's modeled hour split using the repo's core/.

    Mirrors ``engine._plane_poa`` exactly (same horizon beam gate + SVF diffuse
    gate + snow albedo), but keeps the beam+circumsolar and diffuse+ground POA
    components SEPARATE so the shademap's beam-referenced transmittance can be
    trained. Returns the modeled DC energy split for the hour (Wh == mean W *
    1 h), plus the sun position, kc and beam_share needed for the gate + bin
    key. Non-daylight / behind-plane hours yield an all-zero record.
    """
    midpoint = _hour_midpoint(wx.start)
    sun_az, sun_el = solpos.sun_position(midpoint, latitude, longitude)
    doy = midpoint.timetuple().tm_yday
    albedo = _slot_albedo(wx.snow_depth_m)
    # THE shared hourly-kc reduction (clearsky.hourly_kc), same estimator the
    # live nightly trainer applies to its 15-min slots; at this hourly
    # resolution the single sample reduces exactly to clear_sky_index.
    kc = clearsky.hourly_kc(((wx.ghi, sun_el),))

    comps = transpose.hay_davies_poa(
        ghi=wx.ghi,
        dni=wx.dni,
        dhi=wx.dhi,
        sun_az=sun_az,
        sun_el=sun_el,
        plane_az=plane.azimuth_deg,
        plane_tilt=plane.tilt_deg,
        albedo=albedo,
    )
    beam = comps.get("beam", 0.0)
    circ = comps.get("circumsolar", 0.0)
    iso = comps.get("isotropic", 0.0)
    ground = comps.get("ground", 0.0)

    # Incidence-angle modifier (ASHRAE, const IAM_B0) — byte-identical to
    # engine._plane_poa_split: applied to beam+circumsolar BEFORE the ungated
    # reference so the bootstrap trains the same optics-corrected T as live.
    cos_theta = comps.get("cos_theta")
    if cos_theta is not None:
        f_iam = transpose.ashrae_iam(cos_theta)
        beam *= f_iam
        circ *= f_iam

    # UNGATED beam+circumsolar POA (static tau = 1): the counterfactual clear-
    # horizon beam the shademap references, so a shaded bin still has a non-zero
    # modeled beam to divide by (SPEC §5 — the learned T REPLACES the static
    # tau, so the reference must be un-attenuated).
    beam_poa_ungated = max(0.0, beam + circ)

    # Static horizon beam gate (identical rule to engine._plane_poa): the beam
    # the live pure-physics engine actually issues, for the day-ahead bias.
    static_tau = 1.0
    horizon_elev = horizon.interp_elevation(plane, sun_az)
    if sun_el <= horizon_elev:
        static_tau = horizon.transmittance_at(plane, sun_az, doy)
    beam_poa_gated = beam_poa_ungated * static_tau

    # Diffuse sky-view gate: static per-plane isotropic reduction. The ground
    # reflection is unaffected by the horizon (it comes from below).
    diffuse_poa = max(0.0, iso * svf + ground)

    # DC power is NOT linear in the POA split (Ross cell temperature depends on
    # the TOTAL incident POA). The panel's real operating point is the GATED
    # total POA, so we derive a single Wp-per-POA conversion at that cell temp
    # and apply it to every component (ungated beam, gated beam, diffuse). This
    # keeps the diffuse floor and the beam on the same temperature regime for a
    # consistent T = (P_meas - P_diffuse) / P_beam, and makes beam_dc linear in
    # tau (so the ungated beam is exactly beam_gated / static_tau).
    gated_total_poa = beam_poa_gated + diffuse_poa
    gated_total_dc = electrical.dc_power(
        gated_total_poa, plane.wp, wx.temp_c, plane.efficiency
    )
    conv = (gated_total_dc / gated_total_poa) if gated_total_poa > 0.0 else 0.0

    beam_dc_ungated = beam_poa_ungated * conv
    diffuse_dc = diffuse_poa * conv

    # beam_share uses the UNGATED beam so a fully-shaded bin still qualifies for
    # training (it is the shade we want to learn); fraction of the plane Wp.
    beam_share = (beam_dc_ungated / plane.wp) if plane.wp > 0.0 else 0.0

    return PlaneHourReconstruction(
        beam_wh=beam_dc_ungated,   # UNGATED; hourly-mean W == Wh over 1 h
        diffuse_wh=diffuse_dc,
        gated_total_wh=gated_total_dc,
        ghi=wx.ghi,
        kc=kc,
        sun_az=sun_az,
        sun_el=sun_el,
        beam_share=beam_share,
    )


# ===========================================================================
# Bootstrap math (pure) — mirrors the frozen learner contracts EXACTLY so the
# emitted JSON round-trips through store.import_bootstrap and the two learner
# modules once implemented. We re-implement the small closed-form updates here
# (rather than importing the skeleton bias/shademap functions, which are still
# NotImplementedError) but keep them byte-for-byte consistent with the const
# tunables + the docstring'd semantics in core/bias.py and core/shademap.py.
# ===========================================================================


def _clamp(v: float, lo: float, hi: float) -> float:
    if v != v:  # NaN
        return lo
    return lo if v < lo else hi if v > hi else v


# The live shademap functions are now implemented, so the backfill imports them
# directly instead of keeping parallel copies (the old copies drifted: the
# in-house half_year_index put doy 355-366 in the WRONG half, aliasing sparse
# winter bins). Importing the single implementation guarantees byte-identical
# bin keys between the offline bootstrap and live training (backfill:444).
half_year_index = shademap_mod.half_year_index
shademap_bin_key = shademap_mod.shademap_bin_key
is_quasi_clear = shademap_mod.is_quasi_clear


def _shade_update(
    acc: BootstrapAccumulator,
    channel: str,
    bin_key: str,
    measured_t: float,
) -> None:
    """EMA-update one channel/bin with a beam-referenced T (mirrors update_bin).

    New tau = (1-alpha)*old + alpha*clamp(T); a fresh bin seeds at clamp(T).
    n incremented (capped at emit time). Input untouched semantics are moot
    here since we accumulate into the mutable accumulator.
    """
    t = _clamp(measured_t, const.SHADEMAP_TAU_MIN, const.SHADEMAP_TAU_MAX)
    chan = acc.shade.setdefault(channel, {})
    cell = chan.get(bin_key)
    if cell is None:
        chan[bin_key] = [t, 1]
    else:
        old_tau, n = cell
        alpha = const.SHADEMAP_EMA_ALPHA
        cell[0] = (1.0 - alpha) * old_tau + alpha * t
        cell[1] = n + 1
    acc.shade_samples += 1


def _rls_step(cell: BiasCell, modeled: float, measured: float) -> BiasCell:
    """One single-parameter RLS step for the day-ahead bias (mirrors bias).

    Regresses measured on modeled to estimate the multiplicative bias theta:
        y = theta * x           (x = modeled Wh, y = measured Wh)
    Standard scalar RLS with forgetting factor lambda:
        k     = P*x / (lambda + x*P*x)
        theta = theta + k*(y - theta*x)
        P     = (P - k*x*P) / lambda
    theta clamped to [DAY_AHEAD_BIAS_MIN, MAX]; n incremented. Returns a NEW
    BiasCell (frozen).
    """
    x = float(modeled)
    y = float(measured)
    if x <= 0.0:
        # No modeled signal -> the pair carries no bias information; skip but
        # still count the day so RLS_MIN_SAMPLES reflects real evidence only
        # for informative days. Return the cell unchanged.
        return cell
    lam = const.RLS_FORGETTING_FACTOR
    p = cell.covariance if cell.covariance > 0.0 else const.RLS_INIT_COVARIANCE
    denom = lam + x * p * x
    if denom <= 0.0:
        return cell
    k = (p * x) / denom
    theta = cell.theta + k * (y - cell.theta * x)
    theta = _clamp(theta, const.DAY_AHEAD_BIAS_MIN, const.DAY_AHEAD_BIAS_MAX)
    new_p = (p - k * x * p) / lam
    if new_p <= 0.0:
        new_p = const.RLS_INIT_COVARIANCE
    return BiasCell(theta=theta, covariance=new_p, n=cell.n + 1)


# ===========================================================================
# Per-day processing (pure given weather + actuals) — the testable core.
# ===========================================================================


def process_day(
    acc: BootstrapAccumulator,
    site: SiteConfig,
    day_weather: list[HourlyWeather],
    day_actuals: dict[str, float],
    *,
    svf_by_plane: dict[str, float],
    tz: timezone | None = None,
) -> bool:
    """Fold one day's weather + measured per-module Wh into the accumulator.

    Trains BOTH bootstraps from this day:
      * SHADEMAP: for each plane/hour, reconstruct the modeled beam/diffuse
        split, gate quasi-clear, and — where the operator's measured HOURLY
        energy for that module is available — EMA-update the bin with the
        beam-referenced T = (P_measured - P_diffuse_modeled) / P_beam_modeled.
        (Hourly LTS gives one measured value per module per DAY here; see the
        note below on how per-hour attribution is done.)
      * DAY-AHEAD BIAS: aggregate modeled vs. measured SITE Wh per (cloud class
        x day part) and run one RLS step per populated cell.

    Returns True if the day contributed at least one sample, False if it was
    effectively empty (caller counts skips). Never raises on a partial day.

    Measured-energy model: the operator's LTS is hourly per module. For the
    shademap we need a PER-HOUR measured module energy; ``day_actuals`` here is
    the module's total-day Wh, so we distribute it across the day's daylight
    hours in proportion to each hour's MODELED total DC energy for that module
    (a shape-preserving disaggregation). This is deliberately coarse — exactly
    why backfilled bins get their n capped (SPEC §6). When the caller supplies
    true hourly actuals (see ``process_day_hourly``) that path is used instead.
    """
    return _process_day_impl(
        acc, site, day_weather,
        actuals_daily=day_actuals,
        actuals_hourly=None,
        svf_by_plane=svf_by_plane,
        tz=tz,
    )


def process_day_hourly(
    acc: BootstrapAccumulator,
    site: SiteConfig,
    day_weather: list[HourlyWeather],
    hourly_actuals: dict[str, dict[str, float]],
    *,
    svf_by_plane: dict[str, float],
    tz: timezone | None = None,
) -> bool:
    """Like :func:`process_day` but with TRUE hourly measured module energy.

    ``hourly_actuals`` maps ``{module_name: {iso_hour: measured_wh}}`` (the
    shape the WebSocket LTS reader produces). Preferred over the daily
    disaggregation whenever the recorder returns hourly buckets. ``tz`` (when
    given) converts hour starts to local before the day-part / cloud-class
    classification so the RLS cells match the live layer (backfill:842).
    """
    return _process_day_impl(
        acc, site, day_weather,
        actuals_daily=None,
        actuals_hourly=hourly_actuals,
        svf_by_plane=svf_by_plane,
        tz=tz,
    )


def _process_day_impl(
    acc: BootstrapAccumulator,
    site: SiteConfig,
    day_weather: list[HourlyWeather],
    *,
    actuals_daily: dict[str, float] | None,
    actuals_hourly: dict[str, dict[str, float]] | None,
    svf_by_plane: dict[str, float],
    tz: timezone | None = None,
) -> bool:
    if not day_weather:
        return False

    lat = site.latitude
    lon = site.longitude
    planes = site.planes

    # --- 1) Reconstruct every plane's modeled split for every hour. ---
    # recon[plane][iso_hour] = PlaneHourReconstruction
    recon: dict[str, dict[str, PlaneHourReconstruction]] = {
        p.name: {} for p in planes
    }
    # Per-plane modeled GATED total DC Wh (the pure-physics forecast the engine
    # issues), for the daily->hourly disaggregation shape AND the day-ahead bias
    # modeled site energy. NOTE: this uses ``gated_total_wh`` (static-horizon
    # beam gate applied), NOT the ungated shademap reference beam.
    modeled_total_by_plane: dict[str, dict[str, float]] = {
        p.name: {} for p in planes
    }
    # kc per hour (site-level, taken from the GHI which is shared) for the
    # neighbour-stability gate.
    kc_by_hour: dict[str, float] = {}

    for wx in day_weather:
        hkey = wx.start.isoformat()
        for plane in planes:
            r = reconstruct_plane_hour(
                plane, svf_by_plane[plane.name], wx,
                latitude=lat, longitude=lon,
            )
            recon[plane.name][hkey] = r
            modeled_total_by_plane[plane.name][hkey] = r.gated_total_wh
        # kc is GHI/haurwitz at this hour's midpoint sun; reuse any plane's.
        any_r = recon[planes[0].name][hkey] if planes else None
        if any_r is not None:
            kc_by_hour[hkey] = any_r.kc

    hours_sorted = sorted(kc_by_hour.keys())

    contributed = False

    # --- 2) SHADEMAP: per plane, per hour, gate quasi-clear + EMA-update. ---
    # ONLY train the geometric shademap from TRUE hourly actuals. The daily-total
    # disaggregation fabricates per-hour "measured" energy proportional to the
    # statically GATED modeled shape, so a bin's learned T would just re-derive
    # the (possibly wrong) static horizon prior — circular training on the
    # model's own output (backfill:792). The day-ahead bias below still uses the
    # daily disaggregation, where the daily ratio IS the real signal.
    #
    # Day-level hygiene gates (mirror the LIVE nightly trainer, SPEC §5): two
    # years of history certainly contain snow-cover and frozen-sensor days, and
    # without these gates a snow day passes every per-hour check (forecast-side
    # kc is clear, the measured/modeled ratio is uniformly near-zero so the
    # neighbour-stability leg HOLDS) and seeds tau~0 into every winter bin.
    #   * measured-clear day gate (mirrors coordinator._day_is_measured_clear):
    #     the day's TRUE measured site energy must reach
    #     SHADEMAP_MEASURED_CLEAR_MIN_FRAC of the gated modeled forecast, else
    #     the reality was overcast/collapsed and training would write weather
    #     (or snow occlusion) into the geometry;
    #   * per-hour snow gate: hours with snow depth above the albedo threshold
    #     never train (snow on the panels is weather, not geometry);
    #   * frozen-channel gate (mirrors coordinator._is_frozen_channel): a module
    #     whose hourly means repeat byte-identically is a stuck sensor — drop
    #     the module-day.
    snow_by_hour = {wx.start.isoformat(): wx.snow_depth_m for wx in day_weather}
    shademap_day_ok = actuals_hourly is not None
    if shademap_day_ok:
        site_modeled_gated = sum(
            sum(modeled_total_by_plane[p.name].values()) for p in planes
        )
        site_measured_true = sum(
            sum(hours.values()) for hours in actuals_hourly.values()
        )
        if site_modeled_gated <= 0.0 or site_measured_true < (
            const.SHADEMAP_MEASURED_CLEAR_MIN_FRAC * site_modeled_gated
        ):
            shademap_day_ok = False
    for plane in (planes if shademap_day_ok else ()):
        chan = plane.name
        measured_hourly = _resolve_hourly_measured(
            chan,
            actuals_daily=actuals_daily,
            actuals_hourly=actuals_hourly,
            modeled_hourly=modeled_total_by_plane[chan],
        )
        if not measured_hourly:
            continue  # channel dropout for this module today -> skip module
        if _is_frozen_hourly(
            [measured_hourly[h] for h in sorted(measured_hourly)]
        ):
            continue  # stuck Hoymiles/DTU sensor: drop the module-day
        # Per-hour measured/modeled energy ratio for the neighbour-stability leg
        # (ratio-space, identical to the live nightly trainer); None where no
        # usable ratio exists. Gating on the measured ratio — not the smooth
        # forecast k_c — is what rejects a real cloud fluctuation.
        ratio_seq: list[float | None] = []
        for hkey in hours_sorted:
            rr = recon[chan].get(hkey)
            pm = measured_hourly.get(hkey)
            denom = (rr.beam_wh + rr.diffuse_wh) if rr is not None else 0.0
            ratio_seq.append(
                pm / denom if (pm is not None and denom > 0.0) else None
            )
        for idx, hkey in enumerate(hours_sorted):
            r = recon[chan].get(hkey)
            if r is None:
                continue
            p_meas = measured_hourly.get(hkey)
            if p_meas is None:
                continue
            if r.beam_wh <= 0.0:
                continue  # no modeled beam -> transmittance undefined
            if snow_by_hour.get(hkey, 0.0) > const.SNOW_DEPTH_THRESHOLD_M:
                continue  # snow on the panels: weather occlusion, not geometry
            neighbour_ratio = ratio_seq[idx - 1] if idx > 0 else None
            if not is_quasi_clear(
                kc=r.kc,
                sun_el=r.sun_el,
                beam_share=r.beam_share,
                stability_ratio=ratio_seq[idx],
                neighbour_ratio=neighbour_ratio,
            ):
                continue
            # Beam-referenced transmittance (SPEC §5): subtract the modeled
            # diffuse floor, divide by the modeled beam.
            measured_t = (p_meas - r.diffuse_wh) / r.beam_wh
            bin_key = shademap_bin_key(r.sun_az, r.sun_el, _doy_of(hkey))
            _shade_update(acc, chan, bin_key, measured_t)
            contributed = True

    # --- 3) DAY-AHEAD BIAS: aggregate site Wh per (cloud class x day part). ---
    # cell_key -> [modeled_wh, measured_wh]
    cell_agg: dict[str, list[float]] = {}
    # Site measured per hour = sum of modules' measured that hour (best-effort).
    site_measured_hourly = _site_measured_hourly(
        planes, actuals_daily=actuals_daily, actuals_hourly=actuals_hourly,
        modeled_total_by_plane=modeled_total_by_plane,
    )
    for wx in day_weather:
        hkey = wx.start.isoformat()
        modeled_site = sum(
            modeled_total_by_plane[p.name].get(hkey, 0.0) for p in planes
        )
        measured_site = site_measured_hourly.get(hkey)
        if measured_site is None:
            continue
        local_hour, _month = _local_hour(wx.start, tz)
        cloud_class = _classify_cloud(wx, tz)
        day_part = _day_part_for_hour(local_hour, tz)
        key = BiasState.cell_key(cloud_class, day_part)
        agg = cell_agg.setdefault(key, [0.0, 0.0])
        agg[0] += modeled_site
        agg[1] += measured_site

    for key, (modeled_wh, measured_wh) in cell_agg.items():
        if modeled_wh <= 0.0:
            continue
        cell = acc.bias.get(key, BiasCell())
        acc.bias[key] = _rls_step(cell, modeled_wh, measured_wh)
        acc.bias_samples += 1
        contributed = True

    return contributed


# ---------------------------------------------------------------------------
# Small pure helpers used by _process_day_impl.
# ---------------------------------------------------------------------------


def _doy_of(iso_hour: str) -> int:
    dt = datetime.fromisoformat(iso_hour)
    mid = dt + timedelta(minutes=30)
    return mid.timetuple().tm_yday


def _is_frozen_hourly(values: list[float]) -> bool:
    """True when hourly means show a frozen sensor (stuck non-zero value).

    Mirrors ``coordinator._is_frozen_channel`` (which lives in the HA glue and
    cannot be imported here): the SAME non-zero value held for
    ``LABEL_FROZEN_MIN_REPEATS`` or more consecutive hours. A run of identical
    zeros is legitimate night/shade and never trips the gate.
    """
    run = 1
    for i in range(1, len(values)):
        if values[i] == values[i - 1] and values[i] != 0.0:
            run += 1
            if run >= const.LABEL_FROZEN_MIN_REPEATS:
                return True
        else:
            run = 1
    return False


def _resolve_hourly_measured(
    channel: str,
    *,
    actuals_daily: dict[str, float] | None,
    actuals_hourly: dict[str, dict[str, float]] | None,
    modeled_hourly: dict[str, float],
) -> dict[str, float]:
    """Per-hour measured Wh for one module.

    True hourly actuals are used verbatim when present. Otherwise the module's
    daily total is disaggregated across daylight hours in proportion to the
    MODELED total DC energy that hour (shape-preserving; SPEC §6 coarse
    backfill). Returns {} when neither source has data for the module.
    """
    if actuals_hourly is not None:
        return dict(actuals_hourly.get(channel, {}))
    if actuals_daily is None:
        return {}
    total = actuals_daily.get(channel)
    if total is None or total <= 0.0:
        return {}
    modeled_sum = sum(v for v in modeled_hourly.values() if v > 0.0)
    if modeled_sum <= 0.0:
        return {}
    out: dict[str, float] = {}
    for hkey, mod in modeled_hourly.items():
        if mod > 0.0:
            out[hkey] = total * (mod / modeled_sum)
    return out


def _site_measured_hourly(
    planes,
    *,
    actuals_daily: dict[str, float] | None,
    actuals_hourly: dict[str, dict[str, float]] | None,
    modeled_total_by_plane: dict[str, dict[str, float]],
) -> dict[str, float]:
    """Sum module measured energy into a site total per hour (best-effort)."""
    site: dict[str, float] = {}
    have_any = False
    for plane in planes:
        measured = _resolve_hourly_measured(
            plane.name,
            actuals_daily=actuals_daily,
            actuals_hourly=actuals_hourly,
            modeled_hourly=modeled_total_by_plane[plane.name],
        )
        if measured:
            have_any = True
        for hkey, wh in measured.items():
            site[hkey] = site.get(hkey, 0.0) + wh
    return site if have_any else {}


def _local_hour(dt: datetime, tz: timezone | None) -> tuple[int, int]:
    """(local_hour, local_month) for a UTC hour start under ``tz`` (UTC if None).

    Converting to the site's local time before day-part / fog-month
    classification mirrors coordinator._day_part_for_hourkey (backfill:842):
    keying on the raw UTC hour shifts every cell 1-2 h and pollutes the RLS
    prior for the operator's morning-shade site.
    """
    local = dt if tz is None else dt.astimezone(tz)
    return local.hour, local.month


def _classify_cloud(wx: HourlyWeather, tz: timezone | None = None) -> str:
    """Cloud class via the live bias.classify_cloud (SPEC §5).

    The fog-month test uses the LOCAL month (``tz``) so a late-evening UTC hour
    does not fall into the wrong month near a boundary.
    """
    _hr, month = _local_hour(wx.start, tz)
    return bias_mod.classify_cloud(
        cloud_low=wx.cloud_low,
        cloud_mid=wx.cloud_mid,
        cloud_high=wx.cloud_high,
        visibility_m=wx.visibility_m if wx.visibility_m > 0.0 else float("inf"),
        month=month,
    )


def _day_part_for_hour(utc_hour: int, tz: timezone | None = None) -> str:
    """Day part via the live bias.day_part_for_hour (SPEC §5).

    ``utc_hour`` is the hour start's UTC hour; the caller passes ``tz`` so it is
    converted to the site-local hour first (backfill:842). Kept int-in for the
    existing unit tests (tz=None => identity).
    """
    return bias_mod.day_part_for_hour(utc_hour)


# ===========================================================================
# Bootstrap emission (pure) — cap n, build contract JSON.
# ===========================================================================


def build_bootstrap_json(
    acc: BootstrapAccumulator,
    site: SiteConfig,
    *,
    generated_at: datetime | None = None,
    max_bin_n: int = const.BOOTSTRAP_MAX_BIN_N,
) -> dict:
    """Assemble the contract bootstrap dict from the accumulator (pure).

    Caps every shademap bin's ``n`` at ``max_bin_n`` (SPEC §6: backfilled bins
    are hourly-smeared, so live data should override quickly) and clamps every
    factor. The result is exactly what ``store.import_bootstrap`` expects:
    top-level schema/version/site-signature + ``BiasState.to_dict()`` and
    ``ShademapState.to_dict()`` sub-objects.
    """
    gen = (generated_at or datetime.now(UTC)).astimezone(UTC)

    # Shademap: cap n, clamp tau, into ShademapState for a validated round-trip.
    shade_state = ShademapState(
        channels={
            chan: {
                bk: ShademapBin(
                    tau=_clamp(vals[0], const.SHADEMAP_TAU_MIN, const.SHADEMAP_TAU_MAX),
                    n=min(int(vals[1]), max_bin_n),
                )
                for bk, vals in bins.items()
            }
            for chan, bins in acc.shade.items()
        }
    )
    bias_state = BiasState(cells=dict(acc.bias))

    return {
        const.BOOTSTRAP_KEY_SCHEMA: const.BOOTSTRAP_SCHEMA_VERSION,
        const.BOOTSTRAP_KEY_GENERATED_AT: gen.isoformat(),
        const.BOOTSTRAP_KEY_SITE_SIGNATURE: site_signature(site),
        const.BOOTSTRAP_KEY_BIAS: bias_state.to_dict(),
        const.BOOTSTRAP_KEY_SHADEMAP: shade_state.to_dict(),
    }


def site_signature(site: SiteConfig) -> str:
    """Stable lat/lon + plane-name digest for the import sanity check (SPEC §6).

    A short sha256 over the rounded coordinates and the ordered plane names, so
    the import service can refuse a bootstrap built for a different site.
    """
    parts = [
        f"{round(site.latitude, 4)}",
        f"{round(site.longitude, 4)}",
        *[p.name for p in site.planes],
    ]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ===========================================================================
# Site loading — reuse the shipped DEFAULT_SITE (the reference operator site).
# ===========================================================================


def _resolve_tz(name: str | None):
    """Resolve a zoneinfo name to a tzinfo, or None for UTC (backfill:842).

    Passing the site's local timezone lets the day-part / fog-month
    classification match the live layer's LOCAL-hour keying. An unknown name
    degrades to UTC with a warning rather than aborting the whole backfill.
    """
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as err:  # noqa: BLE001 - degrade to UTC
        _LOGGER.warning("Unknown --tz %r (%s); keying day parts on UTC", name, err)
        return None


def load_site(site_json: Path | None) -> SiteConfig:
    """Load a SiteConfig from a JSON file, or the shipped DEFAULT_SITE.

    The operator's live site is the reference DEFAULT_SITE (SPEC §2). A
    ``--site`` override lets a different install reuse this script. The site
    dict must match ``SiteConfig.from_dict`` (the config-flow object shape).
    """
    if site_json is None:
        return SiteConfig.from_dict(const.DEFAULT_SITE)
    data = json.loads(site_json.read_text(encoding="utf-8"))
    return SiteConfig.from_dict(data)


# ===========================================================================
# Network layer (aiohttp, lazy) — Previous-Runs weather + WebSocket LTS.
# ===========================================================================


async def fetch_weather_range(
    session,
    *,
    latitude: float,
    longitude: float,
    start: date,
    end: date,
) -> tuple[list[HourlyWeather], bool]:
    """Fetch hourly as-issued weather for [start, end] (Previous-Runs API).

    Tries the Previous-Runs API (day-1 lead, forecast-as-issued). On failure —
    or if the suffixed radiation variables come back empty — degrades to the
    Historical Forecast API with a WARNING that the data is analysis, not
    forecast (SPEC §6: graceful degrade, still useful for the geometric
    shademap). Returns ``(records, is_as_issued)``.
    """
    suffix = f"_previous_day{PREVIOUS_RUN_LEAD_DAY}"
    prev_vars = [f"{v}{suffix}" for v in _RADIATION_VARS]
    hourly_vars = ",".join([*prev_vars, *_CONTEXT_VARS])
    params = {
        "latitude": f"{latitude:.6f}",
        "longitude": f"{longitude:.6f}",
        "hourly": hourly_vars,
        "models": const.OPEN_METEO_MODEL,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "timezone": "UTC",
    }
    try:
        payload = await _get_json(session, PREVIOUS_RUNS_URL, params)
        records = parse_hourly_payload(payload, var_suffix=suffix)
        if records:
            return records, True
        _LOGGER.warning(
            "Previous-Runs API returned no as-issued radiation for %s..%s; "
            "falling back to the Historical Forecast API (analysis, NOT "
            "as-issued forecast data)",
            start, end,
        )
    except Exception as err:  # noqa: BLE001 - degrade on any provider failure
        _LOGGER.warning(
            "Previous-Runs API fetch failed for %s..%s (%s); falling back to "
            "the Historical Forecast API (analysis, NOT as-issued forecast)",
            start, end, err,
        )

    # --- Degrade: Historical Forecast API (plain, unsuffixed variables). ---
    hourly_vars = ",".join([*_RADIATION_VARS, *_CONTEXT_VARS])
    params["hourly"] = hourly_vars
    payload = await _get_json(session, HISTORICAL_FORECAST_URL, params)
    records = parse_hourly_payload(payload, var_suffix="")
    return records, False


async def _get_json(session, url: str, params: dict) -> dict:
    import aiohttp  # lazy

    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
    async with session.get(url, params=params, timeout=timeout) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(f"HTTP {resp.status} from {url}: {text[:200]}")
        return await resp.json()


# A single hourly statistics response for all modules over a multi-year range
# exceeds HA's 4 MiB WebSocket frame limit (MESSAGE_TOO_BIG). Query in windows
# small enough that each response stays well under the cap: 90 days x 24 h x
# ~8 sensors ~= 17k rows ~= 1 MiB.
_LTS_WINDOW_DAYS = 90


def _lts_windows(
    start_dt: datetime, end_dt: datetime, window_days: int
) -> list[tuple[datetime, datetime]]:
    """Split [start_dt, end_dt) into consecutive [win_start, win_end) chunks."""
    windows: list[tuple[datetime, datetime]] = []
    step = timedelta(days=window_days)
    cur = start_dt
    while cur < end_dt:
        nxt = min(cur + step, end_dt)
        windows.append((cur, nxt))
        cur = nxt
    return windows


async def fetch_lts_hourly(
    session,
    *,
    ha_url: str,
    token: str,
    statistic_ids: list[str],
    start: date,
    end: date,
) -> dict[str, dict[str, float]]:
    """Pull hourly per-statistic mean power from HA LTS via the WebSocket API.

    Connects to ``{ha_url}/api/websocket``, authenticates with the long-lived
    token, and issues ``recorder/statistics_during_period`` commands at hourly
    period — chunked into ``_LTS_WINDOW_DAYS`` windows so no single response
    trips HA's 4 MiB WS frame limit. Returns ``{statistic_id: {iso_hour:
    mean_wh}}`` where mean_wh = mean power (W) over the hour == Wh (matches
    coordinator._async_read_daily_actuals).

    Raises on connection/auth failure — the caller aborts (no measured energy
    means nothing to train against).
    """
    import aiohttp  # lazy

    ws_url = ha_url.rstrip("/").replace("http://", "ws://").replace(
        "https://", "wss://"
    ) + "/api/websocket"
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(
        days=1
    )

    out: dict[str, dict[str, float]] = {sid: {} for sid in statistic_ids}
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
    # max_msg_size=0 lifts the client-side frame cap; chunking keeps each
    # response small regardless (belt and suspenders against the server cap).
    async with session.ws_connect(
        ws_url, timeout=timeout, max_msg_size=0
    ) as ws:
        # 1) auth_required -> auth -> auth_ok
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": token})
        auth = await ws.receive_json()
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"HA WebSocket auth failed: {auth}")

        # 2) statistics_during_period, one command per time window
        for msg_id, (win_start, win_end) in enumerate(
            _lts_windows(start_dt, end_dt, _LTS_WINDOW_DAYS), start=1
        ):
            await ws.send_json(
                {
                    "id": msg_id,
                    "type": "recorder/statistics_during_period",
                    "start_time": win_start.isoformat(),
                    "end_time": win_end.isoformat(),
                    "statistic_ids": statistic_ids,
                    "period": "hour",
                    "types": ["mean"],
                }
            )
            result = await _await_ws_result(ws, msg_id)
            _parse_lts_result(result, out)

    return out


async def _await_ws_result(ws, msg_id: int) -> dict:
    """Receive frames until the result for ``msg_id`` arrives."""
    while True:
        frame = await ws.receive_json()
        if frame.get("id") == msg_id and frame.get("type") == "result":
            if not frame.get("success", False):
                raise RuntimeError(f"HA statistics query failed: {frame.get('error')}")
            return frame.get("result", {})


def _parse_lts_result(
    result: dict, out: dict[str, dict[str, float]]
) -> None:
    """Fold a statistics_during_period result into {sid: {iso_hour: wh}} (pure).

    Each row has ``start`` (ms epoch or ISO) and ``mean`` (W). mean power over
    an hour == Wh. Rows without a mean are skipped. Kept pure + separate so the
    tests can exercise the parse without a WebSocket.
    """
    if not isinstance(result, dict):
        return
    for sid, rows in result.items():
        if sid not in out or not isinstance(rows, list):
            continue
        bucket = out[sid]
        for row in rows:
            if not isinstance(row, dict):
                continue
            mean = _num(row.get("mean"))
            if mean is None:
                continue
            hkey = _stat_row_hour(row.get("start"))
            if hkey is None:
                continue
            bucket[hkey] = bucket.get(hkey, 0.0) + mean  # W*1h = Wh


def _stat_row_hour(start: object) -> str | None:
    """Normalise a statistics row ``start`` to an ISO-UTC hour key.

    HA WebSocket returns ``start`` as epoch MILLISECONDS (number) in modern
    cores; older/other paths may send an ISO string. Handle both.
    """
    if isinstance(start, (int, float)):
        dt = datetime.fromtimestamp(start / 1000.0, tz=UTC)
    elif isinstance(start, str):
        try:
            dt = datetime.fromisoformat(start)
        except ValueError:
            return None
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    else:
        return None
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


# ===========================================================================
# Orchestration (async) — fetch, reconstruct, accumulate, emit.
# ===========================================================================


def _group_by_day(
    records: list[HourlyWeather],
) -> dict[str, list[HourlyWeather]]:
    """Bucket hourly weather records by their UTC calendar date."""
    by_day: dict[str, list[HourlyWeather]] = {}
    for r in records:
        dkey = r.start.date().isoformat()
        by_day.setdefault(dkey, []).append(r)
    for day in by_day.values():
        day.sort(key=lambda w: w.start)
    return by_day


def _filter_actuals_for_day(
    hourly_actuals: dict[str, dict[str, float]],
    day: str,
) -> dict[str, dict[str, float]]:
    """Slice the full hourly-actuals map down to one UTC day (per module)."""
    out: dict[str, dict[str, float]] = {}
    for module, hours in hourly_actuals.items():
        day_hours = {
            hk: wh for hk, wh in hours.items() if hk[:10] == day
        }
        if day_hours:
            out[module] = day_hours
    return out


async def run_backfill(args: argparse.Namespace) -> int:
    """Top-level async driver. Returns a process exit code."""
    import aiohttp  # lazy

    site = load_site(Path(args.site) if args.site else None)
    site_tz = _resolve_tz(getattr(args, "tz", None))
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        _LOGGER.error("--end (%s) is before --start (%s)", end, start)
        return 2

    # Modules with a measured entity are our channels + statistic ids.
    stat_ids = sorted({
        p.actual_entity for p in site.planes if p.actual_entity
    })
    if not stat_ids:
        _LOGGER.error("Site has no planes with actual_entity; nothing to train")
        return 2
    # Map statistic_id -> the plane/channel name(s) it feeds. In the reference
    # site each module has a distinct entity, but two planes can share one
    # sensor (M2/M3 both on port config in some setups); handle the general
    # case by resolving per-plane below.
    svf_by_plane = {p.name: horizon.sky_view_factor(p) for p in site.planes}

    acc = BootstrapAccumulator()

    async with aiohttp.ClientSession() as session:
        _LOGGER.info(
            "Fetching hourly weather %s..%s from Open-Meteo (Previous-Runs)",
            start, end,
        )
        weather, as_issued = await fetch_weather_range(
            session,
            latitude=site.latitude,
            longitude=site.longitude,
            start=start,
            end=end,
        )
        if not weather:
            _LOGGER.error("No weather returned for the requested range")
            return 3
        _LOGGER.info(
            "Got %d hourly weather records (%s)",
            len(weather),
            "as-issued forecast" if as_issued
            else "ANALYSIS fallback (NOT as-issued)",
        )

        _LOGGER.info("Pulling per-module LTS from %s", args.ha_url)
        lts_by_entity = await fetch_lts_hourly(
            session,
            ha_url=args.ha_url,
            token=args.token,
            statistic_ids=stat_ids,
            start=start,
            end=end,
        )

    # Re-key LTS from entity_id -> channel(plane) name for process_day_hourly.
    hourly_actuals = _entity_to_channel_actuals(site, lts_by_entity)

    by_day = _group_by_day(weather)
    for dkey in sorted(by_day.keys()):
        day_weather = by_day[dkey]
        day_actuals = _filter_actuals_for_day(hourly_actuals, dkey)
        if not day_actuals:
            acc.days_skipped += 1
            _LOGGER.debug("Day %s: no measured actuals, skipped", dkey)
            continue
        used = process_day_hourly(
            acc, site, day_weather, day_actuals,
            svf_by_plane=svf_by_plane, tz=site_tz,
        )
        if used:
            acc.days_used += 1
        else:
            acc.days_skipped += 1

    _summarise(acc, as_issued)

    if acc.days_used == 0:
        _LOGGER.error("No usable days — bootstrap would be empty; aborting")
        return 4

    bootstrap = build_bootstrap_json(acc, site)

    if args.dry_run:
        _LOGGER.info("--dry-run: not writing %s", args.out)
        return 0

    out_path = Path(args.out)
    out_path.write_text(json.dumps(bootstrap, indent=2), encoding="utf-8")
    _LOGGER.info("Wrote bootstrap JSON -> %s", out_path.resolve())
    return 0


def _entity_to_channel_actuals(
    site: SiteConfig,
    lts_by_entity: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """Map entity-id-keyed LTS to plane/channel-keyed actuals.

    Each plane's ``actual_entity`` names the statistic it measures; several
    planes MAY share one sensor (then they get the same measured curve — the
    reconstruction disambiguates by plane geometry). Modules without a sensor
    or without any LTS rows are omitted.
    """
    out: dict[str, dict[str, float]] = {}
    for plane in site.planes:
        ent = plane.actual_entity
        if not ent:
            continue
        rows = lts_by_entity.get(ent)
        if rows:
            out[plane.name] = dict(rows)
    return out


def _summarise(acc: BootstrapAccumulator, as_issued: bool) -> None:
    n_bins = sum(len(b) for b in acc.shade.values())
    _LOGGER.info(
        "Bootstrap summary: %d days used, %d skipped | shademap: %d channels, "
        "%d bins, %d samples | day-ahead: %d cells, %d RLS steps | source: %s",
        acc.days_used,
        acc.days_skipped,
        len(acc.shade),
        n_bins,
        acc.shade_samples,
        len(acc.bias),
        acc.bias_samples,
        "as-issued" if as_issued else "ANALYSIS (degraded)",
    )


# ===========================================================================
# CLI
# ===========================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backfill.py",
        description=(
            "Backfill the balcony_solar_forecast learner bootstrap from "
            "Open-Meteo Previous-Runs forecasts + HA long-term statistics "
            "(SPEC §6). Run on the DEV machine, not on HA."
        ),
    )
    p.add_argument("--ha-url", required=True,
                   help="HA base URL, e.g. http://homeassistant.local:8123")
    p.add_argument("--token", required=True,
                   help="HA long-lived access token (WebSocket auth)")
    p.add_argument("--start", required=True,
                   help="Range start, ISO date YYYY-MM-DD (LTS since 2024-07)")
    p.add_argument("--end", required=True, help="Range end, ISO date YYYY-MM-DD")
    p.add_argument("--out", default="bootstrap.json",
                   help="Output bootstrap JSON path (default: bootstrap.json)")
    p.add_argument("--site", default=None,
                   help="Optional site JSON override (defaults to the shipped "
                        "reference site DEFAULT_SITE)")
    p.add_argument("--tz", default=None,
                   help="Site IANA timezone (e.g. Europe/Berlin) for local-hour "
                        "day-part / cloud-class keying; defaults to UTC")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch + reconstruct + summarise WITHOUT writing --out")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Debug logging (per-day skips)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return asyncio.run(run_backfill(args))
    except KeyboardInterrupt:  # pragma: no cover
        _LOGGER.warning("Interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
