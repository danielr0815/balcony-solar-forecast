"""Hay-Davies plane-of-array transposition (stdlib math only).

Owner: irradiance. Pure, HA-free. Implements the SPEC §4 physics musts:
  - Hay-Davies anisotropic diffuse (beam + circumsolar + isotropic rest).
  - Rb capped <= RB_CAP; circumsolar forced to 0 below LOW_SUN_CUTOFF_DEG.
  - Ground-reflected term albedo * GHI * (1 - cos(tilt)) / 2.
The isotropic-diffuse sky-view scaling and the horizon transmittance are
applied by the caller (engine) using horizon.py — this function returns the
raw geometric components so they can be scaled/masked per plane.

Model (Hay & Davies 1980):

  Rb   = cos(theta) / cos(zenith)                 (beam geometric ratio, capped)
  Ai   = DNI / E0n                                (anisotropy index, clamped 0..1)
  beam        = DNI * cos(theta)                  (direct on tilted plane)
  circumsolar = DHI * Ai * Rb                     (forward-scattered diffuse)
  isotropic   = DHI * (1 - Ai) * (1 + cos(tilt))/2
  ground      = albedo * GHI * (1 - cos(tilt))/2

``E0n`` is the extraterrestrial NORMAL irradiance. It varies +/-3.3 % over the
year as the Earth-Sun distance changes (perihelion in early January), so the
mean solar constant systematically under-weights the circumsolar term in
winter and over-weights it in summer. When a day-of-year ``doy`` is supplied
``E0n`` is the mean solar constant scaled by the eccentricity factor
``1 + 0.033*cos(2*pi*doy/365)`` (Spencer 1971 simple form, Duffie & Beckman
"Solar Engineering of Thermal Processes" eq. 1.4.1a); when ``doy`` is None the
mean solar constant is used (backward-compatible for pure callers). ``Ai`` is a
dimensionless weight clamped to [0, 1].
"""

from __future__ import annotations

import math

from ..const import IAM_B0, LOW_SUN_CUTOFF_DEG, RB_CAP

__all__ = ["ashrae_iam", "hay_davies_poa"]

# Mean extraterrestrial normal irradiance (solar constant), W/m^2.
_SOLAR_CONSTANT = 1361.0


def ashrae_iam(cos_theta: float, b0: float = IAM_B0) -> float:
    """ASHRAE incidence-angle modifier for the DIRECT (beam) irradiance.

    Glass reflection at the module front rises steeply with the angle of
    incidence; the single-parameter ASHRAE model captures it as

        f = 1 - b0 * (1 / cos(theta) - 1)

    clamped to [0, 1] (0 at grazing incidence, 1 at normal incidence). The
    ENGINE multiplies beam + circumsolar by this factor — deliberately NOT
    :func:`hay_davies_poa` itself, which stays a pure sky-model transposition
    comparable against the pvlib golden vectors (pvlib likewise applies its IAM
    after the transposition step). Without the modifier the shademap's
    beam-referenced T absorbs the optics deficit as AOI-shaped phantom shading
    on the steep facade planes.

    ``cos_theta <= 0`` (sun behind the plane, no beam anyway) returns 0.0.
    """
    if cos_theta <= 0.0:
        return 0.0
    f = 1.0 - b0 * (1.0 / cos_theta - 1.0)
    if f < 0.0:
        return 0.0
    return f if f < 1.0 else 1.0


def _cos_incidence(
    sun_az: float,
    sun_el: float,
    plane_az: float,
    plane_tilt: float,
) -> float:
    """Cosine of the angle of incidence between the sun and a tilted plane.

    All azimuths INTERNAL (0=N clockwise); elevation and tilt in degrees.
    Standard AOI formula (Duffie & Beckman) rewritten for the surface tilt
    ``beta`` from horizontal and the surface azimuth ``gamma``:

        cos(theta) = cos(el)*sin(beta)*cos(sun_az - plane_az)
                     + sin(el)*cos(beta)

    Azimuth differences are convention-agnostic (only the delta enters), so
    the internal 0=N frame is used directly. May return values < 0 when the
    sun is behind the plane; callers clamp.
    """
    el = math.radians(sun_el)
    beta = math.radians(plane_tilt)
    delta_az = math.radians(sun_az - plane_az)
    return (
        math.cos(el) * math.sin(beta) * math.cos(delta_az)
        + math.sin(el) * math.cos(beta)
    )


