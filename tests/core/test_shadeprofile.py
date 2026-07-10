"""Tests for the shade-profile builder (pure, HA-free).

Covers (SPEC §15):
  - a clear plane (empty horizon + empty shademap): full transmittance, flat
    zero horizon lines, daylight-only monotonic sun path;
  - engine-exact gate replication: every sun-path transmittance equals an
    independently recomputed effective_tau, and every static-horizon grid point
    equals interp_elevation (no drift from the physics core);
  - the learned shademap darkening the path: a dark bin drops the sun-path
    transmittance below the threshold, lifts the learned shade horizon, and
    raises the shaded fraction;
  - the shade horizon's monotonicity in the tau threshold;
  - seasonal geometry (summer noon higher than winter);
  - degenerate inputs: polar night -> empty profile; unknown channel -> static
    prior, no crash.
"""

from __future__ import annotations

from datetime import UTC, date, timedelta, timezone

import pytest
from balcony_solar_forecast.const import (
    ATTR_SP_AXIS_AZ_MAX,
    ATTR_SP_AXIS_AZ_MIN,
    SHADE_PROFILE_TAU_THRESHOLD,
)
from balcony_solar_forecast.core import horizon as horizon_mod
from balcony_solar_forecast.core import shademap as shademap_mod
from balcony_solar_forecast.core import shadeprofile
from balcony_solar_forecast.core.types import (
    HorizonRow,
    PlaneConfig,
    ShademapState,
)

# Landshut / operator reference site (SPEC §4 self-test coordinates).
LAT = 48.3
LON = 12.1
# Fixed CET offset (no DST) so the tests never depend on tzdata/zoneinfo being
# installed; the builder only needs *a* tzinfo to walk the local day.
TZ = timezone(timedelta(hours=1))

SUMMER = date(2026, 6, 21)
WINTER = date(2026, 12, 21)


def _south_wall_plane(horizon: tuple[HorizonRow, ...] = ()) -> PlaneConfig:
    """A vertical south-facing balcony plane with an optional horizon table."""
    return PlaneConfig(
        name="M-south",
        azimuth_deg=180.0,
        tilt_deg=90.0,
        wp=400.0,
        horizon=horizon,
    )


def _profile(plane, state=None, *, day=SUMMER, **kw):
    return shadeprofile.compute_shade_profile(
        plane=plane,
        shademap=state if state is not None else ShademapState(),
        channel=plane.name,
        latitude=LAT,
        longitude=LON,
        day=day,
        tz=TZ,
        **kw,
    )


# ---------------------------------------------------------------------------
# Clear plane
# ---------------------------------------------------------------------------


def test_clear_plane_full_transmittance():
    p = _profile(_south_wall_plane())

    assert p["sample_count"] > 0
    assert p["module"] == "M-south"
    assert p["date"] == SUMMER.isoformat()
    assert p["has_learned_data"] is False
    assert p["learned_bins"] == 0

    # Empty horizon + empty shademap => nothing attenuates the beam anywhere.
    assert all(t == 1.0 for t in p["transmittance"])
    assert all(h == 0.0 for h in p["static_horizon"])
    assert all(h == 0.0 for h in p["shade_horizon"])
    assert p["shaded_fraction"] == 0.0
    assert p["mean_transmittance"] == 1.0

    # Summer noon at lat 48 is well above 60 deg.
    assert p["max_elevation"] > 60.0


def test_sun_path_is_daylight_only_and_monotonic():
    p = _profile(_south_wall_plane())
    az = p["azimuth"]
    el = p["sun_elevation"]
    tm = p["time"]

    n = p["sample_count"]
    assert len(az) == len(el) == len(p["transmittance"]) == len(tm) == n
    # Daylight only.
    assert all(e > 0.0 for e in el)
    # Azimuth increases monotonically through the day at these latitudes.
    assert all(az[i] <= az[i + 1] + 1e-9 for i in range(n - 1))
    # Times are "HH:MM" and sorted; sunrise east of, sunset west of, south.
    assert all(len(t) == 5 and t[2] == ":" for t in tm)
    assert p["sunrise"]["azimuth"] < 180.0 < p["sunset"]["azimuth"]
    assert p["sunrise"]["time"] == tm[0]
    assert p["sunset"]["time"] == tm[-1]


