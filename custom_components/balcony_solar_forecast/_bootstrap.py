"""In-process learner re-bootstrap — the ``run_bootstrap`` action (SPEC §6).

Owner: glue (bootstrap IO). The dev-machine CLI (``scripts/backfill.py``) rebuilds
the three learner states from ~2 years of history, but it needs a long-lived
token, the operator's exported ``site.json`` and a desktop Python. This module
runs the SAME reconstruction IN-PROCESS from the developer tools:

  * Weather: the shared HA-free Open-Meteo Previous-Runs fetch
    (``core.openmeteo_backfill``), driven with the integration's own aiohttp
    session (``aiohttp_client.async_get_clientsession``) — no token, chunked so a
    multi-year request cannot trip a provider limit.
  * Actuals: an IN-PROCESS recorder ``statistics_during_period`` read (in the
    recorder executor) over the planes' configured ``actual_entity`` — NOT the
    WebSocket API, so numeric row ``start`` values are epoch SECONDS; the reduce
    reuses ``_actuals._stat_row_hour_key`` whose magnitude test survives the
    historical seconds-vs-milliseconds bug (SPEC §5, regression-guarded).
  * Config: taken straight from the live coordinator (``coordinator._site``) — no
    ``site.json``, which is the whole point of the in-app path.

The pure reconstruction / bootstrap MATH is the SAME ``core.bootstrap_build``
the CLI uses (byte-identical bootstrap dict), run in the executor because it is a
multi-minute CPU job (~90 s desktop / 2-5 min in the HA container for 320 days).

Safety: ``dry_run`` defaults TRUE — the first call only fetches, reconstructs and
returns the summary WITHOUT touching the store; an explicit ``dry_run: false``
runs the SAME ``coordinator.async_import_bootstrap`` path as ``import_bootstrap``
(rollback snapshot, clamp, refresh). A per-coordinator lock serialises the run
against the nightly training job and rejects a second concurrent ``run_bootstrap``
with a clear ServiceValidationError.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, date, datetime, timedelta

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import aiohttp_client
from homeassistant.util import dt as dt_util

from ._actuals import _stat_row_hour_key
from .const import (
    BOOTSTRAP_DEFAULT_MAX_DAYS,
    BOOTSTRAP_KEY_BIAS,
    BOOTSTRAP_KEY_QUANTILE,
    BOOTSTRAP_KEY_SHADEMAP,
    BOOTSTRAP_WEATHER_CHUNK_DAYS,
)
from .core import horizon
from .core.bootstrap_build import accumulate_days, build_bootstrap_json
from .core.openmeteo_backfill import fetch_weather_range

_LOGGER = logging.getLogger(__name__)

# Field names on the service call.
ATTR_ENTRY_ID = "entry_id"
ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"
ATTR_DRY_RUN = "dry_run"

# Emit an INFO progress line every this-many reconstructed days.
_PROGRESS_EVERY_DAYS = 50


def _bootstrap_lock(coordinator) -> asyncio.Lock:
    """Return (creating once) the coordinator's shared bootstrap/nightly lock.

    The lock is created in ``BalconySolarCoordinator.__init__``; a test double
    built via ``__new__`` (or a fake coordinator) has none, so create it lazily
    and stash it back so the nightly wrapper and a second ``run_bootstrap`` see
    the SAME instance.
    """
    lock = getattr(coordinator, "_bootstrap_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        coordinator._bootstrap_lock = lock
    return lock


async def async_run_bootstrap(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Handle ``balcony_solar_forecast.run_bootstrap`` (SupportsResponse.ONLY)."""
    # Lazy import to avoid an import cycle at package load (_services imports us).
    from ._services import _resolve_single_coordinator

    coordinator = _resolve_single_coordinator(hass, call.data.get(ATTR_ENTRY_ID))
    importer = getattr(coordinator, "async_import_bootstrap", None)
    if not callable(importer):
        raise ServiceValidationError(
            "This installation does not support bootstrap import."
        )

    start, end = _resolve_range(
        call.data.get(ATTR_START_DATE), call.data.get(ATTR_END_DATE)
    )
    dry_run = bool(call.data.get(ATTR_DRY_RUN, True))

    lock = _bootstrap_lock(coordinator)
    if lock.locked():
        raise ServiceValidationError(
            "A re-bootstrap or the nightly training job is already running; "
            "wait for it to finish before starting another run_bootstrap."
        )
    async with lock:
        return await _run_locked(hass, coordinator, importer, start, end, dry_run)


