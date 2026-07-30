"""Recorder / long-term-statistics actuals reader — SPEC §9.7/§9.8 label gates.

Owner: glue (nightly actuals IO). Reads a closed day's per-module measured DC
energy from the recorder's hourly long-term statistics and applies the SPEC §9.8
"Messkanal-Dropout ⇒ ganzen Tag verwerfen" label gates before the numbers may
become training / scoreboard ground truth against the FULL-site modeled energy.

The coordinator keeps ``_async_read_actuals`` / ``_async_read_daily_actuals`` as
thin delegates (tests build the coordinator via ``__new__`` and call them) and
re-imports ``_actuals_from_stats`` + ``_is_frozen_channel`` so their historical
``coordinator.*`` import paths keep resolving. ``async_read_actuals`` takes the
coordinator as ``coord`` and reads exactly ``coord._site`` / ``coord.hass``; the
recorder read itself runs in the recorder executor.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime, timedelta, tzinfo

from homeassistant.util import dt as dt_util

from .const import (
    CHANNEL_PLAUSIBILITY_MAX_WP_FRAC,
    DAY_ACTUALS_MIN_DAYLIGHT_COVERAGE,
    DROPOUT_REASON_DEAD_CHANNEL,
    DROPOUT_REASON_FROZEN_CHANNEL,
    DROPOUT_REASON_IMPLAUSIBLE_CHANNEL,
    DROPOUT_REASON_LOW_COVERAGE,
    LABEL_FROZEN_MIN_REPEATS,
)
from .core import SiteConfig, solpos

_LOGGER = logging.getLogger(__name__)


async def async_read_actuals(
    coord, day: date
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Per-module measured DC energy for ``day``: (daily totals, hourly).

    Reads the hourly ``mean`` rows once and returns BOTH the per-module daily
    Wh totals AND the per-module ``{iso_hour: wh}`` buckets (the shademap
    trainer needs hourly resolution).

    Label gates (SPEC §9.8, all delegated to :func:`_actuals_from_stats`):
    a frozen channel, a configured channel with NO usable rows (dead DTU
    port), or ANY channel covering too little of the daylight span is a
    DROPOUT — the WHOLE day is discarded for BOTH learners so a partial-site
    measurement never poisons the write-once ring ("Messkanal-Dropout ⇒
    ganzen Tag verwerfen"). The window follows the LOCAL calendar day
    exactly so DST (23/25-h) days are bounded correctly (coordinator:1256).

    Side effect (0.23.1): ``coord._last_actuals_dropout`` is set to the reason
    dict of the gate that discarded the day, or to ``None`` when the day was
    accepted. Discarding is otherwise indistinguishable from "nothing measured
    yet" at the call site, and the nightly streak detector must be able to name
    WHICH gate fired — the three cases have different remedies.
    """
    coord._last_actuals_dropout = None
    entity_by_module = {
        p.name: p.actual_entity
        for p in coord._site.planes
        if p.actual_entity
    }
    if not entity_by_module:
        return {}, {}

    start = dt_util.start_of_local_day(
        datetime(day.year, day.month, day.day)
    )
    # Follow the LOCAL calendar day exactly (DST-safe): the next local
    # midnight, not a fixed +24 h (coordinator:1256).
    end = dt_util.start_of_local_day(
        datetime(day.year, day.month, day.day) + timedelta(days=1)
    )

    from homeassistant.components.recorder import get_instance

    def _read() -> tuple[
        dict[str, float], dict[str, dict[str, float]], dict[str, object] | None
    ]:
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        stat_ids = set(entity_by_module.values())
        stats = statistics_during_period(
            coord.hass,
            start,
            end,
            stat_ids,
            "hour",
            None,
            {"mean", "state"},
        )
        expected = _daylight_hours_in_local_day(coord._site, start, end)
        # Wp per module for the SPEC §10 plausibility gate (kW-instead-of-W).
        wp_by_module = {
            p.name: p.wp for p in coord._site.planes if p.actual_entity
        }
        dropout: dict[str, object] = {}
        daily, hourly = _actuals_from_stats(
            stats,
            entity_by_module,
            expected_daylight_hours=expected,
            day=day,
            dropout_out=dropout,
            wp_by_module=wp_by_module,
        )
        return daily, hourly, (dropout or None)

    daily, hourly, dropout = await get_instance(coord.hass).async_add_executor_job(
        _read
    )
    coord._last_actuals_dropout = dropout
    return daily, hourly


