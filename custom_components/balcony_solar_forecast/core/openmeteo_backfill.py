"""Open-Meteo Previous-Runs / Historical-Forecast weather fetch (SPEC §12.4).

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
from datetime import UTC, date, datetime, timedelta, tzinfo

from .. import const
from .bootstrap_build import HourlyWeather

_LOGGER = logging.getLogger(__name__)

# --- Open-Meteo endpoints (SPEC §12.4, verified 2026-07-06) -------------------
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
# Day-1 lead: value predicted ~24 h before valid time (SPEC §12.1/§15.2: as-issued).
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
# Hour-start cloud, visibility and snow inputs. Missing fields are recorded
# alongside the physical inputs used for historical reconstruction.
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
    context_suffix: str | None = None,
) -> list[HourlyWeather]:
    """Parse an Open-Meteo hourly payload into HourlyWeather records (pure).

    ``var_suffix`` is appended to each radiation variable name when reading the
    Previous-Runs API (e.g. "_previous_day1"); the Historical Forecast API uses
    an empty suffix. Radiation is the preceding interval mean. Cloud layers,
    visibility and snow depth use the context at that interval's start. An
    explicit context_suffix identifies a degraded provider request.

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

    context_suffix = var_suffix if context_suffix is None else context_suffix
    columns = {name: _col(name + context_suffix) for name in _CONTEXT_VARS}

    def _at(col: list, i: int) -> float | None:
        return _num(col[i]) if 0 <= i < len(col) else None

    indices = {_as_utc_hour(stamp): i for i, stamp in enumerate(times) if isinstance(stamp, str)}
    out: list[HourlyWeather] = []
    for end, i in indices.items():
        start = end - timedelta(hours=1)
        context_index = indices.get(start, -1)
        context = {name: _at(col, context_index) for name, col in columns.items()}
        missing = tuple(name for name, value in context.items() if value is None)
        ghi = _at(ghi_c, i)
        dni = _at(dni_c, i)
        dhi = _at(dhi_c, i)
        temp = _at(temp_c, i)
        # Physics inputs are mandatory; drop the hour if any is missing.
        if ghi is None or dni is None or dhi is None or temp is None:
            continue
        out.append(
            HourlyWeather(
                # Radiation is the preceding-hour mean, while recorder keys
                # identify the beginning of that interval (also at midnight).
                start=start,
                ghi=max(0.0, ghi),
                dni=max(0.0, dni),
                dhi=max(0.0, dhi),
                temp_c=temp,
                cloud_low=context["cloud_cover_low"] or 0.0,
                cloud_mid=context["cloud_cover_mid"] or 0.0,
                cloud_high=context["cloud_cover_high"] or 0.0,
                visibility_m=context["visibility"],
                snow_depth_m=context["snow_depth"] or 0.0,
                weather_source=("analysis_fallback" if not var_suffix else
                                "day1_radiation_unverified_context" if context_suffix != var_suffix else
                                "day1_partial_context" if missing else "as_issued"),
                missing_context=missing,
            )
        )
    return out


# ===========================================================================
# Network layer (aiohttp, lazy) — Previous-Runs weather with degrade.
# ===========================================================================


class _UnsupportedRequest(ValueError):
    """The provider rejected a variable combination before returning weather."""


async def fetch_weather_range(
    session,
    *,
    latitude: float,
    longitude: float,
    start: date,
    end: date,
    tz: tzinfo = UTC,
) -> tuple[list[HourlyWeather], bool]:
    """Fetch hourly as-issued weather for [start, end] (Previous-Runs API).

    Tries the Previous-Runs API (day-1 lead, forecast-as-issued). On failure —
    or if the suffixed radiation variables come back empty — degrades to the
    Historical Forecast API with a WARNING that the data is analysis, not
    forecast (SPEC §12.1: graceful degrade, still useful for the geometric
    shademap). Returns ``(records, is_as_issued)``.
    """
    if start > end:
        raise ValueError("Weather range must be ordered")
    lower = datetime.combine(start, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    upper = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(UTC)
    # Radiation is stamped at interval END. Include the following UTC date so
    # the final hour has its end stamp, then slice on canonical interval starts.
    def within(records: list[HourlyWeather]) -> list[HourlyWeather]:
        return [record for record in records if lower <= record.start < upper]

    suffix = f"_previous_day{PREVIOUS_RUN_LEAD_DAY}"
    prev_vars = [f"{v}{suffix}" for v in _RADIATION_VARS]
    hourly_vars = ",".join([*prev_vars, *(v + suffix for v in _CONTEXT_VARS)])
    params = {
        "latitude": f"{latitude:.6f}",
        "longitude": f"{longitude:.6f}",
        "hourly": hourly_vars,
        "models": const.OPEN_METEO_MODEL,
        "start_date": lower.date().isoformat(),
        "end_date": upper.date().isoformat(),
        "timezone": "UTC",
    }
    try:
        context_suffix = suffix
        try:
            payload = await _get_json(session, PREVIOUS_RUNS_URL, params)
        except _UnsupportedRequest:
            # Some models archive only the four physical inputs at day-1 lead.
            # Retain them and label the context explicitly in the export.
            context_suffix = ""
            params["hourly"] = ",".join([*prev_vars, *_CONTEXT_VARS])
            payload = await _get_json(session, PREVIOUS_RUNS_URL, params)
        records = within(parse_hourly_payload(payload, var_suffix=suffix, context_suffix=context_suffix))
        if records:
            return records, all(rec.weather_source == "as_issued" for rec in records)
        _LOGGER.warning(
            "Previous-Runs API returned no as-issued radiation for %s..%s; "
            "falling back to the Historical Forecast API (analysis, NOT "
            "as-issued forecast data)",
            start, end,
        )
    except Exception:  # noqa: BLE001 - degrade on any provider failure
        _LOGGER.warning(
            "Previous-Runs API fetch failed for %s..%s; falling back to "
            "the Historical Forecast API (analysis, NOT as-issued forecast)",
            start, end,
        )

    # --- Degrade: Historical Forecast API (plain, unsuffixed variables). ---
    hourly_vars = ",".join([*_RADIATION_VARS, *_CONTEXT_VARS])
    params["hourly"] = hourly_vars
    payload = await _get_json(session, HISTORICAL_FORECAST_URL, params)
    records = within(parse_hourly_payload(payload, var_suffix=""))
    return records, False


async def _get_json(session, url: str, params: dict) -> dict:
    import aiohttp  # lazy

    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
    async with session.get(url, params=params, timeout=timeout) as resp:
        if resp.status >= 400:
            text = await resp.text()
            if resp.status == 400:
                raise _UnsupportedRequest(f"HTTP 400 from {url}: {text[:200]}")
            raise RuntimeError(f"HTTP {resp.status} from {url}: {text[:200]}")
        return await resp.json()
