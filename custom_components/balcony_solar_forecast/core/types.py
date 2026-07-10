"""Immutable data contracts for the pure forecast core.

This module imports NOTHING from Home Assistant. Everything here is a plain,
frozen dataclass over plain Python data so the physics core is testable with
bare pytest (SPEC §4).

Conventions (all internal):
  - Azimuth 0 = North, clockwise (90 = East, 180 = South).
  - Tilt: degrees from horizontal (90 = vertical).
  - Time: timezone-aware UTC datetimes; 15-min slots; slot values are
    interval means (Open-Meteo backward-averaged); sun position uses the
    slot midpoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from ..const import (
    ALBEDO_DEFAULT,
    CONF_ACTUAL_ENTITY,
    CONF_AZIMUTH,
    CONF_EFFICIENCY,
    CONF_GROUP_AC_LIMIT,
    CONF_GROUP_NAME,
    CONF_GROUP_PLANES,
    CONF_GROUPS,
    CONF_HORIZON,
    CONF_HZ_AZIMUTH,
    CONF_HZ_ELEVATION,
    CONF_HZ_SEASONAL,
    CONF_HZ_TAU,
    CONF_HZ_TAU_BARE,
    CONF_HZ_TAU_LEAFED,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PLANE_NAME,
    CONF_PLANES,
    CONF_TILT,
    CONF_WP,
    DAY_AHEAD_BIAS_MAX,
    DAY_AHEAD_BIAS_MIN,
    DAY_AHEAD_BIAS_NEUTRAL,
    DEFAULT_DAY_AHEAD_BIAS_ENABLED,
    DEFAULT_EFFICIENCY,
    DEFAULT_FAST_LEARNER_ENABLED,
    DEFAULT_SLOW_LEARNER_ENABLED,
    RLS_INIT_COVARIANCE,
    SHADEMAP_TAU_MAX,
    SHADEMAP_TAU_MIN,
)

__all__ = [
    "HorizonRow",
    "PlaneConfig",
    "InverterGroup",
    "SiteConfig",
    "WeatherSlot",
    "WeatherSeries",
    "PlaneResult",
    "ForecastResult",
    # learning contract (v0.2.0 + v0.3.0)
    "LearnerConfig",
    "PlaneSlotBreakdown",
    "BiasCell",
    "BiasState",
    "ShademapBin",
    "ShademapState",
    "DriftState",
    "LearnerSnapshot",
    "IssuedSnapshot",
    "PlaneHourlyModeled",
    # v0.4 contract: scoreboard + quantiles
    "ComparisonConfig",
    "DayScore",
    "ScoreboardState",
    "QuantileBands",
    "QuantileState",
]


@dataclass(frozen=True, slots=True)
class HorizonRow:
    """One breakpoint of a plane's horizon profile.

    ``elevation_deg`` is the horizon-line elevation at ``azimuth_deg``
    (0=N clockwise). Values between rows are linearly interpolated. ``tau``
    is the beam transmittance (0 = opaque, 1 = clear) applied to
    beam+circumsolar when the sun sits below this line.

    When ``seasonal`` is True the effective tau ramps between ``tau_bare``
    (winter/leafless) and ``tau_leafed`` (summer) via a cosine foliage ramp
    (SPEC §13); ``tau`` then holds the leafed value as a static fallback.
    """

    azimuth_deg: float
    elevation_deg: float
    tau: float
    seasonal: bool = False
    tau_leafed: float | None = None
    tau_bare: float | None = None

    @classmethod
    def from_dict(cls, d: dict) -> HorizonRow:
        return cls(
            azimuth_deg=float(d[CONF_HZ_AZIMUTH]),
            elevation_deg=float(d[CONF_HZ_ELEVATION]),
            tau=float(d[CONF_HZ_TAU]),
            seasonal=bool(d.get(CONF_HZ_SEASONAL, False)),
            tau_leafed=(
                None if d.get(CONF_HZ_TAU_LEAFED) is None
                else float(d[CONF_HZ_TAU_LEAFED])
            ),
            tau_bare=(
                None if d.get(CONF_HZ_TAU_BARE) is None
                else float(d[CONF_HZ_TAU_BARE])
            ),
        )

    def to_dict(self) -> dict:
        d: dict = {
            CONF_HZ_AZIMUTH: self.azimuth_deg,
            CONF_HZ_ELEVATION: self.elevation_deg,
            CONF_HZ_TAU: self.tau,
        }
        if self.seasonal:
            d[CONF_HZ_SEASONAL] = True
            if self.tau_leafed is not None:
                d[CONF_HZ_TAU_LEAFED] = self.tau_leafed
            if self.tau_bare is not None:
                d[CONF_HZ_TAU_BARE] = self.tau_bare
        return d


@dataclass(frozen=True, slots=True)
class PlaneConfig:
    """A single module plane (one MPPT / measurement channel).

    ``horizon`` is kept sorted by ascending azimuth (validated at the config
    boundary). ``actual_entity`` is the HA entity id of the measured DC power
    for this plane; it is opaque to the pure core (used only by the logger).
    """

    name: str
    azimuth_deg: float  # 0=N clockwise
    tilt_deg: float  # from horizontal, 90 = vertical
    wp: float  # STC peak power, watts
    efficiency: float = DEFAULT_EFFICIENCY
    horizon: tuple[HorizonRow, ...] = ()
    actual_entity: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> PlaneConfig:
        horizon = tuple(
            HorizonRow.from_dict(r) for r in d.get(CONF_HORIZON, [])
        )
        return cls(
            name=str(d[CONF_PLANE_NAME]),
            azimuth_deg=float(d[CONF_AZIMUTH]),
            tilt_deg=float(d[CONF_TILT]),
            wp=float(d[CONF_WP]),
            efficiency=float(d.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)),
            horizon=horizon,
            actual_entity=d.get(CONF_ACTUAL_ENTITY),
        )

    def to_dict(self) -> dict:
        return {
            CONF_PLANE_NAME: self.name,
            CONF_AZIMUTH: self.azimuth_deg,
            CONF_TILT: self.tilt_deg,
            CONF_WP: self.wp,
            CONF_EFFICIENCY: self.efficiency,
            CONF_HORIZON: [r.to_dict() for r in self.horizon],
            CONF_ACTUAL_ENTITY: self.actual_entity,
        }


@dataclass(frozen=True, slots=True)
class InverterGroup:
    """One inverter with a shared AC clamp over its member planes (ports)."""

    name: str
    plane_names: tuple[str, ...]
    ac_limit_w: float

    @classmethod
    def from_dict(cls, d: dict) -> InverterGroup:
        return cls(
            name=str(d[CONF_GROUP_NAME]),
            plane_names=tuple(str(p) for p in d.get(CONF_GROUP_PLANES, [])),
            ac_limit_w=float(d[CONF_GROUP_AC_LIMIT]),
        )

    def to_dict(self) -> dict:
        return {
            CONF_GROUP_NAME: self.name,
            CONF_GROUP_PLANES: list(self.plane_names),
            CONF_GROUP_AC_LIMIT: self.ac_limit_w,
        }


@dataclass(frozen=True, slots=True)
class SiteConfig:
    """Full editable site: location + planes + inverter groups.

    Round-trips through ``from_dict``/``to_dict`` for the config-flow object
    selector. ``actual_entity`` lives on each plane (see PlaneConfig).
    """

    latitude: float
    longitude: float
    planes: tuple[PlaneConfig, ...]
    groups: tuple[InverterGroup, ...]

    @classmethod
    def from_dict(cls, d: dict) -> SiteConfig:
        return cls(
            latitude=float(d[CONF_LATITUDE]),
            longitude=float(d[CONF_LONGITUDE]),
            planes=tuple(
                PlaneConfig.from_dict(p) for p in d.get(CONF_PLANES, [])
            ),
            groups=tuple(
                InverterGroup.from_dict(g) for g in d.get(CONF_GROUPS, [])
            ),
        )

    def to_dict(self) -> dict:
        return {
            CONF_LATITUDE: self.latitude,
            CONF_LONGITUDE: self.longitude,
            CONF_PLANES: [p.to_dict() for p in self.planes],
            CONF_GROUPS: [g.to_dict() for g in self.groups],
        }

    def plane_by_name(self, name: str) -> PlaneConfig | None:
        """Return the plane with ``name`` or None."""
        for p in self.planes:
            if p.name == name:
                return p
        return None


@dataclass(frozen=True, slots=True)
class WeatherSlot:
    """One 15-min weather sample; irradiance values are interval means.

    Hourly fields (clouds, visibility, snow) are carried forward from the
    hourly Open-Meteo arrays onto each 15-min slot by the fetcher.
    """

    start: datetime  # slot start, tz-aware UTC (value = mean over [start, start+15min))
    ghi: float  # global horizontal irradiance, W/m^2
    dni: float  # direct normal irradiance, W/m^2
    dhi: float  # diffuse horizontal irradiance, W/m^2
    temp_c: float  # 2 m air temperature, deg C
    cloud_low: float = 0.0  # %
    cloud_mid: float = 0.0  # %
    cloud_high: float = 0.0  # %
    visibility_m: float = 0.0  # m
    snowfall_cm: float = 0.0  # cm (hourly)
    snow_depth_m: float = 0.0  # m

    @property
    def midpoint(self) -> datetime:
        """Slot midpoint (used for sun position)."""
        from datetime import timedelta

        return self.start + timedelta(minutes=7, seconds=30)


@dataclass(frozen=True, slots=True)
class WeatherSeries:
    """Ordered 15-min weather slots for the whole forecast window."""

    slots: tuple[WeatherSlot, ...]

    def __len__(self) -> int:
        return len(self.slots)

    def __iter__(self):
        return iter(self.slots)


@dataclass(frozen=True, slots=True)
class PlaneResult:
    """Per-plane forecast: aligned 15-min instantaneous DC power (W).

    ``watts`` is the CORRECTED (learner-applied) per-plane clamped power, kept
    as the primary field so every existing caller (coordinator plane_watts,
    tests) is unchanged. The additive fields below carry the raw physics
    breakdown the SLOW learner (shademap) needs to train the beam-referenced
    transmittance ``T = (P_measured - P_diffuse_modeled) / P_beam_modeled``
    (SPEC §5) and the attribution diagnostics (raw vs corrected, SPEC §9).
    All additive fields default to empty so v0.1 constructions still work.

      - ``raw_watts``: pure-physics per-plane clamped power (learner OFF).
      - ``beam_watts`` / ``diffuse_watts``: modeled DC power attributable to
        the beam+circumsolar vs. the diffuse+ground POA components, pre-clamp
        (the shademap references beam only; diffuse is the shade floor).
      - ``kc``: per-slot clear-sky index at the sun position (learner gate).

    All aligned to the ForecastResult ``slot_starts``.
    """

    name: str
    watts: tuple[float, ...]  # CORRECTED per-plane clamped power, aligned to starts
    raw_watts: tuple[float, ...] = ()      # pure-physics per-plane clamped power
    beam_watts: tuple[float, ...] = ()     # modeled DC from beam+circumsolar POA
    diffuse_watts: tuple[float, ...] = ()  # modeled DC from diffuse+ground POA
    kc: tuple[float, ...] = ()             # clear-sky index per slot (gate)
    # --- SLOW-learner training reference (SPEC §5, FIX-3) ---
    # The shademap trains T = (P_measured - P_diffuse) / P_beam where the beam
    # reference must be the UNGATED, unclamped, un-factored beam+circumsolar DC
    # (raw physics with static tau := 1). Sourcing the trainer from the gated /
    # clamped / slot-factored ``beam_watts`` above would make T self-referential
    # (learned T ≈ true_t / applied_tau, whose fixed point is sqrt(true_t)) and
    # leave a wall bin (static tau 0) with ~0 modeled beam, untrainable. These
    # two series mirror scripts/backfill.reconstruct_plane_hour exactly.
    beam_ref_watts: tuple[float, ...] = ()     # UNGATED beam+circumsolar DC (pre-clamp, no slot factor)
    diffuse_ref_watts: tuple[float, ...] = ()  # raw diffuse+ground DC floor (same reference frame)


@dataclass(frozen=True, slots=True)
class ForecastResult:
    """Engine output: aligned 15-min power plus hourly energy roll-ups.

    ``slot_starts`` are the tz-aware UTC 15-min slot starts every power list
    is aligned to. ``total_watts`` is the AC-clamped site total. Hourly Wh
    dicts are keyed by ISO-8601 UTC hour start (for the energy sensors and
    the ``async_get_solar_forecast`` hook).
    """

    slot_starts: tuple[datetime, ...]
    total_watts: tuple[float, ...]
    plane_results: tuple[PlaneResult, ...]
    hourly_wh: dict[str, float]  # {iso_utc_hour: Wh} site total
    daily_kwh: dict[str, float] = field(default_factory=dict)  # {iso_date: kWh}
    # --- Dual-curve attribution (v0.2.0 + v0.3.0, SPEC §5/§9) ---
    # ``total_watts`` / ``hourly_wh`` / ``daily_kwh`` above are the CORRECTED
    # (served) curve. The additive fields below carry the pure-physics RAW
    # curve so the coordinator can snapshot BOTH nightly, expose raw-vs-
    # corrected MAE in diagnostics, and the drift monitor can compare the
    # served curve against physics. All default empty so a v0.1 engine build
    # (raw == corrected, learners off) round-trips unchanged.
    raw_total_watts: tuple[float, ...] = ()          # pure-physics site total
    raw_hourly_wh: dict[str, float] = field(default_factory=dict)  # {iso_hour: Wh}
    raw_daily_kwh: dict[str, float] = field(default_factory=dict)  # {iso_date: kWh}
    # Which learner layer(s) shaped ``total_watts`` this cycle
    # (const.CORRECTION_SOURCE_*). Empty string == not yet set by the engine.
    correction_source: str = ""
    # --- Quantile bands (v0.4, SPEC §6/§10) ---
    # Plane-agnostic TOTAL site power band curves, aligned to ``slot_starts``,
    # in the SAME 15-min instantaneous-watts frame as ``total_watts``. The
    # engine fills these only when a QuantileState is injected via hooks;
    # otherwise they stay empty and every consumer treats "no band" as
    # band == corrected (P50 == corrected, no fabricated spread). ``p50_watts``
    # is the empirical-median-multiplied curve, which need NOT equal
    # ``total_watts`` (a bin's P50 multiplier can differ from 1.0); consumers
    # that want the raw served curve keep using ``total_watts``.
    p10_watts: tuple[float, ...] = ()
    p50_watts: tuple[float, ...] = ()
    p90_watts: tuple[float, ...] = ()
    # Hourly Wh roll-ups of the three band curves (keyed by ISO-8601 UTC hour),
    # mirroring ``hourly_wh`` for the corrected curve. Empty when no bands.
    p10_hourly_wh: dict[str, float] = field(default_factory=dict)
    p50_hourly_wh: dict[str, float] = field(default_factory=dict)
    p90_hourly_wh: dict[str, float] = field(default_factory=dict)

    def with_total(self, total_watts: tuple[float, ...]) -> ForecastResult:
        """Return a copy with a replaced total (e.g. after a learner clamp)."""
        return replace(self, total_watts=total_watts)


def default_albedo() -> float:
    """Convenience re-export of the default ground albedo."""
    return ALBEDO_DEFAULT


# ===========================================================================
# LEARNING CONTRACT dataclasses (v0.2.0 + v0.3.0 — SPEC §5, §6, §7, §9)
# ---------------------------------------------------------------------------
# All frozen, plain-JSON (de)serialisable, HA-free. Owners (bias / shademap /
# engine / store / coordinator) share ONLY these types + the const tunables.
# Every load path is validate-and-clamp: a corrupt blob yields a neutral
# state (factors 1.0 / empty bins), NEVER a raised exception (SPEC §5 "Store
# validate-and-clamp beim Laden — korrupt => Faktoren 1,0, nie Setup-Crash").
# ===========================================================================


def _clamp(v: float, lo: float, hi: float) -> float:
    """Clamp ``v`` into [lo, hi]; NaN/inf-safe (returns lo on non-finite)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return lo
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf guard
        return lo
    return lo if f < lo else hi if f > hi else f


