"""Open-Meteo client + payload parsing for Balcony Solar Forecast.

Owner: glue. One call every 30 min pulls the raw irradiance components
(GHI/DNI/DHI + 2 m temperature) at 15-min resolution plus the hourly
cloud / visibility / snow context. We transpose locally (SPEC §4), so the
fetch does NOT use Open-Meteo's server-side GTI param — hence there is no
azimuth conversion here; the only API-boundary convention in play is
``timezone=UTC`` and the fixed ``models=icon_seamless``.

The module is split so the *pure* parts — URL building, payload SHAPE
validation and the typed parse into a ``WeatherSeries`` — are importable
and unit-testable WITHOUT aiohttp (which only ships inside Home Assistant).
``aiohttp`` is imported lazily inside the network coroutine.

Timestamps: Open-Meteo returns local-naive ISO strings, but we request
``timezone=UTC``, so every ``time`` entry is a UTC wall-clock without a
suffix; we attach ``timezone.utc``. Interval semantics: minutely_15 and
hourly values are backward-averaged means over the interval that *ends* at
the stamped time for radiation — Open-Meteo documents shortwave_radiation
as the mean of the *preceding* 15 min. We therefore shift each 15-min
sample so ``WeatherSlot.start`` is the interval START (stamp − 15 min),
matching the core's "value = mean over [start, start+15min)" contract.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta

from .const import (
    OPEN_METEO_HOURLY,
    OPEN_METEO_MINUTELY_15,
    OPEN_METEO_MODEL,
    OPEN_METEO_URL,
)
from .core.types import WeatherSeries, WeatherSlot

_LOGGER = logging.getLogger(__name__)

# Retry policy (SPEC §4: backoff with jitter, bounded tries).
MAX_TRIES = 3
_BASE_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 20.0
_REQUEST_TIMEOUT_SECONDS = 30.0
# A server-requested wait (429 Retry-After) longer than this must NOT stall the
# recompute tick — raise immediately; the coordinator keeps serving the
# last-good cache and retries on its own cadence (SPEC §7 degradation ladder).
_RETRY_AFTER_MAX_INLINE_SECONDS = 30.0

# Near-term window (in 15-min samples) that must carry at least one non-null
# radiation sample for a payload to count as usable: 24 h * 4 slots/h. The
# model-horizon tail beyond this being null is normal and tolerated.
_NEAR_TERM_M15_SAMPLES = 24 * 4


class FetchError(Exception):
    """Raised when a forecast could not be fetched or validated.

    ``retryable`` marks transient failures (network, 5xx, 429, malformed body)
    versus permanent ones (other 4xx client errors); the coordinator degrades
    the same way for both, but the flag aids logging and future backoff tuning.
    ``retry_after`` carries a server-requested wait in seconds (from a 429
    Retry-After header) when one was parseable; it stays None otherwise.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Pure helpers (no aiohttp) — URL, validation, parse
# ---------------------------------------------------------------------------


def build_params(latitude: float, longitude: float, forecast_days: int) -> dict[str, str]:
    """Build the Open-Meteo query params (SPEC §4, one call).

    Kept as an ordered dict of *strings* so it is trivially comparable in
    tests and free of client-library quirks. No azimuth/tilt params: we
    fetch raw components and transpose locally.
    """
    return {
        "latitude": _fmt_coord(latitude),
        "longitude": _fmt_coord(longitude),
        "minutely_15": ",".join(OPEN_METEO_MINUTELY_15),
        "hourly": ",".join(OPEN_METEO_HOURLY),
        "models": OPEN_METEO_MODEL,
        "forecast_days": str(int(forecast_days)),
        "timezone": "UTC",
    }


def _fmt_coord(value: float) -> str:
    """Format a coordinate with enough precision, trimming noise."""
    return f"{float(value):.6f}"


def _require(payload: dict, key: str) -> dict:
    """Return ``payload[key]`` as a dict or raise a non-retryable FetchError."""
    block = payload.get(key)
    if not isinstance(block, dict):
        raise FetchError(
            f"Open-Meteo payload missing '{key}' block", retryable=False
        )
    return block


