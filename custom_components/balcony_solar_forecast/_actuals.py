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
from typing import NamedTuple

from homeassistant.util import dt as dt_util

from .const import (
    CHANNEL_PLAUSIBILITY_MAX_WP_FRAC,
    DAY_ACTUALS_MIN_DAYLIGHT_COVERAGE,
    DROPOUT_REASON_DEAD_CHANNEL,
    DROPOUT_REASON_FROZEN_CHANNEL,
    DROPOUT_REASON_IMPLAUSIBLE_CHANNEL,
    DROPOUT_REASON_LOW_COVERAGE,
)
from .core import SiteConfig, solpos
from .core.measurement_quality import dc_power, frozen_nonzero, power_watts

_LOGGER = logging.getLogger(__name__)


async def async_read_actuals(
    coord, day: date
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Per-module measured DC energy for ``day``: (daily totals, hourly).

    Reads the five-minute ``min``, ``mean`` and ``max`` rows once and uses
    only complete plausible UTC hours for daily Wh totals and hourly buckets.

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
    coord._last_actuals_local_edges_complete = False
    coord._last_actuals_first_edge_production = None
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
        dict[str, float], dict[str, dict[str, float]], dict[str, object] | None, bool, str | None
    ]:
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        stat_ids = set(entity_by_module.values())
        utc_start = dt_util.as_utc(start)
        first_bucket = utc_start.replace(
            minute=utc_start.minute - utc_start.minute % 5, second=0, microsecond=0,
        )
        stats = statistics_during_period(
            coord.hass,
            first_bucket,
            _next_utc_hour_boundary(end),
            stat_ids,
            "5minute",
            {"power": "W"},
            {"min", "mean", "max"},
        )
        daylight_keys = _daylight_hour_keys_in_local_day(
            coord._site, start, end
        )
        utc_end = dt_util.as_utc(end)
        if utc_end != _next_utc_hour_boundary(utc_end):
            # That local fragment is measured separately. Its full UTC hour
            # requires data from the next day and must not gate this day's read.
            daylight_keys.discard(utc_end.replace(minute=0, second=0, microsecond=0).isoformat())
        expected = len(daylight_keys)
        # Wp per module for the SPEC §10 plausibility gate (kW-instead-of-W).
        wp_by_module = {
            p.name: p.wp for p in coord._site.planes if p.actual_entity
        }
        dropout: dict[str, object] = {}
        edge_proof: dict[str, bool] = {}
        edge_production: list[datetime] = []
        daily, hourly = _actuals_from_stats(
            stats,
            entity_by_module,
            expected_daylight_hours=expected,
            expected_daylight_hour_keys=daylight_keys,
            day=day,
            dropout_out=dropout,
            wp_by_module=wp_by_module,
            local_day_bounds=(utc_start, utc_end),
            local_edge_site=coord._site,
            edge_proof_out=edge_proof,
            edge_production_out=edge_production,
        )
        first = min(edge_production).isoformat() if edge_production and daily else None
        return daily, hourly, (dropout or None), bool(daily) and all(edge_proof.values()), first

    daily, hourly, dropout, local_edges_complete, first = await get_instance(coord.hass).async_add_executor_job(
        _read
    )
    coord._last_actuals_dropout = dropout
    coord._last_actuals_local_edges_complete = local_edges_complete
    coord._last_actuals_first_edge_production = first
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

    Reads the whole-site AC meter (SiteConfig.ac_actual_entity) five-minute
    means over the LOCAL calendar ``day`` and returns
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
            hass, _next_utc_hour_boundary(start), _next_utc_hour_boundary(end),
            {ac_entity}, "5minute", {"power": "W"}, {"min", "mean", "max"},
        )
        rows = _complete_hourly_rows(_five_minute_buckets(stats.get(ac_entity), signed=True))
        return _ac_hourly_from_stats(rows, invert=invert)

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


_FiveMinuteBuckets = dict[datetime, dict[int, tuple[float, float] | None]]