def _safe_int(v: object, default: int = 0, *, minimum: int | None = None) -> int:
    """Coerce ``v`` to an int, returning ``default`` on any garbage.

    The validate-and-clamp contract (SPEC §5) forbids a raised exception on
    load: a corrupt blob with a string / NaN / None where an int belongs must
    degrade to the default, NEVER propagate a ValueError up through
    ``store.validate_state`` into setup. ``minimum`` (when given) floors the
    result so a negative count can never leak in.
    """
    try:
        i = int(v)
    except (TypeError, ValueError):
        i = default
    if i != i:  # pragma: no cover - int() never returns NaN, defensive only
        i = default
    if minimum is not None and i < minimum:
        return minimum
    return i


def _safe_float(v: object, default: float = 0.0, *, minimum: float | None = None) -> float:
    """Coerce ``v`` to a finite float, returning ``default`` on any garbage.

    Same validate-and-clamp guarantee as :func:`_safe_int` for the covariance /
    MAE scalars that would otherwise call bare ``float()`` on a string / NaN and
    crash setup.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    if minimum is not None and f < minimum:
        return minimum
    return f


def _quantile_entries_to_pairs(entries: object) -> list:
    """Normalise a QuantileState bin's entries to ``[iso_date, relerr]`` pairs.

    Tolerant, no-clamp companion to ``QuantileState.from_dict``: a bare number
    (legacy un-dated sample) becomes ``["", number]``, a well-formed pair is
    copied through (a non-str date coerced to ``""``), and anything else is
    dropped. Used by ``to_dict`` so a directly-constructed state carrying bare
    floats still serialises to the canonical pair shape. Values are NOT re-clamped
    here (that is the load-time contract); this only fixes the container shape.
    """
    out: list = []
    if not isinstance(entries, (list, tuple)):
        return out
    for x in entries:
        if isinstance(x, (list, tuple)):
            if len(x) != 2 or not isinstance(x[1], (int, float)):
                continue
            iso = x[0] if isinstance(x[0], str) else ""
            out.append([iso, x[1]])
        elif isinstance(x, (int, float)):
            out.append(["", x])
    return out


@dataclass(frozen=True, slots=True)
class LearnerConfig:
    """Resolved per-entry learner enable flags (options-flow kill switches).

    Built by the coordinator from ``{**entry.data, **entry.options}`` and
    handed to the engine so learner application is a pure function of config +
    persisted state. Defaults come from const (both learners ON — operator
    decision 2026-07-06). ``fast_enabled`` gates the intraday scalar;
    ``day_ahead_enabled`` gates the nightly RLS bias; ``slow_enabled`` gates
    the shademap. A drift-auto-disabled or collapse-frozen layer is expressed
    at RUNTIME (DriftState / collapse detector), NOT here — this is the
    user-facing switch only.
    """

    fast_enabled: bool = DEFAULT_FAST_LEARNER_ENABLED
    slow_enabled: bool = DEFAULT_SLOW_LEARNER_ENABLED
    day_ahead_enabled: bool = DEFAULT_DAY_AHEAD_BIAS_ENABLED

    @classmethod
    def from_dict(cls, d: dict) -> LearnerConfig:
        from ..const import (
            CONF_DAY_AHEAD_BIAS_ENABLED,
            CONF_FAST_LEARNER_ENABLED,
            CONF_SLOW_LEARNER_ENABLED,
        )
        if not isinstance(d, dict):
            return cls()
        return cls(
            fast_enabled=bool(
                d.get(CONF_FAST_LEARNER_ENABLED, DEFAULT_FAST_LEARNER_ENABLED)
            ),
            slow_enabled=bool(
                d.get(CONF_SLOW_LEARNER_ENABLED, DEFAULT_SLOW_LEARNER_ENABLED)
            ),
            day_ahead_enabled=bool(
                d.get(CONF_DAY_AHEAD_BIAS_ENABLED, DEFAULT_DAY_AHEAD_BIAS_ENABLED)
            ),
        )

    def to_dict(self) -> dict:
        from ..const import (
            CONF_DAY_AHEAD_BIAS_ENABLED,
            CONF_FAST_LEARNER_ENABLED,
            CONF_SLOW_LEARNER_ENABLED,
        )
        return {
            CONF_FAST_LEARNER_ENABLED: self.fast_enabled,
            CONF_SLOW_LEARNER_ENABLED: self.slow_enabled,
            CONF_DAY_AHEAD_BIAS_ENABLED: self.day_ahead_enabled,
        }


@dataclass(frozen=True, slots=True)
class PlaneSlotBreakdown:
    """One plane's modeled irradiance breakdown for one 15-min slot.

    The engine emits this per plane per slot so the SLOW learner can train the
    beam-referenced transmittance and the intraday learner can normalise in
    k_c space (SPEC §5). ``beam_dc_w`` / ``diffuse_dc_w`` are the modeled DC
    power split (pre-AC-clamp) attributable to beam+circumsolar vs.
    diffuse+ground POA; their sum is the plane's unclamped DC power.
    ``sun_az`` / ``sun_el`` are the slot-midpoint sun position (0=N internal);
    ``kc`` is the clear-sky index; ``beam_share`` is the modeled beam POA as a
    fraction of the plane Wp (the >5% quasi-clear gate).
    """

    beam_dc_w: float
    diffuse_dc_w: float
    sun_az: float
    sun_el: float
    kc: float
    beam_share: float


# ---------------------------------------------------------------------------
# FAST learner: day-ahead RLS bias (intraday scalar is NEVER persisted, so it
# has no dataclass here — it lives transiently in the coordinator/engine and
# re-inits to 1.0 on restart, SPEC §5).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BiasCell:
    """One recursive-least-squares scalar-bias cell (SPEC §5 day-ahead).

    A single-parameter RLS estimator of the multiplicative bias for one
    (cloud class x day part) cell: ``theta`` is the current estimate (applied,
    clamped to [DAY_AHEAD_BIAS_MIN, MAX]); ``covariance`` is the scalar RLS P;
    ``n`` counts trained days (cells with n < RLS_MIN_SAMPLES apply neutral).
    """

    theta: float = DAY_AHEAD_BIAS_NEUTRAL
    covariance: float = RLS_INIT_COVARIANCE
    n: int = 0

    def clamped_theta(self) -> float:
        """The applied bias: theta clamped into the day-ahead band."""
        return _clamp(self.theta, DAY_AHEAD_BIAS_MIN, DAY_AHEAD_BIAS_MAX)

    @classmethod
    def from_dict(cls, d: dict) -> BiasCell:
        if not isinstance(d, dict):
            return cls()
        return cls(
            theta=_clamp(
                d.get("theta", DAY_AHEAD_BIAS_NEUTRAL),
                DAY_AHEAD_BIAS_MIN,
                DAY_AHEAD_BIAS_MAX,
            ),
            covariance=_safe_float(
                d.get("covariance", RLS_INIT_COVARIANCE), RLS_INIT_COVARIANCE,
                minimum=0.0,
            ),
            n=_safe_int(d.get("n", 0), 0, minimum=0),
        )

    def to_dict(self) -> dict:
        return {"theta": self.theta, "covariance": self.covariance, "n": self.n}


@dataclass(frozen=True, slots=True)
class BiasState:
    """Day-ahead RLS bias: {cell_key: BiasCell}, cell_key = "class|part".

    Cell key format is exactly ``f"{cloud_class}|{day_part}"`` using the const
    CLOUD_CLASS_* / DAY_PART_* string values (e.g. "fog|morning"). Missing
    cells apply neutral. ``version`` guards forward-compat; unknown versions on
    load are discarded to an empty state.
    """

    cells: dict[str, BiasCell] = field(default_factory=dict)
    version: int = 1

    @staticmethod
    def cell_key(cloud_class: str, day_part: str) -> str:
        return f"{cloud_class}|{day_part}"

    def get_bias(self, cloud_class: str, day_part: str) -> float:
        """Applied (clamped) bias for a cell, neutral if untrained/missing."""
        from ..const import DAY_AHEAD_BIAS_NEUTRAL, RLS_MIN_SAMPLES
        cell = self.cells.get(self.cell_key(cloud_class, day_part))
        if cell is None or cell.n < RLS_MIN_SAMPLES:
            return DAY_AHEAD_BIAS_NEUTRAL
        return cell.clamped_theta()

    @classmethod
    def from_dict(cls, d: dict) -> BiasState:
        if not isinstance(d, dict):
            return cls()
        cells_raw = d.get("cells", {})
        cells: dict[str, BiasCell] = {}
        if isinstance(cells_raw, dict):
            for k, v in cells_raw.items():
                if isinstance(k, str):
                    cells[k] = BiasCell.from_dict(v)
        return cls(cells=cells, version=_safe_int(d.get("version", 1), 1))

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "cells": {k: c.to_dict() for k, c in self.cells.items()},
        }


# ---------------------------------------------------------------------------
# SLOW learner: shademap
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShademapBin:
    """One EMA cell of beam-referenced transmittance (SPEC §5 slow learner).

    ``tau`` is the learned transmittance (clamped [SHADEMAP_TAU_MIN, MAX]);
    ``n`` is the effective sample count driving the cold-start shrinkage
    ``w = n / (n + SHADEMAP_SHRINKAGE_K)`` toward the static horizon prior.
    Backfilled bins have ``n`` capped at BOOTSTRAP_MAX_BIN_N so live data
    overrides quickly (SPEC §6).
    """

    tau: float
    n: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> ShademapBin:
        if not isinstance(d, dict):
            return cls(tau=SHADEMAP_TAU_MAX, n=0)
        return cls(
            tau=_clamp(d.get("tau", SHADEMAP_TAU_MAX), SHADEMAP_TAU_MIN, SHADEMAP_TAU_MAX),
            n=_safe_int(d.get("n", 0), 0, minimum=0),
        )

    def to_dict(self) -> dict:
        return {"tau": self.tau, "n": self.n}


@dataclass(frozen=True, slots=True)
class ShademapState:
    """Per-channel shademap: {channel: {bin_key: ShademapBin}}.

    ``channel`` is the plane / measurement-channel name (PlaneConfig.name).
    ``bin_key`` encodes the (sun-azimuth x sun-elevation x half-year) bin as a
    canonical string produced by :func:`shademap_bin_key` (owner: shademap):
    ``f"{az_idx}:{el_idx}:{half}"`` where az_idx = floor(sun_az /
    SHADEMAP_AZ_BIN_DEG), el_idx = floor(sun_el / SHADEMAP_EL_BIN_DEG) and
    half in {0,1} (0 = before summer solstice, 1 = after). The learned tau
    REPLACES the static horizon tau of the matched bin, blended by shrinkage
    against the static prior for that bin's centre azimuth (SPEC §5).
    """

    channels: dict[str, dict[str, ShademapBin]] = field(default_factory=dict)
    version: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> ShademapState:
        if not isinstance(d, dict):
            return cls()
        chans_raw = d.get("channels", {})
        channels: dict[str, dict[str, ShademapBin]] = {}
        if isinstance(chans_raw, dict):
            for chan, bins in chans_raw.items():
                if not isinstance(chan, str) or not isinstance(bins, dict):
                    continue
                channels[chan] = {
                    bk: ShademapBin.from_dict(bv)
                    for bk, bv in bins.items()
                    if isinstance(bk, str)
                }
        return cls(channels=channels, version=_safe_int(d.get("version", 1), 1))

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "channels": {
                chan: {bk: b.to_dict() for bk, b in bins.items()}
                for chan, bins in self.channels.items()
            },
        }


# ---------------------------------------------------------------------------
# Guards: drift monitor state + rollback snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DriftState:
    """Rolling drift-monitor state per learner layer (SPEC §5).

    ``daily_mae`` holds recent per-day daylight MAE triples keyed by ISO date:
    ``{iso_date: {"raw": mae, "corrected": mae, "baseline": mae}}`` (trimmed to
    DRIFT_WINDOW_DAYS), plus an optional ``"slow"`` key on days whose snapshot
    carried a slow-only curve (per-layer attribution, audit #13b).
    ``fast_loss_streak`` / ``slow_loss_streak`` count consecutive days each
    layer lost — the fast (day-ahead) layer on corrected-vs-slow-only, the slow
    (shademap) layer on slow-only-vs-physics; at DRIFT_LOSS_STREAK_DAYS the
    coordinator auto-disables that layer and raises a repair issue, setting
    ``fast_disabled`` / ``slow_disabled``. Disabled layers stay off until the
    user re-enables via the options flow (which clears the flag).
    """

    daily_mae: dict[str, dict[str, float]] = field(default_factory=dict)
    fast_loss_streak: int = 0
    slow_loss_streak: int = 0
    fast_disabled: bool = False
    slow_disabled: bool = False
    # Last-seen option value at the previous rebuild (the OFF->ON transition
    # detector's memory, SPEC §5). ``None`` == never recorded (legacy blob /
    # pre-upgrade): a rebuild with all-default options must NOT be treated as a
    # user re-enable, so a drift auto-disable survives a restart untouched.
    fast_option_seen: bool | None = None
    slow_option_seen: bool | None = None
    # ISO local date the collapse detector froze the geometric learners for
    # (snow / total dropout). Persisted so a mid-day restart keeps the freeze.
    collapse_frozen_date: str | None = None
    version: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> DriftState:
        if not isinstance(d, dict):
            return cls()
        mae_raw = d.get("daily_mae", {})
        daily_mae: dict[str, dict[str, float]] = {}
        if isinstance(mae_raw, dict):
            for k, v in mae_raw.items():
                if isinstance(k, str) and isinstance(v, dict):
                    daily_mae[k] = {
                        kk: _safe_float(vv)
                        for kk, vv in v.items()
                        if isinstance(kk, str) and isinstance(vv, (int, float))
                    }
        cf = d.get("collapse_frozen_date")
        return cls(
            daily_mae=daily_mae,
            fast_loss_streak=_safe_int(d.get("fast_loss_streak", 0), 0, minimum=0),
            slow_loss_streak=_safe_int(d.get("slow_loss_streak", 0), 0, minimum=0),
            fast_disabled=bool(d.get("fast_disabled", False)),
            slow_disabled=bool(d.get("slow_disabled", False)),
            fast_option_seen=(
                None if d.get("fast_option_seen") is None
                else bool(d.get("fast_option_seen"))
            ),
            slow_option_seen=(
                None if d.get("slow_option_seen") is None
                else bool(d.get("slow_option_seen"))
            ),
            collapse_frozen_date=(str(cf) if isinstance(cf, str) and cf else None),
            version=_safe_int(d.get("version", 1), 1),
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "daily_mae": {k: dict(v) for k, v in self.daily_mae.items()},
            "fast_loss_streak": self.fast_loss_streak,
            "slow_loss_streak": self.slow_loss_streak,
            "fast_disabled": self.fast_disabled,
            "slow_disabled": self.slow_disabled,
            "fast_option_seen": self.fast_option_seen,
            "slow_option_seen": self.slow_option_seen,
            "collapse_frozen_date": self.collapse_frozen_date,
        }


@dataclass(frozen=True, slots=True)
class LearnerSnapshot:
    """One rollback snapshot of the persisted learner state (SPEC §5).

    A date-stamped copy of BiasState + ShademapState taken by the nightly job
    BEFORE it applies that night's training, so a drifting layer can be rolled
    back to a prior good state. The coordinator keeps the last
    DRIFT_ROLLBACK_SNAPSHOTS of these in a ring.
    """

    taken_at: str  # iso utc
    bias: BiasState
    shademap: ShademapState

    @classmethod
    def from_dict(cls, d: dict) -> LearnerSnapshot:
        if not isinstance(d, dict):
            return cls(taken_at="", bias=BiasState(), shademap=ShademapState())
        return cls(
            taken_at=str(d.get("taken_at", "")),
            bias=BiasState.from_dict(d.get("bias", {})),
            shademap=ShademapState.from_dict(d.get("shademap", {})),
        )

    def to_dict(self) -> dict:
        return {
            "taken_at": self.taken_at,
            "bias": self.bias.to_dict(),
            "shademap": self.shademap.to_dict(),
        }


# ---------------------------------------------------------------------------
# Issued snapshot v2 (attribution) — SPEC §9, operator decision 2026-07-06
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlaneHourlyModeled:
    """Per-plane per-hour modeled curves stored in the issued snapshot v2.

    Enables training the shademap from HOURLY long-term statistics (the
    backfill and the nightly LTS path both work at hourly resolution, SPEC §6).
    Each dict is keyed by ISO-8601 UTC hour start.
      - ``beam_wh`` / ``diffuse_wh``: modeled DC energy split for the plane;
      - ``ghi_wh`` proxy and ``kc``: the mean clear-sky index that hour, so the
        quasi-clear gate can be reconstructed offline.
    """

    beam_wh: dict[str, float] = field(default_factory=dict)
    diffuse_wh: dict[str, float] = field(default_factory=dict)
    ghi: dict[str, float] = field(default_factory=dict)
    kc: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> PlaneHourlyModeled:
        if not isinstance(d, dict):
            return cls()

        def _fd(key: str) -> dict[str, float]:
            v = d.get(key, {})
            if not isinstance(v, dict):
                return {}
            return {k: float(x) for k, x in v.items()
                    if isinstance(k, str) and isinstance(x, (int, float))}

        return cls(
            beam_wh=_fd("beam_wh"),
            diffuse_wh=_fd("diffuse_wh"),
            ghi=_fd("ghi"),
            kc=_fd("kc"),
        )

    def to_dict(self) -> dict:
        # Store trim: omit EMPTY curves entirely. ``from_dict`` treats a missing
        # key as {}, so this is round-trip safe — and it stops serializing the
        # vestigial ``ghi`` dict (never populated by the coordinator) into every
        # plane of every snapshot of the 90-day issued ring.
        out: dict = {}
        for key, curve in (
            ("beam_wh", self.beam_wh),
            ("diffuse_wh", self.diffuse_wh),
            ("ghi", self.ghi),
            ("kc", self.kc),
        ):
            if curve:
                out[key] = dict(curve)
        return out


@dataclass(frozen=True, slots=True)
class IssuedSnapshot:
    """The v2 forecast-as-issued snapshot (one per calendar day, SPEC §9).

    Stores BOTH hourly curves plus the per-plane modeled beam/diffuse/ghi/kc
    the shademap trainer needs. Round-trips through the issued ring in the
    store. ``version`` == 2 distinguishes it from the v1 issued dict (which had
    only ``hourly_wh`` / ``daily_kwh`` / ``status``); the store carries v1
    entries forward untouched and writes v2 going forward.

    ``slow_only_hourly_wh`` (audit #13b) is the hourly Wh curve with ONLY the
    SLOW layer (shademap beam_tau) applied — no day-ahead factor — so the drift
    monitor can decompose ``corrected = slow ∘ fast`` and attribute a losing day
    to the guilty layer. It is written only when the slow layer was active (else
    it equals raw and is omitted); an empty value means the monitor falls back
    to the legacy shared corrected-vs-raw signal.
    """

    issued_at: str  # iso utc
    status: str
    raw_hourly_wh: dict[str, float] = field(default_factory=dict)
    corrected_hourly_wh: dict[str, float] = field(default_factory=dict)
    raw_daily_kwh: dict[str, float] = field(default_factory=dict)
    corrected_daily_kwh: dict[str, float] = field(default_factory=dict)
    per_plane: dict[str, PlaneHourlyModeled] = field(default_factory=dict)
    # Forecast cloud class per ISO-UTC hour (SPEC §5 day-ahead conditioning): so
    # the nightly RLS trainer can key the (cloud class x day part) cell on the
    # ACTUAL forecast weather, not a fixed "clear" label. Empty on legacy/v0.1.
    cloud_class_by_hour: dict[str, str] = field(default_factory=dict)
    # Slow-only (shademap ∘ physics, NO day-ahead factor) hourly Wh curve for the
    # drift monitor's per-layer attribution (audit #13b). Empty on legacy/v0.1 or
    # a slow-inactive day (slow-only == raw); the monitor then uses the legacy
    # shared signal.
    slow_only_hourly_wh: dict[str, float] = field(default_factory=dict)
    version: int = 2

    @classmethod
    def from_dict(cls, d: dict) -> IssuedSnapshot:
        if not isinstance(d, dict):
            return cls(issued_at="", status="")

        def _fd(key: str) -> dict[str, float]:
            v = d.get(key, {})
            if not isinstance(v, dict):
                return {}
            return {k: float(x) for k, x in v.items()
                    if isinstance(k, str) and isinstance(x, (int, float))}

        per_plane_raw = d.get("per_plane", {})
        per_plane: dict[str, PlaneHourlyModeled] = {}
        if isinstance(per_plane_raw, dict):
            for k, v in per_plane_raw.items():
                if isinstance(k, str):
                    per_plane[k] = PlaneHourlyModeled.from_dict(v)

        cloud_raw = d.get("cloud_class_by_hour", {})
        cloud_class_by_hour: dict[str, str] = {}
        if isinstance(cloud_raw, dict):
            cloud_class_by_hour = {
                k: str(v) for k, v in cloud_raw.items()
                if isinstance(k, str) and isinstance(v, str)
            }

        return cls(
            issued_at=str(d.get("issued_at", "")),
            status=str(d.get("status", "")),
            raw_hourly_wh=_fd("raw_hourly_wh"),
            corrected_hourly_wh=_fd("corrected_hourly_wh"),
            raw_daily_kwh=_fd("raw_daily_kwh"),
            corrected_daily_kwh=_fd("corrected_daily_kwh"),
            per_plane=per_plane,
            cloud_class_by_hour=cloud_class_by_hour,
            slow_only_hourly_wh=_fd("slow_only_hourly_wh"),
            version=_safe_int(d.get("version", 2), 2),
        )

    def to_dict(self) -> dict:
        out: dict = {
            "version": self.version,
            "issued_at": self.issued_at,
            "status": self.status,
            "raw_hourly_wh": dict(self.raw_hourly_wh),
            "corrected_hourly_wh": dict(self.corrected_hourly_wh),
            "raw_daily_kwh": dict(self.raw_daily_kwh),
            "corrected_daily_kwh": dict(self.corrected_daily_kwh),
            "per_plane": {k: v.to_dict() for k, v in self.per_plane.items()},
            "cloud_class_by_hour": dict(self.cloud_class_by_hour),
        }
        # Store trim: write the slow-only curve ONLY when non-empty (slow layer
        # was active). Empty == slow-only == raw, and ``from_dict`` reads a
        # missing key as {}, so omitting it keeps the round-trip exact while
        # avoiding a second full copy of the raw curve in every snapshot of the
        # 90-day issued ring.
        if self.slow_only_hourly_wh:
            out["slow_only_hourly_wh"] = dict(self.slow_only_hourly_wh)
        return out


# ===========================================================================
# v0.4 CONTRACT dataclasses — SKILL SCOREBOARD + QUANTILES (SPEC §6, §9, §10)
# ---------------------------------------------------------------------------
# All frozen, plain-JSON (de)serialisable, HA-free. Owners (quantiles /
# scoreboard / store / coordinator / sensor / diagnostics) share ONLY these
# types + the const tunables. Every load path is validate-and-clamp: a corrupt
# blob yields a neutral state (empty rings / neutral 1.0 bands), NEVER a raised
# exception (SPEC §5 validate-and-clamp-on-load, extended to v0.4 sections).
# ===========================================================================


@dataclass(frozen=True, slots=True)
class ComparisonConfig:
    """One configured external comparison forecast (SPEC §9/§10 scoreboard).

    GENERIC + CONFIGURABLE (D-P9): ``name`` is the operator-chosen label and
    ``daily_entity`` is the HA sensor whose STATE is that comparison's daily-kWh
    forecast for today. The scoreboard reads its RECORDER HISTORY for yesterday
    (the value AS IT STOOD during yesterday — no leakage), never its live state.
    Built by the coordinator from ``CONF_COMPARISON_SENSORS`` (ships EMPTY; the
    operator's two comparisons are documented, not hardcoded).

    ``slug`` is a stable, filesystem/entity-safe derivation of ``name`` used to
    suffix the per-comparison MAE sensor object_id and to key the comparison
    ring; it is derived deterministically so a rename produces a new sensor
    rather than silently rewriting an old one's history.
    """

    name: str
    daily_entity: str

    @property
    def slug(self) -> str:
        """Lowercase ASCII-alnum slug of ``name`` (stable sensor/ring key).

        STRICTLY ASCII: a non-ASCII letter ("Süd") becomes a separator — the
        slug is embedded in the sensor's unique_id AND its pre-set entity_id,
        where non-ASCII characters are invalid. Keeping this aligned with HA's
        own slugify boundary means the documented dashboard id
        ``…_comparison_daily_kwh_mae_<slug>`` always names the real entity.
        """
        out = []
        for ch in self.name.strip().lower():
            if ch.isascii() and ch.isalnum():
                out.append(ch)
            elif out and out[-1] != "_":
                out.append("_")
        slug = "".join(out).strip("_")
        return slug or "comparison"

    @classmethod
    def from_dict(cls, d: dict) -> ComparisonConfig:
        from ..const import CONF_COMPARISON_DAILY_ENTITY, CONF_COMPARISON_NAME

        if not isinstance(d, dict):
            return cls(name="", daily_entity="")
        return cls(
            name=str(d.get(CONF_COMPARISON_NAME, "")),
            daily_entity=str(d.get(CONF_COMPARISON_DAILY_ENTITY, "")),
        )

    def to_dict(self) -> dict:
        from ..const import CONF_COMPARISON_DAILY_ENTITY, CONF_COMPARISON_NAME

        return {
            CONF_COMPARISON_NAME: self.name,
            CONF_COMPARISON_DAILY_ENTITY: self.daily_entity,
        }

    @staticmethod
    def list_from_options(raw: object) -> tuple[ComparisonConfig, ...]:
        """Parse the CONF_COMPARISON_SENSORS options list into configs.

        Skips malformed rows and rows missing a name or a daily_entity so a
        half-filled options row never yields a scoreboard column that can never
        be scored. Never raises (validate-and-clamp).
        """
        if not isinstance(raw, list):
            return ()
        out: list[ComparisonConfig] = []
        for row in raw:
            cfg = ComparisonConfig.from_dict(row) if isinstance(row, dict) else None
            if cfg is not None and cfg.name and cfg.daily_entity:
                out.append(cfg)
        return tuple(out)


@dataclass(frozen=True, slots=True)
class DayScore:
    """One scored day in the rolling scoreboard window (SPEC §9/§10).

    All errors are ABSOLUTE daily-kWh deviations from the measured site energy
    for ``iso_date`` (the operator's primary metric, B9). NO-LEAKAGE contract:
      * ``engine_kwh`` is the engine forecast AS ISSUED for this date (read from
        the issued ring's snapshot logged during that day) — NEVER recomputed
        with today's learned state;
      * each value in ``comparison_kwh`` is that comparison entity's own value
        AS IT STOOD during this date (read from its recorder history) — NEVER
        today's live state;
      * ``measured_kwh`` is the sum of the per-module actuals in the actuals
        ring for this date.

    ``engine_daily_abs_err`` = |engine_kwh - measured_kwh|;
    ``comparison_daily_abs_err`` maps ``{comparison_name: |cmp_kwh - measured|}``
    (only names that had a usable recorded value that day — a missing comparison
    is absent, not zero). ``engine_hourly_mae`` is the engine's mean absolute
    per-daylight-hour Wh error for the day (issued corrected hourly vs measured
    hourly), or None when hourly actuals were unavailable. ``weather_class`` is
    the day's DOMINANT class (const CLOUD_CLASS_*), used for stratification.
    """

    iso_date: str
    weather_class: str
    measured_kwh: float
    engine_kwh: float
    engine_daily_abs_err: float
    comparison_kwh: dict[str, float] = field(default_factory=dict)
    comparison_daily_abs_err: dict[str, float] = field(default_factory=dict)
    engine_hourly_mae: float | None = None

    @classmethod
    def from_dict(cls, d: dict) -> DayScore:
        if not isinstance(d, dict):
            return cls(
                iso_date="", weather_class="", measured_kwh=0.0,
                engine_kwh=0.0, engine_daily_abs_err=0.0,
            )

        def _fmap(key: str) -> dict[str, float]:
            v = d.get(key, {})
            if not isinstance(v, dict):
                return {}
            return {
                k: _safe_float(x)
                for k, x in v.items()
                if isinstance(k, str) and isinstance(x, (int, float))
            }

        hmae = d.get("engine_hourly_mae")
        return cls(
            iso_date=str(d.get("iso_date", "")),
            weather_class=str(d.get("weather_class", "")),
            measured_kwh=_safe_float(d.get("measured_kwh", 0.0), minimum=0.0),
            engine_kwh=_safe_float(d.get("engine_kwh", 0.0), minimum=0.0),
            engine_daily_abs_err=_safe_float(
                d.get("engine_daily_abs_err", 0.0), minimum=0.0
            ),
            comparison_kwh=_fmap("comparison_kwh"),
            comparison_daily_abs_err=_fmap("comparison_daily_abs_err"),
            engine_hourly_mae=(
                None if hmae is None else _safe_float(hmae, minimum=0.0)
            ),
        )

    def to_dict(self) -> dict:
        return {
            "iso_date": self.iso_date,
            "weather_class": self.weather_class,
            "measured_kwh": self.measured_kwh,
            "engine_kwh": self.engine_kwh,
            "engine_daily_abs_err": self.engine_daily_abs_err,
            "comparison_kwh": dict(self.comparison_kwh),
            "comparison_daily_abs_err": dict(self.comparison_daily_abs_err),
            "engine_hourly_mae": self.engine_hourly_mae,
        }


@dataclass(frozen=True, slots=True)
class ScoreboardState:
    """Rolling window of scored days + the kill-gate verdict (SPEC §9/§10).

    ``days`` maps ``{iso_date: DayScore}``; the store/scoreboard trims it to the
    configured window (SCOREBOARD_WINDOW_DAYS). Aggregates (engine daily-kWh MAE,
    per-comparison daily-kWh MAE, engine hourly MAE, engine_vs_best_baseline_pct
    and the per-weather-stratum breakdown) are DERIVED by ``core/scoreboard.py``
    from ``days`` — they are NOT stored here (single source of truth is the day
    ring), so a window-length change re-aggregates cleanly. ``version`` guards
    forward-compat; unknown versions on load discard to an empty state.
    """

    days: dict[str, DayScore] = field(default_factory=dict)
    version: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> ScoreboardState:
        if not isinstance(d, dict):
            return cls()
        days_raw = d.get("days", {})
        days: dict[str, DayScore] = {}
        if isinstance(days_raw, dict):
            for k, v in days_raw.items():
                if isinstance(k, str):
                    days[k] = DayScore.from_dict(v)
        return cls(days=days, version=_safe_int(d.get("version", 1), 1))

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "days": {k: v.to_dict() for k, v in self.days.items()},
        }


@dataclass(frozen=True, slots=True)
class QuantileBands:
    """Empirical P10/P50/P90 multipliers for one (weather class x day part) bin.

    Multiplicative factors applied to the corrected forecast Wh of an hour in
    this bin: ``p50`` is the empirical median relative error, ``p10`` / ``p90``
    the 10th / 90th percentiles (SPEC §6). ``n`` is the sample count backing the
    bin. COLD START (SPEC §6/§10 "no fake spread"): a bin with n <
    QUANTILE_MIN_SAMPLES collapses to P50 (p10 == p50 == p90), and an empty bin
    is the neutral identity (1.0/1.0/1.0). Always p10 <= p50 <= p90 by
    construction (the producer sorts them). This is a DERIVED view emitted by
    ``core/quantiles.py`` from the persisted QuantileState ring; it is not itself
    persisted.
    """

    p10: float = 1.0
    p50: float = 1.0
    p90: float = 1.0
    n: int = 0

    @property
    def collapsed(self) -> bool:
        """True when the band carries no spread (cold start / empty bin)."""
        return self.p10 == self.p50 == self.p90

    @classmethod
    def neutral(cls) -> QuantileBands:
        """The identity band (P50 == 1.0, no spread)."""
        from ..const import QUANTILE_NEUTRAL_MULT as m

        return cls(p10=m, p50=m, p90=m, n=0)


@dataclass(frozen=True, slots=True)
class QuantileState:
    """90-day ring of hourly relative errors keyed by (weather class x day part).

    ``bins`` maps ``{bin_key: [[iso_date, relerr], ...]}`` where ``bin_key`` is
    exactly ``f"{cloud_class}|{day_part}"`` (the SAME cell key as BiasState —
    const CLOUD_CLASS_* / DAY_PART_* — so the scoreboard, day-ahead bias and
    quantiles all share one bin taxonomy). Each entry is a ``[iso_date, relerr]``
    pair: ``relerr`` = measured_wh / corrected_forecast_wh for one sampled
    daylight hour (clamped to [QUANTILE_REL_ERR_MIN, QUANTILE_REL_ERR_MAX]) and
    ``iso_date`` is the trained day's ISO date, so the ring can be DATE-WINDOWED
    to QUANTILE_RING_DAYS and the collapse gate can count distinct days, not just
    correlated hours (owner: quantiles / store). ``bands(bin_key)`` (in
    quantiles.py) computes the empirical QuantileBands with the cold-start
    collapse rule (SPEC §6).

    LEGACY tolerance: ``from_dict`` accepts a bare number per entry (a pre-fix,
    un-dated sample) and normalises it to ``["", relerr]`` (empty date == unknown
    age: never date-trimmed, and it feeds the day gate only via a per-day-cap
    lower bound). Malformed entries are dropped. ``to_dict`` always writes the
    pair form. Readers (quantiles.bands_for_bin / train_quantiles) additionally
    tolerate a bare number at read time, so a directly-constructed QuantileState
    (bare floats, e.g. in tests) keeps working without going through from_dict.

    Persisted in the store (STORE_KEY_QUANTILE_STATE). Validate-and-clamp on
    load: a corrupt blob yields empty bins (no bands -> band collapses to P50).
    """

    bins: dict[str, list] = field(default_factory=dict)
    version: int = 1

    @staticmethod
    def bin_key(cloud_class: str, day_part: str) -> str:
        """Canonical (weather class x day part) key — matches BiasState."""
        return f"{cloud_class}|{day_part}"

    @classmethod
    def from_dict(cls, d: dict) -> QuantileState:
        from ..const import QUANTILE_REL_ERR_MAX, QUANTILE_REL_ERR_MIN

        if not isinstance(d, dict):
            return cls()
        bins_raw = d.get("bins", {})
        bins: dict[str, list] = {}
        if isinstance(bins_raw, dict):
            for k, v in bins_raw.items():
                if not isinstance(k, str) or not isinstance(v, list):
                    continue
                entries: list = []
                for x in v:
                    if isinstance(x, (list, tuple)):
                        # Dated pair [iso_date, relerr]; a non-str date (or any
                        # 2-tuple whose first element isn't a date) is undated.
                        if len(x) != 2 or not isinstance(x[1], (int, float)):
                            continue
                        iso = x[0] if isinstance(x[0], str) else ""
                        entries.append(
                            [iso, _clamp(x[1], QUANTILE_REL_ERR_MIN, QUANTILE_REL_ERR_MAX)]
                        )
                    elif isinstance(x, (int, float)):
                        # LEGACY bare number -> undated pair.
                        entries.append(
                            ["", _clamp(x, QUANTILE_REL_ERR_MIN, QUANTILE_REL_ERR_MAX)]
                        )
                    # else: malformed entry -> dropped
                if entries:
                    bins[k] = entries
        return cls(bins=bins, version=_safe_int(d.get("version", 1), 1))

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "bins": {k: _quantile_entries_to_pairs(v) for k, v in self.bins.items()},
        }