def _require_array(block: dict, key: str, expected_len: int | None) -> list:
    """Return ``block[key]`` as a list, checking length when given."""
    arr = block.get(key)
    if not isinstance(arr, list):
        raise FetchError(
            f"Open-Meteo array '{key}' missing or not a list", retryable=False
        )
    if expected_len is not None and len(arr) != expected_len:
        raise FetchError(
            f"Open-Meteo array '{key}' length {len(arr)} != {expected_len}",
            retryable=False,
        )
    return arr


def validate_payload(payload: object) -> None:
    """Validate the SHAPE of an Open-Meteo response (SPEC §4: not just 200).

    Checks that the ``minutely_15`` and ``hourly`` blocks exist, that every
    requested variable array is present, that each array matches its own
    ``time`` array length, and that the ``time`` arrays themselves are
    non-empty. Value contents (None allowed) are checked at parse time.
    Raises a non-retryable ``FetchError`` on any structural problem — a
    malformed body will not fix itself on retry.
    """
    if not isinstance(payload, dict):
        raise FetchError("Open-Meteo payload is not a JSON object", retryable=False)

    m15 = _require(payload, "minutely_15")
    hourly = _require(payload, "hourly")

    m15_time = _require_array(m15, "time", None)
    if not m15_time:
        raise FetchError("Open-Meteo minutely_15.time is empty", retryable=False)
    n15 = len(m15_time)
    for var in OPEN_METEO_MINUTELY_15:
        _require_array(m15, var, n15)

    h_time = _require_array(hourly, "time", None)
    if not h_time:
        raise FetchError("Open-Meteo hourly.time is empty", retryable=False)
    nh = len(h_time)
    for var in OPEN_METEO_HOURLY:
        _require_array(hourly, var, nh)

    # HTTP-200-with-nulls guard (SPEC §4/§7: validate beyond HTTP status,
    # never degrade silently). Open-Meteo returns 200 with all-null value
    # arrays on a model outage and for the tail beyond the model horizon.
    # A structurally intact but content-empty payload must NOT be treated as
    # a good forecast (it would parse to an all-zero PV day and poison the
    # last-good cache). We require at least one non-null shortwave-radiation
    # sample in the near-term window (the model horizon tail being null is
    # normal and tolerated). This is retryable: a transient outage may clear.
    swr = m15.get("shortwave_radiation")
    if isinstance(swr, list):
        window = swr[:_NEAR_TERM_M15_SAMPLES]
        if window and all(v is None for v in window):
            raise FetchError(
                "Open-Meteo returned a null radiation window "
                "(model outage / empty payload)",
                retryable=True,
            )


def radiation_coverage(payload: dict) -> int:
    """Count non-null shortwave-radiation samples in a payload (0 if absent).

    A coarse "how much real forecast is in here" measure used by the
    coordinator to refuse overwriting a good last-good cache with a payload
    that carries strictly less non-null coverage (e.g. a partial-outage
    response). Purely structural: never raises.
    """
    m15 = payload.get("minutely_15") if isinstance(payload, dict) else None
    if not isinstance(m15, dict):
        return 0
    swr = m15.get("shortwave_radiation")
    if not isinstance(swr, list):
        return 0
    return sum(1 for v in swr if v is not None)


