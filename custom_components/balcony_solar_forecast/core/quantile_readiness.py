"""Evidence summaries count positive target slots and known local label days."""
from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime, timedelta, tzinfo

from ..const import QUANTILE_RING_DAYS
from .types import QuantileState


def dated_evidence_days(state: QuantileState, as_of: date) -> int:
    days = set()
    for ring in state.bins.values():
        for entry in ring:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            stamp, value = entry
            if not isinstance(stamp, str) or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            try:
                day = date.fromisoformat(stamp)
            except ValueError:
                continue
            if math.isfinite(value) and as_of-timedelta(days=QUANTILE_RING_DAYS) <= day <= as_of:
                days.add(day)
    return len(days)


def positive_slot_readiness(slots: Iterable[tuple[datetime, float, bool]], *, tz: tzinfo) -> dict:
    by_day: dict[str, dict] = {}
    for start, watts, trained in slots:
        if not math.isfinite(watts) or watts <= 0:
            continue
        day = start.astimezone(tz).date().isoformat()
        counts = by_day.setdefault(day, {'positive_slots': 0, 'trained_slots': 0})
        counts['positive_slots'] = int(counts['positive_slots']) + 1
        counts['trained_slots'] = int(counts['trained_slots']) + int(trained)
    for counts in by_day.values():
        fraction = int(counts['trained_slots'])/int(counts['positive_slots'])
        counts['trained_fraction'] = fraction
        counts['state'] = 'cold' if fraction == 0 else 'trained' if fraction == 1 else 'partial'
    return by_day
