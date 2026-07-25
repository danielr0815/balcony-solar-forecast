"""Open-Meteo Previous-Runs / Historical-Forecast weather fetch (SPEC §6).

Shared, Home-Assistant-FREE weather beschaffung for the learner bootstrap. Both
the dev-machine CLI (``scripts/backfill.py``) and the in-process
``balcony_solar_forecast.run_bootstrap`` HA action need the SAME as-issued
hourly radiation (day-1-lead Previous-Runs, degrading to the plain Historical
Forecast API), so the fetch + payload parse live here once and are called with an
injected aiohttp ``session`` — the CLI passes its own ``aiohttp.ClientSession``,
the action passes ``aiohttp_client.async_get_clientsession(hass)``.

This is the ONE ``core/`` module that touches the network (aiohttp, imported
lazily). It is a DELIBERATE, documented exception to the "core is pure, no
network" rule so the two callers cannot drift on the provider contract; it stays
HA-free (imports only ``..const`` + the pure ``HourlyWeather``) and so remains
importable from the dev script via the same ``core`` namespace-package shim that
reaches ``bootstrap_build`` (bypassing the HA-importing package ``__init__``).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from .. import const
from .bootstrap_build import HourlyWeather

_LOGGER = logging.getLogger(__name__)

# --- Open-Meteo endpoints (SPEC §6, verified 2026-07-06) -------------------
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
# Day-1 lead: value predicted ~24 h before valid time (SPEC §2/§9: as-issued).
PREVIOUS_RUN_LEAD_DAY = 1
# The radiation + temperature variables we transpose locally. On the
# Previous-Runs API these carry a "_previous_dayN" suffix (as-issued); on the
# Historical Forecast API they are plain.
_RADIATION_VARS = (
    "shortwave_radiation",       # GHI
    "direct_normal_irradiance",  # DNI
    "diffuse_radiation",         # DHI
    "temperature_2m",
)
# Cloud / visibility / snow context for the cloud classifier. Best-effort:
# absent variables degrade the cloud class to "mixed" for that hour rather than
# aborting (the shademap does not need them; only the day-ahead bias does).
_CONTEXT_VARS = (
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "visibility",
    "snow_depth",
)
_HTTP_TIMEOUT_SECONDS = 120.0


# ===========================================================================
# Weather-payload parsing (pure) — Previous-Runs and Historical Forecast.
# ===========================================================================


def _as_utc_hour(value: str) -> datetime:
    """Parse an Open-Meteo (UTC, no suffix) hourly ISO stamp to aware UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def parse_hourly_payload(
    payload: dict,
    *,
    var_suffix: str = "",
) -> list[HourlyWeather]:
    """Parse an Open-Meteo hourly payload into HourlyWeather records (pure).

    ``var_suffix`` is appended to each radiation variable name when reading the
    Previous-Runs API (e.g. "_previous_day1"); the Historical Forecast API uses
    an empty suffix. Context variables (cloud/visibility/snow) are read
    UNSUFFIXED best-effort — the Previous-Runs API exposes forecast-as-issued
    radiation, while cloud context is taken from the same call's plain hourly
    block when present. Hours missing any of GHI/DNI/DHI/temp are dropped
    (the engine cannot transpose them); the caller sees a shorter list.

    Raises ``ValueError`` on a structurally broken payload (missing hourly
    block or time array) — the network layer maps that to a skipped range.
    """
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise ValueError("payload missing 'hourly' block")
    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        raise ValueError("payload 'hourly.time' missing or empty")

    def _col(name: str) -> list:
        col = hourly.get(name)
        return col if isinstance(col, list) else []

    ghi_c = _col(f"{_RADIATION_VARS[0]}{var_suffix}")
    dni_c = _col(f"{_RADIATION_VARS[1]}{var_suffix}")
    dhi_c = _col(f"{_RADIATION_VARS[2]}{var_suffix}")
    temp_c = _col(f"{_RADIATION_VARS[3]}{var_suffix}")

    cl_low = _col("cloud_cover_low")
    cl_mid = _col("cloud_cover_mid")
    cl_high = _col("cloud_cover_high")
    vis = _col("visibility")
    snow = _col("snow_depth")

    def _at(col: list, i: int) -> float | None:
        return _num(col[i]) if i < len(col) else None

    out: list[HourlyWeather] = []
    for i, stamp in enumerate(times):
        if not isinstance(stamp, str):
            continue
        ghi = _at(ghi_c, i)
        dni = _at(dni_c, i)
        dhi = _at(dhi_c, i)
        temp = _at(temp_c, i)
        # Physics inputs are mandatory; drop the hour if any is missing.
        if ghi is None or dni is None or dhi is None or temp is None:
            continue
        out.append(
            HourlyWeather(
                start=_as_utc_hour(stamp),
                ghi=max(0.0, ghi),
                dni=max(0.0, dni),
                dhi=max(0.0, dhi),
                temp_c=temp,
                cloud_low=_at(cl_low, i) or 0.0,
                cloud_mid=_at(cl_mid, i) or 0.0,
                cloud_high=_at(cl_high, i) or 0.0,
                visibility_m=_at(vis, i) or 0.0,
                snow_depth_m=_at(snow, i) or 0.0,
            )
        )
    return out