# ---------------------------------------------------------------------------
# Engine-exact gate replication (no drift from the physics core)
# ---------------------------------------------------------------------------


def test_transmittance_matches_effective_tau_exactly():
    horizon = (
        HorizonRow(azimuth_deg=100.0, elevation_deg=5.0, tau=1.0),
        HorizonRow(azimuth_deg=150.0, elevation_deg=28.0, tau=0.2),
        HorizonRow(azimuth_deg=210.0, elevation_deg=28.0, tau=0.2),
        HorizonRow(azimuth_deg=260.0, elevation_deg=5.0, tau=1.0),
    )
    plane = _south_wall_plane(horizon)
    state = ShademapState()
    p = _profile(plane, state)
    doy = p["doy"]

    # Every plotted transmittance is the same beam attenuation the engine applies.
    for az, el, tau in zip(
        p["azimuth"], p["sun_elevation"], p["transmittance"], strict=True
    ):
        expected = shadeprofile.effective_tau_at(
            plane, state, channel=plane.name, sun_az=az, sun_el=el, doy=doy
        )
        assert tau == pytest.approx(round(expected, 3))


def test_static_horizon_matches_interp_elevation():
    horizon = (
        HorizonRow(azimuth_deg=120.0, elevation_deg=10.0, tau=0.5),
        HorizonRow(azimuth_deg=180.0, elevation_deg=35.0, tau=0.0),
        HorizonRow(azimuth_deg=240.0, elevation_deg=10.0, tau=0.5),
    )
    plane = _south_wall_plane(horizon)
    p = _profile(plane)

    for az, h in zip(p["horizon_azimuth"], p["static_horizon"], strict=True):
        assert h == pytest.approx(
            round(horizon_mod.interp_elevation(plane, az), 2)
        )


# ---------------------------------------------------------------------------
# Learned shademap darkening the path
# ---------------------------------------------------------------------------


def _seed_dark_bin(plane, *, sun_az, sun_el, doy, repeats=200) -> ShademapState:
    """A shademap whose (sun_az, sun_el) bin is EMA-trained to full occlusion."""
    state = ShademapState()
    for _ in range(repeats):
        state = shademap_mod.update_bin(
            state,
            channel=plane.name,
            sun_az=sun_az,
            sun_el=sun_el,
            doy=doy,
            measured_t=0.0,
        )
    return state


def test_learned_dark_bin_shades_the_sun_path():
    plane = _south_wall_plane()
    clear = _profile(plane)
    # Pick the noon sample (highest sun) and darken exactly its bin.
    idx = clear["sun_elevation"].index(clear["max_elevation"])
    noon_az = clear["azimuth"][idx]
    noon_el = clear["sun_elevation"][idx]
    doy = clear["doy"]

    state = _seed_dark_bin(plane, sun_az=noon_az, sun_el=noon_el, doy=doy)
    dark = _profile(plane, state)

    # The learner is now contributing.
    assert dark["has_learned_data"] is True
    assert dark["learned_bins"] >= 1

    # The noon sample (same time grid, same index) is now well shaded.
    assert dark["transmittance"][idx] < SHADE_PROFILE_TAU_THRESHOLD
    assert clear["transmittance"][idx] == 1.0
    # And the day as a whole picked up some shaded fraction.
    assert dark["shaded_fraction"] > clear["shaded_fraction"]

    # The learned shade horizon at the darkened azimuth rose off the floor.
    j = min(
        range(len(dark["horizon_azimuth"])),
        key=lambda k: abs(dark["horizon_azimuth"][k] - noon_az),
    )
    assert dark["shade_horizon"][j] > 0.0


def test_has_learned_data_is_half_year_specific():
    # A bin trained in one half-year must not flag "learned data" for a date in
    # the OTHER half-year — that half's bins can never touch the shown curve.
    plane = _south_wall_plane()
    april = date(2026, 4, 10)     # doy 100 -> half 0 (before solstice)
    october = date(2026, 10, 10)  # doy 283 -> half 1 (after solstice)

    ap = _profile(plane, day=april)
    idx = ap["sun_elevation"].index(ap["max_elevation"])
    state = _seed_dark_bin(
        plane, sun_az=ap["azimuth"][idx], sun_el=ap["sun_elevation"][idx],
        doy=ap["doy"],
    )

    # Same half-year: the learned bin is counted and darkens the path.
    same = _profile(plane, state, day=april)
    assert same["has_learned_data"] is True
    assert same["learned_bins"] >= 1

    # Opposite half-year: not counted, and the whole curve is the static prior.
    other = _profile(plane, state, day=october)
    assert other["has_learned_data"] is False
    assert other["learned_bins"] == 0
    assert all(t == 1.0 for t in other["transmittance"])


