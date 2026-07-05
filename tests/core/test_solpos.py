"""Tests for the NOAA closed-form solar position (core/solpos.py).

Pure pytest, no Home Assistant imports (SPEC §4). Anchor values are the
PVGIS-verified operator-site figures from SPEC §13:

  * Landshut / operator site (48.547853 N, 12.187272 E)
  * 2025-06-21 solar-noon elevation 64.9 +- 0.4 deg
  * 2025-12-21 solar-noon elevation 18.0 +- 0.4 deg
  * June sunrise azimuth ~56 deg at 03:30 UTC
  * azimuth convention 0 = North: solar noon azimuth ~180 deg

The suite also covers the SPEC's silent-error traps for this module:
azimuth-sign errors, the 0=N convention, low-sun behaviour, DST/naive-time
misuse, and monotone azimuth progression across the day.
"""

from __future__ import annotations

import importlib.util
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Import the core module directly from its file so the test stays strictly
# HA-free (SPEC §4). Loading it via the full package path
# ``custom_components.balcony_solar_forecast.core.solpos`` would execute the
# integration-root ``__init__.py``, which imports ``homeassistant`` and is
# unavailable to plain pytest. ``solpos.py`` depends only on stdlib
# math/datetime, so a file-based load is self-contained and robust.
_SOLPOS_PATH = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "balcony_solar_forecast"
    / "core"
    / "solpos.py"
)
_spec = importlib.util.spec_from_file_location("_bsf_core_solpos", _SOLPOS_PATH)
_solpos = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_solpos)

sun_position = _solpos.sun_position
_refraction_correction = _solpos._refraction_correction

# Operator reference site (SPEC §2 / const.DEFAULT_SITE).
LAT = 48.547853
LON = 12.187272

# A couple of extra ZoneInfo-based checks want a real tz; fall back gracefully
# if the platform lacks the tz database (Windows without tzdata installed).
try:
    from zoneinfo import ZoneInfo

    _BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover - platform without tz data
    _BERLIN = None


def _utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _scan_solar_noon(y, mo, d):
    """Brute-force the day's max elevation (1-min grid) -> (elev, az, dt)."""
    best = (-999.0, None, None)
    base = _utc(y, mo, d)
    for m in range(24 * 60):
        dt = base + timedelta(minutes=m)
        az, el = sun_position(dt, LAT, LON)
        if el > best[0]:
            best = (el, az, dt)
    return best


# ---------------------------------------------------------------------------
# Anchor accuracy (PVGIS-verified, SPEC §13)
# ---------------------------------------------------------------------------


def test_summer_solstice_noon_elevation():
    """2025-06-21 solar-noon elevation = 64.9 +- 0.4 deg."""
    elev, _az, _dt = _scan_solar_noon(2025, 6, 21)
    assert elev == pytest.approx(64.9, abs=0.4)


def test_winter_solstice_noon_elevation():
    """2025-12-21 solar-noon elevation = 18.0 +- 0.4 deg."""
    elev, _az, _dt = _scan_solar_noon(2025, 12, 21)
    assert elev == pytest.approx(18.0, abs=0.4)


def test_june_sunrise_azimuth():
    """June sunrise azimuth ~56 deg at 03:30 UTC (0=N convention)."""
    az, el = sun_position(_utc(2025, 6, 21, 3, 30), LAT, LON)
    # Sun is just above the horizon (low but positive after refraction).
    assert 0.0 < el < 6.0
    assert az == pytest.approx(56.0, abs=1.5)
    # Sanity: azimuth must be in the NE quadrant, never a sign-flipped value.
    assert 45.0 < az < 90.0


def test_solar_noon_azimuth_is_south():
    """0 = North convention: at solar noon the sun is due south (~180)."""
    for y, mo, d in [(2025, 6, 21), (2025, 12, 21), (2025, 3, 20)]:
        _elev, az, _dt = _scan_solar_noon(y, mo, d)
        assert az == pytest.approx(180.0, abs=0.5), f"{y}-{mo}-{d}: az={az}"


def test_equinox_noon_elevation():
    """Equinox solar-noon elevation ~ (90 - latitude), small decl offset."""
    elev, _az, _dt = _scan_solar_noon(2025, 3, 20)
    assert elev == pytest.approx(90.0 - LAT, abs=1.0)


# ---------------------------------------------------------------------------
# Azimuth convention & sign-error traps (SPEC Anhang A silent-error class)
# ---------------------------------------------------------------------------


def test_morning_azimuth_is_east_of_south():
    """Before noon the sun is east: azimuth in (0, 180)."""
    az, el = sun_position(_utc(2025, 6, 21, 6, 0), LAT, LON)
    assert el > 0
    assert 0.0 < az < 180.0