def _parse_time(value: object) -> datetime:
    """Parse an Open-Meteo (UTC, no suffix) ISO time into an aware UTC dt."""
    if not isinstance(value, str):
        raise FetchError(f"Non-string time value: {value!r}", retryable=False)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as err:
        raise FetchError(
            f"Unparseable time '{value}': {err}", retryable=False
        ) from err
    # timezone=UTC requested: naive stamps are UTC wall-clock.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _to_float(value: object) -> float | None:
    """Coerce an Open-Meteo scalar to float; None/absent stays None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hourly_lookup(hourly: dict, var: str) -> dict[datetime, float | None]:
    """Map hour-start UTC datetimes to a scalar for one hourly variable."""
    times = hourly["time"]
    values = hourly[var]
    out: dict[datetime, float | None] = {}
    for t, v in zip(times, values, strict=False):
        out[_parse_time(t)] = _to_float(v)
    return out


def parse_weather(payload: dict) -> WeatherSeries:
    """Parse a validated Open-Meteo payload into a ``WeatherSeries``.

    Each 15-min radiation sample is a backward-average whose stamp marks the
    interval END; we shift by −15 min so ``WeatherSlot.start`` is the
    interval START (core contract). Hourly context (clouds/visibility/snow)
    is carried onto each 15-min slot by flooring the slot start to its hour;
    ``snowfall`` is already centimetres in the Open-Meteo hourly block
    (``hourly_units.snowfall == "cm"``) and is passed through unchanged onto
    the slot; ``snow_depth`` is metres. Missing radiation values default to 0
    (night / gap); a missing temperature defaults to 0 °C but is rare.
    """
    validate_payload(payload)  # defensive: parse never trusts an unchecked body

    m15 = payload["minutely_15"]
    hourly = payload["hourly"]

    times = [_parse_time(t) for t in m15["time"]]
    ghi = m15[OPEN_METEO_MINUTELY_15[0]]  # shortwave_radiation
    dni = m15[OPEN_METEO_MINUTELY_15[1]]  # direct_normal_irradiance
    dhi = m15[OPEN_METEO_MINUTELY_15[2]]  # diffuse_radiation
    temp = m15[OPEN_METEO_MINUTELY_15[3]]  # temperature_2m

    cloud_low = _hourly_lookup(hourly, "cloud_cover_low")
    cloud_mid = _hourly_lookup(hourly, "cloud_cover_mid")
    cloud_high = _hourly_lookup(hourly, "cloud_cover_high")
    visibility = _hourly_lookup(hourly, "visibility")
    snowfall = _hourly_lookup(hourly, "snowfall")
    snow_depth = _hourly_lookup(hourly, "snow_depth")

    slots: list[WeatherSlot] = []
    for i, stamp in enumerate(times):
        # Stamp = interval end (backward-averaged); slot start = stamp − 15 min.
        start = stamp - timedelta(minutes=15)
        hour_key = start.replace(minute=0, second=0, microsecond=0)
        # snowfall is already cm (hourly); snow_depth stays metres.
        slots.append(
            WeatherSlot(
                start=start,
                ghi=max(0.0, _to_float(ghi[i]) or 0.0),
                dni=max(0.0, _to_float(dni[i]) or 0.0),
                dhi=max(0.0, _to_float(dhi[i]) or 0.0),
                temp_c=_to_float(temp[i]) or 0.0,
                cloud_low=cloud_low.get(hour_key) or 0.0,
                cloud_mid=cloud_mid.get(hour_key) or 0.0,
                cloud_high=cloud_high.get(hour_key) or 0.0,
                visibility_m=visibility.get(hour_key) or 0.0,
                snowfall_cm=snowfall.get(hour_key) or 0.0,
                snow_depth_m=snow_depth.get(hour_key) or 0.0,
            )
        )
    return WeatherSeries(slots=tuple(slots))


# ---------------------------------------------------------------------------
# Network (aiohttp) — imported lazily so the pure parts test without it
# ---------------------------------------------------------------------------


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter for retry ``attempt`` (1-based)."""
    ceiling = min(_MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)


def _parse_retry_after(value: object) -> float | None:
    """Non-negative seconds from a Retry-After header, or None. Never raises.

    Only the delta-seconds form (RFC 7231) is honoured; an HTTP-date or any
    non-numeric value yields None — a plain retryable error with no server
    wait attached. A negative value is clamped to 0.
    """
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


