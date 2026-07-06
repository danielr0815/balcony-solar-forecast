"""Data update coordinator for Balcony Solar Forecast.

Owner: glue (coordinator). Ties the pure physics core to Home Assistant:

  * fetch Open-Meteo every 30 min (persist last-good in the Store);
  * recompute the forecast every 15 min from the cached weather;
  * walk the degradation ladder — fresh → cached last-good (within an age
    limit) → pure-physics from the last valid weather image → unavailable
    (SPEC §7), each step visible via the ``status`` field;
  * per tick, read every configured ``actual_entity`` (guarded against
    unknown / unavailable / stale states) and feed the FAST learner's
    intraday clear-sky-index scalar (transient, re-init to 1.0 on restart,
    NEVER persisted — SPEC §5);
  * a nightly job (01:30 local, idempotent, date-keyed) that snapshots the
    forecast-as-issued (v2 dual-curve), reads yesterday's per-module actual
    energy from recorder long-term statistics (in the executor), trains the
    day-ahead RLS bias + the shademap under the label gates, runs the drift
    monitor (auto-disable + repair issue + rollback ring) and the collapse
    detector (SPEC §5).

``self.data`` is the single dict every platform reads (see the contract at
the bottom of ``_build_data``). ``None`` data means the coordinator has no
usable forecast yet; entities go ``unavailable`` honestly (SPEC §7).

Everything the learners touch is clamped, gated, disable-able and rollbackable;
degradation is never silent (SPEC §5 Schutzmechanismen).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CLOUD_CLASS_CLEAR,
    COLLAPSE_FORECAST_MIN_WH,
    COLLAPSE_MEASURED_MAX_FRAC,
    CONF_FETCH_INTERVAL,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SITE,
    CORRECTION_SOURCE_BOTH,
    CORRECTION_SOURCE_INTRADAY,
    CORRECTION_SOURCE_NONE,
    CORRECTION_SOURCE_SHADEMAP,
    DATA_KEY_CORRECTED_HOURLY_WH,
    DATA_KEY_CORRECTION_SOURCE,
    DATA_KEY_DRIFT_MAE,
    DATA_KEY_INTRADAY_SCALAR,
    DATA_KEY_LEARNER_STATUS,
    DATA_KEY_RAW_HOURLY_WH,
    DAY_AHEAD_BIAS_NEUTRAL,
    DAY_PART_MIDDAY,
    DOMAIN,
    DRIFT_LOSS_MARGIN,
    DRIFT_LOSS_STREAK_DAYS,
    DRIFT_WINDOW_DAYS,
    FETCH_INTERVAL_SECONDS,
    FORECAST_DAYS,
    INTRADAY_MIN_MODELED_WH,
    INTRADAY_NEUTRAL,
    INTRADAY_TRAILING_WINDOW_MINUTES,
    ISSUE_FAST_LEARNER_DISABLED,
    ISSUE_SLOW_LEARNER_DISABLED,
    LABEL_FROZEN_MIN_REPEATS,
    LABEL_FROZEN_STALE_SECONDS,
    LEARNER_LAYER_DAY_AHEAD,
    LEARNER_LAYER_FAST,
    LEARNER_LAYER_SLOW,
    LEARNER_SNAPSHOT_RING,
    LEARNER_STATUS_ACTIVE,
    LEARNER_STATUS_DISABLED_BY_DRIFT,
    LEARNER_STATUS_FROZEN,
    LEARNER_STATUS_OFF,
    MAX_PAYLOAD_AGE_HOURS,
    MAX_PHYSICS_FALLBACK_AGE_HOURS,
    NIGHTLY_CATCHUP_MAX_DAYS,
    RECOMPUTE_INTERVAL_SECONDS,
    SHADEMAP_MEASURED_CLEAR_MIN_FRAC,
    SHADEMAP_NEIGHBOUR_STABILITY,
    STATUS_CACHED,
    STATUS_FRESH,
    STATUS_PHYSICS_FALLBACK,
    STATUS_UNAVAILABLE,
)
from .core import (
    BiasState,
    DriftState,
    ForecastResult,
    IssuedSnapshot,
    LearnerConfig,
    LearnerHooks,
    LearnerSnapshot,
    PlaneHourlyModeled,
    ShademapState,
    SiteConfig,
    compute_forecast,
)
from .core import bias as bias_mod
from .core import clearsky, shademap as shademap_mod, solpos
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

# Live-actual state guards: states we never treat as a measurement.
_UNUSABLE_STATES = ("unknown", "unavailable", "none", "")


# ---------------------------------------------------------------------------
# Duck-typed sample containers handed to core/bias.py. The bias contract only
# requires attribute access (SPEC §5: "may realise it as a frozen dataclass");
# the coordinator builds these so the two owners share only the const tunables.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _IntradaySample:
    """One trailing-window observation for the intraday scalar (k_c space)."""

    at: datetime
    measured_kc: float
    modeled_kc: float
    modeled_wh: float


@dataclass(frozen=True, slots=True)
class _DayAheadSample:
    """One nightly day-part-aggregated observation for the RLS bias."""

    cloud_class: str
    day_part: str
    measured_wh: float
    modeled_wh: float


class BalconySolarCoordinator(DataUpdateCoordinator[dict[str, Any] | None]):
    """Fetch + physics + learners + degradation ladder for the balcony PV."""

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
            # Bind the coordinator to its config entry explicitly (deprecated
            # implicit inference removed by HA 2025.11/12; the declared minimum
            # HA in hacs.json needs this for the first-refresh call).
            config_entry=entry,
            update_interval=timedelta(seconds=recompute_s),
        )
        self.entry = entry
        self._fetcher = fetcher
        self._store = store
        self._fetch_interval = timedelta(
            seconds=int(cfg.get(CONF_FETCH_INTERVAL, FETCH_INTERVAL_SECONDS))
        )
        self._site = SiteConfig.from_dict(cfg[CONF_SITE])

        # Resolved kill switches (options-flow). Rebuilt on every reload.
        self._learner_config = LearnerConfig.from_dict(cfg)

        # Cached weather image + provenance for the degradation ladder.
        self._last_fetched_at: datetime | None = None
        self._last_fetch_ok: bool = False
        self._last_error: str | None = None

        self._unsub_nightly = None

        # --- FAST learner: transient intraday state (NEVER persisted) -------
        # Re-init to 1.0 on construction => on every HA restart / reload the
        # scalar starts neutral (SPEC §5).
        self._intraday_scalar: float = INTRADAY_NEUTRAL
        # Trailing ring of measured-vs-modeled samples (k_c space), one per
        # tick where a usable measurement + non-trivial modeled energy exist.
        self._intraday_samples: deque[_IntradaySample] = deque()
        # Correction source shaping the served curve this cycle.
        self._correction_source: str = CORRECTION_SOURCE_NONE

        # --- Persisted learner state (validate-and-clamp on load) -----------
        # Loaded lazily from the store on first refresh (store is async-loaded
        # before the coordinator is constructed in __init__.py setup).
        self._bias_state: BiasState = BiasState()
        self._shademap_state: ShademapState = ShademapState()
        self._drift_state: DriftState = DriftState()
        self._learner_states_loaded = False

        # Collapse detector: the frozen local date is persisted in DriftState
        # (collapse_frozen_date) so a mid-day restart keeps the freeze; there is
        # no transient copy here (SPEC §5).

        # Last computed ForecastResult (for the nightly per-plane snapshot).
        self._last_result: ForecastResult | None = None

    # ------------------------------------------------------------------
    # Live provenance (independent of the last update's success)
    # ------------------------------------------------------------------

    @property
    def weather_age_seconds_live(self) -> float | None:
        """Age of the last-good weather image right now, in seconds."""
        if self._last_fetched_at is None:
            return None
        age = (dt_util.utcnow() - self._last_fetched_at).total_seconds()
        return age if age > 0.0 else 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_prime_from_store(self) -> None:
        """Adopt the last-good payload + persisted learner state (warm start).

        Learner state is validate-and-clamp: a corrupt/absent blob yields a
        neutral state (BiasState/ShademapState/DriftState empty), never an
        exception (SPEC §5). The intraday scalar is deliberately NOT loaded
        (transient, re-init 1.0).
        """
        self._load_learner_states()
        # A user who re-enabled a drift-auto-disabled layer via the options flow
        # triggers a full reload; clearing the stale disable flag is gated on a
        # real OFF->ON option transition inside rebuild_learner_config (a plain
        # restart with the option untouched keeps the flag, SPEC §5).
        self.rebuild_learner_config()

        last = self._store.get_last_payload()
        if not last:
            return
        fetched_at = dt_util.parse_datetime(last.get("fetched_at", ""))
        if fetched_at is None:
            return
        self._last_fetched_at = dt_util.as_utc(fetched_at)
        _LOGGER.debug(
            "Primed forecast from stored payload fetched at %s", fetched_at
        )

    def _load_learner_states(self) -> None:
        """Load BiasState / ShademapState / DriftState from the store (once).

        Each getter is expected to already validate-and-clamp; we additionally
        guard the whole load so a store without the v2 getters (older schema in
        flight) or any unexpected error can never crash setup (SPEC §5).
        """
        if self._learner_states_loaded:
            return
        try:
            self._bias_state = self._store.get_bias_state()
            self._shademap_state = self._store.get_shademap_state()
            self._drift_state = self._store.get_drift_state()
        except AttributeError:
            # Store schema v2 not present yet: stay on neutral in-memory state.
            _LOGGER.debug("Store has no learner-state getters; using neutral state")
        except Exception:  # pragma: no cover - defensive, never crash setup
            _LOGGER.warning("Could not load learner state; using neutral", exc_info=True)
        self._learner_states_loaded = True

    def rebuild_learner_config(self) -> None:
        """Re-resolve kill switches. A drift auto-disable is cleared ONLY when
        the user actually re-enables the layer (persisted option transition
        False -> True) — a restart/reload with the option untouched keeps the
        flag and the repair issue (SPEC §5: disabled until the user re-enables).

        UX: with the option still ON, the user clears an auto-disable by
        toggling the layer OFF and then ON again in the options flow (each toggle
        reloads the entry, driving the recorded transition).
        """
        cfg = {**self.entry.data, **self.entry.options}
        self._learner_config = LearnerConfig.from_dict(cfg)
        drift = self._drift_state
        changed = False

        fast_reenabled = (
            self._learner_config.fast_enabled and drift.fast_option_seen is False
        )
        slow_reenabled = (
            self._learner_config.slow_enabled and drift.slow_option_seen is False
        )
        if fast_reenabled and drift.fast_disabled:
            drift = _replace_drift(drift, fast_disabled=False, fast_loss_streak=0)
            self._delete_repair_issue(ISSUE_FAST_LEARNER_DISABLED)
            changed = True
        if slow_reenabled and drift.slow_disabled:
            drift = _replace_drift(drift, slow_disabled=False, slow_loss_streak=0)
            self._delete_repair_issue(ISSUE_SLOW_LEARNER_DISABLED)
            changed = True

        if (
            drift.fast_option_seen != self._learner_config.fast_enabled
            or drift.slow_option_seen != self._learner_config.slow_enabled
        ):
            drift = _replace_drift(
                drift,
                fast_option_seen=self._learner_config.fast_enabled,
                slow_option_seen=self._learner_config.slow_enabled,
            )
            changed = True

        self._drift_state = drift
        if changed:
            self._persist_drift_state()

    async def async_import_bootstrap(self, data: dict) -> dict:
        """Ingest an offline backfill bootstrap (SPEC §6).

        Delegates schema validation, clamping, the site-signature check and the
        n-credit cap to ForecastStore.import_bootstrap (which also pushes a
        rollback snapshot), then re-syncs the in-memory learner state from the
        store — the one-shot _load_learner_states guard would otherwise serve
        the stale pre-import state until the next restart — and recomputes so the
        imported shademap shapes the very next served curve. Raises ValueError on
        a schema / site mismatch (the service layer maps it to
        ServiceValidationError).
        """
        self._store.import_bootstrap(
            data, expected_signature=self._site_signature()
        )  # may raise ValueError
        self._bias_state = self._store.get_bias_state()
        self._shademap_state = self._store.get_shademap_state()
        summary = {
            "bias_cells": len(self._bias_state.cells),
            "shademap_channels": len(self._shademap_state.channels),
            "shademap_bins": sum(
                len(b) for b in self._shademap_state.channels.values()
            ),
        }
        await self.async_request_refresh()
        return summary

    def get_shademap_state(self) -> ShademapState:
        """Current in-memory shademap for the dump_shademap polar diagnostic."""
        self._load_learner_states()
        return self._shademap_state

    def _site_signature(self) -> str:
        """Stable lat/lon + plane-name digest (mirrors backfill.site_signature).

        Lets ForecastStore.import_bootstrap refuse a bootstrap built for a
        different site (wrong coordinates / renamed planes), SPEC §6.
        """
        import hashlib

        parts = [
            f"{round(self._site.latitude, 4)}",
            f"{round(self._site.longitude, 4)}",
            *[p.name for p in self._site.planes],
        ]
        raw = "|".join(parts).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]

    @callback
    def async_start_nightly_job(self) -> None:
        """Schedule the idempotent 01:30-local snapshot / training job."""
        self._unsub_nightly = async_track_time_change(
            self.hass,
            self._async_nightly_job,
            hour=_NIGHTLY_HOUR,
            minute=_NIGHTLY_MINUTE,
            second=0,
        )

    async def async_startup_catchup(self) -> None:
        """Run the nightly training/guard for any day missed while HA was down.

        If HA was offline at 01:30 (updates commonly run at night), that night's
        job never fired and the day is lost to training/drift/collapse. The
        nightly job's own catch-up sweep is date-keyed and idempotent, so running
        it once on startup safely backfills up to NIGHTLY_CATCHUP_MAX_DAYS of
        missed closed days (coordinator:704). Never fatal.
        """
        try:
            await self._async_nightly_job()
        except Exception:  # pragma: no cover - startup best-effort
            _LOGGER.warning("Startup catch-up sweep failed", exc_info=True)

    @callback
    def async_shutdown_extra(self) -> None:
        """Cancel the nightly listener + drop the transient scalar (unload)."""
        if self._unsub_nightly is not None:
            self._unsub_nightly()
            self._unsub_nightly = None
        # Reset the transient FAST-learner state so a reload starts neutral
        # (the intraday scalar is never persisted, SPEC §5).
        self._intraday_scalar = INTRADAY_NEUTRAL
        self._intraday_samples.clear()

    # ------------------------------------------------------------------
    # Update cycle (recompute every tick; fetch on the slower timer)
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any] | None:
        now = dt_util.utcnow()
        self._load_learner_states()
        if self._due_for_fetch(now):
            await self._async_try_fetch(now)

        weather = self._cached_weather()
        if weather is None or self._last_fetched_at is None:
            raise UpdateFailed(
                self._last_error or "No forecast weather available yet"
            )

        age = now - self._last_fetched_at
        status = self._status_for_age(age)
        if status == STATUS_UNAVAILABLE:
            raise UpdateFailed(
                f"Weather image too old ({age}); no forecast issued"
            )

        # FAST learner: refresh the intraday scalar from live actuals BEFORE the
        # engine pass; the engine applies it via hooks.slot_factor so the 15-min,
        # hourly and daily curves stay mutually consistent. Never fatal.
        try:
            self._update_intraday_scalar(now)
        except Exception:  # pragma: no cover - learner never breaks the ladder
            _LOGGER.warning("Intraday-scalar tick failed; serving previous", exc_info=True)

        try:
            result = self._compute(weather, now)
        except Exception as err:  # pragma: no cover - engine owns correctness
            _LOGGER.exception("Forecast engine failed")
            raise UpdateFailed(f"Forecast engine error: {err}") from err

        # Hold the last good result so the nightly job can snapshot the current
        # per-plane modeled breakdown (beam/diffuse/kc) for the shademap trainer.
        self._last_result = result
        self._correction_source = result.correction_source
        return self._build_data(result, dict(result.hourly_wh), now, status, age)

    def _compute(self, weather, now: datetime) -> ForecastResult:
        """Run the engine with the learner hooks bound over the persisted state."""
        tz = dt_util.get_time_zone(self.hass.config.time_zone)
        hooks = self._build_learner_hooks(weather, now)
        return compute_forecast(self._site, weather, now, tz=tz, hooks=hooks)

    def _build_learner_hooks(self, weather, now: datetime) -> LearnerHooks:
        """Bind shademap.effective_tau into beam_tau and compose the intraday
        decay + day-ahead RLS bias into one per-slot factor (engine contract,
        engine.py LearnerHooks docstring)."""
        frozen = self._slow_frozen()
        slow_active = (
            self._learner_config.slow_enabled
            and not self._drift_state.slow_disabled     # honor drift flag
            and not frozen
            and bool(self._shademap_state.channels)
        )
        # Day-ahead RLS is part of the fast/weather-error family (SPEC §5
        # "Schneller Lerner ... optional später: 1 RLS-Bias-Skalar"), so it is
        # gated by fast_disabled; collapse freeze also silences it.
        day_ahead_active = (
            self._learner_config.day_ahead_enabled
            and not self._drift_state.fast_disabled
            and not frozen
            and bool(self._bias_state.cells)
        )
        fast_active = (
            self._learner_config.fast_enabled
            and not self._drift_state.fast_disabled
            and self._intraday_scalar != INTRADAY_NEUTRAL
        )

        beam_tau = None
        if slow_active:
            shd = self._shademap_state

            def beam_tau(channel, sun_az, sun_el, doy, static_prior):
                return shademap_mod.effective_tau(
                    shd, channel=channel, sun_az=sun_az, sun_el=sun_el,
                    doy=doy, static_prior=static_prior,
                )

        # Per-slot day-ahead factor, precomputed over the weather window so the
        # hook is a dict lookup (keyed by the identical slot.start datetimes the
        # engine iterates). Neutral cells (n < RLS_MIN_SAMPLES) are omitted.
        day_factor: dict[datetime, float] = {}
        if day_ahead_active:
            for slot in weather.slots:
                local = dt_util.as_local(slot.start)
                cc = bias_mod.classify_cloud(
                    cloud_low=slot.cloud_low, cloud_mid=slot.cloud_mid,
                    cloud_high=slot.cloud_high,
                    visibility_m=slot.visibility_m, month=local.month,
                )
                dp = bias_mod.day_part_for_hour(local.hour)
                f = self._bias_state.get_bias(cc, dp)
                if f != DAY_AHEAD_BIAS_NEUTRAL:
                    day_factor[slot.start] = f

        slot_factor = None
        scalar = self._intraday_scalar
        if fast_active or day_factor:
            def slot_factor(slot_start: datetime) -> float:
                f = day_factor.get(slot_start, 1.0)
                if fast_active:
                    age_min = (
                        dt_util.as_utc(slot_start) - now
                    ).total_seconds() / 60.0
                    if age_min > -15.0:  # in-progress or future slot only
                        f *= bias_mod.intraday_factor_at(max(0.0, age_min), scalar)
                return f

        if beam_tau is not None and slot_factor is not None:
            source = CORRECTION_SOURCE_BOTH
        elif beam_tau is not None:
            source = CORRECTION_SOURCE_SHADEMAP
        elif slot_factor is not None:
            source = CORRECTION_SOURCE_INTRADAY
        else:
            source = CORRECTION_SOURCE_NONE
        return LearnerHooks(beam_tau=beam_tau, slot_factor=slot_factor,
                            correction_source=source)

    def _due_for_fetch(self, now: datetime) -> bool:
        if self._last_fetched_at is None or not self._last_fetch_ok:
            return True
        return now - self._last_fetched_at >= self._fetch_interval

    async def _async_try_fetch(self, now: datetime) -> None:
        """Fetch once; on success cache + persist, on failure degrade quietly."""
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
        last = self._store.get_last_payload()
        if not last:
            return None
        try:
            return parse_weather(last["payload"])
        except FetchError as err:
            _LOGGER.error("Stored payload no longer parses: %s", err)
            return None

    def _status_for_age(self, age: timedelta) -> str:
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
    # FAST learner: live actual reads + intraday clear-sky-index scalar
    # ------------------------------------------------------------------

    def _slow_frozen(self) -> bool:
        """True when the slow/day-ahead layers are frozen for today (collapse).

        The collapse detector freezes the CURRENT local day when YESTERDAY
        collapsed (snow that is still on the panels this morning). The freeze
        date is persisted in DriftState so a mid-day restart keeps it (SPEC §5).
        """
        today = dt_util.as_local(dt_util.utcnow()).date().isoformat()
        return self._drift_state.collapse_frozen_date == today

    def _update_intraday_scalar(self, now: datetime) -> None:
        """Refresh the transient intraday scalar from live actuals vs the RAW
        curve of the previous tick's result (raw, so the scalar never trains
        against its own applied correction)."""
        fast_on = (self._learner_config.fast_enabled
                   and not self._drift_state.fast_disabled)
        if not fast_on:
            self._intraday_scalar = INTRADAY_NEUTRAL
            return
        prev = self._last_result
        if prev is not None:
            sample = self._build_intraday_sample(prev, now)
            if sample is not None:
                self._intraday_samples.append(sample)
                self._trim_intraday_ring(now)
        try:
            self._intraday_scalar = bias_mod.compute_intraday_scalar(
                list(self._intraday_samples), now=now)
        except NotImplementedError:
            self._intraday_scalar = INTRADAY_NEUTRAL
        except Exception:  # pragma: no cover - defensive
            _LOGGER.debug("compute_intraday_scalar failed", exc_info=True)
            self._intraday_scalar = INTRADAY_NEUTRAL

    def _build_intraday_sample(
        self, result: ForecastResult, now: datetime
    ) -> _IntradaySample | None:
        """Measured-vs-modeled site energy for the current slot, in k_c space.

        Reads every plane's ``actual_entity`` (guarded), and — crucially —
        normalises the modeled side to the SAME subset of planes that produced a
        usable reading (partial channel dropout must not read as a production
        deficit: a DTU serving 4 of 8 ports would otherwise drive the ratio
        toward 0.5, SPEC §5 channel-dropout guard). Both sides are then
        normalised by the Haurwitz clear-sky reference so geometry/season cancel.
        Returns None when no channel is usable or the modeled site energy for the
        usable subset is below INTRADAY_MIN_MODELED_WH.
        """
        read = self._read_live_actuals_total(now)
        if read is None:
            return None
        measured_w, usable_planes = read

        modeled_w = self._modeled_power_for_planes(result, now, usable_planes)
        modeled_wh = modeled_w * 0.25
        if modeled_wh < INTRADAY_MIN_MODELED_WH:
            return None
        measured_wh = measured_w * 0.25

        cs_ref_wh = self._clear_sky_ref_wh(now)
        if cs_ref_wh <= 0.0:
            return None
        return _IntradaySample(
            at=now,
            measured_kc=measured_wh / cs_ref_wh,
            modeled_kc=modeled_wh / cs_ref_wh,
            modeled_wh=modeled_wh,
        )

    def _modeled_power_for_planes(
        self, result: ForecastResult, now: datetime, plane_names: set[str]
    ) -> float:
        """RAW modeled site power at ``now`` restricted to the given plane names.

        Uses the RAW per-plane curve (labels must not depend on the applied
        correction). Scaling the modeled side to exactly the planes that reported
        a usable measurement makes the intraday ratio a pure weather error even
        under a partial DTU dropout (SPEC §5). Falls back to the full-site RAW
        power when the per-plane breakdown is unavailable (empty plane_results).
        """
        idx = _slot_index_at(result.slot_starts, now)
        if idx is None:
            return 0.0
        planes = [pr for pr in result.plane_results if pr.name in plane_names]
        if not planes:
            # No per-plane breakdown to restrict to: use the raw site total.
            return _raw_power_now(result, now)
        total = 0.0
        for pr in planes:
            series = pr.raw_watts or pr.watts
            if idx < len(series):
                total += series[idx]
        return total

    def _clear_sky_ref_wh(self, now: datetime) -> float:
        """Haurwitz clear-sky GHI energy proxy (Wh/m^2) for the current slot.

        Normalising measured and modeled site energy by the SAME clear-sky
        reference removes the geometry/season component from the intraday ratio
        (SPEC §5: condition in k_c space). Returns 0 when the sun is down.
        """
        midpoint = now + timedelta(minutes=7, seconds=30)
        _az, el = solpos.sun_position(midpoint, self._site.latitude, self._site.longitude)
        ghi = clearsky.haurwitz_ghi(el)
        return ghi * 0.25  # W/m^2 over a 15-min slot -> Wh/m^2

    def _read_live_actuals_total(
        self, now: datetime
    ) -> tuple[float, set[str]] | None:
        """Sum every configured plane's live measured DC power (guarded).

        Each plane's ``actual_entity`` is read from the state machine; a state
        that is unknown / unavailable / non-numeric, or one whose value is
        unchanged AND whose ``last_updated`` is older than
        LABEL_FROZEN_STALE_SECONDS (frozen sensor), is skipped. Returns
        ``(sum_over_usable_channels, {plane names that produced a reading})`` so
        the caller can scale the modeled side to the same subset (partial
        dropout guard, SPEC §5), or None when NO channel produced a usable
        reading (nothing to learn from this tick).
        """
        usable_planes: set[str] = set()
        total = 0.0
        for plane in self._site.planes:
            entity_id = plane.actual_entity
            if not entity_id:
                continue
            state = self.hass.states.get(entity_id)
            value = _usable_power(state, now)
            if value is None:
                continue
            total += value
            usable_planes.add(plane.name)
        if not usable_planes:
            return None
        return total, usable_planes

    def _trim_intraday_ring(self, now: datetime) -> None:
        """Drop samples older than the trailing window (bounded memory)."""
        cutoff = now - timedelta(minutes=INTRADAY_TRAILING_WINDOW_MINUTES)
        while self._intraday_samples and self._intraday_samples[0].at < cutoff:
            self._intraday_samples.popleft()

    # ------------------------------------------------------------------
    # Output assembly (the contract every platform reads)
    # ------------------------------------------------------------------

    def _build_data(
        self,
        result: ForecastResult,
        corrected_hourly: dict[str, float],
        now: datetime,
        status: str,
        age: timedelta,
    ) -> dict[str, Any]:
        """Shape the coordinator payload consumed by the platforms.

        v0.1 keys are unchanged (sensor / binary_sensor / energy / diagnostics
        read them). v0.2/v0.3 additive keys carry the raw-vs-corrected curves,
        the intraday scalar, the learner status and the rolling drift MAE (see
        const DATA_KEY_*).
        """
        local_today = dt_util.as_local(now).date()
        daily = _local_daily_kwh(result)

        watts = {
            _iso(start): round(w, 1)
            for start, w in zip(result.slot_starts, result.total_watts)
        }
        wh_period = {
            _iso(start): round(w * 0.25, 2)
            for start, w in zip(result.slot_starts, result.total_watts)
        }

        raw_hourly = result.raw_hourly_wh or result.hourly_wh

        data: dict[str, Any] = {
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
            "hourly_wh": dict(corrected_hourly),
            "daily_kwh": dict(daily),
            "slot_starts": [_iso(s) for s in result.slot_starts],
            "plane_watts": {
                pr.name: [round(w, 1) for w in pr.watts]
                for pr in result.plane_results
            },
            "computed_at": _iso(now),
        }
        # --- v0.2/v0.3 additive learner keys ---
        data[DATA_KEY_RAW_HOURLY_WH] = dict(raw_hourly)
        data[DATA_KEY_CORRECTED_HOURLY_WH] = dict(corrected_hourly)
        data[DATA_KEY_INTRADAY_SCALAR] = round(self._intraday_scalar, 4)
        data[DATA_KEY_CORRECTION_SOURCE] = self._correction_source
        data[DATA_KEY_LEARNER_STATUS] = self._learner_status()
        data[DATA_KEY_DRIFT_MAE] = self._latest_drift_mae()
        return data

    def _learner_status(self) -> dict[str, Any]:
        """Per-layer learner status for the diagnostic entities (SPEC §5).

        Returns the three layer keys ``fast`` / ``slow`` / ``day_ahead`` mapped
        to the ENUM strings the LearnerStatusSensor / LearnerActiveSensor read
        (active / off / disabled_by_drift / frozen), computed from the kill
        switch + drift auto-disable + collapse freeze. The detailed boolean
        flags + loss streaks ride along under ``*_enabled`` / ``*_disabled`` /
        ``*_loss_streak`` keys for diagnostics and the coordinator tests.
        """
        frozen = self._slow_frozen()
        cfg = self._learner_config
        drift = self._drift_state

        def _fast_status() -> str:
            if not cfg.fast_enabled:
                return LEARNER_STATUS_OFF
            if drift.fast_disabled:
                return LEARNER_STATUS_DISABLED_BY_DRIFT
            return LEARNER_STATUS_ACTIVE

        def _slow_status() -> str:
            if not cfg.slow_enabled:
                return LEARNER_STATUS_OFF
            if drift.slow_disabled:
                return LEARNER_STATUS_DISABLED_BY_DRIFT
            if frozen:
                return LEARNER_STATUS_FROZEN
            return LEARNER_STATUS_ACTIVE

        def _day_ahead_status() -> str:
            # Day-ahead RLS shares the fast/weather-error disable flag (SPEC §5).
            if not cfg.day_ahead_enabled:
                return LEARNER_STATUS_OFF
            if drift.fast_disabled:
                return LEARNER_STATUS_DISABLED_BY_DRIFT
            if frozen:
                return LEARNER_STATUS_FROZEN
            return LEARNER_STATUS_ACTIVE

        return {
            # Per-layer ENUM strings (the sensor/binary_sensor contract).
            LEARNER_LAYER_FAST: _fast_status(),
            LEARNER_LAYER_SLOW: _slow_status(),
            LEARNER_LAYER_DAY_AHEAD: _day_ahead_status(),
            # Detailed flags (diagnostics + coordinator tests).
            "fast_enabled": cfg.fast_enabled,
            "fast_disabled": drift.fast_disabled,
            "fast_active": (cfg.fast_enabled and not drift.fast_disabled),
            "slow_enabled": cfg.slow_enabled,
            "slow_disabled": drift.slow_disabled,
            "slow_frozen": frozen,
            "slow_active": (
                cfg.slow_enabled and not drift.slow_disabled and not frozen
            ),
            "day_ahead_enabled": cfg.day_ahead_enabled,
            "fast_loss_streak": drift.fast_loss_streak,
            "slow_loss_streak": drift.slow_loss_streak,
        }

    def _latest_drift_mae(self) -> dict[str, float]:
        """Most-recent day's {raw, corrected, baseline} daylight MAE, if any."""
        if not self._drift_state.daily_mae:
            return {}
        latest = max(self._drift_state.daily_mae)
        return dict(self._drift_state.daily_mae[latest])

    # ------------------------------------------------------------------
    # Nightly job (idempotent, date-keyed) — SPEC §4/§5
    # ------------------------------------------------------------------

    async def _async_nightly_job(self, now: datetime | None = None) -> None:
        """Snapshot today's issued forecast, log actuals, train + guard.

        Order (all idempotent, keyed by ISO date):
          1) snapshot the issued (v2 dual-curve) forecast for today;
          2) read yesterday's measured per-module energy from LTS (day gate);
          3) take a rollback snapshot of the pre-training learner state;
          4) collapse detector on yesterday (freeze BOTH learners today if
             dropout);
          5) train the day-ahead RLS bias + the shademap under label gates;
          6) drift monitor: update rolling MAE, auto-disable a losing layer.

        Every step is wrapped so a single failure never aborts the rest or
        crashes HA (SPEC §5). Recorder reads run in the recorder executor.
        """
        local_now = dt_util.as_local(now or dt_util.utcnow())
        today = local_now.date()

        self._load_learner_states()

        # 1) Snapshot the forecast we are issuing today (v2 dual-curve).
        await self._snapshot_issued(today)

        # 2-6) Catch-up sweep: run the actuals-read + training/guard logic for
        # every closed day back to the last one we processed, bounded to a few
        # days (SPEC §5 idempotent/date-keyed). A missed 01:30 job (HA down at
        # night, multi-day outage) would otherwise silently lose those days'
        # training, drift and collapse detection.
        yesterday = today - timedelta(days=1)
        for day in self._catchup_days(yesterday):
            iso = day.isoformat()
            if not self._store.has_actuals(iso):
                read = await self._read_actuals_safe(day)
                if read is not None:
                    daily, hourly = read
                    # A day that failed the frozen-channel gate returns empty;
                    # do NOT record it, so a later manual re-run can fill it.
                    if daily:
                        self._store.record_actuals(iso, daily)
                    if hourly:
                        self._store.record_hourly_actuals(iso, hourly)
            try:
                await self._train_and_guard(day)
            except Exception:  # pragma: no cover - never crash the scheduler
                _LOGGER.warning(
                    "Nightly training/guard failed for %s", day, exc_info=True
                )

    def _catchup_days(self, latest: date) -> list[date]:
        """Closed local days to (re)process, oldest first, bounded and idempotent.

        Sweeps from the day after the newest already-recorded actuals up to
        ``latest`` (yesterday), capped at NIGHTLY_CATCHUP_MAX_DAYS so a long
        outage does not fan out unboundedly. Every step keyed by ISO date is
        idempotent, so re-processing an already-trained day is safe (the
        date-keyed store guards make it a no-op where state already reflects it).
        """
        try:
            recorded = self._store.actuals_dates()
        except Exception:  # pragma: no cover - defensive
            recorded = []
        start = latest - timedelta(days=NIGHTLY_CATCHUP_MAX_DAYS - 1)
        if recorded:
            newest = date.fromisoformat(recorded[-1])
            candidate = newest + timedelta(days=1)
            if candidate > start:
                start = candidate
        if start > latest:
            start = latest
        days: list[date] = []
        d = start
        while d <= latest:
            days.append(d)
            d += timedelta(days=1)
        return days

    async def _snapshot_issued(self, today: date) -> None:
        """Record today's issued forecast as a v2 dual-curve snapshot."""
        if self.data is None or self._store.get_issued(today.isoformat()) is not None:
            return
        # Slice the full-horizon curves to the snapshot's own LOCAL day so the
        # 90-day issued ring never carries 4 days of hours per snapshot (store
        # size / flash-wear) and every nightly consumer sees exactly one day.
        iso = today.isoformat()
        raw_hourly = _filter_hourly_to_local_day(
            self.data.get(DATA_KEY_RAW_HOURLY_WH, {}), iso)
        corrected_hourly = _filter_hourly_to_local_day(
            self.data.get(DATA_KEY_CORRECTED_HOURLY_WH, {}), iso)
        snapshot = IssuedSnapshot(
            issued_at=dt_util.utcnow().isoformat(),
            status=str(self.data.get("status", "")),
            raw_hourly_wh=raw_hourly,
            corrected_hourly_wh=corrected_hourly,
            raw_daily_kwh=_daily_kwh_from_hourly(raw_hourly),
            corrected_daily_kwh=_daily_kwh_from_hourly(corrected_hourly),
            per_plane=self._per_plane_modeled(iso),
            cloud_class_by_hour=self._cloud_class_by_hour(iso),
        )
        self._store.record_issued(iso, snapshot.to_dict())

    def _cloud_class_by_hour(self, iso: str) -> dict[str, str]:
        """Per-ISO-hour forecast cloud class for ``iso`` (day-ahead RLS input).

        Derived from the cached weather series so the nightly RLS trains the
        real (cloud class x day part) cell rather than a fixed "clear" label
        (SPEC §5). A cloudy/fog/overcast day therefore trains the correct cell,
        and a genuinely clear day is never routed to a fog-poisoned one. Best
        effort: an unparseable weather image yields an empty map.
        """
        weather = self._cached_weather()
        if weather is None:
            return {}
        out: dict[str, str] = {}
        for slot in weather.slots:
            start = dt_util.as_utc(slot.start)
            if dt_util.as_local(start).date().isoformat() != iso:
                continue
            local = dt_util.as_local(start)
            cc = bias_mod.classify_cloud(
                cloud_low=slot.cloud_low, cloud_mid=slot.cloud_mid,
                cloud_high=slot.cloud_high,
                visibility_m=slot.visibility_m, month=local.month,
            )
            hkey = _hour_key(start)
            # First writer per hour wins (slots within an hour share cloud data).
            out.setdefault(hkey, cc)
        return out

    def _per_plane_modeled(self, iso: str) -> dict[str, PlaneHourlyModeled]:
        """Per-plane hourly modeled beam/diffuse/ghi/kc for the shademap trainer.

        Reconstructed from the last computed ForecastResult held on ``self`` via
        ``_last_result``, sliced to the snapshot's LOCAL day ``iso``. The beam /
        diffuse energy is sourced from the engine's UNGATED, unclamped,
        un-factored reference series (``beam_ref_watts`` / ``diffuse_ref_watts``,
        FIX-3): the shademap learns a beam-referenced T that REPLACES the static
        tau, so the reference must be the raw geometric beam — otherwise T
        self-references toward sqrt(true_t) and a wall bin (static tau 0) has ~0
        modeled beam and is untrainable. Engine builds without the reference
        export are simply not trained (no fallback to the gated series). When
        ``_last_result`` is absent (v0.1 build), returns an empty mapping (SPEC
        §6: attempt, not a blocker).
        """
        result = getattr(self, "_last_result", None)
        if result is None:
            return {}
        out: dict[str, PlaneHourlyModeled] = {}
        for pr in result.plane_results:
            if not pr.beam_ref_watts and not pr.diffuse_ref_watts:
                continue  # engine without the reference export: do NOT train
            beam_wh: dict[str, float] = {}
            diffuse_wh: dict[str, float] = {}
            ghi: dict[str, float] = {}
            kc: dict[str, float] = {}
            for i, start in enumerate(result.slot_starts):
                if dt_util.as_local(dt_util.as_utc(start)).date().isoformat() != iso:
                    continue
                hkey = _hour_key(start)
                if i < len(pr.beam_ref_watts):
                    beam_wh[hkey] = beam_wh.get(hkey, 0.0) + pr.beam_ref_watts[i] * 0.25
                if i < len(pr.diffuse_ref_watts):
                    diffuse_wh[hkey] = diffuse_wh.get(hkey, 0.0) + pr.diffuse_ref_watts[i] * 0.25
                if i < len(pr.kc):
                    # mean k_c per hour (last write wins is fine as a proxy; the
                    # trainer uses it only for the quasi-clear gate)
                    kc[hkey] = pr.kc[i]
            out[pr.name] = PlaneHourlyModeled(
                beam_wh=beam_wh, diffuse_wh=diffuse_wh, ghi=ghi, kc=kc
            )
        return out

    async def _read_actuals_safe(
        self, day: date
    ) -> tuple[dict[str, float], dict[str, dict[str, float]]] | None:
        try:
            return await self._async_read_actuals(day)
        except Exception:  # pragma: no cover - recorder is best-effort
            _LOGGER.warning("Could not read actuals for %s", day, exc_info=True)
            return None

    async def _train_and_guard(self, day: date) -> None:
        """Steps 3-6 of the nightly job for a closed calendar ``day``."""
        iso = day.isoformat()
        # Idempotence guard (verify finding 2026-07-06): the startup catch-up
        # re-sweeps the last processed day on EVERY restart / options reload,
        # and neither the RLS update nor the drift-streak counters are
        # internally idempotent — an unguarded re-run double-counts the same
        # training sample and double-increments the loss streak (spurious
        # auto-disable after 4 restarts on a bad-weather streak).
        if self._store.is_day_trained(iso):
            _LOGGER.debug("Training for %s already recorded; skipping", iso)
            return
        # The day whose SERVED forecast the geometric freeze protects: the day
        # AFTER the analyzed collapse (snow still on the panels the next day).
        next_iso = (day + timedelta(days=1)).isoformat()

        issued = self._store.get_issued(iso)
        actuals = self._store.get_actuals(iso)

        # --- 3) Rollback snapshot (pre-training) --------------------------
        # Take one snapshot per night, idempotently (date-keyed by taken-day).
        self._maybe_push_rollback_snapshot(iso)

        # --- 4) Collapse detector -----------------------------------------
        # All channels ~0 while forecast high => snow / total dropout: freeze
        # BOTH geometric learners for the FOLLOWING served day (SPEC §5), and
        # skip training the geometric learners on the collapse day itself.
        if self._is_collapse_day(iso, issued, actuals):
            self._set_collapse_frozen_date(next_iso)
            _LOGGER.info(
                "Collapse detected for %s: freezing geometric learners for %s",
                iso, next_iso,
            )
            # Still run the drift monitor so a persistently bad correction is
            # caught; do NOT train the geometric learners on a collapse day.
        else:
            # A non-collapse day closes: clear any freeze it (or an earlier day)
            # set that has not been superseded by a later collapse.
            frozen = self._drift_state.collapse_frozen_date
            if frozen is not None and frozen <= next_iso:
                self._set_collapse_frozen_date(None)
            # --- 5) Training under label gates ----------------------------
            self._train_day_ahead(iso, issued, actuals)
            self._train_shademap(iso, issued, actuals)

        # --- 6) Drift monitor --------------------------------------------
        self._update_drift(iso, issued, actuals)

        # Mark the day consumed ONLY when both inputs existed: a day whose
        # actuals arrive later (LTS lag, manual re-run) must be retried by a
        # future catch-up instead of being skipped forever.
        if issued and actuals:
            self._store.mark_day_trained(iso)

    def _set_collapse_frozen_date(self, iso: str | None) -> None:
        """Persist the collapse-freeze date into DriftState (survives restart)."""
        if self._drift_state.collapse_frozen_date == iso:
            return
        self._drift_state = _replace_drift(
            self._drift_state, collapse_frozen_date=iso
        )
        self._persist_drift_state()

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def _train_day_ahead(
        self, iso: str, issued: dict | None, actuals: dict | None
    ) -> None:
        """Train the day-ahead RLS bias from the issued (raw) vs actuals day.

        Aggregates the issued raw hourly curve and the measured site energy into
        (cloud class x day part) day-parts and runs one RLS step per part. The
        cloud class is derived from the issued snapshot's per-plane k_c/ghi where
        available; absent that (v0.1 issued), we fall back to CLEAR so the RLS
        still learns a coarse bias. Idempotent: a night already reflected in the
        state is guarded by the date-keyed nightly scheduling.
        """
        if not self._learner_config.day_ahead_enabled:
            return
        if not issued or not actuals:
            return
        snap = IssuedSnapshot.from_dict(issued)
        # Defense-in-depth: an old-code snapshot's rings can span 4 days; slice
        # the modeled curve to the training day before aggregating (FIX-2).
        raw_hourly = _filter_hourly_to_local_day(
            snap.raw_hourly_wh or snap.corrected_hourly_wh, iso
        )
        if not raw_hourly:
            return
        # Prefer TRUE per-hour measured site energy (from the hourly-actuals
        # ring): it gives an independent per-part signal AND real per-part cloud
        # conditioning. Fall back to the daily-apportioned path only when hourly
        # actuals are absent (coordinator:935).
        hourly_actuals = self._store_hourly_actuals(iso)
        site_measured_hourly = self._site_measured_hourly(iso, hourly_actuals)
        samples = self._day_ahead_samples(
            raw_hourly, actuals, snap, site_measured_hourly
        )
        if not samples:
            return
        try:
            self._bias_state = bias_mod.train_day_ahead_bias(self._bias_state, samples)
        except NotImplementedError:
            return
        except Exception:  # pragma: no cover - defensive
            _LOGGER.debug("train_day_ahead_bias failed", exc_info=True)
            return
        self._persist_bias_state()

    def _site_measured_hourly(
        self, iso: str, hourly_actuals: dict[str, dict[str, float]] | None
    ) -> dict[str, float] | None:
        """Sum per-channel hourly measured Wh into a site total per hour.

        Returns ``{iso_hour: wh}`` sliced to the local day ``iso``, or None when
        no hourly actuals exist (the caller then apportions the daily total).
        """
        if not hourly_actuals:
            return None
        site: dict[str, float] = {}
        for hours in hourly_actuals.values():
            for hkey, wh in hours.items():
                dt = dt_util.parse_datetime(hkey)
                if dt is None:
                    continue
                if dt_util.as_local(dt_util.as_utc(dt)).date().isoformat() != iso:
                    continue
                site[hkey] = site.get(hkey, 0.0) + float(wh)
        return site or None

    def _day_ahead_samples(
        self,
        raw_hourly: dict[str, float],
        actuals: dict,
        snap: IssuedSnapshot,
        site_measured_hourly: dict[str, float] | None,
    ) -> list[_DayAheadSample]:
        """Build (cloud class x day part) RLS training samples for one day.

        Modeled Wh per part comes from the issued raw hourly curve; the cloud
        class is the forecast cloud class of each hour (snap.cloud_class_by_hour,
        SPEC §5) so a fog/overcast day trains its own cell, not a fixed "clear"
        one. When TRUE per-hour measured site energy is available
        (``site_measured_hourly``) each (class, part) cell carries its OWN
        measured/modeled pair — a real independent per-part signal. Otherwise the
        day's measured total is apportioned by the modeled shape (coarse
        fallback, daily ring only).
        """
        measured_total = sum(
            float(v) for v in actuals.values() if isinstance(v, (int, float))
        )
        modeled_total = sum(raw_hourly.values())
        if modeled_total <= 0.0 or measured_total <= 0.0:
            return []

        # Aggregate modeled (+ measured, when hourly) per (cloud class, day part)
        # cell keyed on the forecast cloud class of each hour.
        cell_modeled: dict[tuple[str, str], float] = {}
        cell_measured: dict[tuple[str, str], float] = {}
        for hkey, wh in raw_hourly.items():
            part = self._day_part_for_hourkey(hkey)
            if part is None:
                continue
            cc = snap.cloud_class_by_hour.get(hkey, CLOUD_CLASS_CLEAR)
            key = (cc, part)
            cell_modeled[key] = cell_modeled.get(key, 0.0) + float(wh)
            if site_measured_hourly is not None:
                cell_measured[key] = cell_measured.get(
                    key, 0.0
                ) + float(site_measured_hourly.get(hkey, 0.0))

        samples: list[_DayAheadSample] = []
        for (cc, part), modeled_wh in cell_modeled.items():
            if modeled_wh <= 0.0:
                continue
            if site_measured_hourly is not None:
                measured_wh = cell_measured.get((cc, part), 0.0)
            else:
                # Daily-only fallback: apportion the measured total by modeled
                # share of this cell (coarse; only when hourly actuals absent).
                measured_wh = measured_total * (modeled_wh / modeled_total)
            samples.append(
                _DayAheadSample(
                    cloud_class=cc,
                    day_part=part,
                    measured_wh=measured_wh,
                    modeled_wh=modeled_wh,
                )
            )
        return samples

    def _day_part_for_hourkey(self, hkey: str) -> str | None:
        """Local day part for an ISO-UTC hour key, via core/bias.day_part_for_hour."""
        dt = dt_util.parse_datetime(hkey)
        if dt is None:
            return None
        local_hour = dt_util.as_local(dt).hour
        try:
            return bias_mod.day_part_for_hour(local_hour)
        except NotImplementedError:
            # Fall back to the const boundaries directly so training still runs.
            from .const import (
                DAY_PART_AFTERNOON,
                DAY_PART_AFTERNOON_START_HOUR,
                DAY_PART_MORNING,
                DAY_PART_MORNING_END_HOUR,
            )
            if local_hour < DAY_PART_MORNING_END_HOUR:
                return DAY_PART_MORNING
            if local_hour < DAY_PART_AFTERNOON_START_HOUR:
                return DAY_PART_MIDDAY
            return DAY_PART_AFTERNOON

    def _train_shademap(
        self, iso: str, issued: dict | None, actuals: dict | None
    ) -> None:
        """Train the shademap from the issued per-plane hourly modeled vs LTS.

        For each plane and each hour with a quasi-clear sample, compute the
        beam-referenced transmittance ``T = (P_measured - P_diffuse) / P_beam``
        (against the UNGATED beam reference the snapshot stores, FIX-3) and
        EMA-update the matched bin (SPEC §5). Measured hourly per-plane energy
        comes from the store's hourly-actuals ring (populated by the nightly LTS
        read); when absent the shademap does not train that night (SPEC §6
        attempt-not-blocker).

        Measured-side clearness gate (coordinator:1015): the whole day must have
        measured site energy within a band of the modeled forecast, otherwise the
        forecast wrongly called it clear and every hour would write pure weather
        error into the geometric map. A day that fails this gate trains nothing.
        """
        if not self._learner_config.slow_enabled:
            return
        if self._slow_frozen():
            return  # collapse freeze silences the geometric learner today/next
        if not issued:
            return
        snap = IssuedSnapshot.from_dict(issued)
        if not snap.per_plane:
            return  # v0.1 issued or engine breakdown absent: nothing to train
        hourly_actuals = self._store_hourly_actuals(iso)
        if not hourly_actuals:
            return
        # Measured-side clearness gate at the DAY level: reject days the forecast
        # called clear but reality was overcast (a transient weather bust must
        # not darken a geometric bin, SPEC §5). Uses the RAW gated modeled total
        # (the forecast the engine issued) vs the measured site total.
        if not self._day_is_measured_clear(iso, snap, hourly_actuals):
            return
        state = self._shademap_state
        trained = False
        for channel, modeled in snap.per_plane.items():
            measured_by_hour = hourly_actuals.get(channel)
            if not measured_by_hour:
                continue
            state, changed = self._train_channel(
                state, channel, modeled, measured_by_hour
            )
            trained = trained or changed
        if trained:
            self._shademap_state = state
            self._persist_shademap_state()

    def _day_is_measured_clear(
        self,
        iso: str,
        snap: IssuedSnapshot,
        hourly_actuals: dict[str, dict[str, float]],
    ) -> bool:
        """Measured-side clearness gate for shademap training (SPEC §5).

        The candidate day's measured site energy must be at least
        SHADEMAP_MEASURED_CLEAR_MIN_FRAC of the modeled RAW forecast; otherwise
        the forecast over-predicted clearness (overcast reality) and training
        would write weather error into the geometry. The modeled reference is the
        gated RAW hourly total (what the engine issued), sliced to the day.
        """
        modeled = sum(
            _filter_hourly_to_local_day(
                snap.raw_hourly_wh or snap.corrected_hourly_wh, iso
            ).values()
        )
        if modeled <= 0.0:
            return False
        measured = 0.0
        for hours in hourly_actuals.values():
            for hkey, wh in hours.items():
                dt = dt_util.parse_datetime(hkey)
                if dt is None:
                    continue
                if dt_util.as_local(dt_util.as_utc(dt)).date().isoformat() == iso:
                    measured += float(wh)
        return measured >= SHADEMAP_MEASURED_CLEAR_MIN_FRAC * modeled

    def _train_channel(
        self,
        state: ShademapState,
        channel: str,
        modeled: PlaneHourlyModeled,
        measured_by_hour: dict[str, float],
    ) -> tuple[ShademapState, bool]:
        """EMA-update one channel's bins from its quasi-clear hourly samples.

        The neighbour-stability leg of the gate is applied to the MEASURED/
        modeled ratio sequence (not the smooth forecast kc, coordinator:1015): a
        lone bright measured hour between shaded ones is a fluctuation and is
        rejected.
        """
        plane = self._site.plane_by_name(channel)
        if plane is None:
            return state, False
        changed = False
        hkeys = sorted(modeled.beam_wh)
        # Precompute the measured/modeled-gated ratio per hour for the neighbour-
        # stability test (measured-side, not forecast-side).
        ratio_by_hour: dict[str, float] = {}
        for hkey in hkeys:
            beam = modeled.beam_wh.get(hkey, 0.0)
            diff = modeled.diffuse_wh.get(hkey, 0.0)
            meas = measured_by_hour.get(hkey)
            denom = beam + diff
            if meas is not None and denom > 0.0:
                ratio_by_hour[hkey] = float(meas) / denom
        for idx, hkey in enumerate(hkeys):
            beam_wh = modeled.beam_wh.get(hkey, 0.0)
            diffuse_wh = modeled.diffuse_wh.get(hkey, 0.0)
            measured_wh = measured_by_hour.get(hkey)
            if measured_wh is None or beam_wh <= 0.0:
                continue
            kc = modeled.kc.get(hkey, 0.0)
            beam_share = beam_wh / (plane.wp) if plane.wp else 0.0
            dt = dt_util.parse_datetime(hkey)
            if dt is None:
                continue
            mid = dt + timedelta(minutes=30)
            sun_az, sun_el = solpos.sun_position(
                mid, self._site.latitude, self._site.longitude
            )
            # Neighbour stability on the MEASURED/modeled ratio (coordinator:1015).
            neighbour_kc = None
            this_ratio = ratio_by_hour.get(hkey)
            if this_ratio is not None and idx > 0:
                nb = ratio_by_hour.get(hkeys[idx - 1])
                if nb is not None:
                    neighbour_kc = nb
                    kc = this_ratio  # gate the stability leg on the ratio pair
            try:
                if not shademap_mod.is_quasi_clear(
                    kc=modeled.kc.get(hkey, 0.0),
                    sun_el=sun_el,
                    beam_share=beam_share,
                    neighbour_kc=None,
                ):
                    continue
                # Separate neighbour-stability check on the measured/modeled ratio.
                if neighbour_kc is not None and this_ratio is not None:
                    denom = max(this_ratio, neighbour_kc)
                    if denom > 0.0 and (
                        abs(this_ratio - neighbour_kc) / denom
                        >= SHADEMAP_NEIGHBOUR_STABILITY
                    ):
                        continue
                measured_t = shademap_mod.beam_referenced_t(
                    float(measured_wh), diffuse_wh, beam_wh
                )
                if measured_t is None:
                    continue
                doy = mid.timetuple().tm_yday
                state = shademap_mod.update_bin(
                    state,
                    channel=channel,
                    sun_az=sun_az,
                    sun_el=sun_el,
                    doy=doy,
                    measured_t=measured_t,
                )
                changed = True
            except NotImplementedError:
                return state, False
            except Exception:  # pragma: no cover - defensive
                _LOGGER.debug("shademap update failed for %s", channel, exc_info=True)
                continue
        return state, changed

    def _store_hourly_actuals(self, iso: str) -> dict[str, dict[str, float]] | None:
        """Per-plane hourly measured energy for a day from the store ring."""
        try:
            return self._store.get_hourly_actuals(iso)
        except Exception:  # pragma: no cover - defensive
            return None

    # ------------------------------------------------------------------
    # Guards: collapse detector, drift monitor, rollback ring
    # ------------------------------------------------------------------

    def _is_collapse_day(
        self, iso: str, issued: dict | None, actuals: dict | None
    ) -> bool:
        """Total-dropout day: measured << forecast (snow / channel loss).

        True when the modeled day is non-trivial (> COLLAPSE_FORECAST_MIN_WH)
        yet the measured site energy is below COLLAPSE_MEASURED_MAX_FRAC of it
        (SPEC §5). The modeled total is sliced to the training LOCAL day so an
        old 4-day snapshot cannot inflate the threshold (FIX-2). Absent either
        side, not a collapse (can't tell).
        """
        if not issued or not actuals:
            return False
        snap = IssuedSnapshot.from_dict(issued)
        forecast_wh = sum(
            _filter_hourly_to_local_day(
                snap.raw_hourly_wh or snap.corrected_hourly_wh, iso
            ).values()
        )
        if forecast_wh < COLLAPSE_FORECAST_MIN_WH:
            return False
        measured_wh = sum(
            float(v) for v in actuals.values() if isinstance(v, (int, float))
        )
        return measured_wh < COLLAPSE_MEASURED_MAX_FRAC * forecast_wh

    def _update_drift(
        self, iso: str, issued: dict | None, actuals: dict | None
    ) -> None:
        """Rolling daylight-MAE drift monitor with auto-disable (SPEC §5).

        Compares the corrected served curve against pure physics against the
        measured day. A "losing" day is one where the corrected daylight MAE is
        worse than the physics MAE by more than DRIFT_LOSS_MARGIN (relative).
        DRIFT_LOSS_STREAK_DAYS consecutive losing days auto-disables the layer,
        raises a repair issue and keeps the disable flag until the user
        re-enables in the options flow. The window is trimmed to
        DRIFT_WINDOW_DAYS.

        Scope note (FIX-1 residual): the 01:30 issued snapshot's corrected-vs-raw
        delta reflects shademap + day-ahead only (the intraday scalar is neutral
        at night). That is intentional — this monitor bounds the two PERSISTED
        learners; the intraday scalar is transient, restart-neutral and clamped
        to [0.25, 2.5], so it needs no drift bound.
        """
        if not issued or not actuals:
            return
        snap = IssuedSnapshot.from_dict(issued)
        measured_wh = sum(
            float(v) for v in actuals.values() if isinstance(v, (int, float))
        )
        # Slice both curves to the training LOCAL day (FIX-2): an old 4-day
        # snapshot would otherwise blow the MAE up to ~4x the true one-day error.
        raw_hourly = _filter_hourly_to_local_day(
            snap.raw_hourly_wh or snap.corrected_hourly_wh, iso)
        corrected_hourly = _filter_hourly_to_local_day(
            snap.corrected_hourly_wh or snap.raw_hourly_wh, iso)
        raw_total = sum(raw_hourly.values())
        corrected_total = sum(corrected_hourly.values())
        if raw_total <= 0.0 and corrected_total <= 0.0:
            return
        # Daily-kWh absolute error as the MAE proxy (the operator's primary
        # metric is daily kWh, SPEC §10/B9; the issued ring stores hourly so a
        # true daylight-hour MAE is available to a future finer implementation).
        raw_mae = abs(raw_total - measured_wh)
        corrected_mae = abs(corrected_total - measured_wh)
        baseline_mae = raw_mae  # pure physics is the baseline comparison here

        daily = dict(self._drift_state.daily_mae)
        daily[iso] = {
            "raw": round(raw_mae, 2),
            "corrected": round(corrected_mae, 2),
            "baseline": round(baseline_mae, 2),
        }
        # Trim to the window (ISO date order == chronological).
        for stale in sorted(daily)[:-DRIFT_WINDOW_DAYS]:
            daily.pop(stale, None)

        losing = corrected_mae > raw_mae * (1.0 + DRIFT_LOSS_MARGIN)
        # The correction here is the fast (intraday) + slow blend on the served
        # curve; attribute a losing day to whichever geometric layer is active,
        # and to the fast layer (it always shapes the served curve when on).
        fast_streak = self._drift_state.fast_loss_streak
        slow_streak = self._drift_state.slow_loss_streak
        fast_on = self._learner_config.fast_enabled and not self._drift_state.fast_disabled
        slow_on = self._learner_config.slow_enabled and not self._drift_state.slow_disabled
        if losing:
            if fast_on:
                fast_streak += 1
            if slow_on:
                slow_streak += 1
        else:
            fast_streak = 0
            slow_streak = 0

        fast_disabled = self._drift_state.fast_disabled
        slow_disabled = self._drift_state.slow_disabled
        if fast_on and fast_streak >= DRIFT_LOSS_STREAK_DAYS:
            fast_disabled = True
            fast_streak = 0
            self._restore_layer_snapshot(LEARNER_LAYER_FAST)
            self._raise_repair_issue(ISSUE_FAST_LEARNER_DISABLED)
            _LOGGER.warning("Fast learner auto-disabled after %d losing days", DRIFT_LOSS_STREAK_DAYS)
        if slow_on and slow_streak >= DRIFT_LOSS_STREAK_DAYS:
            slow_disabled = True
            slow_streak = 0
            self._restore_layer_snapshot(LEARNER_LAYER_SLOW)
            self._raise_repair_issue(ISSUE_SLOW_LEARNER_DISABLED)
            _LOGGER.warning("Slow learner auto-disabled after %d losing days", DRIFT_LOSS_STREAK_DAYS)

        # Preserve the option-seen + collapse-freeze fields (replace, not
        # reconstruct, so the FIX-5 transition memory + FIX-7 freeze survive).
        self._drift_state = _replace_drift(
            self._drift_state,
            daily_mae=daily,
            fast_loss_streak=fast_streak,
            slow_loss_streak=slow_streak,
            fast_disabled=fast_disabled,
            slow_disabled=slow_disabled,
        )
        self._persist_drift_state()

    def _restore_layer_snapshot(self, layer: str) -> str | None:
        """Roll the auto-disabled layer back to its pre-streak state (SPEC §5).

        Picks the snapshot taken DRIFT_LOSS_STREAK_DAYS nightly runs ago: the
        ring holds LEARNER_SNAPSHOT_RING (> streak) entries, so the state saved
        BEFORE the first losing night is still present; on a shorter ring the
        oldest snapshot is the best available approximation. Restores only the
        named layer so a healthy sibling keeps its learning. Without this, the
        ring would be write-only and a later manual re-enable would resume from
        the exact poisoned state that caused the auto-disable.

        Returns the restored snapshot's ``taken_at``, or None (empty ring).
        """
        try:
            snaps = self._store.get_snapshots()
        except Exception:  # pragma: no cover - defensive
            snaps = []
        if not snaps:
            _LOGGER.warning(
                "No rollback snapshot available for %s layer restore", layer
            )
            return None
        snap = snaps[max(0, len(snaps) - DRIFT_LOSS_STREAK_DAYS)]
        if layer == LEARNER_LAYER_FAST:
            self._bias_state = snap.bias
            self._persist_bias_state()
        else:
            self._shademap_state = snap.shademap
            self._persist_shademap_state()
        _LOGGER.warning(
            "Rolled %s learner state back to pre-streak snapshot %s",
            layer, snap.taken_at,
        )
        return snap.taken_at

    async def async_rollback_learners(
        self, snapshots_back: int = 1
    ) -> dict[str, Any]:
        """Restore BOTH learner states from the rollback ring (service backend).

        ``snapshots_back`` = 1 restores the newest snapshot, 2 the one before,
        capped at the ring length. Enable flags and drift state are untouched:
        re-enabling after an auto-disable stays an explicit operator action in
        the options flow (SPEC §5).
        """
        snaps = self._store.get_snapshots()
        if not snaps:
            raise ValueError("the rollback ring is empty")
        back = max(1, min(int(snapshots_back), len(snaps)))
        snap = snaps[len(snaps) - back]
        self._bias_state = snap.bias
        self._shademap_state = snap.shademap
        self._persist_bias_state()
        self._persist_shademap_state()
        _LOGGER.info(
            "Learner states rolled back %d snapshot(s) to %s", back, snap.taken_at
        )
        await self.async_request_refresh()
        return {
            "restored_taken_at": snap.taken_at,
            "snapshots_back": back,
            "ring_size": len(snaps),
        }

    def _maybe_push_rollback_snapshot(self, iso: str) -> None:
        """Push a pre-training rollback snapshot into the ring (idempotent/day).

        Keeps the last LEARNER_SNAPSHOT_RING snapshots (which exceeds
        DRIFT_LOSS_STREAK_DAYS, so a pre-streak good state survives an
        auto-disable, SPEC §5) via the store's ``push_snapshot`` /
        ``get_snapshots`` (the real ForecastStore API). One snapshot per nightly
        run: the snapshot's ``taken_at`` UTC date is the idempotence key, so a
        second run the same night is a no-op. ``iso`` (the training day) is
        accepted for symmetry; the guard keys on the run's own date.
        """
        try:
            existing = self._store.get_snapshots()
        except Exception:  # pragma: no cover - defensive
            existing = []
        now = dt_util.utcnow()
        run_date = now.date().isoformat()
        # Idempotence: at most one snapshot per calendar run-day.
        for snap in existing:
            if str(snap.taken_at).startswith(run_date):
                return
        snapshot = LearnerSnapshot(
            taken_at=now.isoformat(),
            bias=self._bias_state,
            shademap=self._shademap_state,
        )
        try:
            self._store.push_snapshot(snapshot)
        except Exception:  # pragma: no cover - defensive
            _LOGGER.debug("Could not push rollback snapshot", exc_info=True)

    def _issue_id_for(self, issue_id: str) -> str:
        """Per-entry issue id: suffix with the entry id so one entry's re-enable
        never clears another entry's warning (coordinator:1199)."""
        return f"{issue_id}_{self.entry.entry_id}"

    def _raise_repair_issue(self, issue_id: str) -> None:
        """Create a persistent HA repair issue for an auto-disabled layer.

        Persistent so it survives an HA restart while the disable flag does
        (SPEC §5: never silent degradation); the registry id is entry-scoped but
        the translation key stays the base id so the shared translation applies.
        """
        try:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id_for(issue_id),
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=issue_id,
            )
        except Exception:  # pragma: no cover - repair registry best-effort
            _LOGGER.debug("Could not raise repair issue %s", issue_id, exc_info=True)

    def _delete_repair_issue(self, issue_id: str) -> None:
        """Clear a previously-raised (entry-scoped) repair issue (re-enabled)."""
        try:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id_for(issue_id))
        except Exception:  # pragma: no cover - repair registry best-effort
            _LOGGER.debug("Could not delete repair issue %s", issue_id, exc_info=True)

    # ------------------------------------------------------------------
    # Persistence helpers (guarded against a store without v2 setters)
    # ------------------------------------------------------------------

    def _persist_bias_state(self) -> None:
        self._call_store_setter("set_bias_state", self._bias_state.to_dict())

    def _persist_shademap_state(self) -> None:
        self._call_store_setter("set_shademap_state", self._shademap_state.to_dict())

    def _persist_drift_state(self) -> None:
        self._call_store_setter("set_drift_state", self._drift_state.to_dict())

    def _call_store_setter(self, name: str, payload: Any) -> None:
        setter = getattr(self._store, name, None)
        if setter is None:
            return
        try:
            setter(payload)
        except Exception:  # pragma: no cover - defensive
            _LOGGER.debug("Store setter %s failed", name, exc_info=True)

    # ------------------------------------------------------------------
    # Recorder actuals (per-module daily energy) — SPEC §4
    # ------------------------------------------------------------------

    async def _async_read_daily_actuals(self, day: date) -> dict[str, float]:
        """Per-module measured DC energy for ``day`` (daily total; back-compat).

        Delegates to :meth:`_async_read_actuals` and returns only the daily
        totals. The nightly job uses the richer method to also persist the per-
        hour buckets the shademap trainer needs.
        """
        daily, _hourly = await self._async_read_actuals(day)
        return daily

    async def _async_read_actuals(
        self, day: date
    ) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        """Per-module measured DC energy for ``day``: (daily totals, hourly).

        Reads the hourly ``mean`` rows once and returns BOTH the per-module daily
        Wh totals AND the per-module ``{iso_hour: wh}`` buckets (the shademap
        trainer needs hourly resolution).

        Label gates (SPEC §5): a channel whose daylight hourly means are byte-
        identical across ``LABEL_FROZEN_MIN_REPEATS`` or more consecutive hours
        (a frozen Hoymiles/DTU sensor holding a midday value — the operator's
        known failure mode) is a DROPOUT: the WHOLE day is discarded for BOTH
        learners so a frozen-high over-read never poisons the write-once ring.
        The window follows the LOCAL calendar day exactly so DST (23/25-h) days
        are bounded correctly (coordinator:1256).
        """
        entity_by_module = {
            p.name: p.actual_entity
            for p in self._site.planes
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

        def _read() -> tuple[dict[str, float], dict[str, dict[str, float]]]:
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
            daily: dict[str, float] = {}
            hourly: dict[str, dict[str, float]] = {}
            dropped = False
            for module, entity_id in entity_by_module.items():
                rows = stats.get(entity_id)
                if not rows:
                    continue
                means: list[float] = []
                hkeys: list[str] = []
                for row in rows:
                    mean = row.get("mean")
                    if mean is None:
                        continue
                    hkey = _stat_row_hour_key(row.get("start"))
                    if hkey is None:
                        continue
                    means.append(float(mean))
                    hkeys.append(hkey)
                if not means:
                    continue
                if _is_frozen_channel(means):
                    _LOGGER.warning(
                        "Channel %s (%s) looks frozen on %s (byte-identical "
                        "hourly means during daylight); discarding the whole day "
                        "for both learners (SPEC §5)",
                        module, entity_id, day,
                    )
                    dropped = True
                    break
                per_hour: dict[str, float] = {}
                wh = 0.0
                for hkey, m in zip(hkeys, means):
                    per_hour[hkey] = per_hour.get(hkey, 0.0) + m  # W*1h = Wh
                    wh += m
                daily[module] = round(wh, 1)
                hourly[module] = per_hour
            if dropped:
                return {}, {}
            return daily, hourly

        return await get_instance(self.hass).async_add_executor_job(_read)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _is_frozen_channel(means: list[float]) -> bool:
    """True when hourly means show a frozen sensor: the SAME non-zero value held
    for >= LABEL_FROZEN_MIN_REPEATS consecutive hours (SPEC §5 label gate).

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


def _stat_row_hour_key(start: object) -> str | None:
    """Normalise a statistics row ``start`` to an ISO-UTC hour key, or None.

    HA recorder returns ``start`` as an epoch-ms number in modern cores or an
    aware datetime; handle both (mirrors backfill._stat_row_hour).
    """
    if isinstance(start, datetime):
        dt = dt_util.as_utc(start)
    elif isinstance(start, (int, float)):
        dt = datetime.fromtimestamp(start / 1000.0, tz=timezone.utc)
    elif isinstance(start, str):
        dt = dt_util.parse_datetime(start)
        if dt is None:
            return None
        dt = dt_util.as_utc(dt)
    else:
        return None
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


def _replace_drift(state: DriftState, **changes) -> DriftState:
    """Return a copy of a DriftState with fields replaced (frozen dataclass)."""
    from dataclasses import replace

    return replace(state, **changes)


def _usable_power(state: State | None, now: datetime) -> float | None:
    """Numeric live power from a state, or None if unusable / frozen.

    Guards (SPEC §5 label gates applied live): missing state, unknown /
    unavailable / empty state, non-numeric value, or a stale reading whose
    ``last_updated`` is older than LABEL_FROZEN_STALE_SECONDS (a frozen sensor
    holding an old value — treated as missing). A fresh zero is a legitimate
    night/shade reading and IS usable.
    """
    if state is None:
        return None
    raw = (state.state or "").strip().lower()
    if raw in _UNUSABLE_STATES:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    last_updated = getattr(state, "last_updated", None)
    if last_updated is not None:
        age = (dt_util.as_utc(now) - dt_util.as_utc(last_updated)).total_seconds()
        if age > LABEL_FROZEN_STALE_SECONDS:
            # Frozen: the sensor stopped reporting (value held). Skip it.
            return None
    return value


def _iso(dt: datetime) -> str:
    return dt_util.as_utc(dt).isoformat()


def _hour_key(dt: datetime) -> str:
    """ISO-UTC hour-start key for an aligned slot start."""
    from datetime import timezone

    return (
        dt_util.as_utc(dt)
        .replace(minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
        .isoformat()
    )


def _round3(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _power_at(slot_starts, watts, now: datetime) -> float:
    """Instantaneous power at the 15-min slot containing ``now`` (shared walk)."""
    now_utc = dt_util.as_utc(now)
    slot = timedelta(minutes=15)
    for start, w in zip(slot_starts, watts):
        start_utc = dt_util.as_utc(start)
        if start_utc <= now_utc < start_utc + slot:
            return w
        if start_utc > now_utc:
            break
    return 0.0


def _slot_index_at(slot_starts, now: datetime) -> int | None:
    """Index of the 15-min slot containing ``now``, or None if out of range."""
    now_utc = dt_util.as_utc(now)
    slot = timedelta(minutes=15)
    for i, start in enumerate(slot_starts):
        start_utc = dt_util.as_utc(start)
        if start_utc <= now_utc < start_utc + slot:
            return i
        if start_utc > now_utc:
            break
    return None


def _power_now(result: ForecastResult, now: datetime) -> float:
    """Instantaneous site power at the 15-min slot containing ``now``."""
    return _power_at(result.slot_starts, result.total_watts, now)


def _raw_power_now(result: ForecastResult, now: datetime) -> float:
    """Instantaneous RAW site power at the slot containing ``now``."""
    series = result.raw_total_watts or result.total_watts
    return _power_at(result.slot_starts, series, now)


def _local_daily_kwh(result: ForecastResult) -> dict[str, float]:
    """Roll the 15-min curve up to LOCAL calendar-day kWh."""
    daily: dict[str, float] = {}
    for start, watts in zip(result.slot_starts, result.total_watts):
        local_day = dt_util.as_local(dt_util.as_utc(start)).date().isoformat()
        daily[local_day] = daily.get(local_day, 0.0) + watts * 0.25 / 1000.0
    return {k: round(v, 3) for k, v in daily.items()}


def _filter_hourly_to_local_day(
    hourly_wh: dict[str, float], iso_day: str
) -> dict[str, float]:
    """Keep only hour keys whose LOCAL calendar date equals ``iso_day``.

    The issued curves span the full FORECAST_DAYS window; every trainer/guard
    compares them against ONE day of measured actuals, so they must only ever
    see that day's hours (a 22:00 UTC hour belongs to the NEXT local day in
    CET/CEST — bucket by local date, exactly like _daily_kwh_from_hourly).
    """
    out: dict[str, float] = {}
    for hkey, wh in hourly_wh.items():
        dt = dt_util.parse_datetime(hkey)
        if dt is None:
            continue
        if dt_util.as_local(dt_util.as_utc(dt)).date().isoformat() == iso_day:
            out[hkey] = float(wh)
    return out


def _daily_kwh_from_hourly(hourly_wh: dict[str, float]) -> dict[str, float]:
    """Roll an ISO-UTC-hour Wh curve up to LOCAL calendar-day kWh."""
    daily: dict[str, float] = {}
    for hkey, wh in hourly_wh.items():
        dt = dt_util.parse_datetime(hkey)
        if dt is None:
            continue
        local_day = dt_util.as_local(dt_util.as_utc(dt)).date().isoformat()
        daily[local_day] = daily.get(local_day, 0.0) + float(wh) / 1000.0
    return {k: round(v, 3) for k, v in daily.items()}
