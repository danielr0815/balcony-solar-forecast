"""Recorder platform: keep the bulky forecast-curve attributes out of history.

The energy sensors carry the full 15-min ``watts`` / ``wh_period`` dicts as
attributes so cards and consumers can render the whole curve from one entity.
Those dicts change every recompute and would bloat the recorder database; the
recorder ``exclude_attributes`` hook drops them from stored state history
(pattern copied from rany2/ha-open-meteo-solar-forecast). The live state and
attributes are unaffected -- only what gets written to history is trimmed.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback

from .const import (
    ATTR_SP_AZIMUTH,
    ATTR_SP_HORIZON_AZIMUTH,
    ATTR_SP_SHADE_HORIZON,
    ATTR_SP_STATIC_HORIZON,
    ATTR_SP_SUN_ELEVATION,
    ATTR_SP_TIME,
    ATTR_SP_TRANSMITTANCE,
    ATTR_WATTS,
    ATTR_WH_PERIOD,
)


@callback
def exclude_attributes(hass: HomeAssistant) -> set[str]:
    """Attribute names the recorder must not persist for this integration.

    The energy sensors' 15-min curve dicts and the shade-profile diagram's curve
    arrays change every recompute / selection and would bloat the database; only
    what gets written to history is trimmed (live state/attributes are intact).
    """
    return {
        ATTR_WATTS,
        ATTR_WH_PERIOD,
        ATTR_SP_TIME,
        ATTR_SP_AZIMUTH,
        ATTR_SP_SUN_ELEVATION,
        ATTR_SP_TRANSMITTANCE,
        ATTR_SP_HORIZON_AZIMUTH,
        ATTR_SP_STATIC_HORIZON,
        ATTR_SP_SHADE_HORIZON,
    }
