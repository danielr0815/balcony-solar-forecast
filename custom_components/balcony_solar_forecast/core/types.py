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
    DEFAULT_EFFICIENCY,
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
    """Per-plane forecast: aligned 15-min instantaneous DC power (W)."""

    name: str
    watts: tuple[float, ...]  # one value per weather slot, aligned to starts


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

    def with_total(self, total_watts: tuple[float, ...]) -> ForecastResult:
        """Return a copy with a replaced total (e.g. after a learner clamp)."""
        return replace(self, total_watts=total_watts)


def default_albedo() -> float:
    """Convenience re-export of the default ground albedo."""
    return ALBEDO_DEFAULT
