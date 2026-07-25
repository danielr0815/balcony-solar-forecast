"""Home-Assistant-free learner-bootstrap core (SPEC §6).

The math that turns ~2 years of history into a warm start for the two learning
layers (day-ahead RLS bias + geometric shademap) plus the quantile relerr ring.
This module is PURE: it imports nothing from ``homeassistant`` and does NO
network I/O — data beschaffung (Open-Meteo Previous-Runs + HA WebSocket LTS) is
ENTKOPPELT and lives in the callers:

  * ``scripts/backfill.py`` — the DEV-machine CLI wrapper (aiohttp fetch +
    long-lived token), and
  * the in-process ``balcony_solar_forecast.run_bootstrap`` HA action (recorder
    statistics, no token).

Both fetch their own ``HourlyWeather`` records + per-module hourly actuals and
hand them to :func:`accumulate_days` / :func:`build_bootstrap_json` here, so the
reconstruction / bootstrap MATH is defined once and both paths emit the SAME
bootstrap dict.

The reconstruction re-runs the repo's ``core/`` physics (the SAME the live
engine runs) at the hour midpoint, treating the resulting instantaneous DC power
as the hour's mean (hourly-mean semantics). This is deliberately coarse — the
sub-hour geometry the live 15-min path sees is lost — which is exactly why the
backfilled shademap bins get their ``n`` capped (SPEC §6): live data overrides
them quickly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from .. import const
from . import bias as bias_mod
from . import clearsky, electrical, horizon, solpos, transpose
from . import quantiles as quantiles_mod
from . import shademap as shademap_mod
from .types import (
    BiasCell,
    BiasState,
    PlaneConfig,
    QuantileState,
    ShademapBin,
    ShademapState,
    SiteConfig,
)

_LOGGER = logging.getLogger(__name__)


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
    # Quantile relerr ring (SPEC §6): accumulated ACROSS days via the LIVE
    # quantiles.train_quantiles, so the seeded rings are byte-identical to a
    # live-trained state (date-windowed, count-capped). ``last_iso_date`` is the
    # newest processed day, used to re-window the ring at emit time relative to
    # the last backfill day.
    quantile_state: QuantileState = field(default_factory=QuantileState)
    last_iso_date: str = ""
    days_used: int = 0
    days_skipped: int = 0
    shade_samples: int = 0
    bias_samples: int = 0
    quantile_samples: int = 0


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


def _slot_albedo(
    snow_depth_m: float, base_albedo: float | None = None
) -> float:
    """Snow-aware ground albedo (v0.20: honours the optional site albedo)."""
    if snow_depth_m is not None and snow_depth_m > const.SNOW_DEPTH_THRESHOLD_M:
        return const.ALBEDO_SNOW
    return base_albedo if base_albedo is not None else const.ALBEDO_DEFAULT


def reconstruct_plane_hour(
    plane: PlaneConfig,
    svf: float,
    wx: HourlyWeather,
    *,
    latitude: float,
    longitude: float,
    base_albedo: float | None = None,
    beam_gain: float = 1.0,
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
    albedo = _slot_albedo(wx.snow_depth_m, base_albedo)
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
        doy=doy,
    )
    beam = comps.get("beam", 0.0)
    circ = comps.get("circumsolar", 0.0)
    iso = comps.get("isotropic", 0.0)
    ground = comps.get("ground", 0.0)

    # Incidence-angle modifier (ASHRAE, const IAM_B0) — byte-identical to
    # engine._plane_poa_components: applied to beam+circumsolar BEFORE the ungated
    # reference so the bootstrap trains the same optics-corrected T as live.
    cos_theta = comps.get("cos_theta")
    if cos_theta is not None:
        f_iam = transpose.ashrae_iam(cos_theta)
        beam *= f_iam
        circ *= f_iam

    # Site bifacial beam gain (forensik T6) — byte-identical to
    # engine._plane_poa_components: applied to beam+circumsolar BEFORE the ungated
    # reference so future bootstraps reconstruct the SAME direct-share physics the
    # live engine issues. Default 1.0 => no-op.
    if beam_gain != 1.0:
        beam *= beam_gain
        circ *= beam_gain

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
        # Pass sun_el so an inline tau_points elevation profile resolves at the
        # true sun elevation (v0.22) — byte-for-byte the engine's
        # ``_plane_poa_components`` gate. A row without a profile ignores it and
        # the result is the pre-0.22 scalar tau, so a legacy backfill is
        # unchanged; a tau_points row now gates the reconstructed beam with the
        # SAME el-dependent tau the live engine issues (the SLOW-reference /
        # day-ahead-bias mirror invariant).
        static_tau = horizon.transmittance_at(plane, sun_az, doy, sun_el=sun_el)
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
        gated_total_poa, plane.wp, wx.temp_c, plane.efficiency,
        ross_coeff=plane.ross_coeff,
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
# modules. We re-implement the small closed-form updates here but keep them
# byte-for-byte consistent with the const tunables + the docstring'd semantics
# in core/bias.py and core/shademap.py.
# ===========================================================================


def _clamp(v: float, lo: float, hi: float) -> float:
    if v != v:  # NaN
        return lo
    return lo if v < lo else hi if v > hi else v


# The live shademap functions are now implemented, so the backfill imports them
# directly instead of keeping parallel copies (the old copies drifted: the
# in-house half_year_index put doy 355-366 in the WRONG half, aliasing sparse
# winter bins). Importing the single implementation guarantees byte-identical
# bin keys between the offline bootstrap and live training.
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

    New tau = (1-alpha)*old + alpha*clamp(T) with an ADAPTIVE warm-up alpha =
    max(SHADEMAP_EMA_ALPHA, 1/(n_old+1)); a fresh bin seeds at clamp(T). n
    incremented (capped at emit time). The alpha formula is byte-identical to
    shademap.update_bin so the offline bootstrap and live training stay
    sample-for-sample identical. Input untouched semantics are moot here since
    we accumulate into the mutable accumulator.
    """
    t = _clamp(measured_t, const.SHADEMAP_TAU_MIN, const.SHADEMAP_TAU_MAX)
    chan = acc.shade.setdefault(channel, {})
    cell = chan.get(bin_key)
    if cell is None:
        chan[bin_key] = [t, 1]
    else:
        old_tau, n = cell
        # n is the stored count BEFORE this sample: young bins are the arithmetic
        # mean of their samples until the fixed EMA alpha takes over.
        alpha = max(const.SHADEMAP_EMA_ALPHA, 1.0 / (n + 1))
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
    classification so the RLS cells match the live layer.
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
    # Sun elevation per hour (site-level, hour midpoint) for the k_c cloud
    # classification — the classifier needs it to key on the clear-sky index
    # (A5), matching the live path.
    el_by_hour: dict[str, float] = {}

    # SVF is now doy-dependent (the horizon is semi-transparent to the diffuse,
    # so a seasonal foliage row ramps it with the day). Recompute it per (plane,
    # doy) here — mirroring engine.compute_forecast — so the bootstrap's diffuse
    # floor matches the live engine's exactly (live/backfill parity). This
    # supersedes the static ``svf_by_plane`` (doy=None) the caller passes in.
    svf_doy_cache: dict[tuple[str, int], float] = {}

    def _svf_for(plane: PlaneConfig, doy: int) -> float:
        key = (plane.name, doy)
        svf = svf_doy_cache.get(key)
        if svf is None:
            svf = horizon.sky_view_factor(plane, doy=doy)
            svf_doy_cache[key] = svf
        return svf

    for wx in day_weather:
        hkey = wx.start.isoformat()
        doy = _hour_midpoint(wx.start).timetuple().tm_yday
        for plane in planes:
            r = reconstruct_plane_hour(
                plane, _svf_for(plane, doy), wx,
                latitude=lat, longitude=lon,
                base_albedo=getattr(site, "albedo", None),
                beam_gain=getattr(site, "bifacial_beam_gain", None) or 1.0,
            )
            recon[plane.name][hkey] = r
            modeled_total_by_plane[plane.name][hkey] = r.gated_total_wh
        # kc is GHI/haurwitz at this hour's midpoint sun; reuse any plane's.
        any_r = recon[planes[0].name][hkey] if planes else None
        if any_r is not None:
            kc_by_hour[hkey] = any_r.kc
            el_by_hour[hkey] = any_r.sun_el

    hours_sorted = sorted(kc_by_hour.keys())

    contributed = False

    # --- 2) SHADEMAP: per plane, per hour, gate quasi-clear + EMA-update. ---
    # ONLY train the geometric shademap from TRUE hourly actuals. The daily-total
    # disaggregation fabricates per-hour "measured" energy proportional to the
    # statically GATED modeled shape, so a bin's learned T would just re-derive
    # the (possibly wrong) static horizon prior — circular training on the
    # model's own output. The day-ahead bias below still uses the daily
    # disaggregation, where the daily ratio IS the real signal.
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
        # Storage is ALWAYS per plane (SPEC §5): the learned samples are stored
        # under the plane's OWN channel (its name), mirroring the live nightly
        # trainer. Grouping is applied only at READ time in the coordinator
        # (effective_tau_pooled), so the bootstrap stays group-agnostic and any
        # later grouping/dissolution is fully reversible.
        store_chan = chan
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
            _shade_update(acc, store_chan, bin_key, measured_t)
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
        cloud_class = _classify_cloud(wx, tz, elevation_deg=el_by_hour.get(hkey))
        day_part = _day_part_for_slot(wx.start, lon)
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

    # --- 4) QUANTILE SEED (SPEC §6): per-hour relerr against the theta-CORRECTED
    # gated forecast, mirroring the live train_quantiles_day path so the day-0
    # bands are not cold (only overcast bins were ever trained before A6). Per
    # hour: corrected = clamp(theta_cell) x gated_modeled_site (theta AFTER this
    # day's RLS step above), relerr = measured_site / corrected. Fed through the
    # LIVE quantiles.train_quantiles so the seeded ring is byte-identical to a
    # live-trained one — same (cloud_class x day_part) taxonomy, same clamp
    # [QUANTILE_REL_ERR_MIN, MAX], same >QUANTILE_MIN_FORECAST_WH gate, same
    # date-window (QUANTILE_RING_DAYS) + count-cap. The per-day-per-bin cap
    # (QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN) is enforced here so correlated hours
    # of a single day do not over-weight a bin (the coarse hourly backfill).
    iso_date = day_weather[0].start.date().isoformat()
    if iso_date > acc.last_iso_date:
        acc.last_iso_date = iso_date
    per_bin_today: dict[str, int] = {}
    q_samples: list[quantiles_mod.QuantileSample] = []
    for wx in day_weather:
        hkey = wx.start.isoformat()
        modeled_site = sum(
            modeled_total_by_plane[p.name].get(hkey, 0.0) for p in planes
        )
        if modeled_site <= 0.0:
            continue
        measured_site = site_measured_hourly.get(hkey)
        if measured_site is None:
            continue
        cloud_class = _classify_cloud(wx, tz, elevation_deg=el_by_hour.get(hkey))
        day_part = _day_part_for_slot(wx.start, lon)
        key = QuantileState.bin_key(cloud_class, day_part)
        cell = acc.bias.get(key)
        theta = (
            cell.clamped_theta() if cell is not None
            else const.DAY_AHEAD_BIAS_NEUTRAL
        )
        corrected = theta * modeled_site
        if corrected <= const.QUANTILE_MIN_FORECAST_WH:
            continue  # below-threshold hour never enters the ring (live parity)
        if per_bin_today.get(key, 0) >= const.QUANTILE_MAX_SAMPLES_PER_DAY_PER_BIN:
            continue  # cap correlated hours per bin per day (SPEC §6)
        q_samples.append(
            quantiles_mod.QuantileSample(
                cloud_class=cloud_class,
                day_part=day_part,
                measured_wh=measured_site,
                corrected_wh=corrected,
            )
        )
        per_bin_today[key] = per_bin_today.get(key, 0) + 1
    if q_samples:
        acc.quantile_state = quantiles_mod.train_quantiles(
            acc.quantile_state, q_samples, training_date=iso_date
        )
        acc.quantile_samples += len(q_samples)
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
    classification mirrors coordinator._day_part_for_hourkey: keying on the raw
    UTC hour shifts every cell 1-2 h and pollutes the RLS prior for the
    operator's morning-shade site.
    """
    local = dt if tz is None else dt.astimezone(tz)
    return local.hour, local.month


def _classify_cloud(
    wx: HourlyWeather,
    tz: timezone | None = None,
    *,
    elevation_deg: float | None = None,
) -> str:
    """Cloud class via the live bias.classify_cloud (SPEC §5/§6, A5).

    Passes the hour's GHI and sun ``elevation_deg`` so the classifier keys on the
    CLEAR-SKY INDEX (k_c = GHI / Haurwitz(elevation)) — the SAME taxonomy the live
    coordinator/nightly path uses since A5 — instead of the random-overlap layer
    cover; without the elevation (twilight / not supplied) it falls back to the
    layer cover exactly as live does. The fog-month test uses the LOCAL month
    (``tz``) so a late-evening UTC hour does not fall into the wrong month near a
    boundary.
    """
    _hr, month = _local_hour(wx.start, tz)
    return bias_mod.classify_cloud(
        cloud_low=wx.cloud_low,
        cloud_mid=wx.cloud_mid,
        cloud_high=wx.cloud_high,
        visibility_m=wx.visibility_m if wx.visibility_m > 0.0 else float("inf"),
        month=month,
        ghi=wx.ghi,
        elevation_deg=elevation_deg,
    )


def _day_part_for_slot(dt: datetime, lon: float) -> str:
    """SOLAR day part for an hour-start datetime (v0.19, SPEC §5).

    Bins by APPARENT SOLAR time (``bias.day_part_for_solar`` over
    ``solpos.hours_from_solar_noon``), so the bootstrapped day-ahead bias cells
    match the LIVE coordinator's solar binning — a bootstrapped and a
    live-trained cell for the same ``(cloud_class, day_part)`` then mean the
    same thing (before v0.19 the backfill binned on the clock hour and the live
    path on solar time, a subtle seam near the boundaries). ``dt`` is the hour
    START (tz-aware UTC); ``lon`` the site longitude, degrees East.
    """
    return bias_mod.day_part_for_solar(solpos.hours_from_solar_noon(dt, lon))


# ===========================================================================
# Day orchestration (pure given the fetched inputs).
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


def accumulate_days(
    site: SiteConfig,
    weather: list[HourlyWeather],
    hourly_actuals: dict[str, dict[str, float]],
    *,
    svf_by_plane: dict[str, float],
    tz: timezone | None = None,
) -> BootstrapAccumulator:
    """Fold every processable UTC day into a fresh accumulator.

    Groups ``weather`` by calendar day, and for each day WITH measured actuals
    runs one :func:`process_day_hourly` step, tracking ``days_used`` /
    ``days_skipped``. Days without any measured module are skipped (counted).
    Pure given the fetched inputs — the network layer (Open-Meteo Previous-Runs
    + HA WebSocket / recorder LTS) lives in the callers. Returns the populated
    accumulator ready for :func:`build_bootstrap_json`.
    """
    acc = BootstrapAccumulator()
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
            svf_by_plane=svf_by_plane, tz=tz,
        )
        if used:
            acc.days_used += 1
        else:
            acc.days_skipped += 1
    return acc


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

    # Quantile: re-window every ring to QUANTILE_RING_DAYS RELATIVE TO THE LAST
    # backfill day (a bin last touched early in a multi-year range must not keep
    # samples older than the window measured from the newest day) and enforce the
    # count-cap backstop — reusing the LIVE trim so the seeded ring is
    # byte-identical to a live-trained one (SPEC §6).
    cutoff = quantiles_mod._window_cutoff(acc.last_iso_date)
    quant_bins: dict[str, list] = {}
    for bk, ring in acc.quantile_state.bins.items():
        trimmed = quantiles_mod._trim_ring(list(ring), cutoff=cutoff)
        if trimmed:
            quant_bins[bk] = trimmed
    quantile_state = QuantileState(
        bins=quant_bins, version=acc.quantile_state.version
    )

    return {
        const.BOOTSTRAP_KEY_SCHEMA: const.BOOTSTRAP_SCHEMA_VERSION,
        const.BOOTSTRAP_KEY_GENERATED_AT: gen.isoformat(),
        const.BOOTSTRAP_KEY_SITE_SIGNATURE: site_signature(site),
        const.BOOTSTRAP_KEY_BIAS: bias_state.to_dict(),
        const.BOOTSTRAP_KEY_SHADEMAP: shade_state.to_dict(),
        const.BOOTSTRAP_KEY_QUANTILE: quantile_state.to_dict(),
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
# Site + timezone loading — reuse the shipped DEFAULT_SITE (reference site).
# ===========================================================================


def resolve_tz(name: str | None):
    """Resolve a zoneinfo name to a tzinfo, or None for UTC.

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
        _LOGGER.warning("Unknown timezone %r (%s); keying day parts on UTC", name, err)
        return None


def load_site(site_json: Path | None) -> SiteConfig:
    """Load a SiteConfig from a JSON file, or the shipped DEFAULT_SITE.

    The operator's live site is the reference DEFAULT_SITE (SPEC §2). A
    ``--site`` override lets a different install reuse this. The site dict must
    match ``SiteConfig.from_dict`` (the config-flow object shape).
    """
    if site_json is None:
        return SiteConfig.from_dict(const.DEFAULT_SITE)
    data = json.loads(site_json.read_text(encoding="utf-8"))
    return SiteConfig.from_dict(data)
