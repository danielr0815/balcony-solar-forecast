"""Shared plane physics for live forecasts, historical reconstruction and charts.

A RAW learning reference keeps the static-temperature conversion. Rendering a
changed beam transmittance instead runs the Ross conversion again at the new
POA. Keeping both operations here prevents bootstrap and serving from drifting.
The caller chooses the evaluation time; this module does not choose a timestep.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..const import BEAM_GAIN_DEFAULT
from . import electrical, horizon, transpose
from .types import PlaneConfig


def static_beam_tau(
    plane: PlaneConfig, sun_az: float, sun_el: float, doy: int,
    *, horizon_elevation: float | None = None,
) -> float:
    """Static beam prior: profile below the horizon, open sky above it."""
    edge = (
        horizon.interp_elevation(plane, sun_az)
        if horizon_elevation is None else horizon_elevation
    )
    if sun_el <= edge:
        return horizon.transmittance_at(plane, sun_az, doy, sun_el=sun_el)
    return 1.0


@dataclass(frozen=True, slots=True)
class PlanePoaSplit:
    """POA components split into beam-driven vs. diffuse-driven (W/m^2).

    ``beam_poa`` = gated beam + gated circumsolar (the direct share the
    shademap references); ``diffuse_poa`` = gated isotropic + ground (the shade
    floor). Their sum is the plane POA fed to the DC model.
    ``beam_poa_ungated`` is beam + circumsolar with tau := 1 (clear horizon) —
    the SLOW learner's beam reference (SPEC §9.1, FIX-3): the learned tau REPLACES
    the static tau, so the training reference must be the un-attenuated beam.
    """

    beam_poa: float
    diffuse_poa: float
    beam_poa_ungated: float


@dataclass(frozen=True, slots=True)
class PlanePoaComponents:
    """Tau-independent POA decomposition for one plane in one slot (W/m^2).

    The RAW and CORRECTED curves differ ONLY in which transmittance gates the
    beam+circumsolar (static horizon tau vs a learned tau), so everything that
    does NOT depend on tau is computed ONCE per plane/slot and shared between
    them (audit #9): the IAM-corrected ``beam`` / ``circ`` (pre-gate), the
    ``diffuse_poa`` floor (isotropic*SVF + ground, never touched by the beam
    gate), the ``beam_poa_ungated`` reference (tau := 1) and the plane's
    ``static_tau`` at this sun position (the shademap's ``static_prior``). The
    per-tau gate :func:`gate_split` then derives each curve's
    :class:`PlanePoaSplit` from this shared result.
    """

    beam: float              # beam after IAM, before the horizon gate
    circ: float              # circumsolar after IAM, before the horizon gate
    diffuse_poa: float       # isotropic*SVF + ground, clamped >=0 (gate-independent)
    beam_poa_ungated: float  # max(beam + circ, 0): the shademap's beam reference
    static_tau: float        # static horizon tau at this sun position (static_prior)


def poa_components(
    plane: PlaneConfig,
    svf: float,
    *,
    ghi: float,
    dni: float,
    dhi: float,
    sun_az: float,
    sun_el: float,
    albedo: float,
    doy: int,
    beam_gain: float = BEAM_GAIN_DEFAULT,
) -> PlanePoaComponents:
    """Tau-independent Hay-Davies POA decomposition (W/m^2) for one plane/slot.

    Runs the transposition, the ASHRAE IAM on beam+circumsolar, the horizon
    interpolation (yielding the STATIC transmittance at this sun position) and
    the SVF-scaled diffuse floor exactly ONCE. The RAW and CORRECTED splits are
    then a cheap gate-arithmetic step over this shared result
    (:func:`gate_split`) — the only thing that differs between the two curves is
    which tau attenuates the beam (SPEC §9.1 slow learner). The isotropic diffuse
    is always scaled by the plane's static sky-view factor and the ground
    reflection is never touched by the beam gate, so a fully occluding wall bin
    (tau=0) kills the beam but keeps the diffuse floor.

    ``beam_gain`` (forensik T6) multiplies the beam+circumsolar POA (the direct
    share only) BEFORE the ungated-reference capture and the tau gate — 1.0 is
    the identity default. The diffuse/ground floor is deliberately excluded.
    """
    comps = transpose.hay_davies_poa(
        ghi=ghi,
        dni=dni,
        dhi=dhi,
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

    # Incidence-angle modifier (ASHRAE, const IAM_B0): glass reflection cuts
    # the DIRECT share at high AOI — 5-15% on the steep facade planes. Applied
    # HERE (pvlib-style, after the pure transposition) so the golden vectors
    # stay pvlib-comparable, and BEFORE the ungated-reference capture below so
    # the shademap trains against the optics-corrected beam instead of
    # absorbing the deficit as AOI-shaped phantom shading (SPEC §4.4). A
    # transposition stand-in without the cos_theta key (analytic test fakes)
    # skips the modifier.
    cos_theta = comps.get("cos_theta")
    if cos_theta is not None:
        f_iam = transpose.ashrae_iam(cos_theta)
        beam *= f_iam
        circ *= f_iam

    # Site bifacial beam gain (forensik T6 / A1): lift the honestly under-modeled
    # DIRECT share (beam+circumsolar only) by the configured factor BEFORE the
    # ungated-reference capture and the tau gate, so it feeds BOTH the RAW and the
    # CORRECTED curve identically and both the SLOW-learner beam reference and the
    # day-ahead-bias cells (bounded by their configured limits) get the
    # honest physics instead of absorbing the deficit. Default 1.0 => no-op. The
    # isotropic-diffuse and ground-reflected shares are deliberately untouched.
    if beam_gain != 1.0:
        beam *= beam_gain
        circ *= beam_gain

    # Static horizon beam prior: only when the sun is actually behind the horizon
    # line for this azimuth does the static tau attenuate the direct components.
    # Above the line the static tau is irrelevant (full transmission, 1.0), but
    # a learned bin can still darken the beam (near-field trees / building edge
    # the static table missed), so the CORRECTED gate consults its hook there too
    # — with static_prior = 1.0 above the line, a shrinkage blend leans on the
    # learned tau exactly as intended.
    static_tau = static_beam_tau(plane, sun_az, sun_el, doy)

    # UNGATED beam+circumsolar (tau := 1): the shademap's beam reference. Capture
    # it BEFORE any tau multiply (linear in tau, so ungated == gated / tau).
    beam_poa_ungated = beam + circ
    if beam_poa_ungated < 0.0:
        beam_poa_ungated = 0.0

    # Diffuse sky-view gate: static per-plane reduction of the isotropic sky
    # dome (fixes E4 — diffuse was never reduced by obstructions). Never gated
    # by the beam transmittance, so it is identical for the raw and corrected
    # curves.
    iso *= svf
    diffuse_poa = iso + ground
    if diffuse_poa < 0.0:
        diffuse_poa = 0.0

    return PlanePoaComponents(
        beam=beam,
        circ=circ,
        diffuse_poa=diffuse_poa,
        beam_poa_ungated=beam_poa_ungated,
        static_tau=static_tau,
    )


def gate_split(comps: PlanePoaComponents, tau: float) -> PlanePoaSplit:
    """Gate the shared components with a beam transmittance ``tau`` -> POA split.

    ``tau`` gates beam+circumsolar (the static horizon tau for the RAW curve, the
    learned/blended tau for the CORRECTED curve); the diffuse floor and the
    ungated beam reference are carried straight through from ``comps``. The
    ``tau != 1.0`` guard skips the multiply when the beam is fully transmitted —
    bit-identical to multiplying (``x * 1.0 == x``), and byte-for-byte the same
    arithmetic the single-pass predecessor ran.
    """
    beam = comps.beam
    circ = comps.circ
    if tau != 1.0:
        beam *= tau
        circ *= tau

    beam_poa = beam + circ
    if beam_poa < 0.0:
        beam_poa = 0.0

    return PlanePoaSplit(
        beam_poa=beam_poa,
        diffuse_poa=comps.diffuse_poa,
        beam_poa_ungated=comps.beam_poa_ungated,
    )


def dc_split(
    split: PlanePoaSplit,
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
    total_dc = electrical.dc_power(
        total_poa, plane.wp, temp_c, plane.efficiency,
        ross_coeff=plane.ross_coeff,
    )
    if total_poa <= 0.0 or total_dc <= 0.0:
        return 0.0, 0.0
    beam_frac = split.beam_poa / total_poa
    beam_dc = total_dc * beam_frac
    diffuse_dc = total_dc - beam_dc
    return beam_dc, diffuse_dc
