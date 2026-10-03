"""Forecast engine: dual-curve physics pipeline + learner hooks (stdlib only).

Owner: engine. Pure, HA-free. Ties together solpos -> transpose -> horizon
-> electrical over every 15-min slot and every plane, then rolls the
AC-clamped site total up to hourly Wh and daily kWh (SPEC §4/§5/§6).

Dual curve (SPEC §9/§16.2, operator decision 2026-07-06)
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
     scales the CORRECTED (already shademap-transposed) per-slot site power and
     is then RE-CLAMPED to the inverter groups (``electrical.clamp_groups`` runs
     a SECOND time, after the factor) so an up-correction (factor > 1) can never
     push served DC past the physical ``AC limit / eta`` clip point. The coordinator
     composes intraday decay + day-ahead bias into this one factor so the 15-min
     ``total_watts`` and the hourly ``hourly_wh`` stay mutually consistent (both
     are derived from the same factored-then-re-clamped slot power). A hook that
     returns 1.0 is the identity, and any factor <= 1 leaves the values already
     within limits so the re-clamp is a mathematical no-op (bit-exact). Planes
     in no inverter group have no configured ceiling and pass through both
     clamps unchanged.

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
    slot_factor * clamped, then clamp_groups() AGAIN --> re-clamped served watts
                    (the factor is applied between the two clamps, so factor > 1
                    can never lift the served curve above the AC ceiling)

Attribution split: the per-plane DC power is decomposed into the part driven
by the beam+circumsolar POA vs. the diffuse+ground POA (pre-AC-clamp), so the
SLOW learner can train the beam-referenced transmittance
``T = (P_measured - P_diffuse_modeled) / P_beam_modeled`` (SPEC §9.1). The AC
clamp is applied to the TOTAL, then split back proportionally so the reported
beam/diffuse watts already respect the clamp.

AC-side served curve (Phase 1, AC-side forecast)
------------------------------------------------
On top of the DC pipeline the engine additionally derives the served AC as a
deterministic physical transform (``electrical.clamp_groups_ac``): per inverter
group AC = min(eta_inv * sum(DC_served), ac_limit). The served DC already
contains the postclip learned correction and its second electrical clamp.
This is emitted as the additive ``ac_watts`` / ``ac_hourly_wh`` /
``ac_daily_kwh`` fields.

The served-DC path and the AC transform use the SAME physical clip point
``ac_limit/eta_inv``. ``total_watts`` / ``hourly_wh`` / ``daily_kwh`` remain
the DC learning and scoreboard truth; AC is the additional delivered curve
``min(eta_inv * DC, ac_limit)``.

Aggregation: the clamped site total (raw and corrected) is integrated to
hourly Wh (keyed by ISO-8601 UTC hour) and daily kWh (keyed by ISO date in the
``tz`` calendar, UTC by default). The per-plane, per-slot beam_watts /
diffuse_watts / kc series on each PlaneResult let the coordinator roll up the
per-plane hourly beam_wh / diffuse_wh / ghi / kc (PlaneHourlyModeled) for the
nightly issued snapshot v2 (SPEC §16.2) without bloating the frozen result type.
Slots with missing (None) irradiance/temperature are treated as
zero-production and skipped safely.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo

from ..const import (
    ALBEDO_DEFAULT,
    ALBEDO_SNOW,
    BEAM_GAIN_DEFAULT,
    CORRECTION_SOURCE_NONE,
    DEFAULT_INVERTER_EFFICIENCY,
    SLOT_HOURS,
    SNOW_DEPTH_THRESHOLD_M,
)
from . import clearsky, electrical, horizon, plane_physics, quantiles, solpos
from . import transpose as transpose  # compatibility for independent reference tests
from .curves import EnergyTotals
from .plane_physics import PlanePoaComponents as _PlanePoaComponents
from .plane_physics import (
    PlanePoaSplit as _PlanePoaSplit,  # noqa: F401 - reference-test compatibility
)
from .plane_physics import dc_split as _dc_split
from .plane_physics import gate_split as _gate_split
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
_SLOT_HOURS = SLOT_HOURS


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
    """Optional learner callables injected into the engine (SPEC §9/§16.2).

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

    ``band_by_slot`` (v0.4, SPEC §11.2) is the quantile hook: a mapping from
    each slot-start datetime the engine iterates to that slot's empirical
    ``QuantileBands`` (P10/P50/P90 multipliers for its weather-class x day-part
    bin). When it is None or empty the engine emits NO band curves (the
    ForecastResult band fields stay empty and every consumer reads "no band" as
    band == corrected, no fabricated spread — bit-exact with the pre-quantile
    path). When populated, the engine multiplies the CORRECTED per-slot site
    power by each band factor to produce the p10/p50/p90 15-min watts curves and
    their hourly Wh roll-ups, in the SAME instantaneous frame as ``total_watts``,
    then caps each DC band curve at the slot's physical served-DC ceiling (sum
    of group ``ac_limit / eta`` clip points + corrected watts of ceiling-free
    ungrouped planes) so a P90 factor > 1 cannot exceed what the inverters can
    accept. AC bands use the separate AC ceilings.
    The coordinator builds it (per-slot cloud-class classification -> bin ->
    ``quantiles.bands_for_bin``); the engine treats the bands as opaque scalars.
    """

    beam_tau: BeamTauHook | None = None
    slot_factor: SlotFactorHook | None = None
    correction_source: str = CORRECTION_SOURCE_NONE
    band_by_slot: dict[datetime, QuantileBands] | None = None
    # AC-side inverter efficiency site calibration (AC-side Phase 3): a single
    # LEARNED eta_inv calibrated against the site's TOTAL-AC meter that OVERRIDES
    # the per-group config eta for ALL groups on the AC curve. None (default) =>
    # the engine uses each group's own ``inverter_efficiency`` (ungrouped planes
    # the DEFAULT), so a build without a trusted calibration is bit-identical.
    # NEVER touches the DC path — only the served-AC transform reads it.
    inverter_efficiency: float | None = None


# Module-level identity hooks so the raw pass and a learner-free corrected pass
# share one object (and the identity path is a plain attribute read, no call).
_NEUTRAL_HOOKS = LearnerHooks()


def _slot_albedo(slot: WeatherSlot, base_albedo: float = ALBEDO_DEFAULT) -> float:
    """Snow-aware ground albedo for a slot (SPEC §4.6 physics musts).

    ``base_albedo`` is the site's configured ground albedo (v0.20,
    ``SiteConfig.albedo``; the shipped default when unset) — snow cover still
    overrides it with ALBEDO_SNOW, because fresh snow reflects the same no
    matter what the bare ground underneath looks like.
    """
    depth = slot.snow_depth_m
    if depth is not None and depth > SNOW_DEPTH_THRESHOLD_M:
        return ALBEDO_SNOW
    return base_albedo


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


def _plane_poa_components(
    plane: PlaneConfig,
    svf: float,
    slot: WeatherSlot,
    sun_az: float,
    sun_el: float,
    albedo: float,
    doy: int,
    beam_gain: float = BEAM_GAIN_DEFAULT,
) -> _PlanePoaComponents:
    """Slot adapter to the shared plane physics; no independent math here."""
    return plane_physics.poa_components(
        plane, svf, ghi=slot.ghi, dni=slot.dni, dhi=slot.dhi,
        sun_az=sun_az, sun_el=sun_el, albedo=albedo, doy=doy,
        beam_gain=beam_gain,
    )


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
    scaled by ``hooks.slot_factor`` (fast learner) and RE-CLAMPED to the inverter
    groups (``electrical.clamp_groups`` a second time, after the factor) so an
    up-correction (factor > 1) can never push the served curve past the
    configured served-DC clip point; a factor <= 1 leaves the values within limits so the
    re-clamp is a no-op (bit-exact common path). Planes in no inverter group
    have no configured ceiling and pass both clamps through unchanged. Aggregates
    BOTH curves to hourly Wh (keyed by ISO UTC hour) and daily kWh (keyed by ISO
    date).

    The per-plane per-slot ``beam_watts`` / ``diffuse_watts`` / ``kc`` on each
    ``PlaneResult`` (aligned to ``slot_starts``) give the coordinator every
    input it needs to roll up the per-plane hourly ``PlaneHourlyModeled``
    (beam_wh / diffuse_wh / ghi / kc) for the nightly issued snapshot v2 and to
    train the shademap from hourly LTS (SPEC §9.1); the engine deliberately keeps
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

    lat = site.latitude
    lon = site.longitude
    planes = site.planes
    groups = site.groups
    # Site ground albedo (v0.20): configured value or the shipped default;
    # ``getattr`` keeps analytic test fakes without the field working. Snow
    # still overrides per slot inside ``_slot_albedo``.
    base_albedo = getattr(site, "albedo", None)
    if base_albedo is None:
        base_albedo = ALBEDO_DEFAULT
    # Site bifacial beam gain (forensik T6): configured factor or the shipped
    # identity default; ``getattr`` keeps analytic test fakes without the field
    # working. Applied to the beam+circumsolar POA in ``_plane_poa_components``.
    base_beam_gain = getattr(site, "bifacial_beam_gain", None)
    if base_beam_gain is None:
        base_beam_gain = BEAM_GAIN_DEFAULT

    # Sky-view factor is a per-plane, per-day-of-year property (the horizon is
    # semi-transparent to the diffuse, so a seasonal foliage row ramps the SVF
    # with the day). It is memoized at MODULE level inside
    # ``horizon.sky_view_factor`` (keyed on the plane geometry + doy), so the
    # O(360) quadrature runs at most once per plane per day for the WHOLE process
    # and survives across the 15-min recompute cycles — no per-call memo here
    # (audit #9b).

    # Static AC ceiling ingredients for the per-slot band cap (SPEC §11.2). The
    # most DC the site can serve in a slot is the sum of every group's
    # ac_limit/eta clip point
    # plus the corrected watts of any plane in NO group (ceiling-free). The group
    # sum is static; the ungrouped contribution is added per slot below.
    total_group_ac_limit = sum(g.ac_limit_w for g in groups)
    total_group_dc_limit = sum(
        g.ac_limit_w / electrical._clamp_eta(g.inverter_efficiency)
        for g in groups
    )
    grouped_names = {name for g in groups for name in g.plane_names}

    # AC-side inverter efficiency (AC-side Phase 3): when ``hooks`` carry a
    # trusted site-level LEARNED eta_inv it OVERRIDES the per-group config eta for
    # ALL groups on the AC curve; None => each group keeps its own configured eta.
    # ``ac_groups`` (the groups whose eta the AC transform sees) and ``plane_eta``
    # (the pre-clamp AC weighting) are BOTH sourced from this one decision so the
    # served AC and the pre-clamp AC stay mutually consistent. The DC path below
    # never reads either — it keeps using ``groups`` verbatim, so it is untouched.
    cal_eta = (
        electrical._clamp_eta(hk.inverter_efficiency)
        if hk.inverter_efficiency is not None
        else None
    )
    ac_groups = (
        tuple(replace(g, inverter_efficiency=cal_eta) for g in groups)
        if cal_eta is not None
        else groups
    )

    # Per-plane DC->AC efficiency for the PRE-AC-clamp AC total (the AC analogue
    # of ``corrected_unclamped_watts``, SPEC AC-side Phase 2): a grouped plane
    # uses its group's clamped eta_inv (the learned eta when calibrated), an
    # ungrouped plane the learned eta when calibrated else the flat default —
    # exactly the split ``electrical.clamp_groups_ac`` applies (over ``ac_groups``),
    # so the pre-clamp AC equals the served AC on every UNclipped slot. Static
    # over the window.
    _ungrouped_eta = cal_eta if cal_eta is not None else DEFAULT_INVERTER_EFFICIENCY
    plane_eta = {p.name: _ungrouped_eta for p in planes}
    for g in ac_groups:
        eta_g = electrical._clamp_eta(g.inverter_efficiency)
        for name in g.plane_names:
            if name in plane_eta:
                plane_eta[name] = eta_g

    slot_starts: list[datetime] = []
    # RAW (pure-physics) per-slot site total and per-plane series.
    raw_total_watts: list[float] = []
    raw_plane_series: dict[str, list[float]] = {p.name: [] for p in planes}
    slow_plane_series: dict[str, list[float]] = {p.name: [] for p in planes}
    # CORRECTED per-slot site total and per-plane series.
    total_watts: list[float] = []
    # PRE-re-clamp corrected site total per slot (sum of cor_clamped * factor,
    # BEFORE the second AC clamp). Its difference from ``total_watts`` reveals
    # per slot whether the re-clamp bit; the coordinator's day-ahead headline
    # strip reads it (SPEC §14.1). Always populated, aligned to ``slot_starts``.
    corrected_unclamped_watts: list[float] = []
    plane_series: dict[str, list[float]] = {p.name: [] for p in planes}
    # Per-plane beam / diffuse DC watts (CORRECTED, post-clamp) + kc, aligned.
    beam_series: dict[str, list[float]] = {p.name: [] for p in planes}
    diffuse_series: dict[str, list[float]] = {p.name: [] for p in planes}
    # SLOW-learner training reference (SPEC §9.1, FIX-3): UNGATED beam DC and raw
    # diffuse DC at the RAW operating point — never gated by the learned tau,
    # never clamped, never slot-factored. Labels must not depend on learned
    # state (else T self-references toward sqrt(true_t)).
    beam_ref_series: dict[str, list[float]] = {p.name: [] for p in planes}
    diffuse_ref_series: dict[str, list[float]] = {p.name: [] for p in planes}
    kc_series: list[float] = []  # site-level clear-sky index per slot
    # Physical AC ceiling per slot (aligned to slot_starts), used only to cap the
    # quantile band curves after the main loop (a P90 factor > 1 must never
    # exceed what the inverters can deliver).
    slot_ceilings: list[float] = []

    raw_energy = EnergyTotals(tz=cal_tz)
    corrected_energy = EnergyTotals(tz=cal_tz)
    # AC point forecast: served DC through each group's eta and
    # AC limit. Separate from the DC learning curve, aligned to slot_starts.
    ac_watts: list[float] = []
    ac_energy = EnergyTotals(tz=cal_tz)
    # AC-side PRE-clamp total per slot (Phase 2): the AC analogue of
    # ``corrected_unclamped_watts`` — Sum_planes eta(group) * (cor_clamped *
    # factor) BEFORE the second inverter clamp, aligned to slot_starts.
    ac_corrected_unclamped_watts: list[float] = []
    # Physical AC ceiling per slot (aligned to slot_starts): sum of the group AC
    # limits + the AC of any ceiling-free (ungrouped) planes. Used only to cap the
    # AC quantile band curves after the main loop (mirrors ``slot_ceilings`` for DC).
    ac_slot_ceilings: list[float] = []

    def _append_zero_slot() -> None:
        raw_total_watts.append(0.0)
        total_watts.append(0.0)
        corrected_unclamped_watts.append(0.0)
        ac_watts.append(0.0)
        # No production => no pre-clamp AC and no ungrouped AC contribution; the
        # group ceiling is the slot's AC ceiling (band watts are zero here anyway).
        ac_corrected_unclamped_watts.append(0.0)
        ac_slot_ceilings.append(total_group_ac_limit)
        kc_series.append(0.0)
        # No production => no ungrouped contribution; the group ceiling is the
        # slot's ceiling (band watts are zero here anyway, so this only keeps the
        # list dense and aligned to slot_starts).
        slot_ceilings.append(total_group_dc_limit)
        for p in planes:
            raw_plane_series[p.name].append(0.0)
            slow_plane_series[p.name].append(0.0)
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

        # _slot_is_usable just gated a None temperature; the local re-check
        # keeps the narrowing explicit (WeatherSlot.temp_c is Optional since
        # the fetcher stopped fabricating 0 °C for provider gaps).
        temp_c = slot.temp_c
        if temp_c is None:  # pragma: no cover - gated above, defensive only
            _append_zero_slot()
            continue

        midpoint = slot.midpoint
        sun_az, sun_el = solpos.sun_position(midpoint, lat, lon)
        doy = midpoint.timetuple().tm_yday
        albedo = _slot_albedo(slot, base_albedo)

        # Below the horizon there is no beam, but the tilted plane still
        # receives the isotropic diffuse and the ground term while the sky is
        # bright (civil twilight, winter fog). Only short-circuit to zero when
        # there is no diffuse/global irradiance to transpose at all — never
        # silently clip real twilight diffuse (SPEC E4).
        if sun_el <= 0.0 and slot.dhi <= 0.0 and slot.ghi <= 0.0:
            _append_zero_slot()
            continue

        # Clear-sky index for this slot (learning gate / normaliser, SPEC §4.2).
        kc = clearsky.clear_sky_index(slot.ghi, sun_el)
        kc_series.append(kc)

        # --- per-plane RAW and CORRECTED DC power split ---
        raw_unclamped: dict[str, float] = {}
        cor_unclamped: dict[str, float] = {}
        cor_beam_dc: dict[str, float] = {}
        cor_diffuse_dc: dict[str, float] = {}

        for plane in planes:
            svf = horizon.sky_view_factor(plane, doy=doy)

            # Shared, tau-independent POA decomposition — computed ONCE per
            # plane/slot. The RAW and CORRECTED curves differ ONLY in which
            # transmittance gates the beam+circumsolar, so the Hay-Davies
            # transposition, IAM, horizon interpolation and diffuse floor are
            # shared here instead of being redone for each curve (audit #9).
            comps = _plane_poa_components(
                plane, svf, slot, sun_az, sun_el, albedo, doy, base_beam_gain
            )

            # RAW: the static horizon tau gates the beam.
            raw_split = _gate_split(comps, comps.static_tau)
            raw_beam_dc, raw_diffuse_dc = _dc_split(raw_split, plane, temp_c)
            raw_unclamped[plane.name] = raw_beam_dc + raw_diffuse_dc

            # SLOW-learner label reference (SPEC §9.1, FIX-3): UNGATED beam DC at
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

            # CORRECTED: the learned tau REPLACES the static tau on the SHARED
            # components (shademap; only the beam gate differs). The slot_factor
            # is applied to the site total AFTER the clamp, not here. With NO
            # shademap hook the corrected split IS the raw split, so we reuse the
            # raw DC directly — no second transposition or DC computation.
            if beam_tau_hook is not None:
                cor_tau = beam_tau_hook(
                    plane.name, sun_az, sun_el, doy, comps.static_tau
                )
                cor_split = _gate_split(comps, cor_tau)
                b_dc, d_dc = _dc_split(cor_split, plane, temp_c)
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

        # RE-CLAMP after the factor: the first clamp above ran BEFORE the factor,
        # so an up-correction (factor > 1) would otherwise lift the served curve
        # past the physical inverter DC clip point. Scale the already-clamped per-plane
        # watts by the factor, then clamp the groups AGAIN so the corrected curve
        # is always bounded by the configured DC clip point. When factor <= 1 the
        # values are already within limits and this is a mathematical no-op, so
        # the common path stays bit-exact. Ungrouped planes have no configured
        # ceiling and pass through both clamps (see electrical.clamp_groups).
        correction = electrical.correct_groups(cor_clamped, groups, factor=factor)
        cor_factored = correction.before_clamp
        cor_final = correction.served
        # Redistribute the SECOND clamp onto the beam/diffuse shares (same helper
        # as the first clamp) so the reported attribution still sums to each
        # plane's final watts: scale the first-clamp shares by the factor, then
        # apply the clamped/unclamped ratio of the re-clamp.
        beam_factored = {
            name: watts * factor for name, watts in cor_beam_cl.items()
        }
        diffuse_factored = {
            name: watts * factor for name, watts in cor_diffuse_cl.items()
        }
        cor_beam_final, cor_diffuse_final = _split_clamp(
            beam_factored, diffuse_factored, cor_final, cor_factored
        )

        raw_slot_total = 0.0
        cor_slot_total = 0.0
        for plane in planes:
            rw = raw_clamped.get(plane.name, 0.0)
            raw_plane_series[plane.name].append(rw)
            raw_slot_total += rw

            # Exact pre-fast curve: shademap has been applied and physically
            # clamped, but neither day-ahead nor Intraday has touched it.
            slow_plane_series[plane.name].append(
                cor_clamped.get(plane.name, 0.0)
            )

            cw = cor_final.get(plane.name, 0.0)
            plane_series[plane.name].append(cw)
            cor_slot_total += cw

            bw = cor_beam_final.get(plane.name, 0.0)
            dw = cor_diffuse_final.get(plane.name, 0.0)
            beam_series[plane.name].append(bw)
            diffuse_series[plane.name].append(dw)

        raw_total_watts.append(raw_slot_total)
        total_watts.append(cor_slot_total)
        # Pre-re-clamp corrected total: the factored per-plane watts summed
        # BEFORE the second clamp. On a slot where the up-corrected curve hit the
        # AC ceiling this exceeds ``cor_slot_total`` (the served, re-clamped
        # total); the coordinator uses the gap to detect a clamped slot (SPEC §14.1).
        corrected_unclamped_watts.append(sum(cor_factored.values()))

        # Physical AC ceiling for this slot's band cap: group limits + the
        # ceiling-free (ungrouped) planes' corrected watts (SPEC §11.2).
        ungrouped_cor = sum(
            cor_final.get(p.name, 0.0)
            for p in planes
            if p.name not in grouped_names
        )
        slot_ceilings.append(total_group_dc_limit + ungrouped_cor)

        # Energy views share one UTC-hour/local-day aggregation contract.
        raw_energy.add(start, raw_slot_total)
        corrected_energy.add(start, cor_slot_total)

        # AC must consume the same corrected postclip DC as the learners.
        # Returning to cor_unclamped here resurrected clipped energy under
        # factors < 1 (50 W served DC could produce 90 W AC; ADR-0024).
        ac_input = cor_final
        _, ac_by_plane = electrical.clamp_groups_ac(
            ac_input, ac_groups, ungrouped_eta=_ungrouped_eta
        )
        ac_slot_total = sum(ac_by_plane.values())
        ac_watts.append(ac_slot_total)
        ac_energy.add(start, ac_slot_total)
        # Pre-AC-clamp AC total (AC analogue of corrected_unclamped_watts): the
        # eta-weighted factored DC per plane summed BEFORE the inverter AC clamp.
        # On a clipped slot this exceeds ``ac_slot_total`` (the served, clamped AC);
        # the coordinator's AC day-ahead strip uses the gap to detect a clamped
        # slot (SPEC §14.1). Equals ``ac_slot_total`` exactly on an unclipped slot.
        ac_corrected_unclamped_watts.append(
            sum(plane_eta[name] * w for name, w in cor_factored.items())
        )
        # Physical AC ceiling for this slot's band cap: the group AC limits plus
        # the served AC of any ceiling-free (ungrouped) planes (mirrors the DC
        # ``slot_ceilings`` on the AC curve — a P90 factor > 1 must never exceed
        # what the inverters can deliver).
        ungrouped_ac = sum(
            ac_by_plane.get(p.name, 0.0)
            for p in planes
            if p.name not in grouped_names
        )
        ac_slot_ceilings.append(total_group_ac_limit + ungrouped_ac)

    plane_results = tuple(
        PlaneResult(
            name=plane.name,
            watts=tuple(plane_series[plane.name]),
            raw_watts=tuple(raw_plane_series[plane.name]),
            slow_watts=tuple(slow_plane_series[plane.name]),
            beam_watts=tuple(beam_series[plane.name]),
            diffuse_watts=tuple(diffuse_series[plane.name]),
            kc=tuple(kc_series),
            beam_ref_watts=tuple(beam_ref_series[plane.name]),
            diffuse_ref_watts=tuple(diffuse_ref_series[plane.name]),
        )
        for plane in planes
    )

    # --- Quantile bands (v0.4, SPEC §11.2) ---------------------------------
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
    # AC-side P10 / P90 hourly band roll-ups (Phase 2): the AC analogue of the DC
    # p10/p90 hourly curves, capped at the per-slot AC ceiling. The central AC
    # point forecast is ac_watts, not necessarily the empirical median.
    ac_p10_hourly_wh: dict[str, float] = {}
    ac_p90_hourly_wh: dict[str, float] = {}
    # Per-slot AC P10 / P90 band watts (capped at the AC ceiling): the P10 side
    # feeds the daily-P10 headline strip (FOR-7, the strip needs each slot's
    # served band value, not just the hourly roll-up); the P90 side backs the
    # served 15-min wh_period_ac_p90 curve (SPEC §14.4). Empty unless bands are
    # issued below.
    ac_p10_watts: tuple[float, ...] = ()
    ac_p90_watts: tuple[float, ...] = ()
    band_by_slot = hk.band_by_slot
    if band_by_slot:
        p10_watts, p50_watts, p90_watts = quantiles.band_curve_from_corrected(
            total_watts, slot_starts, band_by_slot,
        )
        # Cap each band curve at the slot's physical AC ceiling BEFORE the hourly
        # Wh roll-up: the P90 factor (> 1) at a clamped clear midday would
        # otherwise report more site power than the inverters can deliver. p50 /
        # p10 rarely touch it (p50 <= corrected total <= ceiling by construction);
        # the band factors in quantiles.py stay pure — the cap lives here next to
        # the ceiling data.
        p10_watts = tuple(
            min(w, slot_ceilings[i]) for i, w in enumerate(p10_watts)
        )
        p50_watts = tuple(
            min(w, slot_ceilings[i]) for i, w in enumerate(p50_watts)
        )
        p90_watts = tuple(
            min(w, slot_ceilings[i]) for i, w in enumerate(p90_watts)
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
            if hkey not in corrected_energy.hourly_wh:
                continue
            p10_hourly_wh[hkey] = p10_hourly_wh.get(hkey, 0.0) + p10_watts[i] * _SLOT_HOURS
            p50_hourly_wh[hkey] = p50_hourly_wh.get(hkey, 0.0) + p50_watts[i] * _SLOT_HOURS
            p90_hourly_wh[hkey] = p90_hourly_wh.get(hkey, 0.0) + p90_watts[i] * _SLOT_HOURS

        # AC-side band curves (Phase 2): the SAME band factors applied to the
        # served AC curve, capped at the per-slot AC ceiling, rolled up to hourly
        # on the SAME hour keys as ``ac_hourly_wh``. Structurally identical to the
        # DC roll-up above, on the AC curve / ceiling. Only the empirical edges
        # are exposed; ac_watts remains the unchanged central point forecast.
        ac_p10_w, _empirical_ac_median, ac_p90_w = quantiles.band_curve_from_corrected(
            ac_watts, slot_starts, band_by_slot,
        )
        ac_p10_w = tuple(
            min(w, ac_slot_ceilings[i]) for i, w in enumerate(ac_p10_w)
        )
        ac_p90_w = tuple(
            min(w, ac_slot_ceilings[i]) for i, w in enumerate(ac_p90_w)
        )
        ac_p10_watts = ac_p10_w
        ac_p90_watts = ac_p90_w
        for i, start in enumerate(slot_starts):
            hkey = (
                start.astimezone(UTC)
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            if hkey not in ac_energy.hourly_wh:
                continue
            ac_p10_hourly_wh[hkey] = ac_p10_hourly_wh.get(hkey, 0.0) + ac_p10_w[i] * _SLOT_HOURS
            ac_p90_hourly_wh[hkey] = ac_p90_hourly_wh.get(hkey, 0.0) + ac_p90_w[i] * _SLOT_HOURS

    return ForecastResult(
        slot_starts=tuple(slot_starts),
        total_watts=tuple(total_watts),
        plane_results=plane_results,
        hourly_wh=corrected_energy.hourly_wh,
        daily_kwh=corrected_energy.daily_kwh,
        ac_watts=tuple(ac_watts),
        ac_hourly_wh=ac_energy.hourly_wh,
        ac_daily_kwh=ac_energy.daily_kwh,
        ac_corrected_unclamped_watts=tuple(ac_corrected_unclamped_watts),
        ac_p10_hourly_wh=ac_p10_hourly_wh,
        ac_p90_hourly_wh=ac_p90_hourly_wh,
        raw_total_watts=tuple(raw_total_watts),
        raw_hourly_wh=raw_energy.hourly_wh,
        raw_daily_kwh=raw_energy.daily_kwh,
        corrected_unclamped_watts=tuple(corrected_unclamped_watts),
        slot_ceilings=tuple(slot_ceilings),
        ac_slot_ceilings=tuple(ac_slot_ceilings),
        ac_p10_watts=ac_p10_watts,
        ac_p90_watts=ac_p90_watts,
        correction_source=hk.correction_source,
        p10_watts=p10_watts,
        p50_watts=p50_watts,
        p90_watts=p90_watts,
        p10_hourly_wh=p10_hourly_wh,
        p50_hourly_wh=p50_hourly_wh,
        p90_hourly_wh=p90_hourly_wh,
    )
