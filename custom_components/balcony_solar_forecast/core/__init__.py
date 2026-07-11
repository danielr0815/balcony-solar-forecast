"""Pure, Home-Assistant-free forecast core.

Nothing in this package imports from ``homeassistant``; everything is pure
functions over the plain data types in ``types.py``, testable with bare
pytest (SPEC §4). This module re-exports the stable public API the HA glue
and the tests depend on.
"""

from __future__ import annotations

from .clearsky import clear_sky_index, haurwitz_ghi
from .electrical import clamp_groups, clamp_groups_ac, dc_power
from .engine import LearnerHooks, compute_forecast
from .horizon import interp_elevation, sky_view_factor, transmittance_at
from .shadeprofile import compute_shade_profile
from .solpos import sun_position
from .transpose import hay_davies_poa
from .types import (
    BiasCell,
    BiasState,
    ComparisonConfig,
    DayScore,
    DriftState,
    ForecastResult,
    HorizonRow,
    InverterGroup,
    IssuedSnapshot,
    LearnerConfig,
    LearnerSnapshot,
    PlaneConfig,
    PlaneHourlyModeled,
    PlaneResult,
    PlaneSlotBreakdown,
    QuantileBands,
    QuantileState,
    ScoreboardState,
    ShademapBin,
    ShademapState,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)

__all__ = [
    # types
    "HorizonRow",
    "PlaneConfig",
    "InverterGroup",
    "SiteConfig",
    "WeatherSlot",
    "WeatherSeries",
    "PlaneResult",
    "ForecastResult",
    # learning contract types (v0.2.0 + v0.3.0)
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
    # v0.4 contract types: scoreboard + quantiles
    "ComparisonConfig",
    "DayScore",
    "ScoreboardState",
    "QuantileBands",
    "QuantileState",
    # solar position
    "sun_position",
    # clear sky
    "haurwitz_ghi",
    "clear_sky_index",
    # transposition
    "hay_davies_poa",
    # horizon
    "interp_elevation",
    "transmittance_at",
    "sky_view_factor",
    # shade profile (visualisation)
    "compute_shade_profile",
    # electrical
    "dc_power",
    "clamp_groups",
    "clamp_groups_ac",
    # engine
    "compute_forecast",
    "LearnerHooks",
]
