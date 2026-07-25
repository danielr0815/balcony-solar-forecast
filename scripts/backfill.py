#!/usr/bin/env python3
"""One-shot Previous-Runs backfill -> learner bootstrap JSON (SPEC §6).

DEV-MACHINE script (NOT run on Home Assistant). It reconstructs a warm start
for the two learning layers from ~2 years of history so the system does not
face its first live winter cold:

  1. Fetch Open-Meteo **Previous-Runs** day-1-lead forecasts-as-issued for the
     site (endpoint ``previous-runs-api.open-meteo.com``, archived since
     01/2024). If the forecast-as-issued variables are unavailable it degrades
     to the plain **Historical Forecast API** with a loud WARNING that the data
     is analysis-not-forecast (still useful for the geometric shademap, less so
     for the weather-error day-ahead bias).
  2. Reconstruct per-plane HOURLY modeled curves (beam / diffuse / ghi / kc) by
     importing the repo's ``core/`` package (the SAME physics the live engine
     runs) — pure Python, no numpy.
  3. Pull measured hourly per-module energy from the operator's HA long-term
     statistics via the **WebSocket API** (``recorder/statistics_during_period``;
     ``--ha-url`` + ``--token`` CLI args).
  4. Compute the two learner bootstraps + the quantile relerr ring and emit a
     bootstrap JSON matching the frozen contract schema; the
     ``balcony_solar_forecast.import_bootstrap`` service ingests it
     (validate + clamp, rejects unknown schema).

Robust to gaps: any day missing weather or actuals is skipped with a warning.

W1 refactor (SPEC §6): the reconstruction / bootstrap MATH is now a HA-free
core module — ``balcony_solar_forecast.core.bootstrap_build`` — shared with the
in-process ``run_bootstrap`` HA action. The Open-Meteo Previous-Runs weather
fetch + payload parse is likewise shared, in the HA-free
``balcony_solar_forecast.core.openmeteo_backfill`` (session injected), so the CLI
and the action cannot drift on the provider contract. This script is the
DEV-machine CLI wrapper: it owns the aiohttp session + the HA WebSocket LTS pull
(long-lived token, which the in-process action replaces with a recorder read),
hands the fetched weather records + per-module hourly actuals to the core, and
writes the JSON. The shared core + weather-fetch names are re-exported below so
``backfill.<name>`` keeps addressing them (the unit-test anchor in
``tests/core/test_backfill_math.py``).

The network (aiohttp) layer is imported lazily inside the async functions so the
unit tests exercise the math with fixture weather and NO network.

Usage (see docs/BACKFILL.md for the full operator runbook):

    python scripts/backfill.py \\
        --ha-url http://homeassistant.local:8123 \\
        --token "$HA_LONG_LIVED_TOKEN" \\
        --start 2024-07-01 --end 2026-07-01 \\
        --out bootstrap.json

Add ``--dry-run`` to fetch + reconstruct + summarise WITHOUT writing the file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import types
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Make the HA-free core importable WITHOUT running the package __init__ (which
# imports Home Assistant). Mirrors tests/core/conftest.py: register the package
# roots as namespace packages pointing at the real dirs, so
# ``import balcony_solar_forecast.core.bootstrap_build`` resolves straight to the
# file.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_CUSTOM_COMPONENTS = _REPO_ROOT / "custom_components"
_PKG_DIR = _CUSTOM_COMPONENTS / "balcony_solar_forecast"


def _register_namespace_package(name: str, path: Path) -> None:
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)]
    mod.__package__ = name
    sys.modules[name] = mod


if str(_CUSTOM_COMPONENTS) not in sys.path:
    sys.path.insert(0, str(_CUSTOM_COMPONENTS))
_register_namespace_package("balcony_solar_forecast", _PKG_DIR)
_register_namespace_package("balcony_solar_forecast.core", _PKG_DIR / "core")

# Now safe to import the pure core (no HA).
from balcony_solar_forecast.core import horizon  # noqa: E402

# Re-export the pure bootstrap core so the existing tests (and any downstream
# importer) keep addressing these via ``backfill.<name>`` after the W1 refactor
# moved the math into core/bootstrap_build.py (SPEC §6). The CLI wrapper below
# only owns fetching + JSON output.
from balcony_solar_forecast.core.bootstrap_build import (  # noqa: E402,F401
    BootstrapAccumulator,
    HourlyWeather,
    PlaneHourReconstruction,
    _classify_cloud,
    _day_part_for_slot,
    _filter_actuals_for_day,
    _group_by_day,
    _is_frozen_hourly,
    _resolve_hourly_measured,
    _rls_step,
    _shade_update,
    accumulate_days,
    build_bootstrap_json,
    half_year_index,
    is_quasi_clear,
    load_site,
    process_day,
    process_day_hourly,
    reconstruct_plane_hour,
    resolve_tz,
    shademap_bin_key,
    site_signature,
)

# The Open-Meteo Previous-Runs weather fetch + payload parse is HA-free and
# shared with the in-process run_bootstrap action (session injected); re-export
# the names so ``backfill.<name>`` (and the fetch-mocking parity test) keep
# addressing them from this module.
from balcony_solar_forecast.core.openmeteo_backfill import (  # noqa: E402,F401
    HISTORICAL_FORECAST_URL,
    PREVIOUS_RUN_LEAD_DAY,
    PREVIOUS_RUNS_URL,
    _as_utc_hour,
    _get_json,
    _num,
    fetch_weather_range,
    parse_hourly_payload,
)

_LOGGER = logging.getLogger("balcony_solar_forecast.backfill")

# aiohttp client timeout for the WebSocket LTS pull below (the Open-Meteo
# weather fetch owns its own copy in ``core.openmeteo_backfill``).
_HTTP_TIMEOUT_SECONDS = 120.0


# ===========================================================================
# Network layer (aiohttp, lazy) — WebSocket LTS pull.
#
# The Open-Meteo Previous-Runs weather fetch + payload parse now live in the
# HA-free ``core.openmeteo_backfill`` (re-exported above), shared with the
# in-process run_bootstrap action. This CLI wrapper keeps only the WebSocket
# long-lived-token LTS reader — the action replaces it with an in-process
# recorder read.
# ===========================================================================


# A single hourly statistics response for all modules over a multi-year range
# exceeds HA's 4 MiB WebSocket frame limit (MESSAGE_TOO_BIG). Query in windows
# small enough that each response stays well under the cap: 90 days x 24 h x
# ~8 sensors ~= 17k rows ~= 1 MiB.
_LTS_WINDOW_DAYS = 90


def _lts_windows(
    start_dt: datetime, end_dt: datetime, window_days: int
) -> list[tuple[datetime, datetime]]:
    """Split [start_dt, end_dt) into consecutive [win_start, win_end) chunks."""
    windows: list[tuple[datetime, datetime]] = []
    step = timedelta(days=window_days)
    cur = start_dt
    while cur < end_dt:
        nxt = min(cur + step, end_dt)
        windows.append((cur, nxt))
        cur = nxt
    return windows


async def fetch_lts_hourly(
    session,
    *,
    ha_url: str,
    token: str,
    statistic_ids: list[str],
    start: date,
    end: date,
) -> dict[str, dict[str, float]]:
    """Pull hourly per-statistic mean power from HA LTS via the WebSocket API.

    Connects to ``{ha_url}/api/websocket``, authenticates with the long-lived
    token, and issues ``recorder/statistics_during_period`` commands at hourly
    period — chunked into ``_LTS_WINDOW_DAYS`` windows so no single response
    trips HA's 4 MiB WS frame limit. Returns ``{statistic_id: {iso_hour:
    mean_wh}}`` where mean_wh = mean power (W) over the hour == Wh (matches
    coordinator._async_read_daily_actuals).

    Raises on connection/auth failure — the caller aborts (no measured energy
    means nothing to train against).
    """
    import aiohttp  # lazy

    ws_url = ha_url.rstrip("/").replace("http://", "ws://").replace(
        "https://", "wss://"
    ) + "/api/websocket"
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(
        days=1
    )

    out: dict[str, dict[str, float]] = {sid: {} for sid in statistic_ids}
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
    # max_msg_size=0 lifts the client-side frame cap; chunking keeps each
    # response small regardless (belt and suspenders against the server cap).
    async with session.ws_connect(
        ws_url, timeout=timeout, max_msg_size=0
    ) as ws:
        # 1) auth_required -> auth -> auth_ok
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": token})
        auth = await ws.receive_json()
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"HA WebSocket auth failed: {auth}")

        # 2) statistics_during_period, one command per time window
        for msg_id, (win_start, win_end) in enumerate(
            _lts_windows(start_dt, end_dt, _LTS_WINDOW_DAYS), start=1
        ):
            await ws.send_json(
                {
                    "id": msg_id,
                    "type": "recorder/statistics_during_period",
                    "start_time": win_start.isoformat(),
                    "end_time": win_end.isoformat(),
                    "statistic_ids": statistic_ids,
                    "period": "hour",
                    "types": ["mean"],
                }
            )
            result = await _await_ws_result(ws, msg_id)
            _parse_lts_result(result, out)

    return out


async def _await_ws_result(ws, msg_id: int) -> dict:
    """Receive frames until the result for ``msg_id`` arrives."""
    while True:
        frame = await ws.receive_json()
        if frame.get("id") == msg_id and frame.get("type") == "result":
            if not frame.get("success", False):
                raise RuntimeError(f"HA statistics query failed: {frame.get('error')}")
            return frame.get("result", {})


def _parse_lts_result(
    result: dict, out: dict[str, dict[str, float]]
) -> None:
    """Fold a statistics_during_period result into {sid: {iso_hour: wh}} (pure).

    Each row has ``start`` (ms epoch or ISO) and ``mean`` (W). mean power over
    an hour == Wh. Rows without a mean are skipped. Kept pure + separate so the
    tests can exercise the parse without a WebSocket.
    """
    if not isinstance(result, dict):
        return
    for sid, rows in result.items():
        if sid not in out or not isinstance(rows, list):
            continue
        bucket = out[sid]
        for row in rows:
            if not isinstance(row, dict):
                continue
            mean = _num(row.get("mean"))
            if mean is None:
                continue
            hkey = _stat_row_hour(row.get("start"))
            if hkey is None:
                continue
            bucket[hkey] = bucket.get(hkey, 0.0) + mean  # W*1h = Wh


def _stat_row_hour(start: object) -> str | None:
    """Normalise a statistics row ``start`` to an ISO-UTC hour key.

    HA WebSocket returns ``start`` as epoch MILLISECONDS; the in-process
    recorder API returns epoch SECONDS. Disambiguate by magnitude (> 1e11 ⇒
    ms) so the backfill can never repeat the live `_actuals` epoch bug, and
    accept ISO strings from older/other paths.
    """
    if isinstance(start, (int, float)):
        ts = float(start)
        if ts > 1e11:  # ms wire format; seconds stay < 1e11 until year 5138
            ts /= 1000.0
        dt = datetime.fromtimestamp(ts, tz=UTC)
    elif isinstance(start, str):
        try:
            dt = datetime.fromisoformat(start)
        except ValueError:
            return None
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    else:
        return None
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


# ===========================================================================
# Orchestration (async) — fetch, reconstruct, accumulate, emit.
# ===========================================================================


async def run_backfill(args: argparse.Namespace) -> int:
    """Top-level async driver. Returns a process exit code."""
    import aiohttp  # lazy

    site = load_site(Path(args.site) if args.site else None)
    site_tz = resolve_tz(getattr(args, "tz", None))
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        _LOGGER.error("--end (%s) is before --start (%s)", end, start)
        return 2

    # Modules with a measured entity are our channels + statistic ids.
    stat_ids = sorted({
        p.actual_entity for p in site.planes if p.actual_entity
    })
    if not stat_ids:
        _LOGGER.error("Site has no planes with actual_entity; nothing to train")
        return 2
    # Map statistic_id -> the plane/channel name(s) it feeds. In the reference
    # site each module has a distinct entity, but two planes can share one
    # sensor (M2/M3 both on port config in some setups); handle the general
    # case by resolving per-plane below.
    svf_by_plane = {p.name: horizon.sky_view_factor(p) for p in site.planes}

    async with aiohttp.ClientSession() as session:
        _LOGGER.info(
            "Fetching hourly weather %s..%s from Open-Meteo (Previous-Runs)",
            start, end,
        )
        weather, as_issued = await fetch_weather_range(
            session,
            latitude=site.latitude,
            longitude=site.longitude,
            start=start,
            end=end,
        )
        if not weather:
            _LOGGER.error("No weather returned for the requested range")
            return 3
        _LOGGER.info(
            "Got %d hourly weather records (%s)",
            len(weather),
            "as-issued forecast" if as_issued
            else "ANALYSIS fallback (NOT as-issued)",
        )

        _LOGGER.info("Pulling per-module LTS from %s", args.ha_url)
        lts_by_entity = await fetch_lts_hourly(
            session,
            ha_url=args.ha_url,
            token=args.token,
            statistic_ids=stat_ids,
            start=start,
            end=end,
        )

    # Re-key LTS from entity_id -> channel(plane) name for accumulate_days.
    hourly_actuals = _entity_to_channel_actuals(site, lts_by_entity)

    acc = accumulate_days(
        site, weather, hourly_actuals, svf_by_plane=svf_by_plane, tz=site_tz
    )

    _summarise(acc, as_issued)

    if acc.days_used == 0:
        _LOGGER.error("No usable days — bootstrap would be empty; aborting")
        return 4

    bootstrap = build_bootstrap_json(acc, site)

    if args.dry_run:
        _LOGGER.info("--dry-run: not writing %s", args.out)
        return 0

    out_path = Path(args.out)
    out_path.write_text(json.dumps(bootstrap, indent=2), encoding="utf-8")
    _LOGGER.info("Wrote bootstrap JSON -> %s", out_path.resolve())
    return 0


def _entity_to_channel_actuals(
    site,
    lts_by_entity: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """Map entity-id-keyed LTS to plane/channel-keyed actuals.

    Each plane's ``actual_entity`` names the statistic it measures; several
    planes MAY share one sensor (then they get the same measured curve — the
    reconstruction disambiguates by plane geometry). Modules without a sensor
    or without any LTS rows are omitted.
    """
    out: dict[str, dict[str, float]] = {}
    for plane in site.planes:
        ent = plane.actual_entity
        if not ent:
            continue
        rows = lts_by_entity.get(ent)
        if rows:
            out[plane.name] = dict(rows)
    return out


def _summarise(acc, as_issued: bool) -> None:
    n_bins = sum(len(b) for b in acc.shade.values())
    _LOGGER.info(
        "Bootstrap summary: %d days used, %d skipped | shademap: %d channels, "
        "%d bins, %d samples | day-ahead: %d cells, %d RLS steps | quantile: "
        "%d bins, %d samples | source: %s",
        acc.days_used,
        acc.days_skipped,
        len(acc.shade),
        n_bins,
        acc.shade_samples,
        len(acc.bias),
        acc.bias_samples,
        len(acc.quantile_state.bins),
        acc.quantile_samples,
        "as-issued" if as_issued else "ANALYSIS (degraded)",
    )


# ===========================================================================
# CLI
# ===========================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backfill.py",
        description=(
            "Backfill the balcony_solar_forecast learner bootstrap from "
            "Open-Meteo Previous-Runs forecasts + HA long-term statistics "
            "(SPEC §6). Run on the DEV machine, not on HA."
        ),
    )
    p.add_argument("--ha-url", required=True,
                   help="HA base URL, e.g. http://homeassistant.local:8123")
    p.add_argument("--token", required=True,
                   help="HA long-lived access token (WebSocket auth)")
    p.add_argument("--start", required=True,
                   help="Range start, ISO date YYYY-MM-DD (LTS since 2024-07)")
    p.add_argument("--end", required=True, help="Range end, ISO date YYYY-MM-DD")
    p.add_argument("--out", default="bootstrap.json",
                   help="Output bootstrap JSON path (default: bootstrap.json)")
    p.add_argument("--site", default=None,
                   help="Optional site JSON override (defaults to the shipped "
                        "reference site DEFAULT_SITE)")
    p.add_argument("--tz", default=None,
                   help="Site IANA timezone (e.g. Europe/Berlin) for local-hour "
                        "day-part / cloud-class keying; defaults to UTC")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch + reconstruct + summarise WITHOUT writing --out")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Debug logging (per-day skips)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return asyncio.run(run_backfill(args))
    except KeyboardInterrupt:  # pragma: no cover
        _LOGGER.warning("Interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