async def async_read_ac_actuals(
    hass,
    ac_entity: str | None,
    day: date,
    tz: tzinfo,
    *,
    invert: bool = False,
) -> dict[str, float]:
    """Hourly measured TOTAL-AC energy for ``day`` from the single site meter.

    Reads the whole-site AC meter (SiteConfig.ac_actual_entity) hourly ``mean``
    long-term-statistics rows over the LOCAL calendar ``day`` and returns
    ``{iso_utc_hour: wh}`` (a mean-W row over one hour == Wh) — the AC-side
    calibration ground truth (AC-side Phase 3). Mirrors the per-module DC reader
    for a SINGLE entity and keys hours by the SAME ISO-UTC hour as
    :func:`_actuals_from_stats`, so the nightly calibration can match AC hours
    against the summed per-module DC hourly actuals directly.

    ``None`` / empty ``ac_entity`` => ``{}`` (no meter configured — the whole
    calibration is a no-op). ``invert`` negates every hour (the operator's meter
    can report fed-in balcony-solar AC as a negative value): the sign flip is
    applied ONCE here at the read boundary so the calibration sees a positive AC.
    No DC label gates are applied — the nightly calibration gates each hour
    (min-load + unclipped) against the DC side, and the eligibility band rejects
    an implausible (e.g. still-negative net-export) reading, so this reader stays
    a thin, sign-correct energy read.
    """
    if not ac_entity:
        return {}
    # Local-midnight bounds built directly from ``tz`` (zoneinfo-aware, so DST
    # 23/25-h days are bounded correctly — NOT via +24 h timedelta arithmetic).
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)

    from homeassistant.components.recorder import get_instance

    def _read() -> dict[str, float]:
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        stats = statistics_during_period(
            hass, start, end, {ac_entity}, "hour", None, {"mean", "state"},
        )
        return _ac_hourly_from_stats(stats.get(ac_entity), invert=invert)

    return await get_instance(hass).async_add_executor_job(_read)


def _ac_hourly_from_stats(
    rows: list[dict] | None, *, invert: bool = False
) -> dict[str, float]:
    """Reduce one entity's hourly LTS rows to ``{iso_utc_hour: wh}`` (pure).

    Each hourly ``mean`` (W) integrates to Wh over its hour; ``invert`` negates
    the value so a sign-inverted fed-in meter reads as positive AC. A negated
    value that is still negative (a net-export meter) is returned AS-IS — the
    calibration's eligibility band self-rejects it, so no clamp is applied here.
    Never raises.
    """
    hourly: dict[str, float] = {}
    sign = -1.0 if invert else 1.0
    for row in rows or ():
        mean = row.get("mean")
        if mean is None:
            continue
        hkey = _stat_row_hour_key(row.get("start"))
        if hkey is None:
            continue
        hourly[hkey] = hourly.get(hkey, 0.0) + sign * float(mean)  # W*1h == Wh
    return hourly


def _daylight_hours_in_local_day(
    site: SiteConfig, start: datetime, end: datetime
) -> int:
    """Count whole hours in ``[start, end)`` whose mid-point sun elevation > 0.

    Used by the day-completeness gate to know how many hourly LTS rows a full
    day SHOULD carry. Pure geometry (solpos); never raises — a bad site/time
    returns 0 (gate skipped). ``start``/``end`` are tz-aware local-day bounds.
    """
    try:
        start_utc = dt_util.as_utc(start)
        end_utc = dt_util.as_utc(end)
    except Exception:  # pragma: no cover - defensive
        return 0
    count = 0
    cur = start_utc
    step = timedelta(hours=1)
    # Guard against a runaway loop (DST safety): a local day is <= 25 hours.
    for _ in range(26):
        if cur >= end_utc:
            break
        mid = cur + timedelta(minutes=30)
        try:
            _az, el = solpos.sun_position(mid, site.latitude, site.longitude)
        except Exception:  # pragma: no cover - defensive
            el = 0.0
        if el > 0.0:
            count += 1
        cur += step
    return count


