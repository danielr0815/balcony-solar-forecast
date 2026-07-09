"""End-to-end tests for core/engine.py: the pure physics pipeline.

Plain pytest, no Home Assistant imports (SPEC §4).

The sibling physics modules (solpos, transpose, horizon) are owned by other
tasks and may still be stubs while this runs, so these tests inject small,
*analytic* stand-ins for their functions by monkeypatching the names the
engine calls (``engine.solpos.sun_position`` etc.). That keeps the engine's
orchestration, horizon gating, group clamping and energy roll-ups under test
without depending on the real physics being merged yet. A second test class
exercises the engine against the shipped DEFAULT_SITE with the same stand-ins
to confirm plausible daily shapes (N plane peaks morning, S plane cut in the
afternoon by its wall horizon row).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone

import pytest
from balcony_solar_forecast.const import DEFAULT_SITE
from balcony_solar_forecast.core import engine
from balcony_solar_forecast.core.types import (
    InverterGroup,
    PlaneConfig,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)

# --------------------------------------------------------------------------
# Analytic stand-ins for the sibling physics modules
# --------------------------------------------------------------------------

# A synthetic "day" of 96 15-min slots. Solar noon is put at index 48 (12:00
# UTC on the test date) with the sun sweeping azimuth from east (90) through
# south (180) to west (270) and a sinusoidal elevation bump peaking at noon.
_TEST_DATE = datetime(2024, 6, 21, 0, 0, tzinfo=UTC)  # summer solstice-ish
_NOON_INDEX = 48
_MAX_ELEV = 62.0


def _slot_index(dt: datetime) -> int:
    """Which 15-min slot midpoint this datetime belongs to (0..95)."""
    delta = dt - _TEST_DATE
    return int(delta.total_seconds() // 900)


def fake_sun_position(dt_utc, lat, lon):
    """Analytic sun: azimuth sweeps E->S->W, elevation a noon sine bump.

    Daylight is slots ~24..72 (06:00..18:00). Outside that the sun is below
    the horizon (negative elevation) so the engine short-circuits the slot.
    """
    idx = _slot_index(dt_utc)
    # Fraction of the day around noon in [-1, 1] over the 06:00..18:00 window.
    day_span = 24  # slots from noon to the edge of daylight (6 h)
    frac = (idx - _NOON_INDEX) / day_span
    if abs(frac) >= 1.0:
        return (90.0 if frac < 0 else 270.0, -5.0)
    elev = _MAX_ELEV * math.cos(frac * (math.pi / 2.0))
    # Azimuth: NE (~45) at sunrise -> S (180) at noon -> NW (~315) at sunset.
    # A high-summer sun at this latitude rises well north of due east, which is
    # exactly what gives the az-25 (NNE) plane its real morning beam and the
    # az-205 (SSW) plane its afternoon beam — a due-east..due-west sweep would
    # never illuminate the NNE plane and is physically wrong for the solstice.
    az = 180.0 + frac * 135.0
    return (az % 360.0, elev)


def _ang_diff(a: float, b: float) -> float:
    """Absolute smallest angle between two azimuths (deg)."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def fake_hay_davies_poa(
    ghi, dni, dhi, sun_az, sun_el, plane_az, plane_tilt, albedo
):
    """Directional POA stand-in.

    Beam ~ dni * cos(incidence proxy): the incidence proxy rewards planes
    whose azimuth faces the sun and drops to zero when the sun is more than
    90 deg off the plane azimuth. Isotropic and ground are simple, positive
    constants scaled by dhi/ghi so the None-handling and SVF scaling paths are
    exercised. Circumsolar forced to 0 below 3 deg elevation (SPEC musts) —
    the engine also skips sub-horizon sun, but keep it faithful here.
    """
    if sun_el <= 0.0:
        return {"beam": 0.0, "circumsolar": 0.0, "isotropic": 0.0, "ground": 0.0}
    off = _ang_diff(sun_az, plane_az)
    facing = max(0.0, math.cos(math.radians(off)))
    # Tilt lifts the effective beam for near-vertical planes hit side-on; keep
    # it simple and elevation-weighted so mornings/afternoons differ by plane.
    # The elevation weight is deliberately soft (0.3 floor) so the azimuth
    # facing term stays the dominant signal — otherwise the noon elevation peak
    # would swamp the directional morning/afternoon skew we assert on.
    elev_weight = 0.3 + 0.7 * max(0.0, math.sin(math.radians(sun_el)))
    beam = dni * facing * elev_weight
    circ = 0.0 if sun_el < 3.0 else 0.10 * beam
    iso = 0.2 * dhi
    ground = albedo * ghi * (1.0 - math.cos(math.radians(plane_tilt))) / 2.0
    return {"beam": beam, "circumsolar": circ, "isotropic": iso, "ground": ground}


