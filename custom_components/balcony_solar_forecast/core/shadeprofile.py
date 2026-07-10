"""Sun-path vs. learned-shade profile for one plane on one date (stdlib only).

Owner: shadeprofile. Pure, HA-free. Builds the data behind the "shade profile"
diagram (SPEC §15): for a chosen module/plane and a chosen local
date it walks the sun across the sky and reports, at each step, the sun's
azimuth + elevation and the *effective* beam transmittance the engine would
apply there — i.e. the currently-known shading (static config horizon blended
with the learned shademap). It also emits, on a fixed azimuth grid over the
day's daylight span, two horizon lines: the static config horizon and the
learned shade horizon (the elevation below which the effective transmittance
stays under a threshold).

The effective transmittance replicates engine.py's gate EXACTLY (engine.py
`_plane_poa_split`): the static prior fed to the shademap is the plane's
horizon transmittance only when the sun sits at/below the interpolated horizon
line, else 1.0; the learned/blended tau then REPLACES it via
:func:`shademap.effective_tau`. So a point plotted here is the same beam
attenuation the forecast uses at that sun position — the coordinator supplies
the learned shademap only when the slow learner is active (else an empty map),
so the diagram tracks what the served forecast actually applies.

    compute_shade_profile(*, plane, shademap, channel, latitude, longitude,
                          day, tz, step_minutes=..., az_step_deg=...,
                          tau_threshold=..., el_scan_deg=..., pool=None) -> dict

Returns a plain, JSON-serialisable dict (parallel arrays + scalar summary) the
HA sensor drops straight into its attributes for an ApexCharts card to plot.
Never raises on a degenerate input (empty horizon, unknown channel, polar
night): it returns empty arrays and a zeroed summary instead.

READ-TIME pooling (SPEC §5): shade learning is stored per plane; grouped planes
are pooled only at read time. When a ``pool`` of channels wider than the plane's
own ``channel`` is supplied, the MAIN ``transmittance`` curve (and the shade
horizon) is the n-weighted POOLED tau the forecast actually applies, and a
second parallel ``transmittance_individual`` array carries the plane's OWN
channel alone so the operator can compare the two and decide groupings.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, tzinfo
from datetime import date as _date

from ..const import (
    ATTR_SP_AXIS_AZ_MAX,
    ATTR_SP_AXIS_AZ_MIN,
    ATTR_SP_AZIMUTH,
    ATTR_SP_HORIZON_AZIMUTH,
    ATTR_SP_SHADE_HORIZON,
    ATTR_SP_STATIC_HORIZON,
    ATTR_SP_SUN_ELEVATION,
    ATTR_SP_TIME,
    ATTR_SP_TRANSMITTANCE,
    ATTR_SP_TRANSMITTANCE_INDIVIDUAL,
    SHADE_PROFILE_AZ_STEP_DEG,
    SHADE_PROFILE_EL_SCAN_DEG,
    SHADE_PROFILE_STEP_MINUTES,
    SHADE_PROFILE_TAU_THRESHOLD,
)
from . import horizon as horizon_mod
from . import shademap as shademap_mod
from . import solpos
from .types import PlaneConfig, ShademapState

__all__ = [
    "axis_azimuth_domain",
    "compute_shade_profile",
    "default_module",
    "effective_tau_at",
    "shade_horizon_at",
]

# Elevation ceiling for the shade-horizon scan; the sun never exceeds it in the
# target latitudes and the config/learned horizon tops out at a wall (90 deg).
_EL_SCAN_MAX_DEG = 90.0


def default_module(planes) -> str:
    """Name of the plane the diagram should open on: the balcony's front.

    The "front" is taken as the azimuth the most planes share — a balcony's main
    face usually carries the most modules (the operator's four 115° ESE modules
    M2/M3/M6/M7 vs. two north + two south), so this opens on a productive front
    module rather than a corner/north one. The FIRST plane in that orientation
    wins (``max`` keeps the earliest on a tie), so the reference site defaults to
    ``M2``. Returns "" for an empty plane list.
    """
    planes = tuple(planes)
    if not planes:
        return ""
    counts: dict[int, int] = {}
    for p in planes:
        az = round(p.azimuth_deg)
        counts[az] = counts.get(az, 0) + 1
    return max(planes, key=lambda p: counts[round(p.azimuth_deg)]).name


def effective_tau_at(
    plane: PlaneConfig,
    state: ShademapState,
    *,
    channel: str,
    sun_az: float,
    sun_el: float,
    doy: int,
    pool: tuple[str, ...] | None = None,
) -> float:
    """Effective beam transmittance at one sun position (engine-exact gate).

    Mirrors ``engine._plane_poa_split``: the static prior is the plane's horizon
    transmittance only when the sun is at/below the interpolated horizon line
    (else 1.0), and the learned shademap then blends over it via
    :func:`shademap.effective_tau`. The result equals what the forecast
    multiplies the beam+circumsolar by at this (azimuth, elevation, doy) —
    PROVIDED the caller passes the same shademap the engine uses. The engine
    only blends the learned map when the slow learner is active, so the
    coordinator passes an empty :class:`ShademapState` when it is off /
    drift-disabled / collapse-frozen (see ``coordinator.build_shade_profile``);
    then the result is the static-only shading the forecast applies.

    READ-TIME pooling (SPEC §5): when ``pool`` names channels beyond the plane's
    own ``channel``, the learned tau is the n-weighted POOL over those channels
    (:func:`shademap.effective_tau_pooled`) — exactly what the served forecast
    applies for a grouped plane. ``pool`` None or equal to ``(channel,)`` reads
    the single channel, bit-identical to the pre-pooling behaviour.
    """
    horizon_elev = horizon_mod.interp_elevation(plane, sun_az)
    if sun_el <= horizon_elev:
        static_prior = horizon_mod.transmittance_at(plane, sun_az, doy)
    else:
        static_prior = 1.0
    if pool is not None and tuple(pool) != (channel,):
        return shademap_mod.effective_tau_pooled(
            state,
            channels=tuple(pool),
            sun_az=sun_az,
            sun_el=sun_el,
            doy=doy,
            static_prior=static_prior,
        )
    return shademap_mod.effective_tau(
        state,
        channel=channel,
        sun_az=sun_az,
        sun_el=sun_el,
        doy=doy,
        static_prior=static_prior,
    )


def shade_horizon_at(
    plane: PlaneConfig,
    state: ShademapState,
    *,
    channel: str,
    sun_az: float,
    doy: int,
    tau_threshold: float,
    el_scan_deg: float,
    pool: tuple[str, ...] | None = None,
) -> float:
    """Learned shade-horizon elevation (deg) at one azimuth.

    Scans elevation from 0 upward in ``el_scan_deg`` steps and returns the
    HIGHEST elevation whose effective transmittance (see :func:`effective_tau_at`)
    stays below ``tau_threshold`` — i.e. the top of the shaded band at this
    azimuth. 0.0 when nothing is shaded (a clear column); ~90 for a full wall.
    Robust to a non-monotone learned field (it takes the top-most shaded slice,
    not the first crossover).

    The elevation-independent lookups (horizon line + static transmittance) are
    hoisted out of the elevation loop so the whole azimuth grid stays cheap. The
    effective tau uses the READ-TIME POOL (SPEC §5) when ``pool`` is wider than
    ``(channel,)``, so the drawn shade horizon matches the pooled sun-path curve.
    """
    horizon_elev = horizon_mod.interp_elevation(plane, sun_az)
    tau_below = horizon_mod.transmittance_at(plane, sun_az, doy)
    step = el_scan_deg if el_scan_deg > 0.0 else SHADE_PROFILE_EL_SCAN_DEG
    pooled = pool is not None and tuple(pool) != (channel,)
    channels = tuple(pool) if pooled else ()
    shade_top = 0.0
    el = 0.0
    while el <= _EL_SCAN_MAX_DEG + 1e-9:
        static_prior = tau_below if el <= horizon_elev else 1.0
        if pooled:
            tau = shademap_mod.effective_tau_pooled(
                state,
                channels=channels,
                sun_az=sun_az,
                sun_el=el,
                doy=doy,
                static_prior=static_prior,
            )
        else:
            tau = shademap_mod.effective_tau(
                state,
                channel=channel,
                sun_az=sun_az,
                sun_el=el,
                doy=doy,
                static_prior=static_prior,
            )
        if tau < tau_threshold:
            shade_top = el
        el += step
    return shade_top


def _local_midnight(day: _date, tz: tzinfo) -> datetime:
    """Local midnight of ``day`` in ``tz`` as a tz-aware datetime."""
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def axis_azimuth_domain(
    *,
    latitude: float,
    longitude: float,
    year: int,
    tz: tzinfo,
    step_minutes: int = 10,
) -> tuple[float, float]:
    """Widest daylight sun-azimuth span of the whole ``year`` at the site.

    Runs the SAME daylight sweep as :func:`compute_shade_profile` (elevation > 0,
    NOAA :func:`solpos.sun_position`, local-midnight anchored) for BOTH solstices
    of ``year`` — ``date(year, 6, 21)`` (northern summer) and
    ``date(year, 12, 21)`` (southern summer) — and returns the ``(min, max)`` of
    every daylight azimuth across both days, rounded to 2 decimals.

    This is the year's widest daylight azimuth span at the site and is
    hemisphere-agnostic: June covers the northern-summer extremes (sunrise NE /
    sunset NW), December the southern-summer extremes, so whichever hemisphere the
    site is in, one of the two days carries the widest span. The card uses it to
    fix its x-axis so the sun path stays comparable from date to date instead of
    rescaling with the season.

    A coarse ``step_minutes`` (default 10) is enough: this bounds the axis, not
    the plotted samples, and the caller defensively unions the returned span with
    the (finer-sampled) per-date data span. Returns the full circle
    ``(0.0, 360.0)`` when NEITHER solstice yields a daylight sample (a degenerate
    polar case), so the axis is never empty. Never raises.
    """
    step = max(1, int(step_minutes))
    max_minute = 24 * 60
    az_values: list[float] = []
    for day in (_date(year, 6, 21), _date(year, 12, 21)):
        midnight = _local_midnight(day, tz)
        minute = 0
        while minute <= max_minute:
            local_dt = midnight + timedelta(minutes=minute)
            minute += step
            utc_dt = local_dt.astimezone(UTC)
            az, el = solpos.sun_position(utc_dt, latitude, longitude)
            if el <= 0.0:
                continue
            az_values.append(az)
    if not az_values:
        return (0.0, 360.0)
    return (round(min(az_values), 2), round(max(az_values), 2))


def _empty_profile(
    channel: str,
    day: _date,
    tau_threshold: float,
    axis_az_min: float,
    axis_az_max: float,
) -> dict:
    """The zeroed result for a day with no daylight (polar night / degenerate).

    Still carries the year-stable axis bounds (``axis_az_min``/``axis_az_max``):
    they are pure site geometry (lat/lon/year), independent of whether THIS day
    has daylight, so the card can fix its x-axis even on an empty (polar-night)
    profile.
    """
    return {
        "module": channel,
        "date": day.isoformat(),
        "doy": day.timetuple().tm_yday,
        "tau_threshold": round(tau_threshold, 3),
        "sample_count": 0,
        "has_learned_data": False,
        "learned_bins": 0,
        "shaded_fraction": 0.0,
        "mean_transmittance": None,
        "max_elevation": None,
        "sunrise": None,
        "sunset": None,
        ATTR_SP_TIME: [],
        ATTR_SP_AZIMUTH: [],
        ATTR_SP_SUN_ELEVATION: [],
        ATTR_SP_TRANSMITTANCE: [],
        ATTR_SP_TRANSMITTANCE_INDIVIDUAL: [],
        ATTR_SP_HORIZON_AZIMUTH: [],
        ATTR_SP_STATIC_HORIZON: [],
        ATTR_SP_SHADE_HORIZON: [],
        ATTR_SP_AXIS_AZ_MIN: axis_az_min,
        ATTR_SP_AXIS_AZ_MAX: axis_az_max,
    }


def compute_shade_profile(
    *,
    plane: PlaneConfig,
    shademap: ShademapState,
    channel: str,
    latitude: float,
    longitude: float,
    day: _date,
    tz: tzinfo,
    step_minutes: int = SHADE_PROFILE_STEP_MINUTES,
    az_step_deg: float = SHADE_PROFILE_AZ_STEP_DEG,
    tau_threshold: float = SHADE_PROFILE_TAU_THRESHOLD,
    el_scan_deg: float = SHADE_PROFILE_EL_SCAN_DEG,
    pool: tuple[str, ...] | None = None,
) -> dict:
    """Build the sun-path + learned-shade profile for ``plane`` on ``day``.

    Walks the local ``day`` at ``step_minutes`` cadence, keeps the daylight
    samples (sun elevation > 0), and for each records azimuth, elevation, the
    effective beam transmittance (:func:`effective_tau_at`) and the local time.
    On a fixed azimuth grid (``az_step_deg``) over the daylight azimuth span it
    also records the static config horizon and the learned shade horizon
    (:func:`shade_horizon_at`).

    ``day`` is interpreted in ``tz`` (the operator's local calendar day, matching
    how "today" is bucketed elsewhere); the day-of-year fed to the seasonal
    foliage ramp and the shademap half-year split is that local date's doy.

    READ-TIME pooling (SPEC §5): when ``pool`` names channels wider than the
    plane's own ``channel``, the MAIN ``transmittance`` array + the shade horizon
    are the n-weighted POOL (what the forecast applies), and a parallel
    ``transmittance_individual`` array carries the plane's OWN channel alone for
    the group-vs-single comparison. ``pool`` None or ``(channel,)`` leaves
    ``transmittance_individual`` an empty list (shape-stable) and the main curve
    single-channel, bit-identical to the pre-pooling behaviour.

    Returns a JSON-serialisable dict of parallel arrays + a scalar summary
    (see :func:`_empty_profile` for the key set). Never raises.
    """
    doy = day.timetuple().tm_yday
    step = max(1, int(step_minutes))
    # Whether a distinct pooled (group) view exists over the plane's own channel.
    pooled = pool is not None and tuple(pool) != (channel,)
    read_pool = tuple(pool) if pooled else (channel,)

    midnight = _local_midnight(day, tz)

    # Year-stable x-axis bounds: the widest daylight azimuth span of the whole
    # year at the site (both solstices). Computed once per call (two coarse
    # sweeps) BEFORE the early-return so the empty-profile branch carries them
    # too — they are pure site geometry, independent of this day's daylight.
    axis_az_min, axis_az_max = axis_azimuth_domain(
        latitude=latitude, longitude=longitude, year=day.year, tz=tz
    )

    times: list[str] = []
    azimuths: list[float] = []
    elevations: list[float] = []
    taus: list[float] = []
    # The plane's OWN-channel curve, only when a wider pool is in play (else it
    # stays [] so the attribute is shape-stable — the group view IS the single).
    taus_individual: list[float] = []

    # Sample the whole local day (inclusive of the final midnight boundary so a
    # late sunset slot is not clipped); keep only sun-above-horizon samples.
    minute = 0
    max_minute = 24 * 60
    while minute <= max_minute:
        local_dt = midnight + timedelta(minutes=minute)
        minute += step
        utc_dt = local_dt.astimezone(UTC)
        az, el = solpos.sun_position(utc_dt, latitude, longitude)
        if el <= 0.0:
            continue
        # MAIN curve = the pooled tau the forecast actually applies.
        tau = effective_tau_at(
            plane, shademap, channel=channel, sun_az=az, sun_el=el, doy=doy,
            pool=read_pool,
        )
        times.append(local_dt.strftime("%H:%M"))
        azimuths.append(round(az, 2))
        elevations.append(round(el, 2))
        taus.append(round(tau, 3))
        if pooled:
            # Comparison curve = this plane's own channel only (single-channel).
            tau_own = effective_tau_at(
                plane, shademap, channel=channel, sun_az=az, sun_el=el, doy=doy
            )
            taus_individual.append(round(tau_own, 3))

    if not azimuths:
        return _empty_profile(
            channel, day, tau_threshold, axis_az_min, axis_az_max
        )

    # Horizon lines on a fixed azimuth grid across the daylight azimuth span.
    # At the target mid-latitudes (poleward of the tropics, DE/AT/CH) the daytime
    # azimuth increases monotonically, so min == sunrise az and max == sunset az.
    # (A tropical observer near a solstice, where the sun crosses due north, would
    # split the span across 0/360 and draw a few extra horizon points the sun
    # never visits — cosmetic only; the sun-path arrays stay correct.)
    az_min = min(azimuths)
    az_max = max(azimuths)
    grid_step = az_step_deg if az_step_deg > 0.0 else SHADE_PROFILE_AZ_STEP_DEG
    horizon_az: list[float] = []
    static_horizon: list[float] = []
    shade_horizon: list[float] = []
    az = math.floor(az_min)
    stop = math.ceil(az_max)
    while az <= stop + 1e-9:
        horizon_az.append(round(az, 2))
        static_horizon.append(round(horizon_mod.interp_elevation(plane, az), 2))
        shade_horizon.append(
            round(
                shade_horizon_at(
                    plane,
                    shademap,
                    channel=channel,
                    sun_az=az,
                    doy=doy,
                    tau_threshold=tau_threshold,
                    el_scan_deg=el_scan_deg,
                    pool=read_pool,
                ),
                2,
            )
        )
        az += grid_step

    n = len(taus)
    shaded = sum(1 for t in taus if t < tau_threshold)
    mean_tau = sum(taus) / n
    # Count only the bins that CAN influence this date's curve: the shademap keys
    # by half-year (before/after summer solstice), and every plotted sample is
    # looked up in the visualised date's half-year, so bins in the other half
    # never touch the shown transmittance. Reporting the channel-wide bin count
    # would flag "learned data present" while the whole curve is the static prior.
    # Count bins that CAN influence the MAIN (pooled) curve for this half-year,
    # summed over every read-pool channel — for an ungrouped plane that is just
    # its own channel, unchanged from before.
    half_suffix = f":{shademap_mod.half_year_index(doy)}"
    date_bins = 0
    for ch in read_pool:
        bins = shademap.channels.get(ch) or {}
        date_bins += sum(1 for k in bins if k.endswith(half_suffix))

    return {
        "module": channel,
        "date": day.isoformat(),
        "doy": doy,
        "tau_threshold": round(tau_threshold, 3),
        "sample_count": n,
        "has_learned_data": date_bins > 0,
        "learned_bins": date_bins,
        "shaded_fraction": round(shaded / n, 4),
        "mean_transmittance": round(mean_tau, 4),
        "max_elevation": max(elevations),
        "sunrise": {"time": times[0], "azimuth": azimuths[0]},
        "sunset": {"time": times[-1], "azimuth": azimuths[-1]},
        ATTR_SP_TIME: times,
        ATTR_SP_AZIMUTH: azimuths,
        ATTR_SP_SUN_ELEVATION: elevations,
        ATTR_SP_TRANSMITTANCE: taus,
        ATTR_SP_TRANSMITTANCE_INDIVIDUAL: taus_individual,
        ATTR_SP_HORIZON_AZIMUTH: horizon_az,
        ATTR_SP_STATIC_HORIZON: static_horizon,
        ATTR_SP_SHADE_HORIZON: shade_horizon,
        ATTR_SP_AXIS_AZ_MIN: axis_az_min,
        ATTR_SP_AXIS_AZ_MAX: axis_az_max,
    }
