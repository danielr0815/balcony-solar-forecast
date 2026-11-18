"""Evidence summaries count positive target slots and known local label days."""
from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime, tzinfo

from .quantiles import windowed_samples
from .types import QuantileState


def dated_evidence_days(state: QuantileState, as_of: date) -> int:
    return len({
        day
        for ring in state.bins.values()
        for day, _ in windowed_samples(ring, as_of_date=as_of.isoformat())
        if day
    })


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
