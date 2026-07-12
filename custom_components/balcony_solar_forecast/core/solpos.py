"""Solar position (NOAA closed-form, stdlib ``math`` only).

Owner: solpos. Pure, HA-free. Accuracy target < 0.3 deg (SPEC §4/§13,
self-test: Landshut/operator site June noon 64.9 deg, Dec 18.0 deg).

Algorithm: the NOAA Solar Calculator formulation of Meeus' *Astronomical
Algorithms* (simplified low-precision solar coordinates). Given a tz-aware
UTC datetime it computes the geocentric apparent sun position, applies an
atmospheric-refraction correction, and returns azimuth in the INTERNAL
convention (0 = North, clockwise) plus true elevation above the horizon.

All intermediate angles are handled in radians via ``math``; only the
public return values are degrees. No external dependencies (stdlib only).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

__all__ = ["sun_position", "hours_from_solar_noon"]


# Julian Day Number of the Unix epoch (1970-01-01T00:00:00Z). NOAA / Meeus
# reckon time in Julian centuries J2000; deriving JD straight from the POSIX
# timestamp sidesteps any calendar edge cases and is DST-agnostic (we require
# a tz-aware datetime and normalise to UTC first).
_JD_UNIX_EPOCH = 2440587.5
_SECONDS_PER_DAY = 86400.0
_JULIAN_CENTURY_DAYS = 36525.0
_JD_J2000 = 2451545.0


def _julian_century(dt_utc: datetime) -> float:
    """Julian centuries since J2000.0 for a tz-aware datetime."""
    # Normalise to UTC. A naive datetime is rejected: the whole core works in
    # tz-aware UTC and a silent local-time assumption is exactly the class of
    # bug the SPEC's DST test guards against.
    if dt_utc.tzinfo is None:
        raise ValueError("sun_position requires a tz-aware UTC datetime")
    ts = dt_utc.astimezone(UTC).timestamp()
    jd = _JD_UNIX_EPOCH + ts / _SECONDS_PER_DAY
    return (jd - _JD_J2000) / _JULIAN_CENTURY_DAYS


def sun_position(dt_utc: datetime, lat: float, lon: float) -> tuple[float, float]:
    """Return (azimuth_deg, elevation_deg) of the sun.

    Args:
        dt_utc: tz-aware UTC datetime (use the 15-min slot midpoint).
        lat: latitude, degrees north.
        lon: longitude, degrees east.

    Returns:
        (azimuth_deg, elevation_deg) where azimuth uses the INTERNAL
        convention 0 = North, clockwise (90 = East, 180 = South), and
        elevation is degrees above the horizon (negative below), corrected
        for atmospheric refraction.
    """
    t = _julian_century(dt_utc)

    # --- Geometric mean longitude & anomaly of the sun (deg) ---
    geom_mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    geom_mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    anom_rad = math.radians(geom_mean_anom)

    # --- Sun equation of centre (deg) ---
    eq_centre = (
        math.sin(anom_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2.0 * anom_rad) * (0.019993 - 0.000101 * t)
        + math.sin(3.0 * anom_rad) * 0.000289
    )
    true_long = geom_mean_long + eq_centre  # deg

    # --- Apparent longitude (nutation + aberration, deg) ---
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    app_long_rad = math.radians(app_long)

    # --- Mean obliquity of the ecliptic + correction (deg) ---
    seconds = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    mean_obliq = 23.0 + (26.0 + seconds / 60.0) / 60.0
    obliq_corr = mean_obliq + 0.00256 * math.cos(math.radians(omega))
    obliq_rad = math.radians(obliq_corr)

    # --- Apparent declination (rad) ---
    # (Right ascension is not needed: azimuth below is derived from the local
    # hour angle via the equation of time, not from RA/sidereal time.)
    declination = math.asin(math.sin(obliq_rad) * math.sin(app_long_rad))

    # --- Equation of time (minutes) ---
    var_y = math.tan(obliq_rad / 2.0) ** 2
    gml_rad = math.radians(geom_mean_long)
    eccent = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    eq_time = 4.0 * math.degrees(
        var_y * math.sin(2.0 * gml_rad)
        - 2.0 * eccent * math.sin(anom_rad)
        + 4.0 * eccent * var_y * math.sin(anom_rad) * math.cos(2.0 * gml_rad)
        - 0.5 * var_y * var_y * math.sin(4.0 * gml_rad)
        - 1.25 * eccent * eccent * math.sin(2.0 * anom_rad)
    )

    # --- True solar time (minutes) & hour angle (deg) ---
    # Minutes elapsed in the UTC day. lon is degrees East; +4 min per degree.
    utc = dt_utc.astimezone(UTC)
    minutes_of_day = (
        utc.hour * 60.0
        + utc.minute
        + utc.second / 60.0
        + utc.microsecond / 60_000_000.0
    )
    # NOAA: true_solar_time = minutes + eq_time + 4*lon_east (timezone = 0/UTC).
    true_solar_time = (minutes_of_day + eq_time + 4.0 * lon) % 1440.0
    hour_angle = true_solar_time / 4.0 - 180.0  # deg; 0 at local solar noon
    if hour_angle < -180.0:
        hour_angle += 360.0
    ha_rad = math.radians(hour_angle)

    lat_rad = math.radians(lat)

    # --- Solar zenith / elevation (deg) ---
    cos_zenith = (
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination) * math.cos(ha_rad)
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.degrees(math.acos(cos_zenith))
    elevation = 90.0 - zenith

    # --- Atmospheric refraction correction (deg), NOAA piecewise fit ---
    refraction = _refraction_correction(elevation)
    elevation_corrected = elevation + refraction

    # --- Azimuth (deg), 0 = North clockwise ---
    # NOAA closed form: derive from zenith, declination, latitude and the sign
    # of the hour angle. Guard the denominator at the poles / exact zenith.
    denom = math.sin(zenith * math.pi / 180.0) * math.cos(lat_rad)
    if abs(denom) < 1e-12:
        # Sun overhead or observer at a pole: azimuth ill-defined; return 180
        # (South) as a stable convention rather than dividing by ~0.
        azimuth = 180.0
    else:
        cos_az = (
            math.sin(lat_rad) * math.cos(math.radians(zenith))
            - math.sin(declination)
        ) / denom
        cos_az = max(-1.0, min(1.0, cos_az))
        az_from_south = math.degrees(math.acos(cos_az))
        if hour_angle > 0.0:
            # Afternoon: sun west of south -> azimuth in (180, 360).
            azimuth = (az_from_south + 180.0) % 360.0
        else:
            # Morning: sun east of south -> azimuth in (0, 180).
            azimuth = (540.0 - az_from_south) % 360.0

    return azimuth, elevation_corrected


def hours_from_solar_noon(dt_utc: datetime, lon: float) -> float:
    """Signed hours of APPARENT SOLAR time from local solar noon.

    Returns ``true_solar_time - 12h`` in hours: negative before solar noon
    (morning), 0 at solar noon, positive after (afternoon), wrapped to
    (-12, +12]. This is the sun's hour angle expressed in hours
    (hour_angle_deg / 15) and is the DST- and season-robust coordinate the
    day-ahead bias uses to bin the day into morning / midday / afternoon:
    it is anchored to the sun (equation of time + longitude), NOT the wall
    clock, so a bin never drifts by an hour across the DST changeover and a
    cell learned in summer applies at the same SOLAR position in winter.

    Derivation mirrors :func:`sun_position`'s true-solar-time step (NOAA):
    ``true_solar_time = minutes_of_day + eq_time + 4*lon`` (UTC, degrees East),
    ``hour_angle = true_solar_time/4 - 180`` (deg), hours = hour_angle / 15.

    Args:
        dt_utc: tz-aware UTC datetime (use the 15-min slot midpoint / start).
        lon: longitude, degrees east.

    Returns:
        Hours from solar noon in (-12, +12] (negative = morning).
    """
    if dt_utc.tzinfo is None:
        raise ValueError("hours_from_solar_noon requires a tz-aware UTC datetime")
    t = _julian_century(dt_utc)

    geom_mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    geom_mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    anom_rad = math.radians(geom_mean_anom)

    seconds = 21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))
    mean_obliq = 23.0 + (26.0 + seconds / 60.0) / 60.0
    omega = 125.04 - 1934.136 * t
    obliq_corr = mean_obliq + 0.00256 * math.cos(math.radians(omega))
    obliq_rad = math.radians(obliq_corr)

    var_y = math.tan(obliq_rad / 2.0) ** 2
    gml_rad = math.radians(geom_mean_long)
    eccent = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    eq_time = 4.0 * math.degrees(
        var_y * math.sin(2.0 * gml_rad)
        - 2.0 * eccent * math.sin(anom_rad)
        + 4.0 * eccent * var_y * math.sin(anom_rad) * math.cos(2.0 * gml_rad)
        - 0.5 * var_y * var_y * math.sin(4.0 * gml_rad)
        - 1.25 * eccent * eccent * math.sin(2.0 * anom_rad)
    )

    utc = dt_utc.astimezone(UTC)
    minutes_of_day = (
        utc.hour * 60.0
        + utc.minute
        + utc.second / 60.0
        + utc.microsecond / 60_000_000.0
    )
    true_solar_time = (minutes_of_day + eq_time + 4.0 * lon) % 1440.0
    hour_angle = true_solar_time / 4.0 - 180.0  # deg; 0 at local solar noon
    if hour_angle <= -180.0:
        hour_angle += 360.0
    return hour_angle / 15.0  # deg -> hours (15 deg per hour)


def _refraction_correction(elevation_deg: float) -> float:
    """Atmospheric refraction (deg) to add to true elevation.

    NOAA Solar Calculator piecewise approximation (Bennett-style). Refraction
    lifts the apparent sun; the correction is largest near the horizon and
    negligible high in the sky. Below ~-0.575 deg true elevation the fit is
    clamped to a small constant (the sun is effectively set; the exact value
    is immaterial to a PV forecast that gates on positive elevation anyway).
    """
    if elevation_deg > 85.0:
        return 0.0

    tan_el = math.tan(math.radians(elevation_deg))
    if elevation_deg > 5.0:
        corr = (
            58.1 / tan_el
            - 0.07 / (tan_el ** 3)
            + 0.000086 / (tan_el ** 5)
        )
    elif elevation_deg > -0.575:
        corr = 1735.0 + elevation_deg * (
            -518.2 + elevation_deg * (103.4 + elevation_deg * (-12.79 + elevation_deg * 0.711))
        )
    else:
        corr = -20.772 / tan_el

    return corr / 3600.0  # arc-seconds -> degrees