def make_fake_horizon(wall_planes: set[str], wall_az: float, wall_from_el=90.0):
    """Build horizon stand-ins that occlude ``wall_planes`` past ``wall_az``.

    ``interp_elevation`` returns a high horizon line (wall) for the named
    planes once the sun azimuth is at/after ``wall_az`` so the engine's
    ``sun_el <= horizon_elev`` gate trips; ``transmittance_at`` returns 0 there
    (opaque wall). All other azimuths / planes see an open horizon.
    """

    def fake_interp_elevation(plane, sun_az):
        if plane.name in wall_planes and sun_az >= wall_az:
            return wall_from_el
        return 0.0

    def fake_transmittance_at(plane, sun_az, doy):
        if plane.name in wall_planes and sun_az >= wall_az:
            return 0.0
        return 1.0

    def fake_sky_view_factor(plane):
        # Slightly reduced dome for the walled planes so SVF scaling is visible.
        return 0.7 if plane.name in wall_planes else 0.9

    return fake_interp_elevation, fake_transmittance_at, fake_sky_view_factor


@pytest.fixture
def patched_physics(monkeypatch):
    """Patch the engine's physics deps with analytic stand-ins.

    Default horizon: a hard wall occludes the S-facing planes once the sun
    passes az 210 (SPEC §13 building edge), leaving N/front planes open.
    """
    monkeypatch.setattr(engine.solpos, "sun_position", fake_sun_position)
    monkeypatch.setattr(engine.transpose, "hay_davies_poa", fake_hay_davies_poa)
    interp, tau, svf = make_fake_horizon({"S", "M4", "M8"}, wall_az=210.0)
    monkeypatch.setattr(engine.horizon, "interp_elevation", interp)
    monkeypatch.setattr(engine.horizon, "transmittance_at", tau)
    monkeypatch.setattr(engine.horizon, "sky_view_factor", svf)
    return monkeypatch


# --------------------------------------------------------------------------
# Synthetic clear-sky weather series
# --------------------------------------------------------------------------


def _clear_sky_series(n=96, temp=20.0):
    """96 slots of a smooth clear-sky day aligned to _TEST_DATE.

    GHI/DNI/DHI follow a noon-peaked bump so night slots carry ~0 irradiance;
    the engine also gates on sun elevation, so exact values only need to be
    plausible and positive around midday.
    """
    slots = []
    for i in range(n):
        frac = (i - _NOON_INDEX) / 24.0
        if abs(frac) >= 1.0:
            ghi = dni = dhi = 0.0
        else:
            bump = math.cos(frac * (math.pi / 2.0))
            ghi = 900.0 * bump
            dni = 800.0 * bump
            dhi = 120.0 * bump
        start = _TEST_DATE + timedelta(minutes=15 * i)
        slots.append(
            WeatherSlot(start=start, ghi=ghi, dni=dni, dhi=dhi, temp_c=temp)
        )
    return WeatherSeries(slots=tuple(slots))


def _two_plane_site():
    """N-facing (az 25) + S-facing (az 205) planes on one clamped inverter."""
    n_plane = PlaneConfig(name="N", azimuth_deg=25.0, tilt_deg=70.0, wp=430.0)
    s_plane = PlaneConfig(name="S", azimuth_deg=205.0, tilt_deg=70.0, wp=430.0)
    group = InverterGroup(name="WR", plane_names=("N", "S"), ac_limit_w=800.0)
    return SiteConfig(
        latitude=48.5, longitude=12.2, planes=(n_plane, s_plane), groups=(group,)
    )


# --------------------------------------------------------------------------
# Shape / correctness tests
# --------------------------------------------------------------------------


