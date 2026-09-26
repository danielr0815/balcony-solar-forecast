"""Energy aggregation of interval-mean forecast power, independent of HA."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo

from ..const import SLOT_HOURS


@dataclass(slots=True)
class EnergyTotals:
    """Owned hourly/daily views of one curve, accumulated in slot order.

    The producer alone mutates these mappings while building the forecast.
    Handing them to a ForecastResult ends that ownership: downstream readers
    must not mutate them. Skipped/missing slots do not introduce empty buckets.
    """

    tz: tzinfo = UTC
    hourly_wh: dict[str, float] = field(default_factory=dict)
    daily_kwh: dict[str, float] = field(default_factory=dict)

    def add(self, start: datetime, watts: float) -> None:
        """Add one interval mean, keeping UTC-hour and local-day totals aligned."""
        hkey = start.astimezone(UTC).replace(
            minute=0, second=0, microsecond=0,
        ).isoformat()
        day = start.astimezone(self.tz).date().isoformat()
        wh = watts * SLOT_HOURS
        self.hourly_wh[hkey] = self.hourly_wh.get(hkey, 0.0) + wh
        self.daily_kwh[day] = self.daily_kwh.get(day, 0.0) + wh / 1000.0
