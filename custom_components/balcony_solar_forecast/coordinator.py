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
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

# Extracted concern groups (HA-glue level). The coordinator keeps every public /
# tested method name as a thin delegate into these modules (code motion only —
# SPEC unchanged); they reach coordinator state back through the ``coord`` param.
from . import _actuals, _nightly, _scoreboard_glue
from ._actuals import (
    _actuals_from_stats,  # noqa: F401  re-exported for tests
    _is_frozen_channel,  # noqa: F401  re-exported for tests
)

# Pure helpers moved to _glue_util; imported for the coordinator's own use AND
# re-exported so the historical ``coordinator.<name>`` import path / monkeypatch
# surface the tests rely on keeps resolving (# noqa: F401 = re-export only).
from ._glue_util import (
    _UNUSABLE_STATES,  # noqa: F401
    _daily_kwh_from_hourly,  # noqa: F401
    _filter_hourly_to_local_day,  # noqa: F401
    _hour_key,  # noqa: F401
    _iso,
    _local_daily_kwh,
    _local_daily_kwh_ac,
    _power_at,  # noqa: F401
    _power_now,
    _power_now_ac,
    _raw_power_now,
    _replace_drift,
    _round3,
    _slot_index_at,
    _usable_power,
)
from ._nightly import _NIGHTLY_HOUR, _NIGHTLY_MINUTE
from .const import (
    BAND_SOURCE_ENSEMBLE,
    BAND_SOURCE_ENVELOPE,
    BAND_SOURCE_LEARNED,
    CONF_COMPARISON_SENSORS,
    CONF_ENSEMBLE_ENABLED,
    CONF_FETCH_INTERVAL,
    CONF_QUANTILES_ENABLED,
    CONF_RECOMPUTE_INTERVAL,
    CONF_SCOREBOARD_ENABLED,
    CONF_SCOREBOARD_GATE_MARGIN,
    CONF_SCOREBOARD_WINDOW_DAYS,
    CONF_SITE,
    CORRECTION_SOURCE_BOTH,
    CORRECTION_SOURCE_INTRADAY,
    CORRECTION_SOURCE_NONE,
    CORRECTION_SOURCE_SHADEMAP,
    DATA_KEY_BAND_SOURCE,
    DATA_KEY_BIAS_CELLS,
    DATA_KEY_CORRECTED_HOURLY_WH,
    DATA_KEY_CORRECTION_SOURCE,
    DATA_KEY_DRIFT_MAE,
    DATA_KEY_INTRADAY_SCALAR,
    DATA_KEY_KILL_GATE_PASSED,
    DATA_KEY_LEARNER_STATUS,
    DATA_KEY_QUANTILE_CURVES,
    DATA_KEY_QUANTILE_CURVES_AC,
    DATA_KEY_RAW_HOURLY_WH,
    DATA_KEY_SCOREBOARD,
    DAY_AHEAD_BIAS_NEUTRAL,
    DEFAULT_ENSEMBLE_ENABLED,
    DEFAULT_QUANTILES_ENABLED,
    DEFAULT_SCOREBOARD_ENABLED,
    DEFAULT_SCOREBOARD_GATE_MARGIN,
    DEFAULT_SCOREBOARD_WINDOW_DAYS,
    DOMAIN,
    ENSEMBLE_FACTOR_MAX,
    ENSEMBLE_FACTOR_MIN,
    ENSEMBLE_FETCH_INTERVAL_S,
    ENSEMBLE_MIN_DET_GHI,
    ENSEMBLE_MIN_MEMBERS,
    FETCH_INTERVAL_SECONDS,
    FORECAST_DAYS,
    FORECAST_RESP_KEY_P10,
    FORECAST_RESP_KEY_P50,
    FORECAST_RESP_KEY_P90,
    INTRADAY_MIN_MODELED_WH,
    INTRADAY_NEUTRAL,
    INTRADAY_TRAILING_WINDOW_MINUTES,
    ISSUE_FAST_LEARNER_DISABLED,
    ISSUE_SLOW_LEARNER_DISABLED,
    LEARNER_LAYER_DAY_AHEAD,
    LEARNER_LAYER_FAST,
    LEARNER_LAYER_SLOW,
    LEARNER_STATUS_ACTIVE,
    LEARNER_STATUS_DISABLED_BY_DRIFT,
    LEARNER_STATUS_FROZEN,
    LEARNER_STATUS_OFF,
    MAX_PAYLOAD_AGE_HOURS,
    MAX_PHYSICS_FALLBACK_AGE_HOURS,
    RECOMPUTE_INTERVAL_SECONDS,
    STATUS_CACHED,
    STATUS_FRESH,
    STATUS_PHYSICS_FALLBACK,
    STATUS_UNAVAILABLE,
)
from .core import (
    BiasState,
    ComparisonConfig,
    DriftState,
    ForecastResult,
    InverterCalState,
    IssuedSnapshot,
    LearnerConfig,
    LearnerHooks,
    PlaneHourlyModeled,
    QuantileBands,
    QuantileState,
    ScoreboardState,
    ShademapState,
    SiteConfig,
    WeatherSeries,
    clearsky,
    compute_forecast,
    solpos,
)
from .core import bias as bias_mod
from .core import (
    ensembleband as ensembleband_mod,
)
from .core import (
    inverter_cal as inverter_cal_mod,
)
from .core import (
    quantiles as quantiles_mod,
)
from .core import (
    shademap as shademap_mod,
)
from .core import (
    shadeprofile as shadeprofile_mod,
)
from .fetcher import (
    FetchError,
    OpenMeteoFetcher,
    parse_ensemble,
    parse_weather,
    radiation_coverage,
)
from .store import ForecastStore

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Duck-typed sample container handed to core/bias.py. The bias contract only
# requires attribute access (SPEC §5: "may realise it as a frozen dataclass");
# the coordinator builds these so the two owners share only the const tunables.
# (The nightly ``_DayAheadSample`` sibling lives in ``_nightly.py``.)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _IntradaySample:
    """One trailing-window observation for the intraday scalar (k_c space)."""

    at: datetime
    measured_kc: float
    modeled_kc: float
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
        # Shade pooling is READ-TIME (SPEC §5): every plane's learning is stored
        # under its OWN channel forever; grouped planes are pooled only when the
        # forecast/diagram reads the map, so grouping stays fully reversible. The
        # pool membership is derived on demand from PlaneConfig.shade_channel +
        # the live ShademapState (see _build_shade_pool_map), never cached.

        # Resolved kill switches (options-flow). Rebuilt on every reload.
        self._learner_config = LearnerConfig.from_dict(cfg)

        # --- Skill scoreboard config (v0.4, SPEC §9/§10) --------------------
        # Enable flag + rolling-window length + kill-gate margin, all from the
        # options flow (defaults from const). The comparison sensors are a
        # GENERIC, CONFIGURABLE list (ships EMPTY, D-P9); a malformed/half-filled
        # row is dropped by ComparisonConfig.list_from_options.
        self._scoreboard_enabled = bool(
            cfg.get(CONF_SCOREBOARD_ENABLED, DEFAULT_SCOREBOARD_ENABLED)
        )
        self._scoreboard_window_days = int(
            cfg.get(CONF_SCOREBOARD_WINDOW_DAYS, DEFAULT_SCOREBOARD_WINDOW_DAYS)
        )
        self._scoreboard_gate_margin = float(
            cfg.get(CONF_SCOREBOARD_GATE_MARGIN, DEFAULT_SCOREBOARD_GATE_MARGIN)
        )
        self._comparisons: tuple[ComparisonConfig, ...] = (
            ComparisonConfig.list_from_options(cfg.get(CONF_COMPARISON_SENSORS))
        )

        # --- Quantile bands (v0.4, SPEC §6/§10) -----------------------------
        # Historical-simulation P10/P50/P90 bands, enable flag from the options
        # flow (default ON; kill switch is BooleanSelector).
        self._quantiles_enabled = bool(
            cfg.get(CONF_QUANTILES_ENABLED, DEFAULT_QUANTILES_ENABLED)
        )

        # --- Ensemble-weather uncertainty bands (v0.16, SPEC §6) ------------
        # Opt-in (default OFF). When ON, an ensemble spread is folded into the
        # learned bands by envelope-max; the ensemble is NEVER load-bearing and
        # is cached in memory only (not persisted). All state below is transient.
        self._ensemble_enabled = bool(
            cfg.get(CONF_ENSEMBLE_ENABLED, DEFAULT_ENSEMBLE_ENABLED)
        )
        # Last-good raw ensemble payload + its own fetch-scheduler anchor (the
        # ensemble runs on ENSEMBLE_FETCH_INTERVAL_S, independent of the main
        # fetch cadence). Both stay None until a first successful ensemble fetch.
        self._ensemble_raw: dict | None = None
        self._ensemble_fetched_at: datetime | None = None
        # Parsed ensemble cache keyed by payload OBJECT IDENTITY (mirrors
        # _weather_cache): parsing is pure, so a stable payload is parsed once.
        self._ensemble_cache: tuple[dict, dict[str, list[float]]] | None = None
        # Per-hour (f10, f90) relative-spread factors, recomputed every cycle
        # against the CURRENT deterministic weather; None when unavailable.
        self._ensemble_factors: dict[str, tuple[float, float]] | None = None
        # Which source shaped TODAY's band slots (const BAND_SOURCE_*), for the
        # P10/P90 sensors' ``band_source`` attribute. Recomputed per hooks build.
        self._band_source: str = BAND_SOURCE_LEARNED

        # Cached weather image + provenance for the degradation ladder.
        # _last_fetched_at is the PAYLOAD's age anchor: it advances ONLY when the
        # stored payload is actually replaced (it mirrors the store's
        # fetched_at), and every age/status consumer keys on it. _last_attempt_at
        # is the fetch SCHEDULER's anchor: it advances on every successful HTTP
        # round-trip — including the keep-richer branch that retains the old
        # payload — so a sustained partial Open-Meteo degradation is not
        # re-fetched every tick yet still ages the served payload honestly
        # through the cached/physics_fallback/unavailable ladder (SPEC §7:
        # never degrade silently).
        self._last_fetched_at: datetime | None = None
        self._last_attempt_at: datetime | None = None
        self._last_fetch_ok: bool = False
        self._last_error: str | None = None
        # Parsed-weather cache: the raw Open-Meteo payload is re-read every 15-min
        # recompute (and by the nightly snapshot), but parsing it into an
        # immutable WeatherSeries is pure and identical between fetches. Cache the
        # parsed series keyed by the payload OBJECT IDENTITY (a new fetch replaces
        # the stored dict wholesale, so ``is`` misses and we re-parse); holding a
        # strong reference means the id can never be reused for another object.
        # WeatherSeries is frozen, so sharing it across cycles is safe (audit #31).
        self._weather_cache: tuple[dict, WeatherSeries] | None = None

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
        # Skill-scoreboard rolling window (validate-and-clamp on load).
        self._scoreboard_state: ScoreboardState = ScoreboardState()
        # Quantile relative-error ring (validate-and-clamp on load).
        self._quantile_state: QuantileState = QuantileState()
        # Inverter DC->AC efficiency site calibration (AC-side Phase 3): a single
        # learned eta_inv, validate-and-clamp on load. Neutral until the nightly
        # AC-meter calibration folds enough eligible hours (never load-bearing).
        self._inverter_cal_state: InverterCalState = InverterCalState()
        self._learner_states_loaded = False

        # Collapse detector: the frozen local date is persisted in DriftState
        # (collapse_frozen_date) so a mid-day restart keeps the freeze; there is
        # no transient copy here (SPEC §5).

        # Last computed ForecastResult (for the nightly per-plane snapshot).
        self._last_result: ForecastResult | None = None

        # --- Shade-profile diagram selection (SPEC §15) ---------------------
        # Which module/plane + local date the shade-profile sensor renders. The
        # select entity owns the persisted module (RestoreEntity) and pushes it
        # here; the date entity always defaults to today (not restored). None =>
        # the front plane / today. Transient here; recomputed on demand by
        # build_shade_profile (pure geometry, no weather), memoised below.
        self._shade_profile_module: str | None = None
        self._shade_profile_date: date | None = None
        # Memo of the last built profile: (cache_key, result). The profile is a
        # pure function of (module, date, slow-active, shademap object), so it is
        # rebuilt only when one of those changes — not on every 15-min tick.
        self._shade_profile_cache: tuple[tuple, dict[str, Any]] | None = None

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
        # v3 scoreboard ring (independently guarded: a v2 store has no getter).
        try:
            self._scoreboard_state = self._store.get_scoreboard_state()
        except AttributeError:
            _LOGGER.debug("Store has no scoreboard getter; using empty state")
        except Exception:  # pragma: no cover - defensive, never crash setup
            _LOGGER.warning(
                "Could not load scoreboard state; using empty", exc_info=True
            )
        # v3 quantile ring (independently guarded: a v2 store has no getter).
        try:
            self._quantile_state = self._store.get_quantile_state()
        except AttributeError:
            _LOGGER.debug("Store has no quantile getter; using empty state")
        except Exception:  # pragma: no cover - defensive, never crash setup
            _LOGGER.warning(
                "Could not load quantile state; using empty", exc_info=True
            )
        # Inverter-efficiency calibration (AC-side Phase 3, independently guarded:
        # a pre-Phase-3 store has no getter -> stay on the neutral in-memory state).
        try:
            self._inverter_cal_state = self._store.get_inverter_cal_state()
        except AttributeError:
            _LOGGER.debug("Store has no inverter-cal getter; using neutral state")
        except Exception:  # pragma: no cover - defensive, never crash setup
            _LOGGER.warning(
                "Could not load inverter calibration; using neutral", exc_info=True
            )
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

    # ------------------------------------------------------------------
    # Shade groups: read-time pool membership (SPEC §5)
    # ------------------------------------------------------------------

    def _build_shade_pool_map(
        self, state: ShademapState
    ) -> dict[str, tuple[str, ...]]:
        """Build ``{plane.name: pool}`` for READ-TIME shade pooling (SPEC §5).

        Storage is always per plane (one channel per plane, keyed by name);
        pooling happens only here, at read time, so grouping stays reversible.
        For a plane ``p`` with group ``g = p.shade_channel`` the pool is:

          * the OWN channels of every plane sharing that group (their per-plane
            names — for an ungrouped plane that is just ``(p.name,)``), PLUS
          * the group channel ``g`` itself as a LEGACY evidence source, but ONLY
            when ``g`` is actually present in ``state`` AND ``g`` is not one of
            the member plane names. That branch catches a group channel produced
            by the earlier v0.12.0 merge migration: its already-pooled evidence
            must keep counting until it is diluted away by live per-plane data.

        Depends on the LIVE state (the legacy source is added only when the group
        channel exists), so it is rebuilt at read/bind time, never cached.

        Reversibility caveat: the per-plane learning (since v0.13) is always
        preserved when a group is dissolved, but a pre-v0.13 merged group channel
        ``g`` (named after the group, not any member) is then orphaned — no plane
        reads it once its members no longer share ``g`` — so its evidence becomes
        unreadable (recoverable via the ``rollback_learners`` service or by
        re-grouping the members under the same name).
        """
        planes = self._site.planes
        channels = getattr(state, "channels", None) or {}
        members: dict[str, list[str]] = {}
        for p in planes:
            members.setdefault(p.shade_channel, []).append(p.name)
        pool_map: dict[str, tuple[str, ...]] = {}
        for p in planes:
            g = p.shade_channel
            names = members[g]
            pool = list(names)
            # Legacy merged group channel: keep counting its evidence, but never
            # when g is already one of the member plane names (no double count).
            if g in channels and g not in names:
                pool.append(g)
            pool_map[p.name] = tuple(pool)
        return pool_map

    # ------------------------------------------------------------------
    # Shade-profile diagram (sun path vs learned shade) — SPEC §15
    # ------------------------------------------------------------------

    def shade_profile_plane_names(self) -> list[str]:
        """Module/plane names selectable for the shade-profile diagram."""
        return [p.name for p in self._site.planes]

    @property
    def shade_profile_module(self) -> str:
        """Selected shade-profile module.

        Falls back to the balcony's FRONT plane (the orientation the most planes
        share — the reference site's 115° modules M2/M3/…), not the first plane,
        so the diagram opens on a productive front module by default.
        """
        if self._shade_profile_module in self.shade_profile_plane_names():
            return self._shade_profile_module
        return shadeprofile_mod.default_module(self._site.planes)

    @property
    def shade_profile_date(self) -> date:
        """Selected shade-profile date (falls back to today, local calendar)."""
        return self._shade_profile_date or dt_util.as_local(dt_util.utcnow()).date()

    @callback
    def set_shade_profile_module(self, module: str) -> None:
        """Set the visualised module and refresh the diagram entities."""
        self._shade_profile_module = module
        self.async_update_listeners()

    @callback
    def set_shade_profile_date(self, day: date) -> None:
        """Set the visualised date and refresh the diagram entities."""
        self._shade_profile_date = day
        self.async_update_listeners()

    def _slow_active(self) -> bool:
        """Whether the SLOW learner (shademap) is currently shaping the forecast.

        Mirrors the ``slow_active`` gate in :meth:`_build_learner_hooks`: the
        shademap only attenuates the served beam when the layer is enabled, not
        drift-auto-disabled, not collapse-frozen for today, and has learned bins.
        The shade-profile diagram consults the SAME gate so it never paints
        learned shading the forecast is not applying (SPEC §15).
        """
        return (
            self._learner_config.slow_enabled
            and not self._drift_state.slow_disabled
            and not self._slow_frozen()
            and bool(self._shademap_state.channels)
        )

    def build_shade_profile(self) -> dict[str, Any]:
        """Sun-path + learned-shade profile for the current module/date selection.

        Pure geometry + shademap lookup (core/shadeprofile.py) over the selected
        plane's config for the selected local date; no weather, never raises.
        Returns an empty dict when no plane is configured or the selected module
        has vanished (renamed away).

        The learned shademap is blended in ONLY when the slow learner is active
        (:meth:`_slow_active`) — matching what the served forecast actually
        applies (engine ``_plane_poa_components``). When it is off / drift-disabled /
        collapse-frozen the diagram shows the static config shading alone, exactly
        as the forecast does. The result is memoised on
        ``(module, date, slow_active, id(shademap))`` so the O(azimuth×elevation)
        horizon scan runs once per real change, not on every coordinator tick.
        """
        module = self.shade_profile_module
        plane = self._site.plane_by_name(module)
        if plane is None:
            return {}
        self._load_learner_states()
        slow_active = self._slow_active()
        day = self.shade_profile_date
        key = (module, day.isoformat(), slow_active, id(self._shademap_state))
        cache = self._shade_profile_cache
        if cache is not None and cache[0] == key:
            return cache[1]
        result = self._compute_shade_profile(module, plane, day, slow_active)
        self._shade_profile_cache = (key, result)
        return result

    def build_shade_profile_for(self, module: str, day: date) -> dict[str, Any]:
        """Sun-path + learned-shade profile for an EXPLICIT module/date pair.

        The on-demand analysis path behind the ``get_shade_profile`` service (the
        card's comparison-date overlay): the SAME pure compute + read-time pool
        map as :meth:`build_shade_profile`, but for a module/date the operator
        asks for WITHOUT changing the coordinator's current selection. Computed
        UNCACHED — the primary diagram memo (:attr:`_shade_profile_cache`) is a
        single slot, so an ad-hoc comparison query must never evict the live
        selection's entry. Returns an empty dict when ``module`` is not a
        configured plane. Never raises.
        """
        plane = self._site.plane_by_name(module)
        if plane is None:
            return {}
        self._load_learner_states()
        return self._compute_shade_profile(module, plane, day, self._slow_active())

    def _compute_shade_profile(
        self, module: str, plane: Any, day: date, slow_active: bool
    ) -> dict[str, Any]:
        """Pure (unmemoised) shade-profile compute for one module/date/plane.

        Shared by :meth:`build_shade_profile` (memoised, current selection) and
        :meth:`build_shade_profile_for` (uncached, ad-hoc query) so the two can
        never diverge. The learned shademap is blended in only when the slow
        learner is active (``slow_active``, computed by the caller); otherwise an
        empty state yields the static-only shading the forecast applies. Storage
        is per plane: the module's OWN channel is its name; the read POOL adds its
        group siblings (+ any legacy group channel), so the main curve is the
        pooled tau the forecast applies and the individual channel rides along as
        a comparison view (SPEC §5).
        """
        shademap = self._shademap_state if slow_active else ShademapState()
        pool = self._build_shade_pool_map(shademap).get(module, (module,))
        tz = dt_util.get_time_zone(self.hass.config.time_zone) or UTC
        return shadeprofile_mod.compute_shade_profile(
            plane=plane,
            shademap=shademap,
            channel=module,
            pool=pool,
            latitude=self._site.latitude,
            longitude=self._site.longitude,
            day=day,
            tz=tz,
        )

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

        # Ensemble-weather uncertainty (v0.16, SPEC §6): opt-in. Runs AFTER the
        # main fetch/weather is confirmed, in its OWN guard — any failure/absence
        # degrades to the learned bands and the main degradation ladder above is
        # NEVER touched by ensemble state. OFF => factors stay None so the hooks
        # path is bit-identical to the pre-v0.16 build. ``getattr`` keeps a bare
        # (__new__-built) coordinator working.
        if getattr(self, "_ensemble_enabled", False):
            await self._async_update_ensemble(now, weather)
        else:
            self._ensemble_factors = None

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

        beam_tau = self._bind_beam_tau() if slow_active else None

        # Per-slot day-ahead factor, precomputed over the weather window so the
        # hook is a dict lookup (keyed by the identical slot.start datetimes the
        # engine iterates). Neutral cells (n < RLS_MIN_SAMPLES) are omitted.
        day_factor: dict[datetime, float] = {}
        if day_ahead_active:
            lon = self._site.longitude
            for slot in weather.slots:
                local = dt_util.as_local(slot.start)
                cc = bias_mod.classify_cloud(
                    cloud_low=slot.cloud_low, cloud_mid=slot.cloud_mid,
                    cloud_high=slot.cloud_high,
                    visibility_m=slot.visibility_m, month=local.month,
                )
                # Bin by APPARENT SOLAR time, not the wall clock (v0.19): the
                # day-part boundary tracks solar noon instead of a fixed local
                # hour like 10:00, so it does not drift with DST / the season.
                # Continuous across boundaries (no hard step): the learned
                # per-part cells are blended by solar time near each internal
                # boundary (bias.day_ahead_factor_solar), so the served
                # correction ramps smoothly instead of cliff-stepping.
                hfn = solpos.hours_from_solar_noon(slot.start, lon)
                f = bias_mod.day_ahead_factor_solar(
                    self._bias_state, cloud_class=cc, hours_from_noon=hfn,
                )
                if f != DAY_AHEAD_BIAS_NEUTRAL:
                    day_factor[slot.start] = f

        # Per-slot quantile bands (SPEC §6/§10): keyed by the identical
        # slot.start datetimes the engine iterates. Each slot's LEARNED band is
        # the empirical P10/P50/P90 of its (forecast cloud class x local day part)
        # bin; a starved / cold-start bin collapses to the neutral band (no fake
        # spread). When the ensemble is ON (v0.16, SPEC §6) each slot's learned
        # band is FUSED by envelope-max with today's ensemble spread for that
        # slot's hour — the wider band wins, never multiplied (no double count),
        # never narrowed. A slot whose FUSED band is still the neutral identity is
        # omitted so the engine passes it through unchanged. Gated on the quantiles
        # kill switch; ensemble factors are None when the ensemble is OFF, so this
        # path is bit-identical to the pre-v0.16 build in that case.
        # ``getattr`` defaults keep a bare (__new__-built) coordinator — and any
        # pre-v0.16 cached state — working: the ensemble attrs only exist after
        # __init__ / an ensemble update.
        self._band_source = BAND_SOURCE_LEARNED
        band_by_slot: dict[datetime, QuantileBands] | None = None
        ens_factors = (
            getattr(self, "_ensemble_factors", None)
            if getattr(self, "_ensemble_enabled", False)
            else None
        )
        have_bins = self._quantiles_enabled and bool(self._quantile_state.bins)
        if self._quantiles_enabled and (have_bins or ens_factors):
            bands: dict[datetime, QuantileBands] = {}
            today = dt_util.as_local(now).date()
            ens_today = False           # ensemble covered >= 1 of today's slots
            learned_spread_today = False  # learned band had spread on a today slot
            for slot in weather.slots:
                local = dt_util.as_local(slot.start)
                if have_bins:
                    cc = bias_mod.classify_cloud(
                        cloud_low=slot.cloud_low, cloud_mid=slot.cloud_mid,
                        cloud_high=slot.cloud_high,
                        visibility_m=slot.visibility_m, month=local.month,
                    )
                    # Solar-time day part (v0.19), consistent with the day-ahead
                    # bias binning above: the quantile bins share the day_part
                    # key space, so both track solar noon rather than the clock.
                    dp = bias_mod.day_part_for_solar(
                        solpos.hours_from_solar_noon(slot.start, self._site.longitude)
                    )
                    learned = quantiles_mod.bands_for_bin(
                        self._quantile_state, cloud_class=cc, day_part=dp
                    )
                else:
                    learned = QuantileBands.neutral()
                ens = None
                if ens_factors:
                    hkey = (
                        dt_util.as_utc(slot.start)
                        .replace(minute=0, second=0, microsecond=0)
                        .isoformat()
                    )
                    ens = ens_factors.get(hkey)
                b = ensembleband_mod.fuse_bands(learned, ens)
                # Omit a neutral (identity) band so the engine's `if band:` path
                # short-circuits an all-1.0 multiply.
                if not (b.p10 == 1.0 and b.p50 == 1.0 and b.p90 == 1.0):
                    bands[slot.start] = b
                # Band-source accounting over TODAY's slots only.
                if local.date() == today:
                    if ens is not None:
                        ens_today = True
                    if not learned.collapsed:
                        learned_spread_today = True
            band_by_slot = bands or None
            # ensemble contributed anywhere today -> envelope, unless the learned
            # band was collapsed on EVERY today slot (then the ensemble supplied
            # the whole spread: the cold-start "ensemble" win). No ensemble today
            # -> the default BAND_SOURCE_LEARNED set above stands.
            if ens_today:
                self._band_source = (
                    BAND_SOURCE_ENVELOPE
                    if learned_spread_today
                    else BAND_SOURCE_ENSEMBLE
                )

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
        # Site-level LEARNED inverter eta_inv (AC-side Phase 3): None until the
        # calibration is trusted (>= INVERTER_CAL_MIN_SAMPLES eligible hours), so
        # the engine falls back to the per-group config eta. Never load-bearing;
        # reshapes only the AC curve. ``getattr`` keeps a bare __new__-built
        # coordinator (tests) working with the neutral (untrusted) default.
        cal_state = getattr(self, "_inverter_cal_state", None) or InverterCalState()
        inverter_efficiency = inverter_cal_mod.effective_eta(cal_state)
        return LearnerHooks(beam_tau=beam_tau, slot_factor=slot_factor,
                            correction_source=source, band_by_slot=band_by_slot,
                            inverter_efficiency=inverter_efficiency)

    def _bind_beam_tau(self):
        """Bind ``shademap.effective_tau_pooled`` over the current ShademapState
        into the engine's ``beam_tau`` hook.

        Factored out so BOTH the served-curve pass (:meth:`_build_learner_hooks`)
        and the nightly slow-only attribution pass (:meth:`_slow_only_hourly`)
        build the closure identically — the binding can never diverge.

        The engine calls the hook per PLANE (channel == plane name). Storage is
        per plane, so pooling happens HERE at read time (SPEC §5): each plane
        reads the n-weighted pool of its group siblings (+ any legacy group
        channel), so the north module reads the shading the south module proved.
        An ungrouped plane's pool is just its own channel — bit-identical to the
        pre-groups single-channel behaviour. The pool membership depends on the
        live state (the legacy source is only added when present), so it is built
        against ``shd`` at bind time.
        """
        shd = self._shademap_state
        pool_map = self._build_shade_pool_map(shd)

        def beam_tau(channel, sun_az, sun_el, doy, static_prior):
            pool = pool_map.get(channel, (channel,))
            return shademap_mod.effective_tau_pooled(
                shd, channels=pool, sun_az=sun_az, sun_el=sun_el,
                doy=doy, static_prior=static_prior,
            )

        return beam_tau

    def _slow_only_hourly(self, iso: str) -> dict[str, float]:
        """Slow-only (shademap ∘ physics, NO day-ahead factor) hourly Wh for the
        local day ``iso`` — the drift monitor's per-layer attribution reference
        (audit #13b).

        Returns {} when the slow layer is not currently active (the same gate as
        :meth:`_build_learner_hooks`, via :meth:`_slow_active`): callers then
        treat slow-only == raw and blame only the fast layer, so the raw curve is
        NOT duplicated into the store. Also {} when no weather is cached or the
        engine pass raises — the nightly snapshot must never fail on this. Runs
        ONCE per nightly snapshot, so the extra engine pass is cheap.
        """
        if not self._slow_active():
            return {}
        weather = self._cached_weather()
        if weather is None:
            return {}
        try:
            tz = dt_util.get_time_zone(self.hass.config.time_zone)
            hooks = LearnerHooks(
                beam_tau=self._bind_beam_tau(),
                slot_factor=None,
                correction_source=CORRECTION_SOURCE_SHADEMAP,
                band_by_slot=None,
            )
            result = compute_forecast(
                self._site, weather, dt_util.utcnow(), tz=tz, hooks=hooks
            )
            return _filter_hourly_to_local_day(result.hourly_wh, iso)
        except Exception:  # pragma: no cover - never fail the nightly snapshot
            _LOGGER.debug(
                "Slow-only hourly compute failed for %s", iso, exc_info=True
            )
            return {}

    def _due_for_fetch(self, now: datetime) -> bool:
        # Scheduling keys on the last ATTEMPT, not the payload age: the
        # keep-richer branch retains the old payload but must still count as a
        # completed round-trip, else a degraded provider would be hammered every
        # recompute tick.
        if self._last_attempt_at is None or not self._last_fetch_ok:
            return True
        return now - self._last_attempt_at >= self._fetch_interval

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
        if (
            prior is not None
            and isinstance(prior.get("payload"), dict)
            and radiation_coverage(payload) < radiation_coverage(prior["payload"])
        ):
            # Keep the richer stored payload. The round-trip succeeded, so the
            # SCHEDULER anchor advances — but the PAYLOAD age anchor must NOT:
            # the served weather is still the old image and has to keep aging
            # through cached/physics_fallback/unavailable. Stamping it "fresh"
            # here would let a sustained partial-degradation serve arbitrarily
            # old weather at age ~0 forever (SPEC §7: never degrade silently).
            self._last_attempt_at = now
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
        self._last_attempt_at = now
        self._last_fetch_ok = True
        self._last_error = None
        self._store.set_last_payload(payload, now.isoformat())

    # ------------------------------------------------------------------
    # Ensemble-weather uncertainty (v0.16, SPEC §6) — NEVER load-bearing
    # ------------------------------------------------------------------

    async def _async_update_ensemble(self, now: datetime, weather) -> None:
        """Refresh the per-hour ensemble spread factors (v0.16, SPEC §6).

        Fetches a fresh ensemble payload only when the cached one is stale (its
        OWN ENSEMBLE_FETCH_INTERVAL_S cadence), parses it (identity-cached),
        derives the deterministic per-hour GHI from the CURRENT weather and
        recomputes the per-hour (f10, f90) factors. Wholly guarded: ANY failure —
        fetch error, malformed body, compute error — logs at debug, drops the
        factors and returns; the forecast then proceeds on the learned bands. The
        main degradation ladder is never touched here.
        """
        try:
            if self._due_for_ensemble_fetch(now):
                await self._async_try_fetch_ensemble(now)
            raw = self._ensemble_raw
            if raw is None:
                self._ensemble_factors = None
                return
            members_by_hour = self._parse_ensemble_cached(raw)
            det = self._det_ghi_by_hour(weather)
            self._ensemble_factors = ensembleband_mod.ensemble_band_factors(
                members_by_hour,
                det,
                min_members=ENSEMBLE_MIN_MEMBERS,
                min_det_ghi=ENSEMBLE_MIN_DET_GHI,
                f_min=ENSEMBLE_FACTOR_MIN,
                f_max=ENSEMBLE_FACTOR_MAX,
            )
        except Exception:  # pragma: no cover - ensemble never breaks the ladder
            _LOGGER.debug(
                "Ensemble update failed; serving learned bands", exc_info=True
            )
            self._ensemble_factors = None

    def _due_for_ensemble_fetch(self, now: datetime) -> bool:
        """True when the cached ensemble payload is stale (own slow cadence)."""
        if self._ensemble_fetched_at is None:
            return True
        return (
            now - self._ensemble_fetched_at
            >= timedelta(seconds=ENSEMBLE_FETCH_INTERVAL_S)
        )

    async def _async_try_fetch_ensemble(self, now: datetime) -> None:
        """Fetch one ensemble payload; on success cache it, on failure stay quiet.

        A fetch failure keeps the last-good raw payload (if any) so a transient
        outage still serves the previous ensemble spread; the fetch anchor is
        advanced only on success, so a failure retries next tick (mirrors the main
        fetch's not-ok retry). Never persists — the ensemble is in-memory only.
        """
        try:
            payload = await self._fetcher.async_fetch_ensemble_raw(
                self._site.latitude,
                self._site.longitude,
            )
        except FetchError as err:
            _LOGGER.debug("Ensemble fetch failed: %s", err)
            return
        self._ensemble_raw = payload
        self._ensemble_fetched_at = now

    def _parse_ensemble_cached(self, raw: dict) -> dict[str, list[float]]:
        """Parse the ensemble payload, reusing the parse while the body is stable.

        Keyed by payload OBJECT IDENTITY (mirrors ``_cached_weather``): a new
        ensemble fetch replaces the dict wholesale, so ``is`` misses and we
        re-parse; otherwise the parse is shared across the 15-min recompute ticks.
        """
        cache = self._ensemble_cache
        if cache is not None and cache[0] is raw:
            return cache[1]
        parsed = parse_ensemble(raw)
        self._ensemble_cache = (raw, parsed)
        return parsed

    def _det_ghi_by_hour(self, weather) -> dict[str, float]:
        """Deterministic per-hour mean GHI (W/m^2) from the current WeatherSeries.

        Aggregates the 15-min slot GHI to hour means keyed by the slot start
        floored to its UTC hour (ISO string) — the SAME key the ensemble parser
        emits for the interval START — so a member's GHI and the deterministic GHI
        line up per hour (all four 15-min slots of an hour share that hour's
        factors downstream). Cheap pure aggregation; never raises.
        """
        sums: dict[str, float] = {}
        counts: dict[str, int] = {}
        for slot in weather.slots:
            key = (
                dt_util.as_utc(slot.start)
                .replace(minute=0, second=0, microsecond=0)
                .isoformat()
            )
            sums[key] = sums.get(key, 0.0) + slot.ghi
            counts[key] = counts.get(key, 0) + 1
        return {k: sums[k] / counts[k] for k in sums}

    def ensemble_state_summary(self) -> dict[str, Any]:
        """Compact ensemble diagnostics block (v0.16, SPEC §6).

        Coordinate-free: the enable flag, the cached payload's age in seconds, the
        representative member count and how many hours currently carry a usable
        spread. Never raises; used by diagnostics.py.
        """
        fetched_at = getattr(self, "_ensemble_fetched_at", None)
        age_s: float | None = None
        if fetched_at is not None:
            age_s = round((dt_util.utcnow() - fetched_at).total_seconds(), 1)
        member_count = 0
        cache = getattr(self, "_ensemble_cache", None)
        if cache is not None:
            counts = [len(m) for m in cache[1].values()]
            member_count = max(counts) if counts else 0
        return {
            "available": True,
            "enabled": bool(getattr(self, "_ensemble_enabled", False)),
            "payload_age_seconds": age_s,
            "member_count": member_count,
            "hours_covered": len(getattr(self, "_ensemble_factors", None) or {}),
            "band_source": getattr(self, "_band_source", BAND_SOURCE_LEARNED),
        }

    def _cached_weather(self):
        last = self._store.get_last_payload()
        if not last:
            return None
        payload = last["payload"]
        # Reuse the parsed series while the SAME payload is served: parsing is
        # pure and the payload object is only replaced when a new fetch lands, so
        # the 15-min recompute cadence (and the nightly snapshot) share one parse
        # instead of re-parsing the identical body every cycle (audit #31).
        # ``getattr`` default keeps a bare (__new__-built) coordinator working.
        cache = getattr(self, "_weather_cache", None)
        if cache is not None and cache[0] is payload:
            return cache[1]
        try:
            weather = parse_weather(payload)
        except FetchError as err:
            _LOGGER.error("Stored payload no longer parses: %s", err)
            return None
        self._weather_cache = (payload, weather)
        return weather

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

    def _dayahead_today_kwh(
        self, result: ForecastResult, now: datetime
    ) -> float | None:
        """Today's kWh with the transient intraday scalar divided back out.

        ``energy_today_kwh`` must be a STABLE day-ahead expectation, not a live
        nowcast. The intraday scalar is applied forward-only and decays to 1.0
        over ``INTRADAY_APPLY_HORIZON_MINUTES`` (see ``_build_learner_hooks``);
        folding it into a full-day SUM makes the displayed total balloon in the
        morning (the whole large day is still forward, so the scalar has maximum
        leverage) and settle back to the day-ahead floor by early afternoon (the
        big hours are now in the frozen past) — a spurious intraday hump of the
        headline even though the underlying forecast has not changed.

        We keep the scalar in the served 15-min ``watts`` / ``wh_period`` curve
        (battery_manager integrates its FORWARD part as the live nowcast) but
        strip it from the headline. The shademap ``beam_tau`` and the day-ahead
        RLS bias stay — those are stable within a day. Each slot's applied
        intraday factor is reconstructed exactly as the ``slot_factor`` closure
        applies it, then divided out; the factor is clamped >= INTRADAY_SCALAR_MIN
        so the divide is always safe. Only the current local day is affected —
        tomorrow/d2 slots lie beyond the apply horizon and already carry factor
        1.0, so they equal the plain ``daily_kwh`` roll-up.

        CLAMP INTERACTION (MED-1): the served ``total_watts`` is POST second AC
        clamp. On a slot where the up-corrected grouped power hit the inverter
        ceiling, that second clamp bit and the scalar had NO effect on the served
        value — dividing the scalar back out there removes a correction that was
        never applied and understates the headline (up to the full factor). We
        detect such a slot from ``result.corrected_unclamped_watts`` (the
        pre-re-clamp corrected total): ``prereclamp - watts > 1e-6`` means the
        re-clamp bit. On a clamped slot we keep ``watts`` UNCHANGED (the ceiling
        — the true day-ahead value lies between ceiling/factor and the ceiling,
        and equals the ceiling whenever the day-ahead curve alone would also
        clamp, the typical clear-midday case); on an unclamped slot we divide the
        factor out exactly as before. A site with NO inverter groups never clamps
        (prereclamp == watts every slot) so its headline is bit-identical to the
        pre-MED-1 divide-always path. When ``corrected_unclamped_watts`` is empty
        (a v0.1 / older cached result) we cannot tell clamped from unclamped, so
        we fall back to divide-always (SPEC §8).
        """
        return self._dayahead_today_kwh_over(
            now, result.slot_starts, result.total_watts,
            result.corrected_unclamped_watts,
        )

    def _dayahead_today_kwh_ac(
        self, result: ForecastResult, now: datetime
    ) -> float | None:
        """AC-side analogue of :meth:`_dayahead_today_kwh` over the served AC curve.

        Identical day-ahead headline logic — strip the transient intraday factor,
        but keep a CLAMPED slot's served ceiling (the factor never reached it) —
        applied to ``ac_watts`` with the AC re-clamp detected from
        ``ac_corrected_unclamped_watts``. See :meth:`_dayahead_today_kwh` for the
        full MED-1 rationale; the only change is DC->AC on both series.
        """
        return self._dayahead_today_kwh_over(
            now, result.slot_starts, result.ac_watts,
            result.ac_corrected_unclamped_watts,
        )

    def _dayahead_today_kwh_over(
        self,
        now: datetime,
        slot_starts,
        watts_series,
        prereclamp,
    ) -> float | None:
        """Shared scalar-stripping headline roll-up for one served curve.

        Sums today's local-day slots of ``watts_series``, dividing the transient
        intraday factor back out on an UNclamped slot and keeping the served
        ceiling on a clamped one (``prereclamp[i] - watts > 1e-6``). The per-slot
        intraday factor is reconstructed by :meth:`_intraday_factor_for_slot` so
        the DC and AC strips share ONE copy of the factor math. Returns None when
        no slot falls on the local today.
        """
        have_prereclamp = len(prereclamp) == len(watts_series)
        local_today = dt_util.as_local(now).date()
        total_wh = 0.0
        seen = False
        for i, (start, watts) in enumerate(
            zip(slot_starts, watts_series, strict=False)
        ):
            start_utc = dt_util.as_utc(start)
            if dt_util.as_local(start_utc).date() != local_today:
                continue
            seen = True
            factor = self._intraday_factor_for_slot(start_utc, now)
            # A clamped slot's served ``watts`` is the AC ceiling the factor never
            # reached, so dividing it out understates the day-ahead value — keep
            # the ceiling. Otherwise recover the pre-factor value by dividing.
            if have_prereclamp and prereclamp[i] - watts > 1e-6:
                total_wh += watts * 0.25
            else:
                total_wh += (watts / factor) * 0.25
        return round(total_wh / 1000.0, 3) if seen else None

    def _intraday_factor_for_slot(
        self, start_utc: datetime, now: datetime
    ) -> float:
        """The intraday factor the ``slot_factor`` closure applies at ``start_utc``.

        Reconstructed EXACTLY as ``_build_learner_hooks``' closure: the fast
        learner must be enabled (kill switch + not drift-disabled + a non-neutral
        scalar) and the slot in-progress or future (age > -15 min); otherwise the
        factor is 1.0. Shared by the DC and AC day-ahead strips so the factor math
        lives in ONE place (never duplicated); ``intraday_factor_at`` clamps the
        result >= INTRADAY_SCALAR_MIN so the caller's divide is always safe.
        """
        scalar = self._intraday_scalar
        fast_active = (
            self._learner_config.fast_enabled
            and not self._drift_state.fast_disabled
            and scalar != INTRADAY_NEUTRAL
        )
        if not fast_active:
            return 1.0
        age_min = (start_utc - now).total_seconds() / 60.0
        if age_min > -15.0:  # matches the slot_factor gate
            return bias_mod.intraday_factor_at(max(0.0, age_min), scalar)
        return 1.0

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
        # AC-side (Phase 2): the served AC local-day roll-up, mirroring ``daily``.
        daily_ac = _local_daily_kwh_ac(result)

        watts = {
            _iso(start): round(w, 1)
            for start, w in zip(result.slot_starts, result.total_watts, strict=False)
        }
        wh_period = {
            _iso(start): round(w * 0.25, 2)
            for start, w in zip(result.slot_starts, result.total_watts, strict=False)
        }
        # AC-side 15-min curves, mirroring ``watts`` / ``wh_period`` on ac_watts.
        watts_ac = {
            _iso(start): round(w, 1)
            for start, w in zip(result.slot_starts, result.ac_watts, strict=False)
        }
        wh_period_ac = {
            _iso(start): round(w * 0.25, 2)
            for start, w in zip(result.slot_starts, result.ac_watts, strict=False)
        }

        raw_hourly = result.raw_hourly_wh or result.hourly_wh

        data: dict[str, Any] = {
            "status": status,
            "degraded": status != STATUS_FRESH,
            "weather_age_seconds": int(age.total_seconds()),
            "last_error": self._last_error,
            "power_now_w": round(_power_now(result, now), 1),
            # Day-ahead (scalar-free) headline: stable across the day. The live
            # intraday nowcast stays in the watts/wh_period curve below, so on
            # the current day energy_today_kwh != sum(today's wh_period) by
            # design (they still match exactly for tomorrow/d2). See
            # _dayahead_today_kwh for why.
            "energy_today_kwh": self._dayahead_today_kwh(result, now),
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
        # v0.16 ensemble bands: which source shaped today's band slots (learned /
        # ensemble / envelope). Always present; "learned" when the ensemble is off
        # or contributed nothing today. ``getattr`` default keeps a bare
        # coordinator (and _build_data called without a hooks pass) working.
        data[DATA_KEY_BAND_SOURCE] = getattr(
            self, "_band_source", BAND_SOURCE_LEARNED
        )
        data[DATA_KEY_LEARNER_STATUS] = self._learner_status()
        data[DATA_KEY_BIAS_CELLS] = self._bias_cells_summary()
        data[DATA_KEY_DRIFT_MAE] = self._latest_drift_mae()
        # --- v0.4 additive scoreboard keys (SPEC §9/§10) ---
        # The rolling-window aggregate view + the kill-gate verdict, derived from
        # the persisted DayScore ring. Cheap pure assembly; never raises.
        summary = self._scoreboard_summary()
        data[DATA_KEY_SCOREBOARD] = summary
        data[DATA_KEY_KILL_GATE_PASSED] = summary.get("kill_gate_passed")
        # --- v0.4 additive quantile band curves (SPEC §6/§10) ---
        # {p10: {iso_slot: Wh}, p50: ..., p90: ...} 15-min band Wh curves, from
        # the engine's per-slot band watts (only present when quantile bands were
        # applied this cycle; absent/cold-start => omitted so the P10/P90 sensors
        # and the get_forecast blocks degrade gracefully to unknown).
        quantile_curves = self._quantile_curves(result)
        if quantile_curves:
            data[DATA_KEY_QUANTILE_CURVES] = quantile_curves
        # --- Phase 2 additive AC keys (SPEC AC-side forecast) ---------------
        # The served-AC siblings of the DC headline / curve / roll-up keys above.
        # The main power / energy / band sensors read THESE (AC is the
        # operator-facing standard); the DC keys stay for the new *_dc diagnostic
        # sensors + the learner/scoreboard truth. Each is its DC sibling recomputed
        # from the ac_* result fields; the DC keys above are byte-unchanged.
        data["power_now_w_ac"] = round(_power_now_ac(result, now), 1)
        data["energy_today_kwh_ac"] = self._dayahead_today_kwh_ac(result, now)
        data["energy_tomorrow_kwh_ac"] = _round3(
            daily_ac.get((local_today + timedelta(days=1)).isoformat())
        )
        data["energy_d2_kwh_ac"] = _round3(
            daily_ac.get((local_today + timedelta(days=2)).isoformat())
        )
        data["watts_ac"] = watts_ac
        data["wh_period_ac"] = wh_period_ac
        data["hourly_wh_ac"] = dict(result.ac_hourly_wh)
        data["daily_kwh_ac"] = dict(daily_ac)
        # AC band curves (Phase 2): {p10/p90: {iso_hour: Wh}} HOURLY, only present
        # when the engine issued AC bands this cycle (bands active); absent
        # otherwise so the AC band sensors degrade to unknown (no fake spread).
        quantile_curves_ac = self._quantile_curves_ac(result)
        if quantile_curves_ac:
            data[DATA_KEY_QUANTILE_CURVES_AC] = quantile_curves_ac
        return data

    def _quantile_curves(self, result: ForecastResult) -> dict[str, dict[str, float]]:
        """Build ``{p10/p50/p90: {iso_slot: Wh}}`` 15-min band curves from result.

        The engine populates ``p10_watts`` / ``p50_watts`` / ``p90_watts`` only
        when band_by_slot was applied (non-empty); each is aligned to
        ``result.slot_starts``. Convert instantaneous watts to per-slot Wh
        (w * 0.25). Returns an empty dict when no bands were issued (quantiles
        off / cold start), so the caller omits DATA_KEY_QUANTILE_CURVES.
        """
        p10w = result.p10_watts
        p50w = result.p50_watts
        p90w = result.p90_watts
        if not p10w and not p50w and not p90w:
            return {}
        starts = result.slot_starts

        def _curve(watts) -> dict[str, float]:
            return {
                _iso(start): round(w * 0.25, 2)
                for start, w in zip(starts, watts, strict=False)
            }

        return {
            FORECAST_RESP_KEY_P10: _curve(p10w),
            FORECAST_RESP_KEY_P50: _curve(p50w),
            FORECAST_RESP_KEY_P90: _curve(p90w),
        }

    def _quantile_curves_ac(self, result: ForecastResult) -> dict[str, dict[str, float]]:
        """Build ``{p10/p90: {iso_hour: Wh}}`` AC band curves from result.

        The engine populates ``ac_p10_hourly_wh`` / ``ac_p90_hourly_wh`` (HOURLY
        Wh, keyed by ISO-UTC hour, capped at the per-slot AC ceiling) only when
        AC bands were issued this cycle. P50 == ``ac_watts`` so it is not carried.
        Returns an empty dict when no AC bands were issued (quantiles off / cold
        start), so the caller omits ``DATA_KEY_QUANTILE_CURVES_AC``.
        """
        p10 = result.ac_p10_hourly_wh
        p90 = result.ac_p90_hourly_wh
        if not p10 and not p90:
            return {}
        return {
            FORECAST_RESP_KEY_P10: {k: round(v, 2) for k, v in p10.items()},
            FORECAST_RESP_KEY_P90: {k: round(v, 2) for k, v in p90.items()},
        }

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

    def _bias_cells_summary(self) -> dict[str, Any]:
        """Current day-ahead RLS bias cells, for the diagnostic sensor (v0.19).

        One entry per learned ``cloud_class|day_part`` cell:
          ``theta``   – the raw learned multiplier (clamped to the bias band),
          ``n``       – trained days,
          ``applied`` – the factor actually served for that cell right now
                        (== ``theta`` once ``n >= RLS_MIN_SAMPLES``, else 1.0
                        while the cell is still cold-starting and stays neutral).
        Ratios are dimensionless (theta scales energy AND average power
        identically), so a dashboard can render them directly or multiply a
        per-part average-power baseline by ``applied`` to show the correction in
        W. ``day_part`` is the SOLAR-time part (v0.19), not a clock hour.
        """
        state = self._bias_state
        out: dict[str, dict[str, Any]] = {}
        for key, cell in sorted(state.cells.items()):
            cc, _, part = key.partition("|")
            out[key] = {
                "cloud_class": cc,
                "day_part": part,
                "theta": round(cell.clamped_theta(), 4),
                "n": int(cell.n),
                "applied": round(state.get_bias(cc, part), 4),
            }
        return out

    # ------------------------------------------------------------------
    # Nightly job (idempotent, date-keyed) — SPEC §4/§5
    # ------------------------------------------------------------------

    async def _async_nightly_job(self, now: datetime | None = None) -> None:
        """Snapshot today's issued forecast, log actuals, train + guard."""
        return await _nightly.async_nightly_job(self, now)

    def _catchup_days(self, latest: date) -> list[date]:
        """Closed local days to (re)process, oldest first, bounded/idempotent."""
        return _nightly.catchup_days(self, latest)

    async def _snapshot_issued(self, today: date) -> None:
        """Record today's issued forecast as a v2 dual-curve snapshot."""
        return await _nightly.snapshot_issued(self, today)

    def _cloud_class_by_hour(self, iso: str) -> dict[str, str]:
        """Per-ISO-hour forecast cloud class for ``iso`` (day-ahead RLS input)."""
        return _nightly.cloud_class_by_hour(self, iso)

    def _per_plane_modeled(self, iso: str) -> dict[str, PlaneHourlyModeled]:
        """Per-plane hourly modeled beam/diffuse/ghi/kc for the shademap trainer."""
        return _nightly.per_plane_modeled(self, iso)

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
        return await _nightly.train_and_guard(self, day)

    def _set_collapse_frozen_date(self, iso: str | None) -> None:
        """Persist the collapse-freeze date into DriftState (survives restart)."""
        return _nightly.set_collapse_frozen_date(self, iso)

    # ------------------------------------------------------------------
    # Skill scoreboard (the kill-gate) — SPEC §9/§10
    # ------------------------------------------------------------------

    async def _score_scoreboard_day(self, day: date) -> None:
        """Score one closed local ``day`` into the rolling scoreboard window."""
        return await _scoreboard_glue.score_scoreboard_day(self, day)

    def _issued_after_cutoff(self, snap: IssuedSnapshot, day: date) -> bool:
        """True when the snapshot was issued after the day-ahead cutoff of ``day``."""
        return _scoreboard_glue.issued_after_cutoff(self, snap, day)

    def _dominant_weather_class(self, snap: IssuedSnapshot, iso: str) -> str:
        """Yesterday's DOMINANT cloud class from the issued snapshot (SPEC §9)."""
        return _scoreboard_glue.dominant_weather_class(self, snap, iso)

    async def _comparison_kwh_for_day(self, day: date) -> dict[str, float]:
        """Per-comparison daily-kWh AS IT STOOD during ``day`` (no leakage)."""
        return await _scoreboard_glue.comparison_kwh_for_day(self, day)

    async def _async_read_comparison_history(
        self, day: date, comparisons: list[ComparisonConfig]
    ) -> dict[str, float]:
        """Read each comparison's daily-kWh AT THE ENGINE'S HORIZON for ``day``."""
        return await _scoreboard_glue.async_read_comparison_history(
            self, day, comparisons
        )

    def _normalise_comparison_kwh(
        self, entity_id: str, value: float
    ) -> float | None:
        """Normalise a raw comparison state to daily kWh, or None if unusable."""
        return _scoreboard_glue.normalise_comparison_kwh(self, entity_id, value)

    def _site_daily_kwh_ceiling(self) -> float | None:
        """The site's physical daily-energy ceiling: installed Wp x 24 h (kWh)."""
        return _scoreboard_glue.site_daily_kwh_ceiling(self)

    def _persist_scoreboard_state(self) -> None:
        self._call_store_setter(
            "set_scoreboard_state", self._scoreboard_state
        )

    def _persist_quantile_state(self) -> None:
        self._call_store_setter("set_quantile_state", self._quantile_state)

    def _persist_inverter_cal_state(self) -> None:
        self._call_store_setter(
            "set_inverter_cal_state", self._inverter_cal_state
        )

    async def _train_inverter_cal(self, day: date) -> None:
        """Calibrate the site inverter DC->AC efficiency for a closed ``day``."""
        return await _nightly.train_inverter_cal(self, day)

    def inverter_efficiency_learned(self) -> dict[str, Any] | None:
        """Lean diagnostic view of the learned site inverter eta_inv (Phase 3).

        Returns ``{"eta": <EMA>, "n": <folded samples>, "effective": <applied>}``
        where ``effective`` is None until the calibration is trusted (the config
        eta then stands). None when the state is unavailable (bare coordinator).
        """
        cal = getattr(self, "_inverter_cal_state", None)
        if cal is None:
            return None
        eff = inverter_cal_mod.effective_eta(cal)
        return {
            "eta": round(float(cal.eta), 4),
            "n": int(cal.n),
            "effective": None if eff is None else round(eff, 4),
        }

    def _train_quantiles_day(self, day: date) -> None:
        """Sample one closed ``day`` into the quantile relative-error ring."""
        return _nightly.train_quantiles_day(self, day)

    def _scoreboard_summary(self) -> dict[str, Any]:
        """The current scoreboard aggregate view for ``self.data`` / platforms."""
        return _scoreboard_glue.scoreboard_summary(self)

    def quantile_state_summary(self) -> dict[str, Any]:
        """Per-bin quantile sample counts for diagnostics (SPEC §6/§10).

        Reports the enable flag plus, per (class x part) bin, the sample count
        and whether the bin is trained (n >= QUANTILE_MIN_SAMPLES, i.e. emits a
        real spread rather than a collapsed-to-P50 band). Bins only — never the
        raw relative-error values or the operator's location.
        """
        from .const import QUANTILE_MIN_SAMPLES

        bins: dict[str, dict[str, Any]] = {}
        for key, ring in self._quantile_state.bins.items():
            n = len(ring)
            bins[key] = {"n": n, "trained": n >= QUANTILE_MIN_SAMPLES}
        return {
            "available": True,
            "enabled": self._quantiles_enabled,
            "bins": bins,
        }

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def _train_day_ahead(
        self, iso: str, issued: dict | None, actuals: dict | None
    ) -> None:
        """Train the day-ahead RLS bias from the issued (raw) vs actuals day."""
        return _nightly.train_day_ahead(self, iso, issued, actuals)

    def _site_measured_hourly(
        self, iso: str, hourly_actuals: dict[str, dict[str, float]] | None
    ) -> dict[str, float] | None:
        """Sum per-channel hourly measured Wh into a site total per hour."""
        return _nightly.site_measured_hourly(self, iso, hourly_actuals)

    def _day_ahead_samples(
        self,
        raw_hourly: dict[str, float],
        actuals: dict,
        snap: IssuedSnapshot,
        site_measured_hourly: dict[str, float] | None,
    ) -> list[_nightly._DayAheadSample]:
        """Build (cloud class x day part) RLS training samples for one day."""
        return _nightly.day_ahead_samples(
            self, raw_hourly, actuals, snap, site_measured_hourly
        )

    def _day_part_for_hourkey(self, hkey: str) -> str | None:
        """Local day part for an ISO-UTC hour key (core/bias.day_part_for_hour)."""
        return _nightly.day_part_for_hourkey(self, hkey)

    def _train_shademap(
        self, iso: str, issued: dict | None, actuals: dict | None
    ) -> None:
        """Train the shademap from the issued per-plane hourly modeled vs LTS."""
        return _nightly.train_shademap(self, iso, issued, actuals)

    def _day_is_measured_clear(
        self,
        iso: str,
        snap: IssuedSnapshot,
        hourly_actuals: dict[str, dict[str, float]],
    ) -> bool:
        """Measured-side clearness gate for shademap training (SPEC §5)."""
        return _nightly.day_is_measured_clear(self, iso, snap, hourly_actuals)

    def _train_channel(
        self,
        state: ShademapState,
        channel: str,
        modeled: PlaneHourlyModeled,
        measured_by_hour: dict[str, float],
    ) -> tuple[ShademapState, bool]:
        """EMA-update one channel's bins from its quasi-clear hourly samples."""
        return _nightly.train_channel(
            self, state, channel, modeled, measured_by_hour
        )

    def _store_hourly_actuals(self, iso: str) -> dict[str, dict[str, float]] | None:
        """Per-plane hourly measured energy for a day from the store ring."""
        return _nightly.store_hourly_actuals(self, iso)

    # ------------------------------------------------------------------
    # Guards: collapse detector, drift monitor, rollback ring
    # ------------------------------------------------------------------

    def _is_collapse_day(
        self, iso: str, issued: dict | None, actuals: dict | None
    ) -> bool:
        """Total-dropout day: measured << forecast (snow / channel loss)."""
        return _nightly.is_collapse_day(self, iso, issued, actuals)

    def _update_drift(
        self, iso: str, issued: dict | None, actuals: dict | None
    ) -> None:
        """Rolling daylight-MAE drift monitor with auto-disable (SPEC §5)."""
        return _nightly.update_drift(self, iso, issued, actuals)

    def _restore_layer_snapshot(self, layer: str) -> str | None:
        """Roll the auto-disabled layer back to its pre-streak state (SPEC §5)."""
        return _nightly.restore_layer_snapshot(self, layer)

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

    async def async_reset_day_ahead_bias(self) -> dict[str, Any]:
        """Clear the day-ahead RLS bias state (service backend, v0.19).

        Drops ALL learned (cloud_class, day_part) cells so the day-ahead layer
        returns to neutral immediately and the served curve falls back to the
        pure-physics + shademap curve; the nightly RLS then re-learns each cell
        from scratch (cold-start neutral until RLS_MIN_SAMPLES days). Use after
        a binning change or when a mis-trained cell is distorting the forecast.
        Enable flags, the shademap, drift state and the rollback ring are all
        untouched (this is a targeted reset, not a rollback). Persists the empty
        state and triggers a recompute so the correction disappears at once.
        """
        cleared = len(self._bias_state.cells)
        self._bias_state = BiasState()
        self._persist_bias_state()
        _LOGGER.info(
            "Day-ahead bias state reset (%d cell(s) cleared); re-learns nightly",
            cleared,
        )
        await self.async_request_refresh()
        return {"cleared_cells": cleared}

    def _maybe_push_rollback_snapshot(self, iso: str) -> None:
        """Push a pre-training rollback snapshot into the ring (idempotent/day)."""
        return _nightly.maybe_push_rollback_snapshot(self, iso)

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
        self._call_store_setter("set_bias_state", self._bias_state)

    def _persist_shademap_state(self) -> None:
        self._call_store_setter("set_shademap_state", self._shademap_state)

    def _persist_drift_state(self) -> None:
        self._call_store_setter("set_drift_state", self._drift_state)

    def _call_store_setter(self, name: str, payload: Any) -> None:
        setter = getattr(self._store, name, None)
        if setter is None:
            return
        try:
            setter(payload)
        except Exception:
            _LOGGER.warning(
                "Store setter %s failed; learner state NOT persisted "
                "(will be lost on restart)", name, exc_info=True
            )

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
        """Per-module measured DC energy for ``day``: (daily totals, hourly)."""
        return await _actuals.async_read_actuals(self, day)

    async def _async_read_ac_actuals(self, day: date) -> dict[str, float]:
        """Whole-site AC hourly energy for ``day`` (sign-corrected at the read
        boundary). ``{}`` when no AC meter is configured."""
        site = self._site
        tz = dt_util.get_time_zone(self.hass.config.time_zone) or UTC
        return await _actuals.async_read_ac_actuals(
            self.hass,
            getattr(site, "ac_actual_entity", None),
            day,
            tz,
            invert=getattr(site, "ac_actual_invert", False),
        )