def test_afternoon_azimuth_is_west_of_south():
    """After noon the sun is west: azimuth in (180, 360)."""
    az, el = sun_position(_utc(2025, 6, 21, 16, 0), LAT, LON)
    assert el > 0
    assert 180.0 < az < 360.0


def test_azimuth_crosses_east_and_west_cardinals():
    """Azimuth passes through ~90 (E) mid-morning and ~270 (W) evening."""
    # Find the slot nearest az=90 and az=270 on the long solstice day.
    base = _utc(2025, 6, 21)
    east_err = 999.0
    west_err = 999.0
    for m in range(0, 24 * 60, 5):
        az, el = sun_position(base + timedelta(minutes=m), LAT, LON)
        if el <= 0:
            continue
        east_err = min(east_err, abs(az - 90.0))
        west_err = min(west_err, abs(az - 270.0))
    assert east_err < 2.0
    assert west_err < 2.0


def test_azimuth_monotone_increasing_over_daylight():
    """Azimuth increases monotonically N->E->S->W through the daylight arc."""
    base = _utc(2025, 6, 21)
    prev = None
    for m in range(0, 24 * 60, 10):
        az, el = sun_position(base + timedelta(minutes=m), LAT, LON)
        if el <= 0:
            continue
        if prev is not None:
            assert az > prev - 1e-6, f"azimuth went backwards at minute {m}"
        prev = az


def test_azimuth_always_in_range():
    """Azimuth is always within [0, 360) for arbitrary times."""
    base = _utc(2025, 9, 15)
    for m in range(0, 24 * 60, 7):
        az, _el = sun_position(base + timedelta(minutes=m), LAT, LON)
        assert 0.0 <= az < 360.0


# ---------------------------------------------------------------------------
# Low-sun / night behaviour (SPEC §4 low-sun trap)
# ---------------------------------------------------------------------------


def test_night_elevation_is_negative():
    """Deep night: the sun is well below the horizon."""
    _az, el = sun_position(_utc(2025, 12, 21, 0, 0), LAT, LON)
    assert el < -20.0


def test_low_sun_returns_finite_values():
    """Near-horizon and sub-horizon times never blow up (finite outputs)."""
    base = _utc(2025, 6, 21)
    for m in range(0, 24 * 60, 3):
        az, el = sun_position(base + timedelta(minutes=m), LAT, LON)
        assert math.isfinite(az)
        assert math.isfinite(el)
        assert -90.0 <= el <= 90.0


def test_refraction_lifts_horizon_sun():
    """Refraction is positive near the horizon: apparent elevation is higher
    than the geometric value would be, keeping the near-horizon curve smooth."""
    # Compare two nearby times bracketing sunrise; elevation must rise, not
    # jump discontinuously (guards the piecewise refraction fit boundaries).
    base = _utc(2025, 6, 21, 3, 0)
    prev = None
    for m in range(0, 120, 2):
        _az, el = sun_position(base + timedelta(minutes=m), LAT, LON)
        if prev is not None:
            assert el >= prev - 0.1  # generally increasing, no big backstep
        prev = el


# ---------------------------------------------------------------------------
# Time-handling traps: tz-awareness, UTC normalisation, DST boundary
# ---------------------------------------------------------------------------


def test_naive_datetime_rejected():
    """A naive datetime must raise, not silently assume local time."""
    with pytest.raises(ValueError):
        sun_position(datetime(2025, 6, 21, 12, 0), LAT, LON)


def test_fixed_offset_tz_is_normalised():
    """A tz-aware non-UTC datetime (fixed +02:00, CEST-equivalent) yields the
    same result as its UTC form. Uses stdlib fixed-offset tz so the trap is
    always exercised, independent of an installed IANA tz database."""
    cest = timezone(timedelta(hours=2))
    # 2025-06-21 13:37 +02:00 == 11:37 UTC.
    local = datetime(2025, 6, 21, 13, 37, tzinfo=cest)
    utc = _utc(2025, 6, 21, 11, 37)
    az_l, el_l = sun_position(local, LAT, LON)
    az_u, el_u = sun_position(utc, LAT, LON)
    assert az_l == pytest.approx(az_u, abs=1e-9)
    assert el_l == pytest.approx(el_u, abs=1e-9)


def test_dst_spring_forward_boundary():
    """Across the CEST spring-forward the same absolute instant is stable
    whether expressed as +01:00 (CET) or +02:00 (CEST) local time (no DST
    double-count). Uses stdlib fixed offsets so it runs without tzdata."""
    cet = timezone(timedelta(hours=1))
    cest = timezone(timedelta(hours=2))
    # 2025-03-30 the clock jumps 02:00 CET -> 03:00 CEST; both label 01:00 UTC.
    instant = _utc(2025, 3, 30, 1, 0)
    as_cet = instant.astimezone(cet)  # 02:00 CET
    as_cest = datetime(2025, 3, 30, 3, 0, tzinfo=cest)  # same instant, CEST
    az_ref, el_ref = sun_position(instant, LAT, LON)
    for labelled in (as_cet, as_cest):
        az, el = sun_position(labelled, LAT, LON)
        assert az == pytest.approx(az_ref, abs=1e-9)
        assert el == pytest.approx(el_ref, abs=1e-9)


