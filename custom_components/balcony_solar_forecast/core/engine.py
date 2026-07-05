"""Forecast engine: orchestrates the pure physics pipeline (stdlib only).

Owner: engine. Pure, HA-free. Ties together solpos -> transpose -> horizon
-> electrical over every 15-min slot and every plane, then rolls the
AC-clamped site total up to hourly Wh and daily kWh (SPEC §4 steps 2-7).

Pipeline per 15-min slot (values are interval means; sun position at the
slot midpoint):

    sun_position(midpoint) --> per plane:
        hay_davies_poa()  (raw beam / circumsolar / isotropic / ground)
        horizon gate:  beam + circumsolar *= tau  when the sun sits below
                       the plane's interpolated horizon line
        diffuse gate:  isotropic *= sky_view_factor (static per plane)
        POA = gated beam + gated circumsolar + gated isotropic + ground
        dc_power(POA, wp, temp, efficiency)
    clamp_groups()  --> per-plane and site-total AC-clamped watts

Aggregation: the clamped site total is integrated to hourly Wh (keyed by
ISO-8601 UTC hour) and daily kWh (keyed by ISO date in the ``tz`` calendar,
UTC by default). Slots with missing (None) irradiance/temperature are
treated as zero-production and skipped safely.
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo

from ..const import (
    ALBEDO_DEFAULT,
    ALBEDO_SNOW,
    SLOT_MINUTES,
    SNOW_DEPTH_THRESHOLD_M,
)
from . import electrical, horizon, solpos, transpose
from .types import (
    ForecastResult,
    PlaneConfig,
    PlaneResult,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)

__all__ = ["compute_forecast"]

# One 15-min slot as a fraction of an hour, for the Wh integration of an
# interval-mean power value (SPEC: slot values are backward-averaged means).
_SLOT_HOURS = SLOT_MINUTES / 60.0


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


def _plane_poa(
    plane: PlaneConfig,
    svf: float,
    slot: WeatherSlot,
    sun_az: float,
    sun_el: float,
    albedo: float,
    doy: int,
) -> float:
    """Horizon-gated Hay-Davies POA (W/m^2) for one plane in one slot.

    Beam + circumsolar are multiplied by the plane's horizon transmittance
    when the sun sits at or below the interpolated horizon line; the isotropic
    diffuse is always scaled by the plane's static sky-view factor. The ground
    reflection term is unaffected by the horizon (it comes from below).
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
    # tau is irrelevant (full transmission).
    horizon_elev = horizon.interp_elevation(plane, sun_az)
    if sun_el <= horizon_elev:
        tau = horizon.transmittance_at(plane, sun_az, doy)
        beam *= tau
        circ *= tau

    # Diffuse sky-view gate: static per-plane reduction of the isotropic sky
    # dome (fixes E4 — diffuse was never reduced by obstructions).
    iso *= svf

    poa = beam + circ + iso + ground
    if poa < 0.0:
        return 0.0
    return poa


def compute_forecast(
    site: SiteConfig,
    weather: WeatherSeries,
    now: datetime,
    tz: tzinfo | None = None,
) -> ForecastResult:
    """Compute the pure-physics forecast for the whole weather window.

    For each 15-min slot: sun position at the slot midpoint, Hay-Davies POA
    per plane, horizon transmittance on beam+circumsolar and sky-view-factor
    on the isotropic diffuse, snow-aware ground albedo, Ross-derated DC
    power, then per-inverter-group AC clamp. Aggregates the clamped total to
    hourly Wh (keyed by ISO UTC hour) and daily kWh (keyed by ISO date).

    Args:
        site: the (already validated) site configuration.
        weather: ordered 15-min weather slots (tz-aware UTC).
        now: current tz-aware UTC time (for "today/tomorrow/d2" bucketing
            and any as-issued stamping by callers).
        tz: local calendar timezone for the daily kWh buckets. Defaults to
            UTC. (Kept an optional keyword so the frozen positional contract
            ``compute_forecast(site, weather, now)`` is preserved; the HA glue
            passes ``hass.config.time_zone`` here so "today/tomorrow/d2" match
            the operator's local midnight.)

    Returns:
        A ForecastResult aligned to ``weather``'s slot starts.
    """
    cal_tz = tz if tz is not None else timezone.utc

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
    total_watts: list[float] = []
    # Per-plane clamped power, aligned to slot_starts.
    plane_series: dict[str, list[float]] = {plane.name: [] for plane in planes}

    hourly_wh: dict[str, float] = {}
    daily_kwh: dict[str, float] = {}

    for slot in weather.slots:
        start = slot.start
        slot_starts.append(start)

        if not _slot_is_usable(slot):
            # Missing weather -> zero production for this slot, but keep the
            # slot present so downstream alignment / hourly bucketing is dense.
            total_watts.append(0.0)
            for plane in planes:
                plane_series[plane.name].append(0.0)
            continue

        midpoint = slot.midpoint
        sun_az, sun_el = solpos.sun_position(midpoint, lat, lon)
        doy = midpoint.timetuple().tm_yday
        albedo = _slot_albedo(slot)

        # Below the horizon there is no beam, but the tilted plane still
        # receives the isotropic diffuse and the ground term while the sky is
        # bright (civil twilight, winter fog). hay_davies_poa handles sun_el
        # <= 0 (it skips all beam/Rb math and returns only the isotropic +
        # ground contribution), so only short-circuit to zero when there is no
        # diffuse/global irradiance to transpose at all — never silently clip
        # real twilight diffuse (SPEC E4: diffuse must never be zeroed).
        if sun_el <= 0.0 and slot.dhi <= 0.0 and slot.ghi <= 0.0:
            total_watts.append(0.0)
            for plane in planes:
                plane_series[plane.name].append(0.0)
            continue

        unclamped: dict[str, float] = {}
        for plane in planes:
            poa = _plane_poa(
                plane,
                svf_by_plane[plane.name],
                slot,
                sun_az,
                sun_el,
                albedo,
                doy,
            )
            unclamped[plane.name] = electrical.dc_power(
                poa, plane.wp, slot.temp_c, plane.efficiency
            )

        clamped = electrical.clamp_groups(unclamped, groups)

        slot_total = 0.0
        for plane in planes:
            w = clamped.get(plane.name, 0.0)
            plane_series[plane.name].append(w)
            slot_total += w
        total_watts.append(slot_total)

        # --- Energy roll-ups (interval-mean power * slot hours) ---
        wh = slot_total * _SLOT_HOURS

        hour_start = start.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        hkey = hour_start.isoformat()
        hourly_wh[hkey] = hourly_wh.get(hkey, 0.0) + wh

        day_key = start.astimezone(cal_tz).date().isoformat()
        daily_kwh[day_key] = daily_kwh.get(day_key, 0.0) + wh / 1000.0

    plane_results = tuple(
        PlaneResult(name=plane.name, watts=tuple(plane_series[plane.name]))
        for plane in planes
    )

    return ForecastResult(
        slot_starts=tuple(slot_starts),
        total_watts=tuple(total_watts),
        plane_results=plane_results,
        hourly_wh=hourly_wh,
        daily_kwh=daily_kwh,
    )