def test_shade_horizon_monotonic_in_threshold():
    plane = _south_wall_plane()
    clear = _profile(plane)
    idx = clear["sun_elevation"].index(clear["max_elevation"])
    state = _seed_dark_bin(
        plane,
        sun_az=clear["azimuth"][idx],
        sun_el=clear["sun_elevation"][idx],
        doy=clear["doy"],
    )

    low = _profile(plane, state, tau_threshold=0.3)
    high = _profile(plane, state, tau_threshold=0.9)
    # A stricter "counts as shaded" bar can only raise (never lower) the horizon.
    assert low["horizon_azimuth"] == high["horizon_azimuth"]
    assert all(
        h >= lo - 1e-9
        for lo, h in zip(
            low["shade_horizon"], high["shade_horizon"], strict=True
        )
    )


# ---------------------------------------------------------------------------
# Seasonal geometry + degenerate inputs
# ---------------------------------------------------------------------------


def test_summer_noon_higher_than_winter():
    plane = _south_wall_plane()
    summer = _profile(plane, day=SUMMER)
    winter = _profile(plane, day=WINTER)
    assert summer["max_elevation"] > winter["max_elevation"] + 20.0


def test_polar_night_returns_empty_profile():
    plane = PlaneConfig(name="P", azimuth_deg=180.0, tilt_deg=90.0, wp=400.0)
    p = shadeprofile.compute_shade_profile(
        plane=plane,
        shademap=ShademapState(),
        channel="P",
        latitude=80.0,  # far north
        longitude=0.0,
        day=WINTER,  # polar night
        tz=UTC,
    )
    assert p["sample_count"] == 0
    assert p["azimuth"] == []
    assert p["shade_horizon"] == []
    assert p["mean_transmittance"] is None
    assert p["sunrise"] is None


def test_unknown_channel_falls_back_to_static_prior():
    plane = _south_wall_plane()
    # A shademap that only knows a DIFFERENT channel: our channel sees no bins.
    other = ShademapState()
    other = shademap_mod.update_bin(
        other, channel="somewhere-else", sun_az=180.0, sun_el=40.0,
        doy=SUMMER.timetuple().tm_yday, measured_t=0.0,
    )
    p = _profile(plane, other)
    assert p["has_learned_data"] is False
    # No horizon + no learned bin for this channel => pure static prior (1.0).
    assert all(t == 1.0 for t in p["transmittance"])


# ---------------------------------------------------------------------------
# Year-stable x-axis: widest daylight azimuth span of the whole year (SPEC §15)
# ---------------------------------------------------------------------------


def test_axis_domain_mid_northern_is_wide():
    # A mid-northern site: summer sunrise is well NE (< 90) and sunset well NW
    # (> 270), so the year's widest daylight azimuth span reaches both.
    lo, hi = shadeprofile.axis_azimuth_domain(
        latitude=LAT, longitude=LON, year=2026, tz=TZ
    )
    assert lo < 90.0
    assert hi > 270.0
    assert lo < hi
    # Rounded to 2 decimals, as documented.
    assert lo == round(lo, 2)
    assert hi == round(hi, 2)


def test_axis_domain_contains_equinox_and_winter_spans():
    # The whole-year domain must CONTAIN every single date's daylight azimuth
    # span, so the axis never rescales and no sample falls off the plot. We check
    # an equinox-ish date and a winter date (both strictly inside the June-wide
    # span). NOT the summer solstice itself: the domain uses a coarse 10-min
    # sweep while a per-date profile samples finer, so a solstice sample can sit a
    # hair outside — which is exactly why the CARD defensively unions the two.
    plane = _south_wall_plane()
    lo, hi = shadeprofile.axis_azimuth_domain(
        latitude=LAT, longitude=LON, year=2026, tz=TZ
    )
    for day in (date(2026, 3, 20), WINTER):
        p = _profile(plane, day=day)
        assert p["sample_count"] > 0
        assert lo <= min(p["azimuth"])
        assert max(p["azimuth"]) <= hi