def _five_minute_buckets(rows: list[dict] | None, *, signed: bool = False) -> _FiveMinuteBuckets:
    """Index valid five-minute means and maxima by UTC hour and offset."""
    buckets: _FiveMinuteBuckets = {}
    for row in rows or ():
        start = _stat_row_datetime(row.get("start"))
        if start is None or start.minute % 5 or start.second or start.microsecond:
            continue
        hour = start.replace(minute=0)
        samples = buckets.setdefault(hour, {})
        # A duplicate timestamp makes this bucket ambiguous even if its first
        # value was a valid number.
        low = power_watts(row.get("min"), signed=signed)
        mean = power_watts(row.get("mean"), signed=signed)
        high = power_watts(row.get("max"), signed=signed)
        if (start.minute not in samples and low is not None and mean is not None
                and high is not None and low <= mean <= high):
            samples[start.minute] = mean, high
        else:
            samples[start.minute] = None
    return buckets


def _complete_hourly_rows(buckets: _FiveMinuteBuckets) -> list[dict]:
    """Integrate twelve distinct five-minute means with ordered finite extrema."""
    hourly = []
    for hour, samples in sorted(buckets.items()):
        usable = [sample for sample in samples.values() if sample is not None]
        if len(usable) == 12:
            hourly.append({
                "start": hour,
                "mean": math.fsum(mean / 12 for mean, _ in usable),
                "max": max(high for _, high in usable),
            })
    return hourly


def _next_utc_hour_boundary(local_bound: datetime) -> datetime:
    """Query all bins of the UTC hours whose starts belong to the local day."""
    instant = dt_util.as_utc(local_bound)
    hour = instant.replace(minute=0, second=0, microsecond=0)
    return hour if hour == instant else hour + timedelta(hours=1)


class _EdgeBucket(NamedTuple):
    hour: datetime
    minute: int
    hours_in_day: float
    daylight: bool


def _local_edge_buckets(start: datetime, end: datetime, site: SiteConfig | None) -> list[_EdgeBucket]:
    """Expected five-minute fragments of the two UTC hours crossing local midnight."""
    leading_hour = start.replace(minute=0, second=0, microsecond=0)
    trailing_hour = end.replace(minute=0, second=0, microsecond=0)
    expected = []
    for hour, first, last in (
        (leading_hour, start, _next_utc_hour_boundary(start)),
        (trailing_hour, trailing_hour, end),
    ):
        if first >= last:
            continue
        for minute in range(0, 60, 5):
            bucket_start = hour + timedelta(minutes=minute)
            inside_start = max(first, bucket_start)
            duration = min(last, bucket_start + timedelta(minutes=5)) - inside_start
            if duration <= timedelta(0):
                continue
            daylight = site is None or solpos.sun_position(
                inside_start + duration / 2, site.latitude, site.longitude,
            )[1] > -6
            expected.append(_EdgeBucket(hour, minute, duration.total_seconds() / 3600.0, daylight))
    return expected


def _local_edge_energy(
    buckets: _FiveMinuteBuckets, edges: list[_EdgeBucket], max_w: float | None,
) -> tuple[float, bool, datetime | None]:
    """Integrate the local fragments and verify coverage wherever yield is possible."""
    wh = 0.0
    incomplete_hours = set()
    positive_hours = set()
    missing_daylight = False
    first_production = None
    for hour, minute, hours_in_day, daylight in edges:
        sample = buckets.get(hour, {}).get(minute)
        if sample is None or (max_w is not None and sample[1] > max_w):
            incomplete_hours.add(hour)
            missing_daylight |= daylight
        else:
            if sample[0] > 0:
                positive_hours.add(hour)
                if first_production is None:
                    first_production = hour + timedelta(minutes=minute)
            wh += sample[0] * hours_in_day
    # Unobserved dark pieces are tolerated while the observed edge is
    # consistent with night. Positive edge production needs complete evidence.
    return wh, not missing_daylight and incomplete_hours.isdisjoint(positive_hours), first_production


def _daylight_hours_in_local_day(
    site: SiteConfig, start: datetime, end: datetime
) -> int:
    """Count whole hours in ``[start, end)`` whose mid-point sun elevation > 0.

    Used by the day-completeness gate to know how many hourly LTS rows a full
    day SHOULD carry. Pure geometry (solpos); never raises — a bad site/time
    returns 0 (gate skipped). ``start``/``end`` are tz-aware local-day bounds.
    """
    return len(_daylight_hour_keys_in_local_day(site, start, end))


def _daylight_hour_keys_in_local_day(
    site: SiteConfig, start: datetime, end: datetime
) -> set[str]:
    """UTC hour keys in the local day whose midpoint is above the horizon."""
    from .core.daylight import daylight_hour_keys

    try:
        return daylight_hour_keys(site.latitude, site.longitude, start, end)
    except Exception:  # pragma: no cover - defensive
        return set()


