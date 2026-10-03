"""Geometric coverage targets on canonical UTC recorder-hour identities."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from . import solpos


def daylight_hour_keys(latitude: float, longitude: float, start: datetime, end: datetime) -> set[str]:
    """Enumerate hour starts in a local-day window, evaluating UTC midpoints.

    A nonintegral timezone offset must not move the recorder's physical hour
    midpoint to :45/:00. DST changes the UTC window, never hour identities.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError('Coverage requires aware local-day boundaries')
    first, last = start.astimezone(UTC), end.astimezone(UTC)
    if not first < last or last-first > timedelta(hours=26):
        raise ValueError('Coverage window must be one local calendar day')
    step = timedelta(hours=1)
    cur = first.replace(minute=0, second=0, microsecond=0)
    if cur < first:
        cur += step
    keys = set()
    while cur < last:
        _, elevation = solpos.sun_position(cur+timedelta(minutes=30), latitude, longitude)
        if elevation > 0:
            keys.add(cur.isoformat())
        cur += step
    return keys
