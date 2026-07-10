"""Forecast engine: dual-curve physics pipeline + learner hooks (stdlib only).

Owner: engine. Pure, HA-free. Ties together solpos -> transpose -> horizon
-> electrical over every 15-min slot and every plane, then rolls the
AC-clamped site total up to hourly Wh and daily kWh (SPEC §4 steps 2-7).

Dual curve (SPEC §5 / §9, operator decision 2026-07-06)
-------------------------------------------------------
Every cycle the engine computes BOTH:

  * the RAW pure-physics curve (learners OFF), and
  * the CORRECTED curve (learners applied).

The two learner layers hook the pipeline at two distinct stages, injected as
optional pure callables in a :class:`LearnerHooks` object (the coordinator
builds them from the persisted learner state; the engine imports NO learner
state and NO Home Assistant — it only calls the callables). When both hooks
are None the corrected curve is the raw curve **bit-exact** (identity), so a
v0.1 build round-trips unchanged.

  1. SLOW learner (shademap), at the TRANSPOSITION stage, per plane per slot:
     ``hooks.beam_tau(channel, sun_az, sun_el, doy, static_prior) -> tau``
     REPLACES the static horizon transmittance that gates beam+circumsolar.
     ``static_prior`` is the plane's static horizon tau for this azimuth/doy,
     so a hook that just returns ``static_prior`` is the identity. tau=0 on a
     wall bin kills the beam but never the diffuse (the diffuse floor is
     scaled by the static sky-view factor only).

  2. FAST learner (intraday scalar + day-ahead RLS bias), at the AGGREGATION
     stage, as a single per-slot multiplicative factor:
     ``hooks.slot_factor(slot_start) -> factor``
     scales the CORRECTED (already shademap-transposed) per-slot site power.
     The coordinator composes intraday decay + day-ahead bias into this one
     factor so the 15-min ``total_watts`` and the hourly ``hourly_wh`` stay
     mutually consistent (both are derived from the same factored slot power).
     A hook that returns 1.0 is the identity.

Per-slot pipeline (values are interval means; sun position at the midpoint):

    sun_position(midpoint) --> per plane:
        hay_davies_poa()  (raw beam / circumsolar / isotropic / ground)
        horizon beam gate: beam + circumsolar *= tau  when the sun sits below
                           the plane's interpolated horizon line, where
                           tau = static horizon tau (RAW) or hooks.beam_tau(...)
                           (CORRECTED)
        diffuse gate:      isotropic *= sky_view_factor (static per plane)
        POA = gated beam + gated circumsolar + gated isotropic + ground
        dc_power(POA, wp, temp, efficiency)   -> beam-DC and diffuse-DC split
    clamp_groups()  --> per-plane and site-total AC-clamped watts

Attribution split: the per-plane DC power is decomposed into the part driven
by the beam+circumsolar POA vs. the diffuse+ground POA (pre-AC-clamp), so the
SLOW learner can train the beam-referenced transmittance
``T = (P_measured - P_diffuse_modeled) / P_beam_modeled`` (SPEC §5). The AC
clamp is applied to the TOTAL, then split back proportionally so the reported
beam/diffuse watts already respect the clamp.

Aggregation: the clamped site total (raw and corrected) is integrated to
hourly Wh (keyed by ISO-8601 UTC hour) and daily kWh (keyed by ISO date in the
``tz`` calendar, UTC by default). The per-plane, per-slot beam_watts /
diffuse_watts / kc series on each PlaneResult let the coordinator roll up the
per-plane hourly beam_wh / diffuse_wh / ghi / kc (PlaneHourlyModeled) for the
nightly issued snapshot v2 (SPEC §9) without bloating the frozen result type.
Slots with missing (None) irradiance/temperature are treated as
zero-production and skipped safely.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

from ..const import (
    ALBEDO_DEFAULT,
    ALBEDO_SNOW,
    CORRECTION_SOURCE_NONE,
    SLOT_MINUTES,
    SNOW_DEPTH_THRESHOLD_M,
)
from . import clearsky, electrical, horizon, quantiles, solpos, transpose
from .types import (
    ForecastResult,
    PlaneConfig,
    PlaneResult,
    QuantileBands,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)

__all__ = ["compute_forecast", "LearnerHooks"]

# One 15-min slot as a fraction of an hour, for the Wh integration of an
# interval-mean power value (SPEC: slot values are backward-averaged means).
_SLOT_HOURS = SLOT_MINUTES / 60.0


# ---------------------------------------------------------------------------
# Learner injection: pure callables, no HA / learner-state imports here.
# ---------------------------------------------------------------------------

# Shademap hook: (channel, sun_az, sun_el, doy, static_prior) -> effective tau.
# Wraps shademap.effective_tau bound to the persisted ShademapState. The engine
# passes the plane's static horizon tau as ``static_prior`` so a hook that just
# echoes it is the identity (corrected == raw).
BeamTauHook = Callable[[str, float, float, int, float], float]

# Fast-learner hook: (slot_start) -> multiplicative factor for the slot's site
# power. Composes the intraday scalar (linear decay over the ~6 h horizon) and
# the day-ahead RLS bias into ONE factor; 1.0 is the identity.
SlotFactorHook = Callable[[datetime], float]


@dataclass(frozen=True, slots=True)
class LearnerHooks:
    """Optional learner callables injected into the engine (SPEC §5 / §9).

    All fields default to the identity: ``beam_tau=None`` => the static horizon
    transmittance gates the beam (raw physics); ``slot_factor=None`` => no
    aggregation-stage scaling. With both None the corrected curve equals the
    raw curve bit-exact. ``correction_source`` is echoed onto the result so the
    coordinator can report which layer(s) shaped the served curve
    (const.CORRECTION_SOURCE_*); it is purely informational and never changes
    the maths.

    The engine imports no learner state and no Home Assistant: the coordinator
    binds ``shademap.effective_tau`` (over the persisted ShademapState) into
    ``beam_tau`` and composes ``bias.apply_intraday_scalar`` +
    ``bias.apply_day_ahead_bias`` into ``slot_factor``, then hands this object
    to :func:`compute_forecast`.

    ``band_by_slot`` (v0.4, SPEC §6/§10) is the quantile hook: a mapping from
    each slot-start datetime the engine iterates to that slot's empirical
    ``QuantileBands`` (P10/P50/P90 multipliers for its weather-class x day-part
    bin). When it is None or empty the engine emits NO band curves (the
    ForecastResult band fields stay empty and every consumer reads "no band" as
    band == corrected, no fabricated spread — bit-exact with the pre-quantile
    path). When populated, the engine multiplies the CORRECTED per-slot site
    power by each band factor to produce the p10/p50/p90 15-min watts curves and
    their hourly Wh roll-ups, in the SAME instantaneous frame as ``total_watts``.
    The coordinator builds it (per-slot cloud-class classification -> bin ->
    ``quantiles.bands_for_bin``); the engine treats the bands as opaque scalars.
    """

    beam_tau: BeamTauHook | None = None
    slot_factor: SlotFactorHook | None = None
    correction_source: str = CORRECTION_SOURCE_NONE
    band_by_slot: dict[datetime, QuantileBands] | None = None


# Module-level identity hooks so the raw pass and a learner-free corrected pass
# share one object (and the identity path is a plain attribute read, no call).
_NEUTRAL_HOOKS = LearnerHooks()


def _slot_albedo(slot: WeatherSlot) -> float:
    """Snow-aware ground albedo for a slot (SPEC §4 physics musts)."""
    depth = slot.snow_depth_m
    if depth is not None and depth > SNOW_DEPTH_THRESHOLD_M:
        return ALBEDO_SNOW
    return ALBEDO_DEFAULT


def _slot_is_usable(slot: WeatherSlot) -> bool:
    """True when the core irradiance / temperature inputs are all present.

    A None in any of GHI / DNI / DHI / temperature means the weather image is
    incomplete for this slot (fetcher gap, provider hole). Rather than guess,
    the engine treats the slot as zero-production (SPEC degradation ethos:
    never silently fabricate). Returns False so the caller emits zeros.
    """
    return (
        slot.ghi is not None
        and slot.dni is not None
        and slot.dhi is not None
        and slot.temp_c is not None
    )


@dataclass(frozen=True, slots=True)
class _PlanePoaSplit:
    """POA components split into beam-driven vs. diffuse-driven (W/m^2).

    ``beam_poa`` = gated beam + gated circumsolar (the direct share the
    shademap references); ``diffuse_poa`` = gated isotropic + ground (the shade
    floor). Their sum is the plane POA fed to the DC model.
    ``beam_poa_ungated`` is beam + circumsolar with tau := 1 (clear horizon) —
    the SLOW learner's beam reference (SPEC §5, FIX-3): the learned tau REPLACES
    the static tau, so the training reference must be the un-attenuated beam.
    """

    beam_poa: float
    diffuse_poa: float
    beam_poa_ungated: float


def _plane_poa_split(
    plane: PlaneConfig,
    svf: float,
    slot: WeatherSlot,
    sun_az: float,
    sun_el: float,
    albedo: float,
    doy: int,
    beam_tau: BeamTauHook | None,
) -> _PlanePoaSplit:
    """Horizon-gated Hay-Davies POA split (W/m^2) for one plane in one slot.

    Beam + circumsolar are multiplied by the plane's transmittance when the sun
    sits at or below the interpolated horizon line. The transmittance is the
    STATIC horizon tau (raw physics) unless ``beam_tau`` is given, in which case
    the learned/blended tau REPLACES it (SPEC §5 slow learner). The isotropic
    diffuse is always scaled by the plane's static sky-view factor; the ground
    reflection is unaffected by the horizon (it comes from below). Neither the
    diffuse nor the ground term is ever touched by the beam transmittance, so a
    fully occluding wall bin (tau=0) kills the beam but keeps the diffuse floor.
    """
    comps = transpose.hay_davies_poa(
        ghi=slot.ghi,
        dni=slot.dni,
        dhi=slot.dhi,
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

    # Horizon beam gate: only when the sun is actually behind the horizon line
    # for this azimuth do we attenuate the direct components. Above the line
    # the static tau is irrelevant (full transmission), but a learned bin can
    # still darken the beam (near-field trees / building edge the static table
    # missed), so consult the hook there too — with static_prior = 1.0 above
    # the line, a shrinkage blend leans on the learned tau exactly as intended.
    horizon_elev = horizon.interp_elevation(plane, sun_az)
    if sun_el <= horizon_elev:
        static_tau = horizon.transmittance_at(plane, sun_az, doy)
    else:
        static_tau = 1.0

    # UNGATED beam+circumsolar (tau := 1): the shademap's beam reference. Capture
    # it BEFORE the tau multiply (linear in tau, so ungated == gated / tau).
    beam_poa_ungated = beam + circ
    if beam_poa_ungated < 0.0:
        beam_poa_ungated = 0.0

    if beam_tau is not None:
        tau = beam_tau(plane.name, sun_az, sun_el, doy, static_tau)
    else:
        tau = static_tau

    if tau != 1.0:
        beam *= tau
        circ *= tau

    # Diffuse sky-view gate: static per-plane reduction of the isotropic sky
    # dome (fixes E4 — diffuse was never reduced by obstructions). Never gated
    # by the beam transmittance.
    iso *= svf

    beam_poa = beam + circ
    if beam_poa < 0.0:
        beam_poa = 0.0
    diffuse_poa = iso + ground
    if diffuse_poa < 0.0:
        diffuse_poa = 0.0

    return _PlanePoaSplit(
        beam_poa=beam_poa,
        diffuse_poa=diffuse_poa,
        beam_poa_ungated=beam_poa_ungated,
    )


def _dc_split(
    split: _PlanePoaSplit,
    plane: PlaneConfig,
    temp_c: float,
) -> tuple[float, float]:
    """DC power attributable to the beam vs. diffuse POA for one plane (W).

    The Ross temperature derate is a function of the TOTAL POA (cell heating is
    driven by the whole irradiance), so both shares are computed at the total
    cell temperature and split by their POA fraction. This keeps
    ``beam_dc + diffuse_dc == dc_power(total_poa, ...)`` exactly, so the split
    is a faithful decomposition of the plane's unclamped DC power.
    """
    total_poa = split.beam_poa + split.diffuse_poa
    total_dc = electrical.dc_power(total_poa, plane.wp, temp_c, plane.efficiency)
    if total_poa <= 0.0 or total_dc <= 0.0:
        return 0.0, 0.0
    beam_frac = split.beam_poa / total_poa
    beam_dc = total_dc * beam_frac
    diffuse_dc = total_dc - beam_dc
    return beam_dc, diffuse_dc


def _split_clamp(
    beam_dc: dict[str, float],
    diffuse_dc: dict[str, float],
    clamped_total: dict[str, float],
    unclamped_total: dict[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
    """Redistribute the AC clamp back onto each plane's beam/diffuse shares.

    ``clamp_groups`` returns the per-plane clamped total; we scale each plane's
    beam and diffuse DC by the same clamp factor (clamped/unclamped) so the
    reported beam/diffuse watts sum to the clamped plane total. A plane whose
    unclamped total is zero passes through untouched.
    """
    beam_out: dict[str, float] = {}
    diffuse_out: dict[str, float] = {}
    for name in beam_dc:
        unc = unclamped_total.get(name, 0.0)
        cl = clamped_total.get(name, 0.0)
        f = cl / unc if unc > 0.0 else 1.0
        beam_out[name] = beam_dc[name] * f
        diffuse_out[name] = diffuse_dc[name] * f
    return beam_out, diffuse_out


def compute_forecast(
    site: SiteConfig,
    weather: WeatherSeries,
    now: datetime,
    tz: tzinfo | None = None,
    *,
    hooks: LearnerHooks | None = None,
) -> ForecastResult:
    """Compute the RAW and CORRECTED forecast for the whole weather window.

    For each 15-min slot: sun position at the slot midpoint, Hay-Davies POA per
    plane, horizon transmittance on beam+circumsolar (static for the RAW curve;
    ``hooks.beam_tau`` for the CORRECTED curve) and sky-view-factor on the
    isotropic diffuse, snow-aware ground albedo, Ross-derated DC power, then
    per-inverter-group AC clamp. The corrected per-slot site power is then
    scaled by ``hooks.slot_factor`` (fast learner). Aggregates BOTH curves to
    hourly Wh (keyed by ISO UTC hour) and daily kWh (keyed by ISO date).

    The per-plane per-slot ``beam_watts`` / ``diffuse_watts`` / ``kc`` on each
    ``PlaneResult`` (aligned to ``slot_starts``) give the coordinator every
    input it needs to roll up the per-plane hourly ``PlaneHourlyModeled``
    (beam_wh / diffuse_wh / ghi / kc) for the nightly issued snapshot v2 and to
    train the shademap from hourly LTS (SPEC §9); the engine deliberately keeps
    that hourly-per-plane aggregation OUT of ForecastResult so the frozen result
    contract stays minimal.

    When ``hooks`` is None (or its callables are None) the corrected curve
    equals the raw curve bit-exact, so a learner-free build is unchanged.

    Args:
        site: the (already validated) site configuration.
        weather: ordered 15-min weather slots (tz-aware UTC).
        now: current tz-aware UTC time (for as-issued stamping by callers).
        tz: local calendar timezone for the daily kWh buckets. Defaults to
            UTC. Kept an optional positional so the frozen contract
            ``compute_forecast(site, weather, now)`` is preserved; the HA glue
            passes ``hass.config.time_zone`` here so "today/tomorrow/d2" match
            the operator's local midnight.
        hooks: optional learner callables (SLOW-learner beam transmittance and
            FAST-learner per-slot factor). None => corrected == raw.

    Returns:
        A ForecastResult aligned to ``weather``'s slot starts. ``total_watts``
        / ``hourly_wh`` / ``daily_kwh`` are the CORRECTED (served) curve;
        ``raw_total_watts`` / ``raw_hourly_wh`` / ``raw_daily_kwh`` are the pure
        physics. Each ``PlaneResult`` carries corrected ``watts`` plus the raw
        ``raw_watts`` and the per-slot ``beam_watts`` / ``diffuse_watts`` / ``kc``.
    """
    cal_tz = tz if tz is not None else UTC
    hk = hooks if hooks is not None else _NEUTRAL_HOOKS
    beam_tau_hook = hk.beam_tau
    slot_factor_hook = hk.slot_factor
    learners_active = beam_tau_hook is not None or slot_factor_hook is not None

    lat = site.latitude
    lon = site.longitude
    planes = site.planes
    groups = site.groups

    # Sky-view factor is a static per-plane property of geometry + horizon;
    # compute it once, not per slot.
    svf_by_plane: dict[str, float] = {
        plane.name: horizon.sky_view_factor(plane) for plane in planes
    }

    slot_starts: list[datetime] = []
    # RAW (pure-physics) per-slot site total and per-plane series.
    raw_total_watts: list[float] = []
    raw_plane_series: dict[str, list[float]] = {p.name: [] for p in planes}
    # CORRECTED per-slot site total and per-plane series.
    total_watts: list[float] = []
    plane_series: dict[str, list[float]] = {p.name: [] for p in planes}
    # Per-plane beam / diffuse DC watts (CORRECTED, post-clamp) + kc, aligned.
    beam_series: dict[str, list[float]] = {p.name: [] for p in planes}
    diffuse_series: dict[str, list[float]] = {p.name: [] for p in planes}
    # SLOW-learner training reference (SPEC §5, FIX-3): UNGATED beam DC and raw
    # diffuse DC at the RAW operating point — never gated by the learned tau,
    # never clamped, never slot-factored. Labels must not depend on learned
    # state (else T self-references toward sqrt(true_t)).
    beam_ref_series: dict[str, list[float]] = {p.name: [] for p in planes}
    diffuse_ref_series: dict[str, list[float]] = {p.name: [] for p in planes}
    kc_series: list[float] = []  # site-level clear-sky index per slot

    raw_hourly_wh: dict[str, float] = {}
    raw_daily_kwh: dict[str, float] = {}
    hourly_wh: dict[str, float] = {}
    daily_kwh: dict[str, float] = {}

    def _append_zero_slot() -> None:
        raw_total_watts.append(0.0)
        total_watts.append(0.0)
        kc_series.append(0.0)
        for p in planes:
            raw_plane_series[p.name].append(0.0)
            plane_series[p.name].append(0.0)
            beam_series[p.name].append(0.0)
            diffuse_series[p.name].append(0.0)
            beam_ref_series[p.name].append(0.0)
            diffuse_ref_series[p.name].append(0.0)

    for slot in weather.slots:
        start = slot.start
        slot_starts.append(start)

        if not _slot_is_usable(slot):
            # Missing weather -> zero production for this slot, but keep the
            # slot present so downstream alignment / hourly bucketing is dense.
            _append_zero_slot()
            continue

        midpoint = slot.midpoint
        sun_az, sun_el = solpos.sun_position(midpoint, lat, lon)
        doy = midpoint.timetuple().tm_yday
        albedo = _slot_albedo(slot)

        # Below the horizon there is no beam, but the tilted plane still
        # receives the isotropic diffuse and the ground term while the sky is
        # bright (civil twilight, winter fog). Only short-circuit to zero when
        # there is no diffuse/global irradiance to transpose at all — never
        # silently clip real twilight diffuse (SPEC E4).
        if sun_el <= 0.0 and slot.dhi <= 0.0 and slot.ghi <= 0.0:
            _append_zero_slot()
            continue

        # Clear-sky index for this slot (learning gate / normaliser, SPEC §5).
        kc = clearsky.clear_sky_index(slot.ghi, sun_el)
        kc_series.append(kc)

        # --- per-plane RAW and CORRECTED DC power split ---
        raw_unclamped: dict[str, float] = {}
        cor_unclamped: dict[str, float] = {}
        cor_beam_dc: dict[str, float] = {}
        cor_diffuse_dc: dict[str, float] = {}

        for plane in planes:
            svf = svf_by_plane[plane.name]

            # RAW: static horizon tau gates the beam.
            raw_split = _plane_poa_split(
                plane, svf, slot, sun_az, sun_el, albedo, doy, beam_tau=None
            )
            raw_beam_dc, raw_diffuse_dc = _dc_split(raw_split, plane, slot.temp_c)
            raw_unclamped[plane.name] = raw_beam_dc + raw_diffuse_dc

            # SLOW-learner label reference (SPEC §5, FIX-3): UNGATED beam DC at
            # the RAW operating point, pre-clamp, no slot factor. DC is linear in
            # POA at a fixed cell temperature, so derive one Wp-per-POA
            # conversion from the raw total (== engine's real operating point)
            # and apply it to the ungated beam POA — byte-for-byte the same
            # semantics as backfill.reconstruct_plane_hour.
            raw_total_poa = raw_split.beam_poa + raw_split.diffuse_poa
            conv = (
                raw_unclamped[plane.name] / raw_total_poa
                if raw_total_poa > 0.0 else 0.0
            )
            beam_ref_series[plane.name].append(
                raw_split.beam_poa_ungated * conv
            )
            diffuse_ref_series[plane.name].append(raw_diffuse_dc)

            if learners_active:
                # CORRECTED: learned tau gates the beam (shademap). Only the
                # beam_tau hook changes the POA; the slot_factor is applied to
                # the site total after the clamp.
                cor_split = _plane_poa_split(
                    plane, svf, slot, sun_az, sun_el, albedo, doy,
                    beam_tau=beam_tau_hook,
                )
                b_dc, d_dc = _dc_split(cor_split, plane, slot.temp_c)
            else:
                b_dc, d_dc = raw_beam_dc, raw_diffuse_dc

            cor_beam_dc[plane.name] = b_dc
            cor_diffuse_dc[plane.name] = d_dc
            cor_unclamped[plane.name] = b_dc + d_dc

        # --- AC clamp both curves ---
        raw_clamped = electrical.clamp_groups(raw_unclamped, groups)
        cor_clamped = electrical.clamp_groups(cor_unclamped, groups)
        # Split the corrected clamp back onto beam/diffuse for attribution.
        cor_beam_cl, cor_diffuse_cl = _split_clamp(
            cor_beam_dc, cor_diffuse_dc, cor_clamped, cor_unclamped
        )

        # --- fast-learner per-slot factor (aggregation stage) ---
        factor = 1.0
        if slot_factor_hook is not None:
            try:
                factor = float(slot_factor_hook(start))
            except (TypeError, ValueError):
                factor = 1.0

        raw_slot_total = 0.0
        cor_slot_total = 0.0
        for plane in planes:
            rw = raw_clamped.get(plane.name, 0.0)
            raw_plane_series[plane.name].append(rw)
            raw_slot_total += rw

            cw = cor_clamped.get(plane.name, 0.0) * factor
            plane_series[plane.name].append(cw)
            cor_slot_total += cw

            bw = cor_beam_cl.get(plane.name, 0.0) * factor
            dw = cor_diffuse_cl.get(plane.name, 0.0) * factor
            beam_series[plane.name].append(bw)
            diffuse_series[plane.name].append(dw)

        raw_total_watts.append(raw_slot_total)
        total_watts.append(cor_slot_total)

        # --- energy roll-ups (interval-mean power * slot hours) ---
        hour_start = start.astimezone(UTC).replace(
            minute=0, second=0, microsecond=0
        )
        hkey = hour_start.isoformat()
        day_key = start.astimezone(cal_tz).date().isoformat()

        raw_wh = raw_slot_total * _SLOT_HOURS
        raw_hourly_wh[hkey] = raw_hourly_wh.get(hkey, 0.0) + raw_wh
        raw_daily_kwh[day_key] = raw_daily_kwh.get(day_key, 0.0) + raw_wh / 1000.0

        cor_wh = cor_slot_total * _SLOT_HOURS
        hourly_wh[hkey] = hourly_wh.get(hkey, 0.0) + cor_wh
        daily_kwh[day_key] = daily_kwh.get(day_key, 0.0) + cor_wh / 1000.0

    plane_results = tuple(
        PlaneResult(
            name=plane.name,
            watts=tuple(plane_series[plane.name]),
            raw_watts=tuple(raw_plane_series[plane.name]),
            beam_watts=tuple(beam_series[plane.name]),
            diffuse_watts=tuple(diffuse_series[plane.name]),
            kc=tuple(kc_series),
            beam_ref_watts=tuple(beam_ref_series[plane.name]),
            diffuse_ref_watts=tuple(diffuse_ref_series[plane.name]),
        )
        for plane in planes
    )

    # --- Quantile bands (v0.4, SPEC §6/§10) ---------------------------------
    # When a per-slot band map is injected, emit the P10/P50/P90 site-power
    # curves by multiplying the CORRECTED per-slot total by each band factor,
    # then roll them up to hourly Wh using the SAME UTC hour key as ``hourly_wh``
    # so the corrected and band hourly curves stay index-aligned. When no band
    # map is present the fields stay empty and every consumer treats "no band"
    # as band == corrected (bit-exact with the pre-quantile path).
    p10_watts: tuple[float, ...] = ()
    p50_watts: tuple[float, ...] = ()
    p90_watts: tuple[float, ...] = ()
    p10_hourly_wh: dict[str, float] = {}
    p50_hourly_wh: dict[str, float] = {}
    p90_hourly_wh: dict[str, float] = {}
    band_by_slot = hk.band_by_slot
    if band_by_slot:
        p10_watts, p50_watts, p90_watts = quantiles.band_curve_from_corrected(
            total_watts, slot_starts, band_by_slot,
        )
        for i, start in enumerate(slot_starts):
            hkey = (
                start.astimezone(UTC)
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            # Keep the band hourly key set IDENTICAL to the corrected
            # ``hourly_wh``: slots that short-circuited to zero-production (fully
            # dark twilight / missing weather) never created a corrected hour
            # bucket, and their band watts are zero anyway, so they must not
            # introduce spurious empty band hours either.
            if hkey not in hourly_wh:
                continue
            p10_hourly_wh[hkey] = p10_hourly_wh.get(hkey, 0.0) + p10_watts[i] * _SLOT_HOURS
            p50_hourly_wh[hkey] = p50_hourly_wh.get(hkey, 0.0) + p50_watts[i] * _SLOT_HOURS
            p90_hourly_wh[hkey] = p90_hourly_wh.get(hkey, 0.0) + p90_watts[i] * _SLOT_HOURS

    return ForecastResult(
        slot_starts=tuple(slot_starts),
        total_watts=tuple(total_watts),
        plane_results=plane_results,
        hourly_wh=hourly_wh,
        daily_kwh=daily_kwh,
        raw_total_watts=tuple(raw_total_watts),
        raw_hourly_wh=raw_hourly_wh,
        raw_daily_kwh=raw_daily_kwh,
        correction_source=hk.correction_source,
        p10_watts=p10_watts,
        p50_watts=p50_watts,
        p90_watts=p90_watts,
        p10_hourly_wh=p10_hourly_wh,
        p50_hourly_wh=p50_hourly_wh,
        p90_hourly_wh=p90_hourly_wh,
    )