def hay_davies_poa(
    ghi: float,
    dni: float,
    dhi: float,
    sun_az: float,
    sun_el: float,
    plane_az: float,
    plane_tilt: float,
    albedo: float,
    doy: int | None = None,
) -> dict[str, float]:
    """Hay-Davies POA irradiance components for one plane and one slot.

    All azimuths use the INTERNAL convention (0=N clockwise); tilt is degrees
    from horizontal. Below LOW_SUN_CUTOFF_DEG elevation the circumsolar term
    is 0 and Rb is capped at RB_CAP (low-sun explosion guard).

    The anisotropy index ``Ai = DNI / E0n`` uses the extraterrestrial NORMAL
    irradiance ``E0n``. When ``doy`` is given, ``E0n`` carries the Earth-Sun
    eccentricity factor ``1 + 0.033*cos(2*pi*doy/365)`` (Spencer 1971 simple
    form, Duffie & Beckman eq. 1.4.1a; +/-3.3 % over the year, perihelion early
    January) so the circumsolar weight is correct across the seasons; ``doy``
    None falls back to the mean solar constant (backward-compatible).

    Args:
        ghi: global horizontal irradiance, W/m^2 (interval mean).
        dni: direct normal irradiance, W/m^2.
        dhi: diffuse horizontal irradiance, W/m^2.
        sun_az: solar azimuth, deg (0=N clockwise).
        sun_el: solar elevation, deg above horizon.
        plane_az: plane azimuth, deg (0=N clockwise).
        plane_tilt: plane tilt, deg from horizontal.
        albedo: ground albedo (0.2 default, 0.5 with snow), caller-supplied.
        doy: day-of-year (1..366) for the Earth-Sun eccentricity correction of
            the anisotropy index; None uses the mean solar constant.

    Returns:
        Dict with keys ``beam``, ``circumsolar``, ``isotropic``, ``ground``
        (each W/m^2) plus ``cos_theta`` (the clamped cosine of the angle of
        incidence, for the engine's ASHRAE IAM — see :func:`ashrae_iam`).
        The plane total before horizon/SVF handling is the component sum;
        the engine applies the IAM to ``beam``+``circumsolar``, scales
        ``isotropic`` by the sky-view factor and masks ``beam``+``circumsolar``
        by the horizon transmittance.
    """
    # --- inputs are physical: no negative irradiance ---
    ghi = max(ghi, 0.0)
    dni = max(dni, 0.0)
    dhi = max(dhi, 0.0)
    albedo = max(albedo, 0.0)

    tilt_factor_sky = (1.0 + math.cos(math.radians(plane_tilt))) / 2.0
    tilt_factor_gnd = (1.0 - math.cos(math.radians(plane_tilt))) / 2.0

    # Ground-reflected diffuse: independent of sun geometry, always present.
    ground = albedo * ghi * tilt_factor_gnd

    # --- diffuse split needs the anisotropy index ---
    # Ai weights how much of DHI behaves like the beam (forward-scattered),
    # Ai = DNI / E0n. E0n is the mean solar constant scaled by the day-of-year
    # eccentricity factor when a doy is supplied (Spencer/Duffie-Beckman simple
    # form), else the bare mean solar constant (backward-compatible).
    if doy is not None:
        dni_extra = _SOLAR_CONSTANT * (
            1.0 + 0.033 * math.cos(2.0 * math.pi * doy / 365.0)
        )
    else:
        dni_extra = _SOLAR_CONSTANT
    ai = dni / dni_extra
    if ai < 0.0:
        ai = 0.0
    elif ai > 1.0:
        ai = 1.0

    # Below the horizon the sun contributes no beam or circumsolar; only the
    # full isotropic diffuse and the ground term remain.
    if sun_el <= 0.0:
        return {
            "beam": 0.0,
            "circumsolar": 0.0,
            "isotropic": dhi * tilt_factor_sky,
            "ground": ground,
            "cos_theta": 0.0,
        }

    cos_theta = _cos_incidence(sun_az, sun_el, plane_az, plane_tilt)
    cos_theta = max(cos_theta, 0.0)  # sun behind the plane -> no direct

    cos_zenith = math.sin(math.radians(sun_el))  # = cos(zenith), > 0 here
    # Geometric beam ratio, capped to guard the low-sun 1/cos(zenith) blow-up.
    rb = cos_theta / cos_zenith if cos_zenith > 0.0 else 0.0
    if rb > RB_CAP:
        rb = RB_CAP

    beam = dni * cos_theta

    # Below the low-sun cutoff the anisotropic (circumsolar) geometry is
    # unreliable (SPEC §4 must). We collapse the distrusted circumsolar share
    # into the isotropic dome by forcing ai = 0 BEFORE splitting the diffuse,
    # so the full DHK*tilt_factor_sky is retained (energy-conserving) instead
    # of dropping dhi*ai*tilt_factor of real diffuse. Zeroing only the
    # circumsolar product would leave the isotropic term at (1 - ai), losing
    # that share entirely — a one-sided downward dawn/dusk bias.
    if sun_el < LOW_SUN_CUTOFF_DEG:
        ai = 0.0

    circumsolar = dhi * ai * rb

    # Isotropic remainder scaled by the raw sky-view geometry; the engine
    # further multiplies by the per-plane horizon sky-view factor.
    isotropic = dhi * (1.0 - ai) * tilt_factor_sky

    return {
        "beam": beam,
        "circumsolar": circumsolar,
        "isotropic": isotropic,
        "ground": ground,
        "cos_theta": cos_theta,
    }
