"""BIT-EQUALITY proof for the audit-#9 engine double-pass refactor.

Plain pytest, no Home Assistant imports (SPEC §4).

Phase-D optimisation #1 replaced the per-plane/per-slot ``_plane_poa_split``
(run TWICE — once for the RAW static-tau curve and once for the CORRECTED
learned-tau curve) with a single shared decomposition ``_plane_poa_components``
plus a cheap per-tau gate ``_gate_split``. The refactor must be BIT-IDENTICAL:
the only thing that ever differed between the two curves is which transmittance
gates the beam, so factoring the tau-independent work out changes nothing.

Two independent proofs, both against a FROZEN verbatim copy of the pre-refactor
``engine._plane_poa_split`` embedded below (clearly marked; do NOT "improve" it):

  * ``TestSplitPrimitiveEquivalence`` — the split PRIMITIVE
    (``_plane_poa_components`` + ``_gate_split``, resolved exactly as the engine
    loop resolves the tau) is byte-for-byte the frozen monolith over thousands
    of seeded-random primitive inputs (real transpose -> ``cos_theta`` present,
    so the ASHRAE IAM path is exercised too).

  * ``TestComputeForecastEquivalence`` — the WHOLE ``compute_forecast`` output
    (every field: total/raw watts, per-plane series, beam/diffuse splits,
    hourly/daily roll-ups, band curves, kc, beam_ref/diffuse_ref) is identical
    between the real engine and a reference run that MONKEYPATCHES the engine's
    two split primitives to route through the frozen monolith. Randomised but
    SEEDED configs cover 2-3 planes, seasonal + static horizon rows, grouped and
    ungrouped planes, learners on/off, band factors and a factor>1 slot scalar
    (the AC re-clamp path). Equality is ``==`` (bit-exact), never approx.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from balcony_solar_forecast.core import engine
from balcony_solar_forecast.core.engine import LearnerHooks
from balcony_solar_forecast.core.types import (
    HorizonRow,
    InverterGroup,
    PlaneConfig,
    QuantileBands,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)

# ===========================================================================
# FROZEN pre-refactor reference — a VERBATIM copy of engine._plane_poa_split as
# it stood BEFORE the audit-#9 refactor. This is the ground truth the new
# split primitives are proven equal to. DO NOT refactor / tidy this: its whole
# value is being an unchanged snapshot of the old algorithm.
# ===========================================================================


def _frozen_old_plane_poa_split(
    plane, svf, slot, sun_az, sun_el, albedo, doy, beam_tau
):
    comps = engine.transpose.hay_davies_poa(
        ghi=slot.ghi,
        dni=slot.dni,
        dhi=slot.dhi,
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

    cos_theta = comps.get("cos_theta")
    if cos_theta is not None:
        f_iam = engine.transpose.ashrae_iam(cos_theta)
        beam *= f_iam
        circ *= f_iam

    horizon_elev = engine.horizon.interp_elevation(plane, sun_az)
    if sun_el <= horizon_elev:
        static_tau = engine.horizon.transmittance_at(plane, sun_az, doy)
    else:
        static_tau = 1.0

    beam_poa_ungated = beam + circ
    if beam_poa_ungated < 0.0:
        beam_poa_ungated = 0.0

    if beam_tau is not None:
        tau = beam_tau(plane.name, sun_az, sun_el, doy, static_tau)
    else:
        tau = static_tau

    if tau != 1.0:
        beam *= tau
        circ *= tau

    iso *= svf

    beam_poa = beam + circ
    if beam_poa < 0.0:
        beam_poa = 0.0
    diffuse_poa = iso + ground
    if diffuse_poa < 0.0:
        diffuse_poa = 0.0

    return engine._PlanePoaSplit(
        beam_poa=beam_poa,
        diffuse_poa=diffuse_poa,
        beam_poa_ungated=beam_poa_ungated,
    )


def _frozen_static_tau(plane, sun_az, sun_el, doy) -> float:
    """The frozen static-horizon prior (same lines the monolith used)."""
    horizon_elev = engine.horizon.interp_elevation(plane, sun_az)
    if sun_el <= horizon_elev:
        return engine.horizon.transmittance_at(plane, sun_az, doy)
    return 1.0


# ---------------------------------------------------------------------------
# New split as the ENGINE LOOP resolves it (components once, gate per tau).
# ---------------------------------------------------------------------------


def _new_split_via_engine(plane, svf, slot, sun_az, sun_el, albedo, doy, beam_tau):
    comps = engine._plane_poa_components(
        plane, svf, slot, sun_az, sun_el, albedo, doy
    )
    if beam_tau is not None:
        tau = beam_tau(plane.name, sun_az, sun_el, doy, comps.static_tau)
    else:
        tau = comps.static_tau
    return engine._gate_split(comps, tau)


# ---------------------------------------------------------------------------
# Reference primitives for the compute_forecast-level proof: the engine loop
# calls ``_plane_poa_components`` once + ``_gate_split`` per tau; we monkeypatch
# both so their composition IS the frozen monolith (a constant-tau hook injects
# the loop-resolved tau into the verbatim old function).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RefComps:
    plane: PlaneConfig
    svf: float
    slot: WeatherSlot
    sun_az: float
    sun_el: float
    albedo: float
    doy: int
    static_tau: float


def _ref_components(plane, svf, slot, sun_az, sun_el, albedo, doy):
    return _RefComps(
        plane, svf, slot, sun_az, sun_el, albedo, doy,
        _frozen_static_tau(plane, sun_az, sun_el, doy),
    )


def _ref_gate_split(comps: _RefComps, tau: float):
    # Feed the loop-resolved tau back through the verbatim old monolith via a
    # constant hook, reproducing the pre-refactor double-pass exactly.
    return _frozen_old_plane_poa_split(
        comps.plane, comps.svf, comps.slot, comps.sun_az, comps.sun_el,
        comps.albedo, comps.doy, beam_tau=lambda *a: tau,
    )


# ===========================================================================
# Bit-equality assertions over the full ForecastResult
# ===========================================================================


def _assert_result_bit_equal(a, b) -> None:
    assert a.slot_starts == b.slot_starts
    assert a.total_watts == b.total_watts
    assert a.raw_total_watts == b.raw_total_watts
    assert a.hourly_wh == b.hourly_wh
    assert a.raw_hourly_wh == b.raw_hourly_wh
    assert a.daily_kwh == b.daily_kwh
    assert a.raw_daily_kwh == b.raw_daily_kwh
    assert a.correction_source == b.correction_source
    assert a.p10_watts == b.p10_watts
    assert a.p50_watts == b.p50_watts
    assert a.p90_watts == b.p90_watts
    assert a.p10_hourly_wh == b.p10_hourly_wh
    assert a.p50_hourly_wh == b.p50_hourly_wh
    assert a.p90_hourly_wh == b.p90_hourly_wh
    assert len(a.plane_results) == len(b.plane_results)
    for pa, pb in zip(a.plane_results, b.plane_results, strict=True):
        assert pa.name == pb.name
        assert pa.watts == pb.watts
        assert pa.raw_watts == pb.raw_watts
        assert pa.beam_watts == pb.beam_watts
        assert pa.diffuse_watts == pb.diffuse_watts
        assert pa.kc == pb.kc
        assert pa.beam_ref_watts == pb.beam_ref_watts
        assert pa.diffuse_ref_watts == pb.diffuse_ref_watts


# ===========================================================================
# Seeded random config / weather / hook generators
# ===========================================================================

_BASE = datetime(2024, 6, 21, 3, 0, tzinfo=UTC)  # summer, real sun path


def _rand_horizon(rng: random.Random) -> tuple[HorizonRow, ...]:
    """0-3 sorted horizon rows, a mix of static and seasonal (foliage)."""
    n = rng.randint(0, 3)
    if n == 0:
        return ()
    azs = sorted(rng.sample(range(0, 360, 10), n))
    rows = []
    for az in azs:
        el = rng.uniform(0.0, 45.0)
        tau = rng.uniform(0.0, 1.0)
        if rng.random() < 0.4:
            bare = rng.uniform(0.4, 1.0)
            leafed = rng.uniform(0.0, bare)
            rows.append(
                HorizonRow(float(az), el, leafed, seasonal=True,
                           tau_leafed=leafed, tau_bare=bare)
            )
        else:
            rows.append(HorizonRow(float(az), el, tau))
    return tuple(rows)


def _rand_site(rng: random.Random) -> SiteConfig:
    n_planes = rng.randint(2, 3)
    planes = []
    for i in range(n_planes):
        planes.append(
            PlaneConfig(
                name=f"P{i}",
                azimuth_deg=rng.uniform(0.0, 360.0),
                tilt_deg=rng.uniform(5.0, 89.0),
                wp=rng.uniform(150.0, 600.0),
                efficiency=rng.uniform(0.15, 0.22),
                horizon=_rand_horizon(rng),
                ross_coeff=(None if rng.random() < 0.5
                            else rng.uniform(0.02, 0.056)),
            )
        )
    # Groups: sometimes group the first two planes under a tight-ish AC limit so
    # the clamp (and, with a factor>1 hook, the re-clamp) actually bites.
    groups: tuple[InverterGroup, ...] = ()
    if rng.random() < 0.7 and n_planes >= 2:
        limit = rng.choice([300.0, 500.0, 800.0])
        groups = (
            InverterGroup(name="WR", plane_names=("P0", "P1"), ac_limit_w=limit),
        )
    return SiteConfig(
        latitude=48.5, longitude=12.2, planes=tuple(planes), groups=groups
    )


def _rand_weather(rng: random.Random, n: int = 44) -> WeatherSeries:
    """A daytime window with a noon-peaked clear-ish bump + noise + gaps/snow."""
    slots = []
    for i in range(n):
        frac = (i - n / 2) / (n / 2)
        bump = max(0.0, math.cos(frac * (math.pi / 2.0)))
        ghi = 950.0 * bump * rng.uniform(0.7, 1.0)
        dni = 820.0 * bump * rng.uniform(0.6, 1.0)
        dhi = 130.0 * bump * rng.uniform(0.7, 1.3)
        temp = rng.uniform(-2.0, 32.0)
        snow = 0.0 if rng.random() < 0.85 else rng.uniform(0.0, 0.2)
        start = _BASE + timedelta(minutes=15 * i)
        # Occasionally punch a None gap (missing-weather path).
        if rng.random() < 0.05:
            slots.append(
                WeatherSlot(start=start, ghi=None, dni=dni, dhi=dhi, temp_c=temp)
            )
        else:
            slots.append(
                WeatherSlot(start=start, ghi=ghi, dni=dni, dhi=dhi,
                            temp_c=temp, snow_depth_m=snow)
            )
    return WeatherSeries(slots=tuple(slots))


def _hook_variants(rng: random.Random, weather: WeatherSeries) -> list[LearnerHooks]:
    """A spread of learner-hook combos, incl. factor>1 (AC re-clamp) + bands."""

    def echo(channel, sun_az, sun_el, doy, static_prior):
        return static_prior

    def darken(channel, sun_az, sun_el, doy, static_prior):
        return 0.3

    def brighten(channel, sun_az, sun_el, doy, static_prior):
        return 1.1  # SHADEMAP_TAU_MAX: learned-clearer-than-static

    def wall(channel, sun_az, sun_el, doy, static_prior):
        return 0.0 if (channel == "P0" and sun_az >= 180.0) else static_prior

    now = _BASE + timedelta(hours=3)

    def decaying(slot_start):
        age = (slot_start - now).total_seconds() / 60.0
        if age < 0.0 or age >= 360.0:
            return 1.0
        return 1.5 + (1.0 - 1.5) * (age / 360.0)

    # A non-neutral band on a handful of slots (exercises the band-curve path +
    # the AC-ceiling cap for P90 > 1).
    bands = {}
    for slot in weather.slots[: len(weather.slots) // 2]:
        bands[slot.start] = QuantileBands(p10=0.8, p50=1.0, p90=1.25, n=50)

    return [
        LearnerHooks(),  # neutral -> corrected == raw
        LearnerHooks(beam_tau=echo, slot_factor=lambda s: 1.0),  # identity echo
        LearnerHooks(beam_tau=darken),  # shademap only
        LearnerHooks(slot_factor=lambda s: 1.5),  # factor>1: AC re-clamp path
        LearnerHooks(beam_tau=wall, slot_factor=decaying),  # both, intraday decay
        LearnerHooks(beam_tau=brighten, slot_factor=lambda s: 1.3,
                     band_by_slot=bands),  # both + factor>1 + bands
        LearnerHooks(band_by_slot=bands),  # bands only
    ]


# ===========================================================================
# Proof 1: the split PRIMITIVE is byte-for-byte the frozen monolith.
# ===========================================================================


class TestSplitPrimitiveEquivalence:
    def test_gate_split_matches_frozen_monolith_random(self):
        rng = random.Random(20260710)
        hooks = {
            "none": None,
            "echo": lambda ch, az, el, doy, sp: sp,
            "darken": lambda ch, az, el, doy, sp: 0.3,
            "bright": lambda ch, az, el, doy, sp: 1.1,
            "half": lambda ch, az, el, doy, sp: 0.5 * sp,
            "wall": lambda ch, az, el, doy, sp: 0.0,
        }
        for _ in range(4000):
            plane = PlaneConfig(
                name="X",
                azimuth_deg=rng.uniform(0.0, 360.0),
                tilt_deg=rng.uniform(0.0, 90.0),
                wp=rng.uniform(100.0, 600.0),
                horizon=_rand_horizon(rng),
            )
            slot = WeatherSlot(
                start=_BASE,
                ghi=rng.uniform(0.0, 1100.0),
                dni=rng.uniform(0.0, 950.0),
                dhi=rng.uniform(0.0, 300.0),
                temp_c=rng.uniform(-5.0, 35.0),
            )
            sun_az = rng.uniform(0.0, 360.0)
            sun_el = rng.uniform(-5.0, 80.0)
            albedo = rng.choice([0.2, 0.5])
            doy = rng.randint(1, 366)
            beam_tau = hooks[rng.choice(list(hooks))]

            # SVF is just a scalar input to both splits; a random value (incl. the
            # 1.0 boundary) proves the gate arithmetic without paying for the real
            # O(360) quadrature 4000x — the SVF impl is covered by test_horizon.
            svf = rng.choice([1.0, rng.random()])
            expected = _frozen_old_plane_poa_split(
                plane, svf, slot, sun_az, sun_el, albedo, doy, beam_tau
            )
            got = _new_split_via_engine(
                plane, svf, slot, sun_az, sun_el, albedo, doy, beam_tau
            )
            # Bit-exact: identical field floats (dataclass eq is field-wise ==).
            assert got == expected
            assert got.beam_poa == expected.beam_poa
            assert got.diffuse_poa == expected.diffuse_poa
            assert got.beam_poa_ungated == expected.beam_poa_ungated


# ===========================================================================
# Proof 2: the WHOLE compute_forecast output is identical to a reference run
# whose split primitives route through the frozen monolith.
# ===========================================================================


class TestComputeForecastEquivalence:
    @pytest.mark.parametrize("seed", [1, 2, 3, 7, 11, 42, 123, 777, 2026, 99999])
    def test_every_output_field_bit_identical(self, seed, monkeypatch):
        rng = random.Random(seed)
        site = _rand_site(rng)
        weather = _rand_weather(rng)
        tz = UTC
        for hooks in _hook_variants(rng, weather):
            # Real engine (shared components + per-tau gate).
            real = engine.compute_forecast(
                site, weather, now=_BASE, tz=tz, hooks=hooks
            )
            # Reference engine: monkeypatch the two split primitives so their
            # composition is the frozen pre-refactor monolith (double-pass).
            with monkeypatch.context() as m:
                m.setattr(engine, "_plane_poa_components", _ref_components)
                m.setattr(engine, "_gate_split", _ref_gate_split)
                ref = engine.compute_forecast(
                    site, weather, now=_BASE, tz=tz, hooks=hooks
                )
            _assert_result_bit_equal(real, ref)

    def test_neutral_hooks_corrected_equals_raw_bit_exact(self):
        """Sanity within this suite: with no hooks the two curves coincide."""
        rng = random.Random(555)
        site = _rand_site(rng)
        weather = _rand_weather(rng)
        res = engine.compute_forecast(site, weather, now=_BASE, tz=UTC)
        assert res.total_watts == res.raw_total_watts
        assert res.hourly_wh == res.raw_hourly_wh
        for pr in res.plane_results:
            assert pr.watts == pr.raw_watts