def _actuals_from_stats(
    stats: dict[str, list[dict]],
    entity_by_module: dict[str, str],
    *,
    expected_daylight_hours: int,
    day: date,
    dropout_out: dict[str, object] | None = None,
    wp_by_module: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Pure post-processing of LTS rows into per-module (daily, hourly) actuals.

    Applies the SPEC §9.8 label gates, each of which discards the WHOLE day
    ("Messkanal-Dropout ⇒ ganzen Tag verwerfen") so a partial-site measurement
    never becomes the training/scoreboard ground truth against the FULL-site
    modeled energy (which would read as a production deficit — the same failure
    mode the intraday scalar guards against with its usable-planes subset):

      * frozen channel — byte-identical non-zero hourly means held for
        LABEL_FROZEN_MIN_REPEATS+ consecutive hours (:func:`_is_frozen_channel`);
      * IMPLAUSIBLE channel (only with ``wp_by_module``) — a sustained hourly
        mean above CHANNEL_PLAUSIBILITY_MAX_WP_FRAC x the module's configured
        Wp is physically impossible (cloud-edge peaks are sub-hourly), so the
        sensor is mis-scaled (kW instead of W); SPEC §10;
      * MISSING channel — a configured module with no LTS rows at all, or rows
        without a single usable mean (a DTU port that went unavailable);
      * per-module day-completeness — EVERY configured module must cover at
        least DAY_ACTUALS_MIN_DAYLIGHT_COVERAGE of the day's daylight hours; a
        healthy sibling must never mask a module that died mid-day. Skipped when
        the daylight span is unknown (``expected_daylight_hours`` <= 0).

    A discarded day returns ``({}, {})`` and is NOT recorded, so a later nightly
    catch-up re-reads it once LTS is complete. A permanently dead (but still
    configured) sensor therefore blocks training until the operator removes it —
    the warnings name the module + entity id to make that actionable.

    ``dropout_out`` (optional, 0.23.1) is FILLED IN PLACE with
    ``{"reason": DROPOUT_REASON_*, "modules": [...], "entities": [...]}``
    whenever a gate discards the day, and left untouched otherwise. That is the
    machine-readable half of the warnings above: the nightly streak detector
    persists it and the repair issue names the reason, because a dead channel
    (wrong/removed entity id), a frozen channel (stuck DTU sensor) and a
    coverage break (recorder gap) each need a different remedy.
    """
    def _dropout(reason: str, module: str) -> tuple[dict, dict]:
        if dropout_out is not None:
            dropout_out.clear()
            dropout_out.update(
                reason=reason,
                modules=[module],
                entities=[entity_by_module.get(module, "")],
            )
        return {}, {}

    daily: dict[str, float] = {}
    hourly: dict[str, dict[str, float]] = {}
    covered_hours: dict[str, int] = {}
    for module, entity_id in entity_by_module.items():
        rows = stats.get(entity_id)
        means: list[float] = []
        hkeys: list[str] = []
        for row in rows or ():
            mean = row.get("mean")
            if mean is None:
                continue
            hkey = _stat_row_hour_key(row.get("start"))
            if hkey is None:
                continue
            means.append(float(mean))
            hkeys.append(hkey)
        if not means:
            _LOGGER.warning(
                "Channel %s (%s) has no usable LTS rows on %s (dead/unavailable "
                "DTU port?); discarding the whole day for both learners "
                "(channel dropout, SPEC §9.8)",
                module, entity_id, day,
            )
            return _dropout(DROPOUT_REASON_DEAD_CHANNEL, module)
        if _is_frozen_channel(means):
            _LOGGER.warning(
                "Channel %s (%s) looks frozen on %s (byte-identical "
                "hourly means during daylight); discarding the whole day "
                "for both learners (SPEC §9.8)",
                module, entity_id, day,
            )
            return _dropout(DROPOUT_REASON_FROZEN_CHANNEL, module)
        if wp_by_module is not None:
            wp = wp_by_module.get(module)
            if wp is not None and wp > 0.0:
                worst = max(means)
                if worst > CHANNEL_PLAUSIBILITY_MAX_WP_FRAC * wp:
                    _LOGGER.warning(
                        "Channel %s (%s) measured a sustained %.0f W in an "
                        "hour on %s — above %.2f x its configured %.0f Wp, "
                        "which is physically impossible; the sensor is likely "
                        "mis-scaled (kW instead of W?). Discarding the whole "
                        "day for learning and scoring (SPEC §10); fix the "
                        "entity's unit/scaling and the day trains again.",
                        module, entity_id, worst, day,
                        CHANNEL_PLAUSIBILITY_MAX_WP_FRAC, wp,
                    )
                    return _dropout(DROPOUT_REASON_IMPLAUSIBLE_CHANNEL, module)
        per_hour: dict[str, float] = {}
        wh = 0.0
        for hkey, m in zip(hkeys, means, strict=False):
            per_hour[hkey] = per_hour.get(hkey, 0.0) + m  # W*1h = Wh
            wh += m
        daily[module] = round(wh, 1)
        hourly[module] = per_hour
        covered_hours[module] = len(set(hkeys))
    # Per-module day-completeness gate: a mid-day recorder/LTS gap OR a module
    # dying mid-day yields a partial-hour sum that must NOT become the day's
    # ground truth. EVERY configured module has to clear the bar — using the
    # best-covered module here would let one healthy sibling mask a partial one.
    if expected_daylight_hours > 0 and covered_hours:
        need = int(
            math.ceil(expected_daylight_hours * DAY_ACTUALS_MIN_DAYLIGHT_COVERAGE)
        )
        worst = min(covered_hours, key=lambda m: covered_hours[m])
        if covered_hours[worst] < need:
            _LOGGER.warning(
                "Actuals for %s: module %s (%s) covers only %d of ~%d daylight "
                "hours (< %.0f%%); discarding the day as incomplete (recorder "
                "gap or mid-day channel dropout). A later catch-up will refill "
                "it.",
                day, worst, entity_by_module.get(worst),
                covered_hours[worst], expected_daylight_hours,
                DAY_ACTUALS_MIN_DAYLIGHT_COVERAGE * 100.0,
            )
            return _dropout(DROPOUT_REASON_LOW_COVERAGE, worst)
    return daily, hourly


def _is_frozen_channel(means: list[float]) -> bool:
    """True when hourly means show a frozen sensor: the SAME non-zero value held
    for >= LABEL_FROZEN_MIN_REPEATS consecutive hours (SPEC §9.8 label gate).

    A frozen Hoymiles/DTU sensor holds its last value (never goes unavailable),
    so the recorder carries the same non-zero mean forward hour after hour. A run
    of identical zeros is legitimate night/shade and never trips the gate.
    """
    run = 1
    for i in range(1, len(means)):
        if means[i] == means[i - 1] and means[i] != 0.0:
            run += 1
            if run >= LABEL_FROZEN_MIN_REPEATS:
                return True
        else:
            run = 1
    return False


# Numeric statistics-row ``start`` values above this are epoch-MILLISECONDS
# (the recorder WebSocket wire format); at or below they are epoch-SECONDS
# (the in-process ``statistics_during_period`` API returns ``start_ts`` float
# seconds). 1e11 s ≈ year 5138, 1e11 ms ≈ 1973 — every real timestamp from
# either API is unambiguously on one side of the threshold.
_EPOCH_MS_THRESHOLD = 1e11


def _stat_row_datetime(start: object) -> datetime | None:
    """Normalise a statistics row ``start`` to an aware UTC datetime, or None.

    The in-process recorder API (``statistics_during_period``) returns
    ``start`` as a float of epoch SECONDS; the WebSocket layer multiplies to
    epoch MILLISECONDS; older cores handed out aware datetimes. Disambiguate
    numerics by magnitude (``_EPOCH_MS_THRESHOLD``) — treating seconds as ms
    collapsed every hour of a day onto one 1970 key, which made the
    day-completeness gate discard EVERY day ("covers only 1 of ~16 daylight
    hours") and silently starved all nightly learning (bias / shademap /
    scoreboard / quantiles / drift) since the gate landed. The intraday
    ring re-arm (A7) reuses this to place reconstructed samples at their true
    minute-resolution timestamps.
    """
    if isinstance(start, datetime):
        return dt_util.as_utc(start)
    if isinstance(start, (int, float)):
        ts = float(start)
        if ts > _EPOCH_MS_THRESHOLD:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    if isinstance(start, str):
        dt = dt_util.parse_datetime(start)
        if dt is None:
            return None
        return dt_util.as_utc(dt)
    return None


def _stat_row_hour_key(start: object) -> str | None:
    """Normalise a statistics row ``start`` to an ISO-UTC hour key, or None.

    Thin wrapper over :func:`_stat_row_datetime` that truncates to the hour;
    the epoch-seconds-vs-milliseconds disambiguation lives in that helper.
    """
    dt = _stat_row_datetime(start)
    if dt is None:
        return None
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()
