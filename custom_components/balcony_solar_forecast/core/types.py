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
    DRIFT_WINDOW_DAYS). ``fast_loss_streak`` / ``slow_loss_streak`` count
    consecutive days the corrected curve lost to physics for each layer; at
    DRIFT_LOSS_STREAK_DAYS the coordinator auto-disables that layer and raises
    a repair issue, setting ``fast_disabled`` / ``slow_disabled``. Disabled
    layers stay off until the user re-enables via the options flow (which
    clears the flag).
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
        return {
            "beam_wh": dict(self.beam_wh),
            "diffuse_wh": dict(self.diffuse_wh),
            "ghi": dict(self.ghi),
            "kc": dict(self.kc),
        }


@dataclass(frozen=True, slots=True)
class IssuedSnapshot:
    """The v2 forecast-as-issued snapshot (one per calendar day, SPEC §9).

    Stores BOTH hourly curves plus the per-plane modeled beam/diffuse/ghi/kc
    the shademap trainer needs. Round-trips through the issued ring in the
    store. ``version`` == 2 distinguishes it from the v1 issued dict (which had
    only ``hourly_wh`` / ``daily_kwh`` / ``status``); the store carries v1
    entries forward untouched and writes v2 going forward.
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
            version=_safe_int(d.get("version", 2), 2),
        )

    def to_dict(self) -> dict:
        return {
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
