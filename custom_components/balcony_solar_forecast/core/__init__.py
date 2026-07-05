"""Pure, Home-Assistant-free forecast core.

Nothing in this package imports from ``homeassistant``; everything is pure
functions over the plain data types in ``types.py``, testable with bare
pytest (SPEC §4). This module re-exports the stable public API the HA glue
and the tests depend on.
"""

from __future__ import annotations

from .clearsky import clear_sky_index, haurwitz_ghi
from .electrical import clamp_groups, dc_power
from .engine import compute_forecast
from .horizon import interp_elevation, sky_view_factor, transmittance_at
from .solpos import sun_position
from .transpose import hay_davies_poa
from .types import (
    ForecastResult,
    HorizonRow,
    InverterGroup,
    PlaneConfig,
    PlaneResult,
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
    # electrical
    "dc_power",
    "clamp_groups",
    # engine
    "compute_forecast",
]
