"""Last-good weather state and transitions, independent of Home Assistant.

WeatherCache owns the payload, parsed image, provenance, retry anchor and
availability ladder. The coordinator supplies only fetch/persist operations;
it does not mirror weather state. Stored payloads are adopted explicitly at
warm start, and accepted provider candidates replace the owned image atomically.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite

from .const import (
    FAILED_FETCH_MIN_INTERVAL_SECONDS,
    FETCH_INTERVAL_SECONDS,
    MAX_PAYLOAD_AGE_HOURS,
    MAX_PHYSICS_FALLBACK_AGE_HOURS,
    SLOT_MINUTES,
    STATUS_CACHED,
    STATUS_FRESH,
    STATUS_PHYSICS_FALLBACK,
    STATUS_UNAVAILABLE,
)
from .core.types import WeatherSeries
from .fetcher import FetchError, parse_weather

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeatherCandidate:
    """A fully parsed candidate, safe to commit as one cache image."""

    weather: WeatherSeries
    retain_prior: bool


def future_coverage(payload: dict, weather: WeatherSeries, now: datetime) -> int:
    """Count usable radiation intervals whose end is still in the future."""
    values = payload["minutely_15"]["shortwave_radiation"]
    duration = timedelta(minutes=SLOT_MINUTES)
    count = 0
    for slot, value in zip(weather.slots, values, strict=True):
        try:
            usable = value is not None and isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            usable = False
        if usable and slot.temp_c is not None and slot.start + duration > now:
            count += 1
    return count


def prepare_candidate(payload: dict, prior: dict | None,
                      prior_age: timedelta | None, now: datetime) -> WeatherCandidate:
    """Validate before persistence; expired history cannot veto recovery.

    A still-fresh, richer future horizon can bridge a partial provider outage.
    Only actual future coverage counts, and this preference ends at the normal
    cache-age limit. Invalid persisted weather is never a reason to refuse a
    usable replacement.
    """
    weather = parse_weather(payload)
    coverage = future_coverage(payload, weather, now)
    if coverage == 0:
        raise FetchError("Payload has no usable future weather intervals")
    retain = False
    if (prior is not None and prior_age is not None
            and prior_age <= timedelta(hours=MAX_PAYLOAD_AGE_HOURS)):
        try:
            previous = parse_weather(prior)
            retain = future_coverage(prior, previous, now) > coverage
        except FetchError:
            pass
    return WeatherCandidate(weather, retain)


@dataclass(slots=True)
class WeatherCache:
    """One owned weather image with separate payload-age and retry clocks.

    Payload dictionaries are transferred wholesale and treated as read-only.
    A successful but poorer candidate advances the retry clock only; it never
    rejuvenates the image operators and learners actually receive (SPEC §13).
    ``restore`` is the warm-start boundary. After that, only ``async_refresh``
    replaces the image; the persistence adapter is not a second runtime owner.
    """

    fetch_interval: timedelta = timedelta(seconds=FETCH_INTERVAL_SECONDS)
    payload: dict | None = None
    fetched_at: datetime | None = None
    attempted_at: datetime | None = None
    fetch_ok: bool = False
    last_error: str | None = None
    _parsed: WeatherSeries | None = field(default=None, init=False, repr=False)

    def restore(self, stored: dict | None) -> None:
        """Adopt persisted weather without claiming a successful live fetch."""
        payload = stored.get("payload") if stored else None
        if payload is not self.payload:
            self._parsed = None
        self.payload = payload
        stamp = stored.get("fetched_at") if stored else None
        try:
            fetched = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
        except ValueError:
            fetched = None
        if fetched is not None:
            fetched = fetched.replace(tzinfo=UTC) if fetched.tzinfo is None else fetched.astimezone(UTC)
        self.fetched_at = fetched

    def weather(self) -> WeatherSeries | None:
        """Reuse the parsed owned image until a replacement is committed."""
        if self.payload is None:
            return None
        if self._parsed is None:
            try:
                self._parsed = parse_weather(self.payload)
            except FetchError as err:
                _LOGGER.error("Stored payload no longer parses: %s", err)
                return None
        return self._parsed

    def age(self, now: datetime) -> timedelta | None:
        """Actual image age, independent of the most recent HTTP attempt."""
        return None if self.fetched_at is None else now - self.fetched_at

    def age_seconds(self, now: datetime) -> float | None:
        age = self.age(now)
        return None if age is None else max(0.0, age.total_seconds())

    def due(self, now: datetime) -> bool:
        """Use the success cadence or bounded failure retry interval."""
        if self.attempted_at is None:
            return True
        interval = self.fetch_interval if self.fetch_ok else min(
            self.fetch_interval, timedelta(seconds=FAILED_FETCH_MIN_INTERVAL_SECONDS),
        )
        return now - self.attempted_at >= interval

    def status_for_age(self, age: timedelta) -> str:
        """Degradation ladder with the original inclusive cache boundaries."""
        age = max(timedelta(0), age)
        if self.fetch_ok and age < self.fetch_interval:
            return STATUS_FRESH
        if age <= timedelta(hours=MAX_PAYLOAD_AGE_HOURS):
            return STATUS_CACHED
        if age <= timedelta(hours=MAX_PHYSICS_FALLBACK_AGE_HOURS):
            return STATUS_PHYSICS_FALLBACK
        return STATUS_UNAVAILABLE

    async def async_refresh(
        self,
        now: datetime,
        *,
        fetch: Callable[[], Awaitable[dict]],
        persist: Callable[[dict, str], None],
    ) -> None:
        """Fetch, validate/select, persist and commit one candidate.

        Invalid candidates leave both the image and its age intact. Cancellation
        propagates to the operation owner; it is not a provider failure.
        """
        try:
            payload = await fetch()
            candidate = prepare_candidate(payload, self.payload, self.age(now), now)
        except FetchError as err:
            self.attempted_at = now
            self.fetch_ok = False
            self.last_error = str(err)
            _LOGGER.warning("Open-Meteo fetch failed: %s", err)
            return
        self.attempted_at = now
        self.fetch_ok = True
        self.last_error = None
        if candidate.retain_prior:
            _LOGGER.warning("Keeping last-good payload with richer future coverage")
            return
        # A persistence failure must not leave the in-memory age pointing at a
        # payload that was never committed. The store setter itself schedules IO.
        persist(payload, now.isoformat())
        self.payload = payload
        self.fetched_at = now
        self._parsed = candidate.weather