async def _run_locked(
    hass: HomeAssistant,
    coordinator,
    importer,
    start: date,
    end: date,
    dry_run: bool,
) -> ServiceResponse:
    t0 = time.monotonic()
    site = coordinator._site
    if not any(getattr(p, "actual_entity", None) for p in site.planes):
        raise ServiceValidationError(
            "No plane has an 'actual_entity' configured; there is no measured "
            "history to re-bootstrap from."
        )
    tz = dt_util.get_time_zone(hass.config.time_zone) or UTC

    _LOGGER.info(
        "run_bootstrap: fetching Open-Meteo weather %s..%s (dry_run=%s)",
        start, end, dry_run,
    )
    weather, as_issued = await _fetch_weather(hass, site, start, end)
    if not weather:
        raise ServiceValidationError(
            f"Open-Meteo returned no usable weather for {start}..{end}; the range "
            "may predate the Previous-Runs archive (since 01/2024)."
        )

    _LOGGER.info(
        "run_bootstrap: reading measured history from the recorder %s..%s", start, end
    )
    hourly_actuals = await _read_hourly_actuals(hass, site, start, end)
    if not hourly_actuals:
        raise ServiceValidationError(
            f"No measured module energy in the recorder for {start}..{end}; check "
            "that long-term statistics exist for the planes' actual_entity ids."
        )

    svf_by_plane = {p.name: horizon.sky_view_factor(p) for p in site.planes}

    acc = await hass.async_add_executor_job(
        _build_accumulator, site, weather, hourly_actuals, svf_by_plane, tz
    )
    if acc.days_used == 0:
        raise ServiceValidationError(
            "No usable days in the range (every day lacked measured actuals or "
            "failed the label gates); the bootstrap would be empty."
        )

    bootstrap = build_bootstrap_json(acc, site)
    summary = _summary(acc, bootstrap, start, end, as_issued)

    imported = False
    if not dry_run:
        _LOGGER.info(
            "run_bootstrap: importing the rebuilt bootstrap (%d bias cells, "
            "%d shademap bins, %d quantile bins)",
            summary["bias_cells"],
            summary["shademap_bins"],
            summary["quantile_bins"],
        )
        try:
            await importer(bootstrap)
        except ValueError as err:
            # A schema / site-signature mismatch (should not happen — we built it
            # from the live site — but map it cleanly rather than as a traceback).
            raise ServiceValidationError(
                f"Bootstrap rejected on import: {err}"
            ) from err
        imported = True

    summary["imported"] = imported
    summary["duration_s"] = round(time.monotonic() - t0, 1)
    if dry_run:
        summary["hint"] = (
            "Dry run: nothing imported. Re-run with dry_run: false to apply this "
            "bootstrap to the live learners."
        )
    _LOGGER.info(
        "run_bootstrap done: %d/%d days used, imported=%s, %.1fs",
        acc.days_used, acc.days_used + acc.days_skipped, imported,
        summary["duration_s"],
    )
    return summary


def _resolve_range(
    raw_start: str | None, raw_end: str | None
) -> tuple[date, date]:
    """Resolve the [start, end] LOCAL date range with sane defaults.

    Default end is yesterday (local); default start is today - N days (a cap —
    days without actuals are skipped, so an over-wide start self-corrects).
    Raises ServiceValidationError on unparseable dates or an inverted range.
    """
    today = dt_util.now().date()
    if raw_end is None:
        end = today - timedelta(days=1)
    else:
        end = _parse_date(raw_end, "end_date")
    if raw_start is None:
        start = today - timedelta(days=BOOTSTRAP_DEFAULT_MAX_DAYS)
    else:
        start = _parse_date(raw_start, "start_date")
    if end < start:
        raise ServiceValidationError(
            f"end_date ({end.isoformat()}) is before start_date "
            f"({start.isoformat()})."
        )
    return start, end


def _parse_date(raw: str, field: str) -> date:
    try:
        return date.fromisoformat(str(raw))
    except (ValueError, TypeError) as err:
        raise ServiceValidationError(
            f"Invalid {field} '{raw}'; expected ISO format YYYY-MM-DD."
        ) from err


async def _fetch_weather(
    hass: HomeAssistant, site, start: date, end: date
) -> tuple[list, bool]:
    """Fetch hourly as-issued weather over [start, end], chunked (SPEC §6).

    Splits the range into consecutive windows and calls the shared HA-free
    Previous-Runs fetch per window (each degrades to the Historical Forecast API
    on its own), concatenating the records. ``as_issued`` is True only if EVERY
    window returned as-issued forecast data. A provider failure that even the
    per-window degrade cannot recover raises, mapped to a ServiceValidationError
    by the caller.
    """
    session = aiohttp_client.async_get_clientsession(hass)
    records: list = []
    all_as_issued = True
    try:
        for win_start, win_end in _date_windows(
            start, end, BOOTSTRAP_WEATHER_CHUNK_DAYS
        ):
            chunk, chunk_issued = await fetch_weather_range(
                session,
                latitude=site.latitude,
                longitude=site.longitude,
                start=win_start,
                end=win_end,
            )
            records.extend(chunk)
            all_as_issued = all_as_issued and chunk_issued
    except Exception as err:  # noqa: BLE001 - map any provider failure cleanly
        raise ServiceValidationError(
            f"Open-Meteo weather fetch failed for {start}..{end}: {err}"
        ) from err
    return records, all_as_issued


