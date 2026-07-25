"""Season regression for the v0.22 tau_points migration (ADR §2.8 Integration).

These are the design-proof tests for replacing the Interim-az-Rampe (a tau(az)
projection anchored to one day) with an inline ``tau_points`` tau(sun-el) profile.
They run the REAL engine over the reference-site geometry (M-planes az 115, east
tree line, edge el 10) with real NOAA sun positions, so the elevation-dependent
gate actually bites.

The Interim-az-Rampe and the tau_points profile encode the SAME measured crown
(tau ~0 below el 4.5, ramping to 1 at el 9.5). On the anchor day they agree; away
from it the az-ramp drifts (~0.3 deg/day) because a fixed azimuth maps to a lower
sun elevation later in the season, so the ramp's high-tau azimuths start feeding
PHANTOM beam into the twilight. The tau_points profile is anchored to the physical
quantity (sun elevation) and cannot drift. Two checks:

  (a) 25.08. twilight: below the first tau_points knot (el < 4.5) the profile
      gates the beam to ~0, while the Interim-az-Rampe fabricates hundreds of
      watts of phantom beam at the same low-sun azimuths (THE design proof).
  (b) 21.07. equivalence: near the anchor season the 04:00-04:45Z raw curves stay
      within +-40 Wh, and above the crown edge (el > 10) the beam gate never fires
      for EITHER config (transmittance == 1.0), so the beam physics is untouched.

Plain pytest, no Home Assistant (SPEC §2).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from balcony_solar_forecast.core import horizon, solpos
from balcony_solar_forecast.core.engine import compute_forecast
from balcony_solar_forecast.core.types import (
    HorizonRow,
    PlaneConfig,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)

_LAT = 48.13
_LON = 11.57
_EDGE_EL = 10.0

# The measured crown as a tau(sun-el) profile: opaque below el 4.5, transmissive
# by el 9.5 (ADR §2.4, conservatively rounded medians).
_TAU_POINTS = ((4.5, 0.0), (5.5, 0.25), (6.5, 0.45), (8.0, 0.85), (9.5, 1.0))

# The Interim-az-Rampe: the SAME profile projected onto the 21.07. sun path, i.e.
# tau assigned BY AZIMUTH at the crown edge. az 64->el 4.5, az 66->el ~6, az
# 68->el ~8.4, az 70->el >9.5, so on the anchor day it tracks the profile; away
# from it the fixed azimuths drift off their elevations.
_INTERIM_ROWS = (
    HorizonRow(azimuth_deg=52.0, elevation_deg=_EDGE_EL, tau=0.0),
    HorizonRow(azimuth_deg=64.0, elevation_deg=_EDGE_EL, tau=0.0),
    HorizonRow(azimuth_deg=66.0, elevation_deg=_EDGE_EL, tau=0.35),
    HorizonRow(azimuth_deg=68.0, elevation_deg=_EDGE_EL, tau=0.85),
    HorizonRow(azimuth_deg=70.0, elevation_deg=_EDGE_EL, tau=1.0),
    HorizonRow(azimuth_deg=89.0, elevation_deg=_EDGE_EL, tau=1.0),
)
_TAU_POINTS_ROWS = (
    HorizonRow(azimuth_deg=52.0, elevation_deg=_EDGE_EL, tau=0.0, tau_points=_TAU_POINTS),
    HorizonRow(azimuth_deg=89.0, elevation_deg=_EDGE_EL, tau=0.0, tau_points=_TAU_POINTS),
)


def _site(rows: tuple[HorizonRow, ...]) -> SiteConfig:
    return SiteConfig(
        latitude=_LAT,
        longitude=_LON,
        planes=(
            PlaneConfig(
                name="M", azimuth_deg=115.0, tilt_deg=70.0, wp=430.0,
                efficiency=0.96, horizon=rows, actual_entity="sensor.m",
            ),
        ),
        groups=(),
    )


def _clear_morning(day: datetime, h0: int = 3, h1: int = 8) -> WeatherSeries:
    """A clear-sky morning of 15-min slots (a simple GHI = DNI*sin(el) + DHI)."""
    slots: list[WeatherSlot] = []
    t = day.replace(hour=h0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=h1, minute=0, second=0, microsecond=0)
    while t < end:
        mid = t + timedelta(minutes=7, seconds=30)
        _az, el = solpos.sun_position(mid, _LAT, _LON)
        if el > 0.0:
            dni, dhi = 850.0, 60.0
            ghi = max(0.0, dni * math.sin(math.radians(el)) + dhi)
        else:
            dni = dhi = ghi = 0.0
        slots.append(WeatherSlot(start=t, ghi=ghi, dni=dni, dhi=dhi, temp_c=15.0))
        t += timedelta(minutes=15)
    return WeatherSeries(slots=tuple(slots))


def _sun_at(slot: WeatherSlot) -> tuple[float, float]:
    mid = slot.start + timedelta(minutes=7, seconds=30)
    return solpos.sun_position(mid, _LAT, _LON)


def test_august_twilight_phantom_beam_is_the_tau_points_win():
    """25.08. below the first tau_points knot (el < 4.5): the profile gates the
    beam to ~0 while the Interim-az-Rampe fabricates phantom beam (ADR §2.8a).

    This is the structural proof of the design: the az-ramp, anchored to 21.07.,
    now assigns high transmittance to azimuths the late-August dawn reaches while
    the sun is still in the crown, so it manufactures beam that physically is not
    there.
    """
    day = datetime(2026, 8, 25, tzinfo=UTC)
    wx = _clear_morning(day)
    interim = compute_forecast(_site(_INTERIM_ROWS), wx, day.replace(hour=12), tz=UTC)
    tau_pts = compute_forecast(_site(_TAU_POINTS_ROWS), wx, day.replace(hour=12), tz=UTC)
    bi = interim.plane_results[0].beam_watts
    bt = tau_pts.plane_results[0].beam_watts

    # Twilight slots: sun up but below the first tau_points knot, inside the crown
    # azimuth span (52..89). This is the tau_points opaque region.
    twilight = [
        i for i, s in enumerate(wx.slots)
        if 0.0 < _sun_at(s)[1] < 4.5 and 52.0 <= _sun_at(s)[0] <= 89.0
    ]
    assert twilight, "expected at least one sub-4.5-deg twilight slot in the crown"

    # tau_points: no phantom beam at all in the twilight band.
    assert all(bt[i] == 0.0 for i in twilight)
    # Interim ramp: substantial phantom beam (ADR: +35-100 Wh/day; here stronger).
    phantom_wh = sum(bi[i] for i in twilight) * 0.25
    assert phantom_wh > 50.0, phantom_wh
    # And per slot the ramp beam dwarfs the (zero) tau_points beam.
    for i in twilight:
        assert bi[i] > 100.0, (i, bi[i])


def test_july_migration_dawn_within_40wh_and_beam_gate_untouched_above_edge():
    """21.07. (near the anchor season) the migration is a near-null step where it
    matters and a NO-OP above the crown edge (ADR §2.8b).

    * 04:00Z raw hourly Wh: |interim - tau_points| < 40 Wh (both encode the same
      measured crown, so near the anchor day they agree).
    * Above the edge (el > 10): the beam gate never fires for EITHER config
      (static transmittance == 1.0), so the beam physics is byte-untouched — the
      only residual raw difference there is the intended diffuse SVF refinement
      (the tau_points band integral models the semi-transparent crown's diffuse
      more correctly), which stays a small bounded floor offset.
    """
    day = datetime(2026, 7, 21, tzinfo=UTC)
    wx = _clear_morning(day)
    interim = compute_forecast(_site(_INTERIM_ROWS), wx, day.replace(hour=12), tz=UTC)
    tau_pts = compute_forecast(_site(_TAU_POINTS_ROWS), wx, day.replace(hour=12), tz=UTC)

    # (b1) 04:00Z hour raw difference < 40 Wh.
    hkey = day.replace(hour=4).isoformat()
    raw_i = interim.raw_hourly_wh.get(hkey, 0.0)
    raw_t = tau_pts.raw_hourly_wh.get(hkey, 0.0)
    assert abs(raw_i - raw_t) < 40.0, (raw_i, raw_t)

    # (b2) Above the crown edge the beam GATE is 1.0 for BOTH configs -> the beam
    # is untouched; the residual raw-total difference is the bounded diffuse floor.
    interim_plane = _site(_INTERIM_ROWS).planes[0]
    tau_plane = _site(_TAU_POINTS_ROWS).planes[0]
    above = [i for i, s in enumerate(wx.slots) if _sun_at(s)[1] > _EDGE_EL + 1.0]
    assert above, "expected midday slots above the crown edge"
    for i in above:
        az, el = _sun_at(wx.slots[i])
        # Sun above the interpolated horizon line -> the engine sets static_tau to
        # 1.0 UNCONDITIONALLY (it never even consults transmittance_at), so the
        # beam is byte-untouched for BOTH configs no matter what the tables say.
        assert el > horizon.interp_elevation(interim_plane, az)
        assert el > horizon.interp_elevation(tau_plane, az)
    # The served raw totals above the edge differ only by the small diffuse floor
    # offset (the SVF refinement), never the beam.
    ri = interim.raw_total_watts
    rt = tau_pts.raw_total_watts
    for i in above:
        assert abs(ri[i] - rt[i]) < 2.0, (i, ri[i], rt[i])