def _actuals_from_stats(
    stats: dict[str, list[dict]],
    entity_by_module: dict[str, str],
    *,
    expected_daylight_hours: int,
    day: date,
    expected_daylight_hour_keys: set[str] | None = None,
    dropout_out: dict[str, object] | None = None,
    wp_by_module: dict[str, float] | None = None,
    local_day_bounds: tuple[datetime, datetime] | None = None,
    local_edge_site: SiteConfig | None = None,
    edge_proof_out: dict[str, bool] | None = None,
    edge_production_out: list[datetime] | None = None,
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
    edges = _local_edge_buckets(*local_day_bounds, local_edge_site) if local_day_bounds else []
    for module, entity_id in entity_by_module.items():
        rows = stats.get(entity_id)
        wp = (wp_by_module or {}).get(module)
        max_w = CHANNEL_PLAUSIBILITY_MAX_WP_FRAC * wp if wp is not None and wp > 0.0 else None
        edge_wh = 0.0
        if local_day_bounds is not None:
            start, end = local_day_bounds
            buckets = _five_minute_buckets(rows)
            edge_wh, edge_complete, first_production = _local_edge_energy(buckets, edges, max_w)
            if edge_proof_out is not None:
                edge_proof_out[module] = edge_complete
            if edge_production_out is not None and first_production is not None:
                edge_production_out.append(max(start, first_production))
            rows = [row for row in _complete_hourly_rows(buckets) if start <= row["start"] < end]
        means: list[float] = []
        local_hour_means: list[float] = []
        hkeys: list[str] = []
        for row in rows or ():
            mean = row.get("mean")
            if mean is None:
                continue
            hkey = _stat_row_hour_key(row.get("start"))
            if hkey is None:
                continue
            watts = dc_power(mean)
            if watts is None:
                _LOGGER.warning("Invalid DC statistics for %s (%s) on %s; "
                                "discarding the whole day", module, entity_id, day)
                return _dropout(DROPOUT_REASON_IMPLAUSIBLE_CHANNEL, module)
            # A peak above the channel limit invalidates this hour. Keep a
            # sustained hour above the same limit for the whole-day diagnosis.
            overlaps_next_day = local_day_bounds is not None and row["start"] + timedelta(hours=1) > end
            if (local_day_bounds is not None and max_w is not None and row["max"] > max_w
                    and (overlaps_next_day or watts <= max_w)):
                continue
            means.append(watts)
            hkeys.append(hkey)
            if not overlaps_next_day:
                local_hour_means.append(watts)
        if not means:
            _LOGGER.warning(
                "Channel %s (%s) has no usable LTS rows on %s (dead/unavailable "
                "DTU port?); discarding the whole day for both learners "
                "(channel dropout, SPEC §9.8)",
                module, entity_id, day,
            )
            return _dropout(DROPOUT_REASON_DEAD_CHANNEL, module)
        if _is_frozen_channel(local_hour_means):
            _LOGGER.warning(
                "Channel %s (%s) looks frozen on %s (byte-identical "
                "hourly means during daylight); discarding the whole day "
                "for both learners (SPEC §9.8)",
                module, entity_id, day,
            )
            return _dropout(DROPOUT_REASON_FROZEN_CHANNEL, module)
        worst = max(local_hour_means, default=0.0)
        if max_w is not None and worst > max_w:
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
        for hkey, m in zip(hkeys, means, strict=False):
            per_hour[hkey] = per_hour.get(hkey, 0.0) + m  # W*1h = Wh
        if local_day_bounds is None:
            wh = sum(per_hour.values())
        else:
            # Full UTC target hours may cross the end boundary. Its in-day
            # share was integrated from the actual five-minute means above.
            last_full_hour_end = end.replace(minute=0, second=0, microsecond=0)
            wh = edge_wh + sum(
                mean for hour, mean in per_hour.items() if datetime.fromisoformat(hour) < last_full_hour_end
            )
        daily[module] = round(wh, 1)
        hourly[module] = per_hour
        observed = set(hkeys)
        covered_hours[module] = (
            len(observed & expected_daylight_hour_keys)
            if expected_daylight_hour_keys is not None
            else len(observed)
        )
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
    return frozen_nonzero(means)


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
    if isinstance(start, (int, float)) and not isinstance(start, bool):
        ts = float(start)
        if ts > _EPOCH_MS_THRESHOLD:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
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