# ===========================================================================
# Network layer (aiohttp, lazy) — Previous-Runs weather with degrade.
# ===========================================================================


async def fetch_weather_range(
    session,
    *,
    latitude: float,
    longitude: float,
    start: date,
    end: date,
) -> tuple[list[HourlyWeather], bool]:
    """Fetch hourly as-issued weather for [start, end] (Previous-Runs API).

    Tries the Previous-Runs API (day-1 lead, forecast-as-issued). On failure —
    or if the suffixed radiation variables come back empty — degrades to the
    Historical Forecast API with a WARNING that the data is analysis, not
    forecast (SPEC §6: graceful degrade, still useful for the geometric
    shademap). Returns ``(records, is_as_issued)``.
    """
    suffix = f"_previous_day{PREVIOUS_RUN_LEAD_DAY}"
    prev_vars = [f"{v}{suffix}" for v in _RADIATION_VARS]
    hourly_vars = ",".join([*prev_vars, *_CONTEXT_VARS])
    params = {
        "latitude": f"{latitude:.6f}",
        "longitude": f"{longitude:.6f}",
        "hourly": hourly_vars,
        "models": const.OPEN_METEO_MODEL,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "timezone": "UTC",
    }
    try:
        payload = await _get_json(session, PREVIOUS_RUNS_URL, params)
        records = parse_hourly_payload(payload, var_suffix=suffix)
        if records:
            return records, True
        _LOGGER.warning(
            "Previous-Runs API returned no as-issued radiation for %s..%s; "
            "falling back to the Historical Forecast API (analysis, NOT "
            "as-issued forecast data)",
            start, end,
        )
    except Exception as err:  # noqa: BLE001 - degrade on any provider failure
        _LOGGER.warning(
            "Previous-Runs API fetch failed for %s..%s (%s); falling back to "
            "the Historical Forecast API (analysis, NOT as-issued forecast)",
            start, end, err,
        )

    # --- Degrade: Historical Forecast API (plain, unsuffixed variables). ---
    hourly_vars = ",".join([*_RADIATION_VARS, *_CONTEXT_VARS])
    params["hourly"] = hourly_vars
    payload = await _get_json(session, HISTORICAL_FORECAST_URL, params)
    records = parse_hourly_payload(payload, var_suffix="")
    return records, False


async def _get_json(session, url: str, params: dict) -> dict:
    import aiohttp  # lazy

    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
    async with session.get(url, params=params, timeout=timeout) as resp:
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(f"HTTP {resp.status} from {url}: {text[:200]}")
        return await resp.json()