def _date_windows(
    start: date, end: date, window_days: int
) -> list[tuple[date, date]]:
    """Split the inclusive [start, end] date span into <= window_days chunks."""
    windows: list[tuple[date, date]] = []
    cur = start
    step = timedelta(days=window_days - 1)
    while cur <= end:
        win_end = min(cur + step, end)
        windows.append((cur, win_end))
        cur = win_end + timedelta(days=1)
    return windows


async def _read_hourly_actuals(
    hass: HomeAssistant, site, start: date, end: date
) -> dict[str, dict[str, float]]:
    """Per-module hourly measured DC energy over [start, end] (recorder, executor).

    In-process ``statistics_during_period`` at hourly period over the planes'
    ``actual_entity`` ids, reduced to ``{module_name: {iso_utc_hour: wh}}`` — the
    exact shape ``accumulate_days`` consumes. The reduce keys hours via
    ``_stat_row_hour_key`` (epoch SECONDS from the in-process API; the magnitude
    test survives the seconds-vs-ms bug). Raises ServiceValidationError when the
    recorder is not set up.
    """
    entity_by_module = {
        p.name: p.actual_entity for p in site.planes if p.actual_entity
    }
    stat_ids = set(entity_by_module.values())
    # UTC-day bounds (the core groups weather + filters actuals by UTC date).
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)

    try:
        from homeassistant.components.recorder import get_instance
    except ImportError as err:  # pragma: no cover - recorder always present in HA
        raise ServiceValidationError(
            "The recorder integration is not available; cannot read history."
        ) from err

    try:
        instance = get_instance(hass)
    except (KeyError, RuntimeError, AttributeError) as err:
        raise ServiceValidationError(
            "The recorder integration is not set up; cannot read measured "
            "history for the re-bootstrap."
        ) from err

    def _read() -> dict[str, dict[str, float]]:
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        stats = statistics_during_period(
            hass, start_dt, end_dt, stat_ids, "hour", None, {"mean"}
        )
        return _reduce_stats(stats, entity_by_module)

    return await instance.async_add_executor_job(_read)


def _reduce_stats(
    stats: dict[str, list[dict]],
    entity_by_module: dict[str, str],
) -> dict[str, dict[str, float]]:
    """Fold recorder hourly ``mean`` rows into ``{module: {iso_hour: wh}}`` (pure).

    A mean power (W) over one hour integrates to Wh. Rows without a usable mean
    or an unparseable ``start`` are skipped; a module with no rows is omitted
    (the core then skips those days). Kept separate so a test can exercise the
    seconds-epoch reduce without a recorder.
    """
    out: dict[str, dict[str, float]] = {}
    for module, entity_id in entity_by_module.items():
        rows = stats.get(entity_id)
        if not rows:
            continue
        bucket: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            mean = row.get("mean")
            if mean is None:
                continue
            hkey = _stat_row_hour_key(row.get("start"))
            if hkey is None:
                continue
            try:
                bucket[hkey] = bucket.get(hkey, 0.0) + float(mean)  # W*1h == Wh
            except (TypeError, ValueError):
                continue
        if bucket:
            out[module] = bucket
    return out


def _build_accumulator(site, weather, hourly_actuals, svf_by_plane, tz):
    """Run the pure reconstruction in the executor with periodic progress logs."""

    def _progress(done: int, total: int) -> None:
        if done % _PROGRESS_EVERY_DAYS == 0 or done == total:
            _LOGGER.info("run_bootstrap: reconstructed %d/%d days", done, total)

    return accumulate_days(
        site,
        weather,
        hourly_actuals,
        svf_by_plane=svf_by_plane,
        tz=tz,
        progress_cb=_progress,
    )


def _summary(
    acc,
    bootstrap: dict,
    start: date,
    end: date,
    as_issued: bool,
) -> dict:
    """Build the always-returned re-bootstrap summary from the accumulator + JSON."""
    bias = bootstrap.get(BOOTSTRAP_KEY_BIAS, {}).get("cells", {})
    channels = bootstrap.get(BOOTSTRAP_KEY_SHADEMAP, {}).get("channels", {})
    quant = bootstrap.get(BOOTSTRAP_KEY_QUANTILE, {}).get("bins", {})
    return {
        "days_used": acc.days_used,
        "days_skipped": acc.days_skipped,
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "weather_source": "as_issued" if as_issued else "analysis_fallback",
        "bias_cells": len(bias),
        "shademap_channels": len(channels),
        "shademap_bins": sum(len(b) for b in channels.values()),
        "shademap_samples": acc.shade_samples,
        "quantile_bins": len(quant),
        "quantile_samples": acc.quantile_samples,
    }
