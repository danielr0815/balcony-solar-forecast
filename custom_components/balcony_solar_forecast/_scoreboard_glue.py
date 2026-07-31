"""Nightly skill-scoreboard scorer — SPEC §15.2.

Owner: scoreboard (glue). The LEAK-FREE IO around the pure ``core/scoreboard.py``
math: score each closed local day's engine forecast AS ISSUED (from the issued
ring) against the measured site energy (from the actuals ring), then persist a
DayScore into the rolling window.

Every function takes the coordinator as ``coord`` and reads exactly the same
attributes the methods did (``coord._store`` / ``coord._site`` /
``coord._scoreboard_state`` / …); the persistence and the shared nightly helpers
(``_store_hourly_actuals`` / ``_site_measured_hourly``) stay on the coordinator
and are reached back through ``coord``. The coordinator exposes each of these as
a 1-2 line delegate (the tests build it via ``__new__`` and call the delegates
directly).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from ._glue_util import _filter_hourly_to_local_day
from .const import (
    CLOUD_CLASS_CLEAR,
)
from .core import IssuedSnapshot, ScoreboardState
from .core import scoreboard as scoreboard_mod

_LOGGER = logging.getLogger(__name__)

# Scoreboard leakage guard: a day is only scored when its issued snapshot was
# logged BEFORE this local-hour cutoff of the scored day. A snapshot issued
# later (e.g. a mid-day startup catch-up recomputed from a fresh Open-Meteo
# fetch that has assimilated the scored day's observed weather) is a
# hindcast/nowcast, not a day-ahead forecast, and must not flatter the engine.
_SCOREBOARD_ISSUE_CUTOFF_HOUR = 6


async def score_scoreboard_day(coord, day: date) -> None:
    """Score one closed local ``day`` into the rolling scoreboard window.

    NO-LEAKAGE (SPEC §15.2):
      * the ENGINE number is the forecast AS ISSUED for ``day`` — read from
        the issued ring's snapshot logged during that day (the CORRECTED
        served curve, sliced to the local day), NEVER recomputed with today's
        learned state;
      * the MEASURED number is the sum of the per-module actuals in the
        actuals ring for ``day``.
    Idempotent + date-keyed: a day already present in the ring is re-scored
    (engine + measured are deterministic from the rings, so a re-run is a
    no-op).
    """
    if not coord._scoreboard_enabled:
        return
    iso = day.isoformat()
    issued = coord._store.get_issued(iso)
    actuals = coord._store.get_actuals(iso)
    # Need both the issued snapshot and the measured actuals to score a day;
    # a day missing either is retried by a later catch-up (like training).
    if not issued or not actuals:
        return

    snap = IssuedSnapshot.from_dict(issued)
    # LEAKAGE GUARD (SPEC §15.2): only score a day whose snapshot was issued
    # before the early-morning cutoff of that local day. A snapshot issued
    # later (a mid-day startup catch-up recomputed from a fresh weather fetch
    # that has assimilated the scored day's observed weather) is a
    # hindcast/nowcast, not a day-ahead forecast; leave the day UNSCORED so
    # it never flatters the engine.
    if coord._issued_after_cutoff(snap, day):
        _LOGGER.debug(
            "Skipping scoreboard for %s: snapshot issued after the "
            "day-ahead cutoff (issued_at=%s)", iso, snap.issued_at,
        )
        return

    # Engine AS ISSUED: the CORRECTED served hourly curve, sliced to the day.
    corrected_hourly = _filter_hourly_to_local_day(
        snap.corrected_hourly_wh or snap.raw_hourly_wh, iso
    )
    engine_kwh = sum(corrected_hourly.values()) / 1000.0
    # Measured site energy for the day = sum of the per-module actuals.
    measured_kwh = (
        sum(
            float(v)
            for v in actuals.values()
            if isinstance(v, (int, float))
        )
        / 1000.0
    )
    weather_class = coord._dominant_weather_class(snap, iso)

    # Engine hourly MAE: issued corrected hourly (Wh) vs measured hourly (Wh).
    engine_hourly_mae = None
    hourly_actuals = coord._store_hourly_actuals(iso)
    measured_hourly = coord._site_measured_hourly(iso, hourly_actuals)
    if measured_hourly:
        engine_hourly_mae = scoreboard_mod.hourly_mae(
            corrected_hourly, measured_hourly
        )

    day_score = scoreboard_mod.score_day(
        iso_date=iso,
        weather_class=weather_class,
        measured_kwh=measured_kwh,
        engine_kwh=engine_kwh,
        engine_hourly_mae=engine_hourly_mae,
    )
    if day_score is None:
        # Non-finite / negative measured or engine number: the day stays
        # UNSCORED — no ring entry, no persist (a fabricated 0.0-kWh day
        # would poison the window, SPEC §15.2).
        _LOGGER.warning(
            "Scoreboard: %s unscorable (measured=%r, engine=%r); day skipped",
            iso, measured_kwh, engine_kwh,
        )
        return
    # eMMC-wear guard (minor): a deterministic re-score with an identical
    # DayScore (restart-heavy day, same rings) is a no-op — skip the write.
    if coord._scoreboard_state.days.get(iso) == day_score:
        return
    days = dict(coord._scoreboard_state.days)
    days[iso] = day_score
    state = ScoreboardState(days=days, version=coord._scoreboard_state.version)
    # Trim to the configured window so the ring never grows unbounded.
    state = scoreboard_mod.trim_window(
        state, window_days=coord._scoreboard_window_days
    )
    coord._scoreboard_state = state
    coord._persist_scoreboard_state()


def issued_after_cutoff(coord, snap: IssuedSnapshot, day: date) -> bool:
    """True when the snapshot was issued after the day-ahead cutoff of ``day``.

    The cutoff is ``_SCOREBOARD_ISSUE_CUTOFF_HOUR`` local time of ``day``.
    A snapshot with an unparseable / empty ``issued_at`` is treated as valid
    (not after cutoff) — a legacy/v0.1 snapshot pre-dates the catch-up
    recompute path this guard defends against.
    """
    issued_at = dt_util.parse_datetime(snap.issued_at or "")
    if issued_at is None:
        return False
    cutoff = dt_util.start_of_local_day(
        datetime(day.year, day.month, day.day)
    ) + timedelta(hours=_SCOREBOARD_ISSUE_CUTOFF_HOUR)
    return dt_util.as_utc(issued_at) > dt_util.as_utc(cutoff)


def dominant_weather_class(coord, snap: IssuedSnapshot, iso: str) -> str:
    """Yesterday's DOMINANT cloud class from the issued snapshot (SPEC §15.2).

    The issued snapshot stores the forecast cloud class per ISO-UTC hour
    (``cloud_class_by_hour``); the dominant class is the one carrying the most
    forecast ENERGY over the local day (weighted by the issued Wh per hour),
    so nocturnal / near-zero-Wh hours cannot outvote the handful of daylight
    hours where the PV error actually lives — a radiation-fog morning is filed
    under 'fog', not 'clear'. Ties broken by the const CLOUD_CLASSES order.
    Falls back to CLEAR when the snapshot carries no per-hour classes (v0.1
    issued / empty), so a day is never left unstratified.
    """
    weight_by_hour = snap.corrected_hourly_wh or snap.raw_hourly_wh or {}
    weights: dict[str, float] = {}
    for hkey, cc in snap.cloud_class_by_hour.items():
        dt = dt_util.parse_datetime(hkey)
        if dt is None:
            continue
        if dt_util.as_local(dt_util.as_utc(dt)).date().isoformat() != iso:
            continue
        # Weight by issued Wh (daylight energy); a near-zero-Wh night hour
        # contributes essentially nothing. Fall back to an equal +1 vote when
        # no Wh curve exists so an all-zero-weight day still stratifies.
        w = weight_by_hour.get(hkey, 0.0)
        weights[cc] = weights.get(cc, 0.0) + (float(w) if w > 0.0 else 0.0)
    if not any(v > 0.0 for v in weights.values()):
        # No daylight-energy signal: fall back to an unweighted hour count so
        # a Wh-less (legacy) snapshot still gets a class.
        weights = {}
        for hkey, cc in snap.cloud_class_by_hour.items():
            dt = dt_util.parse_datetime(hkey)
            if dt is None:
                continue
            if dt_util.as_local(dt_util.as_utc(dt)).date().isoformat() != iso:
                continue
            weights[cc] = weights.get(cc, 0.0) + 1.0
    if not weights:
        return CLOUD_CLASS_CLEAR
    best_n = max(weights.values())
    from .const import CLOUD_CLASSES

    for cls in CLOUD_CLASSES:
        if abs(weights.get(cls, 0.0) - best_n) < 1e-9:
            return cls
    # A class outside the canonical tuple (defensive): return any max.
    return max(weights, key=lambda c: weights[c])


def scoreboard_summary(coord) -> dict[str, Any]:
    """The current scoreboard aggregate view for ``self.data`` / platforms."""
    return scoreboard_mod.scoreboard_summary(
        coord._scoreboard_state,
        window_days=coord._scoreboard_window_days,
    )
