"""Data update coordinator for Balcony Solar Forecast.

Owner: glue. Ties the pure physics core to Home Assistant:

  * fetch Open-Meteo every 30 min (persist last-good in the Store);
  * recompute the forecast every 15 min from the cached weather;
  * walk the degradation ladder — fresh → cached last-good (within an age
    limit) → pure-physics from the last valid weather image → unavailable
    (SPEC §7), each step visible via the ``status`` field;
  * a nightly job (01:30 local, idempotent, date-keyed) that snapshots the
    forecast-as-issued and reads yesterday's per-module actual energy from
    recorder long-term statistics (in the executor).

``self.data`` is the single dict every platform reads (see the contract at
the bottom of ``_build_data``). ``None`` data means the coordinator has no
usable forecast yet; entities go ``unavailable`` honestly (SPEC §7).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FETCH_INTERVAL,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SITE,
    DOMAIN,
    FETCH_INTERVAL_SECONDS,
    FORECAST_DAYS,
    MAX_PAYLOAD_AGE_HOURS,
    MAX_PHYSICS_FALLBACK_AGE_HOURS,
    RECOMPUTE_INTERVAL_SECONDS,
    STATUS_CACHED,
    STATUS_FRESH,
    STATUS_PHYSICS_FALLBACK,
    STATUS_UNAVAILABLE,
)
from .core import ForecastResult, SiteConfig, compute_forecast
from .fetcher import (
    FetchError,
    OpenMeteoFetcher,
    parse_weather,
    radiation_coverage,
)
from .store import ForecastStore

_LOGGER = logging.getLogger(__name__)

# Nightly training/snapshot job local wall-clock (SPEC §4: ~01:30 local).
_NIGHTLY_HOUR = 1
_NIGHTLY_MINUTE = 30


class BalconySolarCoordinator(DataUpdateCoordinator[dict[str, Any] | None]):
    """Fetch + physics + degradation ladder for the balcony PV forecast."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        fetcher: OpenMeteoFetcher,
        store: ForecastStore,
    ) -> None:
        cfg = {**entry.data, **entry.options}
        recompute_s = int(cfg.get(CONF_RECOMPUTE_INTERVAL, RECOMPUTE_INTERVAL_SECONDS))
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # Bind the coordinator to its config entry explicitly. Implicit
            # inference of the current entry was deprecated in HA 2024.12 and
            # removed by 2025.11/12; async_config_entry_first_refresh() requires
            # a config-entry-bound coordinator on the declared minimum HA
            # (hacs.json: 2026.1.0), so this MUST be passed.
            config_entry=entry,
            # The coordinator's own tick IS the recompute cadence; the
            # fetch runs on a slower internal timer (multiple of the tick).
            update_interval=timedelta(seconds=recompute_s),
        )
        self.entry = entry
        self._fetcher = fetcher
        self._store = store
        self._fetch_interval = timedelta(
            seconds=int(cfg.get(CONF_FETCH_INTERVAL, FETCH_INTERVAL_SECONDS))
        )
        self._site = SiteConfig.from_dict(cfg[CONF_SITE])

        # Cached weather image + provenance for the degradation ladder.
        self._last_fetched_at: datetime | None = None
        self._last_fetch_ok: bool = False
        self._last_error: str | None = None

        self._unsub_nightly = None

    # ------------------------------------------------------------------
    # Live provenance (independent of the last update's success)
    # ------------------------------------------------------------------

    @property
    def weather_age_seconds_live(self) -> float | None:
        """Age of the last-good weather image right now, in seconds.

        Computed live from ``_last_fetched_at`` rather than from the frozen
        ``self.data`` snapshot, so the always-available diagnostics keep
        climbing during an outage (when the coordinator raises UpdateFailed and
        HA holds the previous ``self.data``) instead of freezing at the last
        computed age (SPEC §7: the diagnostics must never go silent/stale).
        """
        if self._last_fetched_at is None:
            return None
        age = (dt_util.utcnow() - self._last_fetched_at).total_seconds()
        return age if age > 0.0 else 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_prime_from_store(self) -> None:
        """Adopt the last-good payload from the Store (warm start)."""
        last = self._store.get_last_payload()
        if not last:
            return
        fetched_at = dt_util.parse_datetime(last.get("fetched_at", ""))
        if fetched_at is None:
            return
        self._last_fetched_at = dt_util.as_utc(fetched_at)
        # We do NOT set _last_fetch_ok: a warm cache is "cached", not "fresh",
        # until the first live fetch this session succeeds.
        _LOGGER.debug(
            "Primed forecast from stored payload fetched at %s", fetched_at
        )

    @callback
    def async_start_nightly_job(self) -> None:
        """Schedule the idempotent 01:30-local snapshot / actuals job."""
        self._unsub_nightly = async_track_time_change(
            self.hass,
            self._async_nightly_job,
            hour=_NIGHTLY_HOUR,
            minute=_NIGHTLY_MINUTE,
            second=0,
        )

    @callback
    def async_shutdown_extra(self) -> None:
        """Cancel the nightly listener (called on unload)."""
        if self._unsub_nightly is not None:
            self._unsub_nightly()
            self._unsub_nightly = None

    # ------------------------------------------------------------------
    # Update cycle (recompute every tick; fetch on the slower timer)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any] | None:
        now = dt_util.utcnow()
        if self._due_for_fetch(now):
            await self._async_try_fetch(now)

        weather = self._cached_weather()
        if weather is None or self._last_fetched_at is None:
            # No usable weather at all — honest unavailable (SPEC §7).
            raise UpdateFailed(
                self._last_error or "No forecast weather available yet"
            )

        age = now - self._last_fetched_at
        status = self._status_for_age(age)
        if status == STATUS_UNAVAILABLE:
            raise UpdateFailed(
                f"Weather image too old ({age}); no forecast issued"
            )

        try:
            result = compute_forecast(self._site, weather, now)
        except Exception as err:  # pragma: no cover - engine owns correctness
            _LOGGER.exception("Forecast engine failed")
            raise UpdateFailed(f"Forecast engine error: {err}") from err

        return self._build_data(result, now, status, age)

    def _due_for_fetch(self, now: datetime) -> bool:
        """True when the fetch timer (30 min) has elapsed since last success."""
        if self._last_fetched_at is None or not self._last_fetch_ok:
            return True
        return now - self._last_fetched_at >= self._fetch_interval

    async def _async_try_fetch(self, now: datetime) -> None:
        """Fetch once; on success cache + persist, on failure degrade quietly.

        A failed fetch never raises here: the ladder keeps serving the
        last-good weather until it ages out (SPEC §7). The error is stashed
        for the diagnostic sensor and used only if there is nothing to serve.
        """
        try:
            payload = await self._fetcher.async_fetch_raw(
                self._site.latitude,
                self._site.longitude,
                FORECAST_DAYS,
            )
        except FetchError as err:
            self._last_fetch_ok = False
            self._last_error = str(err)
            _LOGGER.warning("Open-Meteo fetch failed: %s", err)
            return
        # Never overwrite a good last-good cache with a payload that carries
        # strictly LESS non-null radiation coverage (partial-outage response).
        # The fetcher already rejects an all-null near-term window, so a fresh
        # payload has some coverage; this additionally protects a rich cache
        # from being clobbered by a thinner one (SPEC §7: never poison cache).
        prior = self._store.get_last_payload()
        if prior is not None and isinstance(prior.get("payload"), dict):
            if radiation_coverage(payload) < radiation_coverage(prior["payload"]):
                self._last_fetched_at = now
                self._last_fetch_ok = True
                self._last_error = None
                _LOGGER.warning(
                    "Keeping richer last-good payload; new fetch has less "
                    "radiation coverage (%d < %d)",
                    radiation_coverage(payload),
                    radiation_coverage(prior["payload"]),
                )
                return
        self._last_fetched_at = now
        self._last_fetch_ok = True
        self._last_error = None
        self._store.set_last_payload(payload, now.isoformat())

    def _cached_weather(self):
        """Parse the last-good payload from the Store into a WeatherSeries."""
        last = self._store.get_last_payload()
        if not last:
            return None
        try:
            return parse_weather(last["payload"])
        except FetchError as err:
            _LOGGER.error("Stored payload no longer parses: %s", err)
            return None

    def _status_for_age(self, age: timedelta) -> str:
        """Map the weather-image age to a degradation-ladder status."""
        if age < timedelta(0):
            age = timedelta(0)
        if self._last_fetch_ok and age < self._fetch_interval:
            return STATUS_FRESH
        if age <= timedelta(hours=MAX_PAYLOAD_AGE_HOURS):
            return STATUS_CACHED
        if age <= timedelta(hours=MAX_PHYSICS_FALLBACK_AGE_HOURS):
            return STATUS_PHYSICS_FALLBACK
        return STATUS_UNAVAILABLE

    # ------------------------------------------------------------------
    # Output assembly (the contract every platform reads)
    # ------------------------------------------------------------------

    def _build_data(
        self,
        result: ForecastResult,
        now: datetime,
        status: str,
        age: timedelta,
    ) -> dict[str, Any]:
        """Shape the coordinator payload consumed by the platforms.

        Keys (stable contract for sensor/binary_sensor/energy/diagnostics):
          - ``power_now_w``: instantaneous site power at the current slot.
          - ``energy_today_kwh`` / ``_tomorrow`` / ``_d2``: daily roll-ups
            keyed off local calendar days.
          - ``watts``: {iso_utc: W} 15-min curve (energy-sensor attribute).
          - ``wh_period``: {iso_utc: Wh} per-slot energy.
          - ``hourly_wh``: {iso_utc_hour: Wh} (energy dashboard hook).
          - ``status`` / ``degraded`` / ``weather_age_seconds`` / ``last_error``.
        """
        local_today = dt_util.as_local(now).date()
        daily = _local_daily_kwh(result)

        watts = {
            _iso(start): round(w, 1)
            for start, w in zip(result.slot_starts, result.total_watts)
        }
        wh_period = {
            _iso(start): round(w * 0.25, 2)  # 15-min slot: W * 0.25 h = Wh
            for start, w in zip(result.slot_starts, result.total_watts)
        }

        return {
            "status": status,
            "degraded": status != STATUS_FRESH,
            "weather_age_seconds": int(age.total_seconds()),
            "last_error": self._last_error,
            "power_now_w": round(_power_now(result, now), 1),
            "energy_today_kwh": _round3(daily.get(local_today.isoformat())),
            "energy_tomorrow_kwh": _round3(
                daily.get((local_today + timedelta(days=1)).isoformat())
            ),
            "energy_d2_kwh": _round3(
                daily.get((local_today + timedelta(days=2)).isoformat())
            ),
            "watts": watts,
            "wh_period": wh_period,
            "hourly_wh": dict(result.hourly_wh),
            "daily_kwh": dict(daily),
            "slot_starts": [_iso(s) for s in result.slot_starts],
            "plane_watts": {
                pr.name: [round(w, 1) for w in pr.watts]
                for pr in result.plane_results
            },
            "computed_at": _iso(now),
        }

    # ------------------------------------------------------------------
    # Nightly job (idempotent, date-keyed) — SPEC §4
    # ------------------------------------------------------------------

    async def _async_nightly_job(self, now: datetime | None = None) -> None:
        """Snapshot today's issued forecast and log yesterday's actuals.

        Idempotent: keyed by ISO date, safe to run twice. Recorder reads run
        in the executor. Failures are logged, never fatal (SPEC §5).
        """
        local_now = dt_util.as_local(now or dt_util.utcnow())
        today = local_now.date()
        yesterday = today - timedelta(days=1)

        # 1) Snapshot the forecast we are issuing today (if we have one).
        if self.data is not None and self._store.get_issued(today.isoformat()) is None:
            self._store.record_issued(
                today.isoformat(),
                {
                    "issued_at": dt_util.utcnow().isoformat(),
                    "hourly_wh": self.data.get("hourly_wh", {}),
                    "daily_kwh": self.data.get("daily_kwh", {}),
                    "status": self.data.get("status"),
                },
            )

        # 2) Read yesterday's measured per-module energy (idempotent).
        if not self._store.has_actuals(yesterday.isoformat()):
            try:
                actuals = await self._async_read_daily_actuals(yesterday)
            except Exception:  # pragma: no cover - recorder is best-effort
                _LOGGER.warning(
                    "Could not read actuals for %s", yesterday, exc_info=True
                )
                actuals = None
            if actuals:
                self._store.record_actuals(yesterday.isoformat(), actuals)

    async def _async_read_daily_actuals(
        self, day: date
    ) -> dict[str, float]:
        """Per-module measured DC energy for ``day`` via recorder statistics.

        Uses long-term statistics (``statistics_during_period``) integrated
        from each module's actual-power sensor. Runs in the executor. Returns
        a ``{module_name: wh}`` dict; modules without a sensor are skipped.
        """
        entity_by_module = {
            p.name: p.actual_entity
            for p in self._site.planes
            if p.actual_entity
        }
        if not entity_by_module:
            return {}

        start = dt_util.start_of_local_day(
            datetime(day.year, day.month, day.day)
        )
        end = start + timedelta(days=1)

        # Imported lazily: recorder is an after_dependency, present at runtime
        # but not needed for the pure unit tests.
        from homeassistant.components.recorder import get_instance

        def _read() -> dict[str, float]:
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )

            stat_ids = set(entity_by_module.values())
            stats = statistics_during_period(
                self.hass,
                start,
                end,
                stat_ids,
                "hour",
                None,
                {"mean", "state"},
            )
            out: dict[str, float] = {}
            for module, entity_id in entity_by_module.items():
                rows = stats.get(entity_id)
                if not rows:
                    continue
                # Power sensors: integrate mean power over each hour to Wh.
                wh = 0.0
                for row in rows:
                    mean = row.get("mean")
                    if mean is not None:
                        wh += float(mean)  # W * 1 h = Wh
                out[module] = round(wh, 1)
            return out

        # Statistics reads MUST run on the recorder's own executor to serialize
        # database access (HA recorder API contract, SPEC Anhang B): the general
        # executor would contend with recorder writes/purge (nightly window) and
        # can hit "database is locked" on the default SQLite backend.
        return await get_instance(self.hass).async_add_executor_job(_read)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt_util.as_utc(dt).isoformat()


