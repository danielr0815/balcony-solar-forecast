"""Versioned persistent store for Balcony Solar Forecast.

Owner: glue. One HA ``Store`` per config entry holds three things
(SPEC §4, §6):

  * the last-good Open-Meteo payload + its fetch timestamp (survives a
    restart so the degradation ladder starts from a warm cache);
  * a forecast-as-issued ring — one snapshot per calendar day (the curve we
    published that day, for later error / bias / quantile analysis);
  * a daily actuals ring — measured DC energy per module per day (read from
    recorder statistics by the nightly job).

Writes are bundled via ``async_delay_save`` (eMMC-friendly, ≤ a few
writes/day) with an explicit flush on unload / HA stop. Loading is
*validate-and-default*: a corrupt or wrong-version blob never crashes
setup — we fall back to an empty, well-formed state and log once
(SPEC §5 "Store validate-and-clamp beim Laden").

Everything here is plain JSON-serialisable dicts; the split between this
wrapper's own schema version and the outer HA ``Store`` version follows the
const contract (``STORAGE_VERSION`` = envelope, ``STORAGE_DATA_VERSION`` =
inner schema).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    PAYLOAD_MIN_SAVE_INTERVAL_SECONDS,
    STORAGE_DATA_VERSION,
    STORAGE_SAVE_DELAY_SECONDS,
    STORAGE_VERSION,
    STORE_KEY_ACTUALS_LOG,
    STORE_KEY_ISSUED_LOG,
    STORE_KEY_LAST_PAYLOAD,
)

_LOGGER = logging.getLogger(__name__)

# Ring sizes (SPEC §4/§6: 90-day error buffer + as-issued log).
_ISSUED_RING_DAYS = 90
_ACTUALS_RING_DAYS = 90

_SCHEMA_KEY = "schema_version"


def _empty_state() -> dict[str, Any]:
    """A well-formed, empty inner state."""
    return {
        _SCHEMA_KEY: STORAGE_DATA_VERSION,
        STORE_KEY_LAST_PAYLOAD: None,  # {"fetched_at": iso, "payload": {...}}
        STORE_KEY_ISSUED_LOG: {},  # {iso_date: snapshot}
        STORE_KEY_ACTUALS_LOG: {},  # {iso_date: {module: wh}}
    }


def _trim_ring(ring: dict[str, Any], keep: int) -> dict[str, Any]:
    """Keep the ``keep`` most recent ISO-date-keyed entries (lexicographic
    order == chronological for ISO dates)."""
    if len(ring) <= keep:
        return ring
    for stale in sorted(ring)[:-keep]:
        ring.pop(stale, None)
    return ring


class ForecastStore:
    """Thin, validating wrapper around one HA ``Store``."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION,
            f"balcony_solar_forecast.{entry_id}",
        )
        self._data: dict[str, Any] = _empty_state()
        # Monotonic timestamp of the last time the last-good payload was
        # scheduled for disk write; None until the first payload persist.
        # Used to time-gate payload writes (eMMC-wear budget, SPEC §4).
        self._last_payload_save_at: float | None = None

    # ------------------------------------------------------------------
    # Load / persist
    # ------------------------------------------------------------------

    async def async_load(self) -> None:
        """Load and validate; on any problem, reset to an empty state."""
        raw = await self._store.async_load()
        self._data = self._validate(raw)

    def _validate(self, raw: Any) -> dict[str, Any]:
        """Coerce a loaded blob into a well-formed state (never raises)."""
        if not isinstance(raw, dict):
            return _empty_state()
        version = raw.get(_SCHEMA_KEY)
        if version != STORAGE_DATA_VERSION:
            # No inner migrations yet: a foreign version is discarded but the
            # setup must not crash (SPEC §5). Add migrations here later.
            if version is not None:
                _LOGGER.warning(
                    "Discarding forecast store: schema %s != expected %s",
                    version,
                    STORAGE_DATA_VERSION,
                )
            return _empty_state()

        state = _empty_state()
        last = raw.get(STORE_KEY_LAST_PAYLOAD)
        if (
            isinstance(last, dict)
            and isinstance(last.get("payload"), dict)
            and isinstance(last.get("fetched_at"), str)
        ):
            state[STORE_KEY_LAST_PAYLOAD] = {
                "fetched_at": last["fetched_at"],
                "payload": last["payload"],
            }
        issued = raw.get(STORE_KEY_ISSUED_LOG)
        if isinstance(issued, dict):
            state[STORE_KEY_ISSUED_LOG] = _trim_ring(
                {k: v for k, v in issued.items() if isinstance(k, str)},
                _ISSUED_RING_DAYS,
            )
        actuals = raw.get(STORE_KEY_ACTUALS_LOG)
        if isinstance(actuals, dict):
            state[STORE_KEY_ACTUALS_LOG] = _trim_ring(
                {
                    k: v
                    for k, v in actuals.items()
                    if isinstance(k, str) and isinstance(v, dict)
                },
                _ACTUALS_RING_DAYS,
            )
        return state

    def _schedule_save(self) -> None:
        """Bundle a delayed write (eMMC-friendly)."""
        self._store.async_delay_save(lambda: self._data, STORAGE_SAVE_DELAY_SECONDS)

    async def async_flush(self) -> None:
        """Write immediately, cancelling any pending delayed save.

        Called on unload / HA stop so a reload cannot beat the delayed write
        and read back a stale last-good cache (cf. battery_manager review).
        Resets the payload write-gate: the in-memory payload is now on disk.
        """
        await self._store.async_save(self._data)
        self._last_payload_save_at = time.monotonic()

    async def async_remove(self) -> None:
        """Delete the persisted file (entry removal)."""
        await self._store.async_remove()

    # ------------------------------------------------------------------
    # Last-good payload
    # ------------------------------------------------------------------

    def set_last_payload(self, payload: dict[str, Any], fetched_at: str) -> None:
        """Record the freshest good Open-Meteo payload and its fetch time.

        The in-memory copy is always updated (so the degradation ladder and a
        clean unload/HA-stop flush see the latest weather), but a disk write is
        *time-gated*: it is only scheduled at most every
        ``PAYLOAD_MIN_SAVE_INTERVAL_SECONDS``. The fetch cadence (30 min) is
        far shorter than that gate, so without it the multi-hundred-KB store
        would be rewritten ~48x/day, blowing the eMMC-wear budget (SPEC §4).
        The nightly job and the unload/HA-stop flush guarantee the latest
        payload still reaches disk; only a hard crash may lose a few hours of
        last-good cache, which the spec explicitly accepts.
        """
        self._data[STORE_KEY_LAST_PAYLOAD] = {
            "fetched_at": fetched_at,
            "payload": payload,
        }
        now = time.monotonic()
        if (
            self._last_payload_save_at is not None
            and now - self._last_payload_save_at < PAYLOAD_MIN_SAVE_INTERVAL_SECONDS
        ):
            # Too soon since the last disk write: keep the fresh in-memory copy
            # but do not touch the disk (a later ring write, the nightly job, or
            # the unload flush will persist it).
            return
        self._last_payload_save_at = now
        self._schedule_save()

    def get_last_payload(self) -> dict[str, Any] | None:
        """Return ``{"fetched_at": iso, "payload": {...}}`` or None."""
        return self._data.get(STORE_KEY_LAST_PAYLOAD)

    # ------------------------------------------------------------------
    # Forecast-as-issued ring
    # ------------------------------------------------------------------

    def record_issued(self, iso_date: str, snapshot: dict[str, Any]) -> None:
        """Store the forecast published on ``iso_date`` (idempotent per day)."""
        ring = self._data[STORE_KEY_ISSUED_LOG]
        ring[iso_date] = snapshot
        _trim_ring(ring, _ISSUED_RING_DAYS)
        self._schedule_save()

    def get_issued(self, iso_date: str) -> dict[str, Any] | None:
        return self._data[STORE_KEY_ISSUED_LOG].get(iso_date)

    def issued_dates(self) -> list[str]:
        return sorted(self._data[STORE_KEY_ISSUED_LOG])

    # ------------------------------------------------------------------
    # Daily actuals ring
    # ------------------------------------------------------------------

    def record_actuals(self, iso_date: str, per_module_wh: dict[str, float]) -> None:
        """Store measured per-module DC energy for ``iso_date`` (idempotent)."""
        ring = self._data[STORE_KEY_ACTUALS_LOG]
        ring[iso_date] = dict(per_module_wh)
        _trim_ring(ring, _ACTUALS_RING_DAYS)
        self._schedule_save()

    def get_actuals(self, iso_date: str) -> dict[str, float] | None:
        return self._data[STORE_KEY_ACTUALS_LOG].get(iso_date)

    def has_actuals(self, iso_date: str) -> bool:
        return iso_date in self._data[STORE_KEY_ACTUALS_LOG]

    def actuals_dates(self) -> list[str]:
        return sorted(self._data[STORE_KEY_ACTUALS_LOG])
