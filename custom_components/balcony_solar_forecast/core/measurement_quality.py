"""Shared DC label boundaries for recorder, bootstrap and diagnostics."""
from __future__ import annotations

import math
from collections.abc import Sequence

from ..const import (
    COLLAPSE_FORECAST_MIN_WH,
    COLLAPSE_MEASURED_MAX_FRAC,
    LABEL_FROZEN_MIN_REPEATS,
)


def power_watts(value: object, unit: str = "W", *, signed: bool = False) -> float | None:
    """Finite nonnegative watts; energy units and boolean states are invalid."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        watts = float(value)
    except (ValueError, OverflowError):
        return None
    if unit not in ("W", "kW") or not math.isfinite(watts) or (watts < 0.0 and not signed):
        return None
    normalized = watts * (1000.0 if unit == "kW" else 1.0)
    return normalized if math.isfinite(normalized) else None


def dc_power(value: object, unit: str = "W") -> float | None:
    """DC power never permits a negative signed meter convention."""
    return power_watts(value, unit)


def frozen_nonzero(values: Sequence[float]) -> bool:
    """Repeated positive hourly means indicate a stuck channel, unlike zero."""
    run = 1
    for previous, current in zip(values, values[1:], strict=False):
        run = run + 1 if current == previous and current != 0.0 else 1
        if run >= LABEL_FROZEN_MIN_REPEATS:
            return True
    return False


def production_collapsed(forecast_wh: float, measured_wh: float) -> bool:
    """Quarantine negligible production only against a meaningful forecast."""
    return (forecast_wh >= COLLAPSE_FORECAST_MIN_WH
            and measured_wh < COLLAPSE_MEASURED_MAX_FRAC * forecast_wh)


def power_source_problem(attributes: dict) -> str | None:
    """Validate explicit source metadata without treating unavailable as zero.

    Absent metadata is unknown (e.g. a temporarily unavailable registered sensor),
    not proof of unsuitable units. Recorder access remains a separate runtime
    capability; a measurement state_class alone cannot guarantee retention.
    """
    if attributes.get('unit_of_measurement') not in (None, 'W', 'kW'):
        return 'invalid_power_source'
    if attributes.get('device_class') not in (None, 'power'):
        return 'invalid_power_source'
    if attributes.get('state_class') not in (None, 'measurement'):
        return 'invalid_power_source'
    return None