def _round3(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _power_now(result: ForecastResult, now: datetime) -> float:
    """Instantaneous site power at the 15-min slot containing ``now``.

    Returns 0 when ``now`` falls before the first slot or after the last
    slot's window (no extrapolation past the horizon).
    """
    now_utc = dt_util.as_utc(now)
    slot = timedelta(minutes=15)
    for start, watts in zip(result.slot_starts, result.total_watts):
        start_utc = dt_util.as_utc(start)
        if start_utc <= now_utc < start_utc + slot:
            return watts
        if start_utc > now_utc:
            break  # now precedes this slot and all later ones
    return 0.0


def _local_daily_kwh(result: ForecastResult) -> dict[str, float]:
    """Roll the 15-min curve up to LOCAL calendar-day kWh.

    The engine's ``daily_kwh`` may key by UTC date; the energy sensors are
    "today/tomorrow" in the user's timezone, so we re-bucket the aligned
    15-min curve by local day. This keeps DST-correct day boundaries.
    """
    daily: dict[str, float] = {}
    for start, watts in zip(result.slot_starts, result.total_watts):
        local_day = dt_util.as_local(dt_util.as_utc(start)).date().isoformat()
        daily[local_day] = daily.get(local_day, 0.0) + watts * 0.25 / 1000.0
    return {k: round(v, 3) for k, v in daily.items()}
