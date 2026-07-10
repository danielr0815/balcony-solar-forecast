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
    _power_at,  # noqa: F401
    _power_now,
    _raw_power_now,
    _replace_drift,
    _round3,
    _slot_index_at,
    _usable_power,
)
from ._nightly import _NIGHTLY_HOUR, _NIGHTLY_MINUTE
from .const import (
    CONF_COMPARISON_SENSORS,
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
    DATA_KEY_CORRECTED_HOURLY_WH,
    DATA_KEY_CORRECTION_SOURCE,
    DATA_KEY_DRIFT_MAE,
    DATA_KEY_INTRADAY_SCALAR,
    DATA_KEY_KILL_GATE_PASSED,
    DATA_KEY_LEARNER_STATUS,
    DATA_KEY_QUANTILE_CURVES,
    DATA_KEY_RAW_HOURLY_WH,
    DATA_KEY_SCOREBOARD,
    DAY_AHEAD_BIAS_NEUTRAL,
    DEFAULT_QUANTILES_ENABLED,
    DEFAULT_SCOREBOARD_ENABLED,
    DEFAULT_SCOREBOARD_GATE_MARGIN,
    DEFAULT_SCOREBOARD_WINDOW_DAYS,
    DOMAIN,
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
    IssuedSnapshot,
    LearnerConfig,
    LearnerHooks,
    PlaneHourlyModeled,
    QuantileBands,
    QuantileState,
    ScoreboardState,
    ShademapState,
    SiteConfig,
    clearsky,
    compute_forecast,
    solpos,
)
from .core import bias as bias_mod
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
        # Plane → shademap-channel mapping (SPEC §5 shade groups): grouped planes
        # pool their shade learning into one channel. Rebuilt on every reload
        # (site is immutable within a coordinator lifetime); the single source of
        # truth is PlaneConfig.shade_channel.
        self._shade_channel_map = self._build_shade_channel_map()

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
        # If the operator just grouped existing planes, pool their stale per-plane
        # shademap channels into the group channel once (idempotent, SPEC §5).
        self._migrate_shademap_channels()

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
        # Refresh the plane → shade-channel mapping on rebuild (single source of
        # truth: PlaneConfig.shade_channel).
        self._shade_channel_map = self._build_shade_channel_map()
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
    # Shade groups: plane -> shademap-channel mapping + migration (SPEC §5)
    # ------------------------------------------------------------------

    def _build_shade_channel_map(self) -> dict[str, str]:
        """Build ``{plane.name: plane.shade_channel}`` from the current site."""
        return {p.name: p.shade_channel for p in self._site.planes}

    def _shade_channel_mapping(self) -> dict[str, str]:
        """The plane → shademap-channel mapping (grouped planes share a channel).

        Returns the cached map built on construction / rebuild, falling back to
        building it from the current site so a bare coordinator (tests using
        ``__new__``) that never ran ``__init__`` still resolves the mapping. The
        single source of truth is ``PlaneConfig.shade_channel``.
        """
        mapping = getattr(self, "_shade_channel_map", None)
        if mapping is None:
            return self._build_shade_channel_map()
        return mapping

    def _migrate_shademap_channels(self) -> None:
        """Pool stale per-plane shademap channels into their group ONCE (SPEC §5).

        When the operator groups existing planes (sets a ``shade_group``), the
        persisted shademap still carries the old per-plane channels. If a loaded
        channel IS a current plane name whose ``shade_channel`` now differs (the
        plane was just grouped), the n-weighted ``shademap.merge_channels`` folds
        every such channel into its group channel and the result is persisted
        through the normal store path. Idempotent: after the merge those source
        channels are gone, so a re-run finds nothing to migrate.

        DOCUMENTED LIMITATION (SPEC §5): dissolving a group does NOT split the
        learned map back — those planes restart from the static prior and the
        group channel lingers as a harmless orphan (recoverable via
        ``rollback_learners``).
        """
        self._load_learner_states()
        state = self._shademap_state
        needs_merge = any(
            p.name in state.channels and p.shade_channel != p.name
            for p in self._site.planes
        )
        if not needs_merge:
            return
        self._shademap_state = shademap_mod.merge_channels(
            state, self._shade_channel_mapping()
        )
        self._persist_shademap_state()
        # A merged shademap changes what the next served curve applies; drop the
        # shade-profile memo so the diagram reflects the pooled channel.
        self._shade_profile_cache = None
        _LOGGER.info("Migrated per-plane shademap channels into shade groups")

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
        applies (engine ``_plane_poa_split``). When it is off / drift-disabled /
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
        shademap = self._shademap_state if slow_active else ShademapState()
        # Grouped planes share a shade channel: the diagram for either member
        # renders THAT channel's learned shading for this module's own geometry,
        # which is exactly the semantics the served forecast applies (SPEC §5).
        channel = self._shade_channel_mapping().get(module, module)
        tz = dt_util.get_time_zone(self.hass.config.time_zone) or UTC
        result = shadeprofile_mod.compute_shade_profile(
            plane=plane,
            shademap=shademap,
            channel=channel,
            latitude=self._site.latitude,
            longitude=self._site.longitude,
            day=day,
            tz=tz,
        )
        self._shade_profile_cache = (key, result)
        return result

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

        beam_tau = self._bind_beam_tau() if slow_active else None

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

        # Per-slot quantile bands (SPEC §6/§10): keyed by the identical
        # slot.start datetimes the engine iterates. Each slot's band is the
        # empirical P10/P50/P90 of its (forecast cloud class x local day part)
        # bin; a starved / cold-start bin collapses to the neutral band (no fake
        # spread). Gated on the quantiles kill switch; a slot with a neutral
        # band is omitted so the engine passes those slots through unchanged.
        band_by_slot: dict[datetime, QuantileBands] | None = None
        if self._quantiles_enabled and self._quantile_state.bins:
            bands: dict[datetime, QuantileBands] = {}
            for slot in weather.slots:
                local = dt_util.as_local(slot.start)
                cc = bias_mod.classify_cloud(
                    cloud_low=slot.cloud_low, cloud_mid=slot.cloud_mid,
                    cloud_high=slot.cloud_high,
                    visibility_m=slot.visibility_m, month=local.month,
                )
                dp = bias_mod.day_part_for_hour(local.hour)
                b = quantiles_mod.bands_for_bin(
                    self._quantile_state, cloud_class=cc, day_part=dp
                )
                # Omit a neutral (identity) band so the engine's `if band:` path
                # short-circuits an all-1.0 multiply.
                if not (b.p10 == 1.0 and b.p50 == 1.0 and b.p90 == 1.0):
                    bands[slot.start] = b
            band_by_slot = bands or None

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
                            correction_source=source, band_by_slot=band_by_slot)

    def _bind_beam_tau(self):
        """Bind ``shademap.effective_tau`` over the current ShademapState into
        the engine's ``beam_tau`` hook.

        Factored out so BOTH the served-curve pass (:meth:`_build_learner_hooks`)
        and the nightly slow-only attribution pass (:meth:`_slow_only_hourly`)
        build the closure identically — the binding can never diverge.

        The engine calls the hook per PLANE (channel == plane name); the mapping
        redirects grouped planes to their shared shade channel (SPEC §5), so the
        north module reads the shading the south module proved. Ungrouped planes
        map to their own name — bit-identical to the pre-groups behaviour.
        """
        shd = self._shademap_state
        mapping = self._shade_channel_mapping()

        def beam_tau(channel, sun_az, sun_el, doy, static_prior):
            channel = mapping.get(channel, channel)
            return shademap_mod.effective_tau(
                shd, channel=channel, sun_az=sun_az, sun_el=sun_el,
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
        """
        scalar = self._intraday_scalar
        fast_active = (
            self._learner_config.fast_enabled
            and not self._drift_state.fast_disabled
            and scalar != INTRADAY_NEUTRAL
        )
        local_today = dt_util.as_local(now).date()
        total_wh = 0.0
        seen = False
        for start, watts in zip(result.slot_starts, result.total_watts, strict=False):
            start_utc = dt_util.as_utc(start)
            if dt_util.as_local(start_utc).date() != local_today:
                continue
            seen = True
            factor = 1.0
            if fast_active:
                age_min = (start_utc - now).total_seconds() / 60.0
                if age_min > -15.0:  # matches the slot_factor gate
                    factor = bias_mod.intraday_factor_at(max(0.0, age_min), scalar)
            total_wh += (watts / factor) * 0.25
        return round(total_wh / 1000.0, 3) if seen else None

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
            for start, w in zip(result.slot_starts, result.total_watts, strict=False)
        }
        wh_period = {
            _iso(start): round(w * 0.25, 2)
            for start, w in zip(result.slot_starts, result.total_watts, strict=False)
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
        data[DATA_KEY_LEARNER_STATUS] = self._learner_status()
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