class OpenMeteoFetcher:
    """Async Open-Meteo client with SHAPE validation and bounded retries.

    The ``aiohttp.ClientSession`` is injected (HA supplies a shared one via
    ``async_get_clientsession``); the fetcher never owns or closes it.
    """

    def __init__(self, session, *, url: str = OPEN_METEO_URL) -> None:
        self._session = session
        self._url = url

    async def async_fetch(
        self, latitude: float, longitude: float, forecast_days: int
    ) -> WeatherSeries:
        """Fetch, validate and parse one forecast into a ``WeatherSeries``.

        Retries transient failures up to ``MAX_TRIES`` with jittered backoff;
        a 4xx or a structurally invalid body fails fast (non-retryable).
        Always raises ``FetchError`` on failure — never returns partial data.
        """
        payload = await self._async_fetch_payload(
            latitude, longitude, forecast_days
        )
        return parse_weather(payload)

    async def async_fetch_raw(
        self, latitude: float, longitude: float, forecast_days: int
    ) -> dict:
        """Fetch + validate and return the raw payload (for the last-good store)."""
        return await self._async_fetch_payload(latitude, longitude, forecast_days)

    async def _async_fetch_payload(
        self, latitude: float, longitude: float, forecast_days: int
    ) -> dict:
        """Fetch + SHAPE-validate one payload with bounded retries.

        Transient failures retry up to ``MAX_TRIES`` with jittered exponential
        backoff. A 429 carrying a parseable ``Retry-After`` overrides the
        jitter — we honour the server's wait exactly, but only inline it when
        it is short enough (<= ``_RETRY_AFTER_MAX_INLINE_SECONDS``) to hold the
        recompute tick open; a longer wait is re-raised immediately so the
        coordinator keeps serving the last-good cache and retries on its own
        cadence (SPEC §7). Non-retryable errors (other 4xx, malformed body)
        fail fast.
        """
        import aiohttp  # lazy: only present inside Home Assistant

        params = build_params(latitude, longitude, forecast_days)
        last_error: FetchError | None = None
        for attempt in range(1, MAX_TRIES + 1):
            try:
                payload = await self._request_once(aiohttp, params)
                validate_payload(payload)
                return payload
            except FetchError as err:
                last_error = err
                if not err.retryable or attempt == MAX_TRIES:
                    raise
                if err.retry_after is not None:
                    # A too-long server wait must not stall the tick (SPEC §7).
                    if err.retry_after > _RETRY_AFTER_MAX_INLINE_SECONDS:
                        raise
                    delay = err.retry_after
                else:
                    delay = _backoff_delay(attempt)
                _LOGGER.debug(
                    "Open-Meteo fetch attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    MAX_TRIES,
                    err,
                    delay,
                )
                await asyncio.sleep(delay)
        # Unreachable: the loop either returns or raises.
        raise last_error or FetchError("Open-Meteo fetch failed")

    async def _request_once(self, aiohttp, params: dict[str, str]) -> dict:
        """One HTTP GET; classify errors into retryable / non-retryable."""
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
        try:
            async with self._session.get(
                self._url, params=params, timeout=timeout
            ) as resp:
                status = resp.status
                if status >= 500:
                    raise FetchError(
                        f"Open-Meteo server error {status}", retryable=True
                    )
                # 429 is a transient rate limit, NOT a permanent client error:
                # honour Retry-After when the server sends a parseable seconds
                # value so we do not hammer it inside the backoff budget.
                if status == 429:
                    raise FetchError(
                        "Open-Meteo rate limited (429)",
                        retryable=True,
                        retry_after=_parse_retry_after(
                            resp.headers.get("Retry-After")
                        ),
                    )
                if status >= 400:
                    raise FetchError(
                        f"Open-Meteo client error {status}", retryable=False
                    )
                try:
                    return await resp.json()
                except (aiohttp.ContentTypeError, ValueError) as err:
                    raise FetchError(
                        f"Open-Meteo returned non-JSON body: {err}", retryable=True
                    ) from err
        except FetchError:
            raise
        except TimeoutError as err:
            raise FetchError(
                f"Open-Meteo request timed out: {err}", retryable=True
            ) from err
        except aiohttp.ClientError as err:
            raise FetchError(
                f"Open-Meteo request failed: {err}", retryable=True
            ) from err