class TestPipeline:
    def test_output_aligned_to_slots(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)

        assert len(res.slot_starts) == len(weather)
        assert len(res.total_watts) == len(weather)
        assert res.slot_starts[0] == weather.slots[0].start
        for pr in res.plane_results:
            assert len(pr.watts) == len(weather)

    def test_night_slots_are_zero(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        # Midnight (slot 0) and slot 95 are well outside daylight.
        assert res.total_watts[0] == 0.0
        assert res.total_watts[-1] == 0.0

    def test_midday_produces_power(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert res.total_watts[_NOON_INDEX] > 0.0

    def test_twilight_diffuse_not_clipped(self, monkeypatch):
        """Regression (engine §E4): when the sun is below the horizon but the
        sky still carries diffuse (civil twilight / winter fog), the engine must
        transpose the isotropic + ground terms instead of hard-zeroing the slot.
        """
        # A transpose stand-in faithful to the real contract: below the horizon
        # only the isotropic diffuse + ground survive (beam/circumsolar = 0).
        def twilight_poa(ghi, dni, dhi, sun_az, sun_el, plane_az, plane_tilt, albedo):
            if sun_el <= 0.0:
                return {
                    "beam": 0.0,
                    "circumsolar": 0.0,
                    "isotropic": 0.5 * dhi,  # some positive tilted projection
                    "ground": albedo * ghi * 0.1,
                }
            return {"beam": 100.0, "circumsolar": 0.0, "isotropic": 0.5 * dhi, "ground": 0.0}

        monkeypatch.setattr(engine.solpos, "sun_position", fake_sun_position)
        monkeypatch.setattr(engine.transpose, "hay_davies_poa", twilight_poa)
        interp, tau, svf = make_fake_horizon(set(), wall_az=999.0)
        monkeypatch.setattr(engine.horizon, "interp_elevation", interp)
        monkeypatch.setattr(engine.horizon, "transmittance_at", tau)
        monkeypatch.setattr(engine.horizon, "sky_view_factor", svf)

        # Build a series where a below-horizon slot (slot 0, sun_el = -5) still
        # carries diffuse irradiance.
        slots = []
        for i in range(96):
            dhi = 15.0 if i == 0 else 0.0  # dawn diffuse before sunrise
            ghi = 15.0 if i == 0 else 0.0
            start = _TEST_DATE + timedelta(minutes=15 * i)
            slots.append(WeatherSlot(start=start, ghi=ghi, dni=0.0, dhi=dhi, temp_c=5.0))
        weather = WeatherSeries(slots=tuple(slots))

        res = engine.compute_forecast(_two_plane_site(), weather, now=_TEST_DATE)
        # Slot 0: sun below horizon but diffuse present -> nonzero production.
        assert res.total_watts[0] > 0.0
        # A truly dark slot (no diffuse, sun below horizon) stays exactly zero.
        assert res.total_watts[1] == 0.0

    def test_north_plane_peaks_in_the_morning(self, patched_physics):
        # Az-25 plane faces NNE: sun passes closest before noon.
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        n_watts = next(p.watts for p in res.plane_results if p.name == "N")
        peak_idx = max(range(len(n_watts)), key=lambda i: n_watts[i])
        assert peak_idx < _NOON_INDEX  # morning peak

    def test_south_plane_cut_in_the_afternoon_by_wall(self, patched_physics):
        # The S plane's beam should collapse once the sun passes az 210 (wall).
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        s_watts = next(p.watts for p in res.plane_results if p.name == "S")

        # Find the slot where the sun crosses az 210 (after noon).
        cross = None
        for i in range(_NOON_INDEX, len(s_watts)):
            mid = weather.slots[i].midpoint
            az, el = fake_sun_position(mid, 48.5, 12.2)
            if el > 0 and az >= 210.0:
                cross = i
                break
        assert cross is not None
        # Before the wall the S plane still has diffuse+ground; after the wall
        # its beam is gone, so afternoon power must drop noticeably vs. the
        # slot just before the crossing.
        assert s_watts[cross] < s_watts[cross - 1]

    def test_group_clamp_holds_site_total_within_ac_limit(self, patched_physics):
        # Force both planes to huge Wp so the unclamped sum exceeds 800 W and
        # verify the clamp caps the site total.
        n_plane = PlaneConfig(name="N", azimuth_deg=180.0, tilt_deg=30.0, wp=5000.0)
        s_plane = PlaneConfig(name="S", azimuth_deg=180.0, tilt_deg=30.0, wp=5000.0)
        group = InverterGroup(name="WR", plane_names=("N", "S"), ac_limit_w=800.0)
        site = SiteConfig(
            latitude=48.5, longitude=12.2, planes=(n_plane, s_plane), groups=(group,)
        )
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert max(res.total_watts) == pytest.approx(800.0)
        assert all(w <= 800.0 + 1e-6 for w in res.total_watts)

    def test_hourly_wh_sums_to_slot_energy(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        # Sum of hourly Wh must equal sum(total_watts) * 0.25 h.
        expected = sum(res.total_watts) * (15.0 / 60.0)
        assert sum(res.hourly_wh.values()) == pytest.approx(expected)

    def test_hourly_keys_are_iso_utc_hours(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        for key in res.hourly_wh:
            dt = datetime.fromisoformat(key)
            assert dt.tzinfo is not None
            assert dt.minute == 0 and dt.second == 0

    def test_daily_kwh_matches_hourly_total(self, patched_physics):
        site = _two_plane_site()
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        total_kwh = sum(res.daily_kwh.values())
        assert total_kwh == pytest.approx(sum(res.hourly_wh.values()) / 1000.0)

    def test_daily_kwh_local_calendar_differs_from_utc(self, patched_physics):
        # A +02:00 zone shifts local midnight, so the single UTC test day can
        # split across two local dates. Assert the bucketing honours the tz.
        site = _two_plane_site()
        weather = _clear_sky_series()
        tz = timezone(timedelta(hours=-6))  # push some late slots to prev day
        res = engine.compute_forecast(site, weather, now=_TEST_DATE, tz=tz)
        # Still conserves energy, just possibly across two local dates.
        assert sum(res.daily_kwh.values()) == pytest.approx(
            sum(res.hourly_wh.values()) / 1000.0
        )


# --------------------------------------------------------------------------
# None / missing-weather robustness
# --------------------------------------------------------------------------


class TestNoneRobustness:
    def _series_with_gap(self):
        slots = list(_clear_sky_series().slots)
        # Punch a None into a midday slot's GHI (fetcher hole).
        bad = slots[_NOON_INDEX]
        slots[_NOON_INDEX] = WeatherSlot(
            start=bad.start, ghi=None, dni=bad.dni, dhi=bad.dhi, temp_c=bad.temp_c
        )
        return WeatherSeries(slots=tuple(slots))

    def test_none_ghi_slot_is_zero_not_crash(self, patched_physics):
        site = _two_plane_site()
        weather = self._series_with_gap()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert res.total_watts[_NOON_INDEX] == 0.0
        # Neighbouring good slots still produce.
        assert res.total_watts[_NOON_INDEX - 1] > 0.0

    def test_none_slot_keeps_alignment_dense(self, patched_physics):
        site = _two_plane_site()
        weather = self._series_with_gap()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        assert len(res.total_watts) == len(weather)
        for pr in res.plane_results:
            assert len(pr.watts) == len(weather)

    def test_none_temperature_slot_is_zero(self, patched_physics):
        slots = list(_clear_sky_series().slots)
        bad = slots[_NOON_INDEX]
        slots[_NOON_INDEX] = WeatherSlot(
            start=bad.start, ghi=bad.ghi, dni=bad.dni, dhi=bad.dhi, temp_c=None
        )
        site = _two_plane_site()
        res = engine.compute_forecast(
            site, WeatherSeries(slots=tuple(slots)), now=_TEST_DATE
        )
        assert res.total_watts[_NOON_INDEX] == 0.0

    def test_all_none_series_yields_all_zero(self, patched_physics):
        slots = tuple(
            WeatherSlot(
                start=_TEST_DATE + timedelta(minutes=15 * i),
                ghi=None,
                dni=None,
                dhi=None,
                temp_c=None,
            )
            for i in range(8)
        )
        site = _two_plane_site()
        res = engine.compute_forecast(site, WeatherSeries(slots=slots), now=_TEST_DATE)
        assert all(w == 0.0 for w in res.total_watts)
        assert res.hourly_wh == {}
        assert res.daily_kwh == {}


# --------------------------------------------------------------------------
# Snow albedo and DST-boundary robustness
# --------------------------------------------------------------------------


class TestPhysicsEdges:
    def test_snow_raises_ground_reflection(self, patched_physics):
        # Same slot with vs. without snow: snow albedo (0.5) lifts the ground
        # term, so a plane's power should be >= the no-snow case.
        base = _clear_sky_series()
        s0 = base.slots[_NOON_INDEX]
        snowy = WeatherSlot(
            start=s0.start, ghi=s0.ghi, dni=s0.dni, dhi=s0.dhi,
            temp_c=s0.temp_c, snow_depth_m=0.10,
        )
        dry_series = WeatherSeries(slots=(s0,))
        wet_series = WeatherSeries(slots=(snowy,))
        site = _two_plane_site()
        dry = engine.compute_forecast(site, dry_series, now=_TEST_DATE)
        wet = engine.compute_forecast(site, wet_series, now=_TEST_DATE)
        assert wet.total_watts[0] >= dry.total_watts[0]

    def test_thin_snow_below_threshold_is_default_albedo(self, patched_physics):
        base = _clear_sky_series()
        s0 = base.slots[_NOON_INDEX]
        thin = WeatherSlot(
            start=s0.start, ghi=s0.ghi, dni=s0.dni, dhi=s0.dhi,
            temp_c=s0.temp_c, snow_depth_m=0.005,  # below 0.01 threshold
        )
        site = _two_plane_site()
        dry = engine.compute_forecast(site, WeatherSeries(slots=(s0,)), now=_TEST_DATE)
        edge = engine.compute_forecast(
            site, WeatherSeries(slots=(thin,)), now=_TEST_DATE
        )
        assert edge.total_watts[0] == pytest.approx(dry.total_watts[0])

    def test_dst_style_local_bucketing_is_stable(self, patched_physics):
        # A tz with a fractional offset must not throw and must conserve energy.
        site = _two_plane_site()
        weather = _clear_sky_series()
        tz = timezone(timedelta(hours=5, minutes=30))  # e.g. IST
        res = engine.compute_forecast(site, weather, now=_TEST_DATE, tz=tz)
        assert sum(res.daily_kwh.values()) == pytest.approx(
            sum(res.hourly_wh.values()) / 1000.0
        )


# --------------------------------------------------------------------------
# Default operator site smoke test (still with analytic physics stand-ins)
# --------------------------------------------------------------------------


class TestDefaultSite:
    def test_default_site_runs_and_is_plausible(self, patched_physics):
        site = SiteConfig.from_dict(DEFAULT_SITE)
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)

        assert len(res.plane_results) == 8
        # Every WR pair stays within its 800 W AC clamp at all times.
        pairs = [("M1", "M2"), ("M3", "M4"), ("M5", "M6"), ("M7", "M8")]
        by_name = {p.name: p.watts for p in res.plane_results}
        for a, b in pairs:
            for i in range(len(weather)):
                assert by_name[a][i] + by_name[b][i] <= 800.0 + 1e-6
        # Non-trivial midday production overall.
        assert res.total_watts[_NOON_INDEX] > 0.0
        # Energy conserved between the two roll-ups.
        assert sum(res.daily_kwh.values()) == pytest.approx(
            sum(res.hourly_wh.values()) / 1000.0
        )

    def test_default_site_south_modules_cut_afternoon(self, patched_physics):
        # M4/M8 (az 205, S) are in the walled set -> beam gone after az 210.
        site = SiteConfig.from_dict(DEFAULT_SITE)
        weather = _clear_sky_series()
        res = engine.compute_forecast(site, weather, now=_TEST_DATE)
        m4 = next(p.watts for p in res.plane_results if p.name == "M4")

        cross = None
        for i in range(_NOON_INDEX, len(m4)):
            az, el = fake_sun_position(weather.slots[i].midpoint, 48.5, 12.2)
            if el > 0 and az >= 210.0:
                cross = i
                break
        assert cross is not None
        assert m4[cross] < m4[cross - 1]
