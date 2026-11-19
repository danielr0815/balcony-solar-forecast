"""Coverage checks for quarter-hour forecasts on the UTC time axis."""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from ..const import SLOT_MINUTES

UTC_HOUR_SLOT_OFFSETS = tuple(timedelta(minutes=i) for i in range(0, 60, SLOT_MINUTES))


def complete_utc_hours(
    slot_starts: Iterable[datetime],
    invalid_starts: Iterable[datetime] = (),
) -> set[datetime]:
    """Return UTC hours with all four expected starts and no invalid slots."""
    valid = {start.astimezone(UTC) for start in slot_starts}
    valid.difference_update(start.astimezone(UTC) for start in invalid_starts)
    candidates = {start for start in valid if not (start.minute or start.second or start.microsecond)}
    return {
        hour for hour in candidates
        if all(hour + offset in valid for offset in UTC_HOUR_SLOT_OFFSETS[1:])
    }