def test_zoneinfo_tz_matches_utc_when_available():
    """Bonus: a real IANA tz (Europe/Berlin) normalises identically to UTC.
    Skips gracefully where no tz database is installed."""
    if _BERLIN is None:
        pytest.skip("no IANA tz database available")
    local = datetime(2025, 6, 21, 13, 37, tzinfo=_BERLIN)
    utc = _utc(2025, 6, 21, 11, 37)
    az_l, el_l = sun_position(local, LAT, LON)
    az_u, el_u = sun_position(utc, LAT, LON)
    assert az_l == pytest.approx(az_u, abs=1e-9)
    assert el_l == pytest.approx(el_u, abs=1e-9)


def test_longitude_east_advances_solar_time():
    """A more easterly longitude reaches solar noon earlier (UTC)."""
    _e_here, _a, dt_here = _scan_solar_noon(2025, 6, 21)

    def noon_dt(lon):
        best = (-999.0, None)
        base = _utc(2025, 6, 21)
        for m in range(24 * 60):
            dt = base + timedelta(minutes=m)
            _az, el = sun_position(dt, LAT, lon)
            if el > best[0]:
                best = (el, dt)
        return best[1]

    east = noon_dt(LON + 15.0)  # +15 deg east -> ~1 h earlier
    west = noon_dt(LON - 15.0)
    assert east < dt_here < west
    # ~4 min per degree -> 15 deg ~ 60 min each way (grid is 1-min).
    assert (west - east).total_seconds() == pytest.approx(2 * 3600, abs=180)


# ---------------------------------------------------------------------------
# Accuracy budget: agreement with an independent NOAA re-derivation
# ---------------------------------------------------------------------------


def _independent_noaa(dt_utc, lat, lon):
    """A deliberately separate re-implementation of the NOAA algorithm used
    as an accuracy oracle (no refraction) to bound our internal error."""
    jd = 2440587.5 + dt_utc.timestamp() / 86400.0
    t = (jd - 2451545.0) / 36525.0
    L0 = math.radians((280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0)
    M = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
    C = (
        (1.914602 - t * (0.004817 + 0.000014 * t)) * math.sin(M)
        + (0.019993 - 0.000101 * t) * math.sin(2 * M)
        + 0.000289 * math.sin(3 * M)
    )
    true_long = math.degrees(L0) + C
    omega = math.radians(125.04 - 1934.136 * t)
    lam = math.radians(true_long - 0.00569 - 0.00478 * math.sin(omega))
    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(eps) * math.sin(lam))
    # Greenwich mean sidereal time -> local hour angle.
    gmst = (280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360.0
    ra = math.degrees(math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))) % 360.0
    ha = math.radians((gmst + lon - ra) % 360.0)
    latr = math.radians(lat)
    el = math.asin(
        math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(ha)
    )
    return math.degrees(el)


def test_elevation_matches_independent_noaa_within_budget():
    """Our geometric elevation tracks an independent NOAA re-derivation
    (via a different apparent-sidereal-time hour-angle path) to well under
    the 0.3 deg accuracy target.

    The oracle omits refraction, so we subtract our refraction correction to
    compare pure geometry. This isolates the closed-form solar-position error
    from the (separately tested) refraction model."""
    base = _utc(2025, 7, 5)
    max_err = 0.0
    for m in range(0, 24 * 60, 5):
        dt = base + timedelta(minutes=m)
        _az, el = sun_position(dt, LAT, LON)
        ref = _independent_noaa(dt, LAT, LON)
        if ref > 3.0:  # oracle is refraction-free; compare where sun is up
            # Back out our refraction to compare geometry against geometry.
            geometric = el - _refraction_correction(el)
            max_err = max(max_err, abs(geometric - ref))
    assert max_err < 0.1, f"elevation error {max_err:.4f} deg exceeds budget"


def test_smoothness_no_discontinuity_at_noon():
    """Azimuth is continuous through solar noon (no 180 wrap-around glitch)."""
    _e, _a, noon = _scan_solar_noon(2025, 6, 21)
    prev = None
    for m in range(-30, 31):
        az, _el = sun_position(noon + timedelta(minutes=m), LAT, LON)
        if prev is not None:
            step = abs(az - prev)
            assert step < 5.0, f"azimuth jumped {step:.2f} deg near noon"
        prev = az