def test_axis_domain_southern_hemisphere_is_comparably_wide():
    # Hemisphere-agnostic: at lat -35 the December (southern-summer) solstice
    # supplies the wide span (the noon sun crosses through the north, so daylight
    # azimuths sweep across 0/360). The domain is comparably wide to the northern
    # site — min well below 90, max well above 270.
    lo, hi = shadeprofile.axis_azimuth_domain(
        latitude=-35.0, longitude=LON, year=2026, tz=TZ
    )
    assert lo < 90.0
    assert hi > 270.0


def test_profile_carries_axis_keys_populated_and_empty():
    # The exact ATTR_SP_ strings the card reads, present in BOTH branches.
    assert ATTR_SP_AXIS_AZ_MIN == "axis_azimuth_min"
    assert ATTR_SP_AXIS_AZ_MAX == "axis_azimuth_max"

    lo, hi = shadeprofile.axis_azimuth_domain(
        latitude=LAT, longitude=LON, year=SUMMER.year, tz=TZ
    )

    # Populated profile: the two keys equal the standalone helper for the year.
    p = _profile(_south_wall_plane(), day=SUMMER)
    assert p[ATTR_SP_AXIS_AZ_MIN] == lo
    assert p[ATTR_SP_AXIS_AZ_MAX] == hi

    # Empty (polar-night) profile still carries the axis bounds: pure site
    # geometry, independent of whether THIS day has daylight. The June solstice
    # (polar day at lat 80) supplies the span even though December is dark.
    empty = shadeprofile.compute_shade_profile(
        plane=PlaneConfig(name="P", azimuth_deg=180.0, tilt_deg=90.0, wp=400.0),
        shademap=ShademapState(),
        channel="P",
        latitude=80.0,
        longitude=0.0,
        day=WINTER,
        tz=UTC,
    )
    assert empty["sample_count"] == 0
    epLo, epHi = shadeprofile.axis_azimuth_domain(
        latitude=80.0, longitude=0.0, year=WINTER.year, tz=UTC
    )
    assert empty[ATTR_SP_AXIS_AZ_MIN] == epLo
    assert empty[ATTR_SP_AXIS_AZ_MAX] == epHi
    # The June polar day yields daylight, so the bounds are real (not the
    # degenerate 0/360 fallback).
    assert (epLo, epHi) != (0.0, 360.0)


# NOTE: the degenerate (0.0, 360.0) fallback (NEITHER solstice yields daylight)
# is not exercised because it is unreachable for any real latitude: on June 21
# every site with lat > -66.6° has daylight and on Dec 21 every site with
# lat < 66.6° does, and no latitude is simultaneously above 66.6° and below
# -66.6°. So at least one solstice always contributes a daylight sample.


# ---------------------------------------------------------------------------
# Default module = the balcony front (the orientation the most planes share)
# ---------------------------------------------------------------------------


def _plane(name: str, az: float) -> PlaneConfig:
    return PlaneConfig(name=name, azimuth_deg=az, tilt_deg=70.0, wp=400.0)


def test_default_module_picks_the_front_orientation():
    # Reference-site layout: N (25) x2, front (115) x4, S (205) x2. The front is
    # the modal azimuth, so the FIRST 115° plane (M2) is the default.
    planes = (
        _plane("M1", 25.0), _plane("M2", 115.0), _plane("M3", 115.0),
        _plane("M4", 205.0), _plane("M5", 25.0), _plane("M6", 115.0),
        _plane("M7", 115.0), _plane("M8", 205.0),
    )
    assert shadeprofile.default_module(planes) == "M2"


def test_default_module_edge_cases():
    assert shadeprofile.default_module(()) == ""
    assert shadeprofile.default_module((_plane("only", 200.0),)) == "only"
    # A tie in counts keeps the FIRST plane (config order) — deterministic.
    tie = (_plane("A", 90.0), _plane("B", 270.0))
    assert shadeprofile.default_module(tie) == "A"
