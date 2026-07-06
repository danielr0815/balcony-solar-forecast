"""Versioned persistent store for Balcony Solar Forecast.

Owner: store. One HA ``Store`` per config entry holds (SPEC §4, §5, §6, §9):

  * the last-good Open-Meteo payload + its fetch timestamp (survives a
    restart so the degradation ladder starts from a warm cache);
  * a forecast-as-issued ring — one snapshot per calendar day. v1 entries
    carried only ``hourly_wh`` / ``daily_kwh`` / ``status``; v2 (this schema)
    stores BOTH the raw-physics and the corrected hourly curves plus the
    per-plane modeled beam/diffuse/ghi/kc components the SLOW learner needs
    to train from hourly long-term statistics (SPEC §9, operator decision
    2026-07-06);
  * a daily actuals ring — measured DC energy per module per day (read from
    recorder statistics by the nightly job);
  * the LEARNER state (schema v2, additive): the day-ahead RLS ``BiasState``
    (the intraday scalar is NEVER persisted — SPEC §5), the ``ShademapState``,
    the ``DriftState`` (rolling MAE + loss streaks + auto-disable flags) and a
    small ring of ``LearnerSnapshot`` rollback points.

**Migration.** The inner schema bumps v1 -> v2 -> v3; the outer HA ``Store``
envelope (``STORAGE_VERSION``) stays 1. A live v1 install MUST survive an upgrade
losslessly: the three v1 rings are carried through untouched and the four learner
sections are injected at their neutral defaults (empty bias / shademap / drift,
no snapshots). This is additive — nothing is dropped.

v2 -> v3 is ADDITIVE (SPEC §14, v0.4). Every v2 key is carried through
BYTE-FAITHFUL and three new v3 sections are default-injected empty:
  * STORE_KEY_QUANTILE_STATE   -> QuantileState().to_dict()  (empty relerr ring)
  * STORE_KEY_SCOREBOARD_STATE -> ScoreboardState().to_dict() (empty day ring)
  * STORE_KEY_COMPARISON_RING  -> {}  ({iso_date: {comparison_name: daily_kwh}})

CRITICAL INVARIANT (SPEC §14): the LIVE install (entry
01KWT809F7MHH97F8XCKEJTZ0M) has a POPULATED v2 store on disk RIGHT NOW —
shademap 7 channels / 851 bins, day-ahead 12 cells, drift + rollback +
trained_days. A v2 -> v3 migration that drops or RESETS any of that learner
state is a CRITICAL failure. The migration is inner-schema only (the
STORAGE_VERSION envelope stays pinned at 1); it MUST default-inject the three new
sections and keep EVERYTHING else byte-faithful.

The EXACT v3 inner-schema layout (superset of v2 — every key below is required
in a well-formed v3 state; the first block is v1, the second v2, the third the
v0.4 additions):

    {
      "schema_version": 3,                       # _SCHEMA_KEY == STORAGE_DATA_VERSION_V3
      # --- v1 rings (carried through EVERY migration untouched) ---
      "last_payload":        {"fetched_at": iso, "payload": {...}} | None,
      "forecast_issued_log": {iso_date: snapshot(v1|v2 dict)},      # 90-day ring
      "daily_actuals_log":   {iso_date: {module: wh}},              # 90-day ring
      # --- v2 learner sections (carried through v2->v3 BYTE-FAITHFUL) ---
      "hourly_actuals_log":  {iso_date: {channel: {iso_hour: wh}}}, # short ring
      "bias_state":          BiasState.to_dict(),                   # day-ahead RLS
      "shademap_state":      ShademapState.to_dict(),               # 7 ch / 851 bins live
      "drift_state":         DriftState.to_dict(),                  # MAE + streaks + flags
      "learner_snapshots":   [LearnerSnapshot.to_dict(), ...],      # rollback ring
      "trained_days":        [iso_date, ...],                       # idempotence markers
      # --- v3 additions (default-injected empty on v2->v3 migration) ---
      "quantile_state":      QuantileState.to_dict(),               # {bin_key: [relerr,...]}
      "scoreboard_state":    ScoreboardState.to_dict(),             # {iso_date: DayScore}
      "comparison_ring":     {iso_date: {comparison_name: daily_kwh}},
    }

**Load is validate-and-clamp** (SPEC §5 "Store validate-and-clamp beim
Laden"): a corrupt / wrong-shaped / unknown-version blob NEVER crashes setup.
Every learner section round-trips through its ``from_dict`` (which itself
clamps every factor into its legal band and yields a neutral state on
garbage), so a corrupt learner blob degrades to neutral factors (1.0 / empty
bins), logging a warning rather than raising.

**Writes** are bundled via ``async_delay_save`` (eMMC-friendly, ≤ a few
writes/day) with an explicit flush on unload / HA stop. The last-good payload
write is additionally time-gated (``PAYLOAD_MIN_SAVE_INTERVAL_SECONDS``) so
the 30-min fetch cadence cannot rewrite the multi-hundred-KB store ~48x/day.

All state here is plain JSON-serialisable dicts; the learner dataclasses live
in ``core/types.py`` and are (de)serialised only at the section boundaries.
The pure state logic (validate / migrate / trim / ingest / clamp) is exposed
as module-level functions so it is testable without a running ``hass``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    BOOTSTRAP_KEY_BIAS,
    BOOTSTRAP_KEY_SCHEMA,
    BOOTSTRAP_KEY_SHADEMAP,
    BOOTSTRAP_KEY_SITE_SIGNATURE,
    BOOTSTRAP_MAX_BIN_N,
    BOOTSTRAP_SCHEMA_VERSION,
    HOURLY_ACTUALS_RING_DAYS,
    LEARNER_SNAPSHOT_RING,
    PAYLOAD_MIN_SAVE_INTERVAL_SECONDS,
    STORAGE_DATA_VERSION,
    STORAGE_DATA_VERSION_V2,
    STORAGE_DATA_VERSION_V3,
    STORAGE_SAVE_DELAY_SECONDS,
    STORAGE_VERSION,
    STORE_KEY_ACTUALS_LOG,
    STORE_KEY_BIAS_STATE,
    STORE_KEY_COMPARISON_RING,
    STORE_KEY_DRIFT_STATE,
    STORE_KEY_HOURLY_ACTUALS,
    STORE_KEY_ISSUED_LOG,
    STORE_KEY_LAST_PAYLOAD,
    STORE_KEY_LEARNER_SNAPSHOTS,
    STORE_KEY_QUANTILE_STATE,
    STORE_KEY_SCOREBOARD_STATE,
    STORE_KEY_TRAINED_DAYS,
    TRAINED_DAYS_RING,
    STORE_KEY_SHADEMAP_STATE,
)
from .core.types import (
    BiasState,
    DriftState,
    LearnerSnapshot,
    QuantileState,
    ScoreboardState,
    ShademapState,
)

_LOGGER = logging.getLogger(__name__)

# Ring sizes (SPEC §4/§6: 90-day error buffer + as-issued log).
_ISSUED_RING_DAYS = 90
_ACTUALS_RING_DAYS = 90

_SCHEMA_KEY = "schema_version"

# The current inner schema this build writes. The const contract pins v3 as the
# scoreboard/quantile-bearing schema; v2 (learner-bearing) and the legacy v1
# value are retained only for the migration branches below.
_CURRENT_SCHEMA = STORAGE_DATA_VERSION_V3
_SCHEMA_V2 = STORAGE_DATA_VERSION_V2  # == 2 (learner sections)
_LEGACY_SCHEMA = STORAGE_DATA_VERSION  # == 1


# ===========================================================================
# Pure state helpers (no HA; unit-testable without a running hass)
# ===========================================================================


def _empty_state() -> dict[str, Any]:
    """A well-formed, empty inner state at the CURRENT (v3) schema.

    v1 rings + v2 learner sections + v3 scoreboard/quantile sections, all at
    neutral defaults.
    """
    return {
        _SCHEMA_KEY: _CURRENT_SCHEMA,
        # --- v1 rings (carried through migration unchanged) ---
        STORE_KEY_LAST_PAYLOAD: None,  # {"fetched_at": iso, "payload": {...}}
        STORE_KEY_ISSUED_LOG: {},  # {iso_date: snapshot (v1 or v2 dict)}
        STORE_KEY_ACTUALS_LOG: {},  # {iso_date: {module: wh}}
        STORE_KEY_HOURLY_ACTUALS: {},  # {iso_date: {channel: {iso_hour: wh}}} (short ring)
        # --- v2 learner sections (neutral) ---
        STORE_KEY_BIAS_STATE: BiasState().to_dict(),
        STORE_KEY_SHADEMAP_STATE: ShademapState().to_dict(),
        STORE_KEY_DRIFT_STATE: DriftState().to_dict(),
        STORE_KEY_LEARNER_SNAPSHOTS: [],  # list[LearnerSnapshot dict], newest last
        STORE_KEY_TRAINED_DAYS: [],  # sorted list[iso_date] (training idempotence)
        # --- v3 scoreboard + quantile sections (neutral / empty) ---
        STORE_KEY_QUANTILE_STATE: QuantileState().to_dict(),  # {bin_key: [relerr,...]}
        STORE_KEY_SCOREBOARD_STATE: ScoreboardState().to_dict(),  # {iso_date: DayScore}
        STORE_KEY_COMPARISON_RING: {},  # {iso_date: {comparison_name: daily_kwh}}
    }


def _trim_ring(ring: dict[str, Any], keep: int) -> dict[str, Any]:
    """Keep the ``keep`` most recent ISO-date-keyed entries (lexicographic
    order == chronological for ISO dates). Mutates and returns ``ring``."""
    if len(ring) <= keep:
        return ring
    for stale in sorted(ring)[:-keep]:
        ring.pop(stale, None)
    return ring


def _coerce_last_payload(last: Any) -> dict[str, Any] | None:
    """Return a well-formed last-good payload wrapper or None."""
    if (
        isinstance(last, dict)
        and isinstance(last.get("payload"), dict)
        and isinstance(last.get("fetched_at"), str)
    ):
        return {"fetched_at": last["fetched_at"], "payload": last["payload"]}
    return None


def _coerce_issued_ring(raw: Any) -> dict[str, Any]:
    """Keep string-keyed issued entries whose value is a dict (v1 or v2)."""
    if not isinstance(raw, dict):
        return {}
    return _trim_ring(
        {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)},
        _ISSUED_RING_DAYS,
    )


def _coerce_actuals_ring(raw: Any) -> dict[str, Any]:
    """Keep string-keyed actuals entries whose value is a dict."""
    if not isinstance(raw, dict):
        return {}
    return _trim_ring(
        {
            k: v
            for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, dict)
        },
        _ACTUALS_RING_DAYS,
    )


def _coerce_hourly_actuals(raw: Any) -> dict[str, Any]:
    """Keep well-formed hourly-actuals entries ``{date: {channel: {hour: wh}}}``.

    Trimmed to the short HOURLY_ACTUALS_RING_DAYS window (this ring is far
    heavier than the daily rings — per-hour, per-channel — so it must stay
    small, SPEC §4 eMMC budget).
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for day, channels in raw.items():
        if not isinstance(day, str) or not isinstance(channels, dict):
            continue
        chan_out: dict[str, dict[str, float]] = {}
        for chan, hours in channels.items():
            if not isinstance(chan, str) or not isinstance(hours, dict):
                continue
            hour_out = {
                hk: float(wh)
                for hk, wh in hours.items()
                if isinstance(hk, str) and isinstance(wh, (int, float))
            }
            if hour_out:
                chan_out[chan] = hour_out
        if chan_out:
            out[day] = chan_out
    return _trim_ring(out, HOURLY_ACTUALS_RING_DAYS)


def _coerce_snapshots(raw: Any) -> list[dict[str, Any]]:
    """Validate the rollback ring: a list of well-formed LearnerSnapshot dicts.

    Each element round-trips through ``LearnerSnapshot.from_dict`` (which
    clamps its nested bias/shademap), so a corrupt element degrades to a
    neutral snapshot rather than poisoning the ring. Trimmed to the ring size.
    """
    if not isinstance(raw, list):
        return []
    snaps = [LearnerSnapshot.from_dict(e).to_dict() for e in raw]
    if len(snaps) > LEARNER_SNAPSHOT_RING:
        snaps = snaps[-LEARNER_SNAPSHOT_RING:]
    return snaps


def validate_state(raw: Any) -> dict[str, Any]:
    """Coerce a loaded blob into a well-formed CURRENT-schema (v3) state.

    Never raises (SPEC §5). Handles:
      * non-dict / missing-schema blobs -> empty neutral state;
      * a v1 blob -> migrated v1->v2->v3 (rings kept, learner + scoreboard/
        quantile sections injected at neutral defaults);
      * a v2 blob -> migrated v2->v3 ADDITIVELY: EVERY v2 key (rings + all four
        learner sections + hourly actuals + trained_days) carried through
        byte-faithful, the three v3 sections default-injected empty (SPEC §14
        CRITICAL: never drop/reset live learner state);
      * a v3 blob -> validated + clamped in place;
      * an unknown (future) schema -> discarded to empty (warned).

    Every learner / scoreboard / quantile section is passed through its dataclass
    ``from_dict`` so a corrupt section yields neutral factors (1.0 / empty bins /
    empty ring) — never a setup crash.
    """
    if not isinstance(raw, dict):
        return _empty_state()

    version = raw.get(_SCHEMA_KEY)

    if version == _LEGACY_SCHEMA:
        # v1 -> v2 (learner sections) -> v3 (scoreboard/quantile sections).
        return _migrate_v2_to_v3(_migrate_v1_to_v2(raw))

    if version == _SCHEMA_V2:
        return _migrate_v2_to_v3(raw)

    if version == _CURRENT_SCHEMA:
        return _validate_v3(raw)

    # Unknown / missing / future schema: discard but never crash setup.
    if version is not None:
        _LOGGER.warning(
            "Discarding forecast store: schema %s not in {%s, %s, %s}; "
            "starting from an empty, well-formed state",
            version,
            _LEGACY_SCHEMA,
            _SCHEMA_V2,
            _CURRENT_SCHEMA,
        )
    return _empty_state()


def _migrate_v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """Lossless additive migration of a v1 blob to the v2 schema.

    The three v1 rings (last payload, issued log, actuals log) are preserved
    exactly; the four learner sections are injected at their neutral defaults.
    A live v1 install therefore keeps every byte of its warm cache and history
    across the upgrade (SPEC §5/§6).
    """
    _LOGGER.info(
        "Migrating forecast store schema %s -> %s (additive; learner state "
        "injected at neutral defaults)",
        _LEGACY_SCHEMA,
        _SCHEMA_V2,
    )
    state = _empty_state()
    state[STORE_KEY_LAST_PAYLOAD] = _coerce_last_payload(
        raw.get(STORE_KEY_LAST_PAYLOAD)
    )
    state[STORE_KEY_ISSUED_LOG] = _coerce_issued_ring(raw.get(STORE_KEY_ISSUED_LOG))
    state[STORE_KEY_ACTUALS_LOG] = _coerce_actuals_ring(
        raw.get(STORE_KEY_ACTUALS_LOG)
    )
    # v1 had no hourly-actuals ring; a freshly-migrated install starts empty.
    state[STORE_KEY_HOURLY_ACTUALS] = {}
    # Learner + v3 sections stay at the neutral defaults already set by
    # _empty_state(): a freshly-migrated v1 install has learned nothing yet.
    # (The schema key is re-stamped to _CURRENT_SCHEMA by the following
    # _migrate_v2_to_v3 pass in validate_state.)
    return state


def _validate_learner_sections(
    raw: dict[str, Any], state: dict[str, Any]
) -> None:
    """Copy the v1 rings + v2 learner sections from ``raw`` into ``state``.

    Shared by the v2->v3 migration and the v3 in-place validation: every section
    round-trips through its clamping dataclass so a corrupt section collapses to
    a neutral state instead of crashing (SPEC §5), and a WELL-FORMED section is
    preserved byte-faithful (the round-trip is the identity on clean data —
    exactly what the CRITICAL v2->v3 invariant requires for the live 851-bin
    shademap / 12-cell bias / drift / rollback / trained_days).
    """
    state[STORE_KEY_LAST_PAYLOAD] = _coerce_last_payload(
        raw.get(STORE_KEY_LAST_PAYLOAD)
    )
    state[STORE_KEY_ISSUED_LOG] = _coerce_issued_ring(raw.get(STORE_KEY_ISSUED_LOG))
    state[STORE_KEY_ACTUALS_LOG] = _coerce_actuals_ring(
        raw.get(STORE_KEY_ACTUALS_LOG)
    )
    state[STORE_KEY_HOURLY_ACTUALS] = _coerce_hourly_actuals(
        raw.get(STORE_KEY_HOURLY_ACTUALS)
    )
    state[STORE_KEY_BIAS_STATE] = BiasState.from_dict(
        raw.get(STORE_KEY_BIAS_STATE, {})
    ).to_dict()
    state[STORE_KEY_SHADEMAP_STATE] = ShademapState.from_dict(
        raw.get(STORE_KEY_SHADEMAP_STATE, {})
    ).to_dict()
    state[STORE_KEY_DRIFT_STATE] = DriftState.from_dict(
        raw.get(STORE_KEY_DRIFT_STATE, {})
    ).to_dict()
    state[STORE_KEY_LEARNER_SNAPSHOTS] = _coerce_snapshots(
        raw.get(STORE_KEY_LEARNER_SNAPSHOTS)
    )
    trained = raw.get(STORE_KEY_TRAINED_DAYS)
    state[STORE_KEY_TRAINED_DAYS] = sorted(
        {d for d in trained if isinstance(d, str)}
    )[-TRAINED_DAYS_RING:] if isinstance(trained, list) else []


def _coerce_comparison_ring(raw: Any) -> dict[str, Any]:
    """Validate the comparison ring: {iso_date: {comparison_name: daily_kwh}}.

    Keeps string-date-keyed entries whose value is a dict of
    {str name: finite number}; trimmed to the issued/actuals ring window so it
    stays bounded alongside the daily rings. Never raises.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for day, cmps in raw.items():
        if not isinstance(day, str) or not isinstance(cmps, dict):
            continue
        row = {
            name: float(v)
            for name, v in cmps.items()
            if isinstance(name, str) and isinstance(v, (int, float))
        }
        out[day] = row
    return _trim_ring(out, _ACTUALS_RING_DAYS)


def _migrate_v2_to_v3(raw: dict[str, Any]) -> dict[str, Any]:
    """ADDITIVE v2 -> v3 migration (SPEC §14 CRITICAL — never reset learners).

    EVERY v2 key (the three v1 rings + hourly actuals + all four learner
    sections + trained_days) is carried through BYTE-FAITHFUL via
    :func:`_validate_learner_sections` (the clamping round-trip is the identity
    on clean data — the live 851-bin shademap / 12-cell bias / drift / rollback /
    trained_days survive intact). The three v3 sections are default-injected
    empty. Re-stamps the schema key to v3. Never raises.
    """
    _LOGGER.info(
        "Migrating forecast store schema %s -> %s (additive; scoreboard + "
        "quantile sections injected empty; all learner state preserved)",
        _SCHEMA_V2,
        _CURRENT_SCHEMA,
    )
    state = _empty_state()  # already carries empty v3 sections
    _validate_learner_sections(raw, state)
    # v3 sections stay at the empty defaults from _empty_state() (a v2 install
    # has no scoreboard / quantile history yet).
    return state


def _validate_v3(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate + clamp a v3 blob into a well-formed state (never raises)."""
    state = _empty_state()
    _validate_learner_sections(raw, state)
    state[STORE_KEY_QUANTILE_STATE] = QuantileState.from_dict(
        raw.get(STORE_KEY_QUANTILE_STATE, {})
    ).to_dict()
    state[STORE_KEY_SCOREBOARD_STATE] = ScoreboardState.from_dict(
        raw.get(STORE_KEY_SCOREBOARD_STATE, {})
    ).to_dict()
    state[STORE_KEY_COMPARISON_RING] = _coerce_comparison_ring(
        raw.get(STORE_KEY_COMPARISON_RING)
    )
    return state


def ingest_bootstrap(
    current_shademap: ShademapState,
    current_bias: BiasState,
    payload: Any,
    *,
    expected_signature: str | None = None,
) -> tuple[ShademapState, BiasState]:
    """Validate + clamp a backfill bootstrap payload into learner states.

    Returns the ``(shademap, bias)`` to persist. Raises ``ValueError`` ONLY on
    a schema mismatch / non-dict payload / site-signature mismatch (the import
    service surfaces that to the operator); every value inside a well-formed
    payload is clamped, never rejected. The backfilled shademap bins have their
    sample count ``n`` capped at ``BOOTSTRAP_MAX_BIN_N`` so live 15-min data
    overrides the hourly-smeared backfill quickly (SPEC §6).

    When ``expected_signature`` is given, the payload's
    ``BOOTSTRAP_KEY_SITE_SIGNATURE`` MUST match it (a bootstrap built for a
    different site — wrong coordinates / renamed planes — would replace the
    learner state with geometrically wrong bins, SPEC §6). A payload with no
    signature is accepted (older backfill files) but logged.

    Ingestion is a REPLACE of the learner state from the (trusted, offline-
    generated) bootstrap, not a merge: the backfill job produces a complete
    cold-start map, and merging partial bins with capped credit would be
    ambiguous. Callers snapshot the prior state first (rollback ring).
    """
    if not isinstance(payload, dict):
        raise ValueError("bootstrap payload is not an object")
    schema = payload.get(BOOTSTRAP_KEY_SCHEMA)
    if schema != BOOTSTRAP_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported bootstrap schema {schema!r} "
            f"(expected {BOOTSTRAP_SCHEMA_VERSION})"
        )
    if expected_signature is not None:
        got_sig = payload.get(BOOTSTRAP_KEY_SITE_SIGNATURE)
        if got_sig is None:
            _LOGGER.warning(
                "Bootstrap has no site signature; importing without the site "
                "sanity check"
            )
        elif got_sig != expected_signature:
            raise ValueError(
                f"bootstrap site signature {got_sig!r} does not match this "
                f"site ({expected_signature!r}); it was built for a different "
                "configuration"
            )

    # Shademap: validate/clamp via the dataclass, then cap every bin's n.
    shademap = ShademapState.from_dict(payload.get(BOOTSTRAP_KEY_SHADEMAP, {}))
    shademap = _cap_shademap_credit(shademap, BOOTSTRAP_MAX_BIN_N)

    # Day-ahead bias: validate/clamp via the dataclass (already band-clamped).
    bias = BiasState.from_dict(payload.get(BOOTSTRAP_KEY_BIAS, {}))

    return shademap, bias


def _cap_shademap_credit(state: ShademapState, max_n: int) -> ShademapState:
    """Return a copy of ``state`` with every bin's ``n`` capped at ``max_n``.

    Uses the dataclass round-trip so tau stays clamped; only the sample count
    is reduced (never raised). A bin already below the cap is untouched.
    """
    from dataclasses import replace

    from .core.types import ShademapBin

    capped: dict[str, dict[str, ShademapBin]] = {}
    for chan, bins in state.channels.items():
        capped[chan] = {
            bk: replace(b, n=min(int(b.n), int(max_n))) for bk, b in bins.items()
        }
    return ShademapState(channels=capped, version=state.version)


# ===========================================================================
# HA-wired store
# ===========================================================================


def _build_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Build the HA ``Store`` (seam: tests may inject a fake instead)."""
    return Store(
        hass,
        STORAGE_VERSION,
        f"balcony_solar_forecast.{entry_id}",
    )


class ForecastStore:
    """Thin, validating wrapper around one HA ``Store`` (schema v2)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        *,
        store: Store[dict[str, Any]] | None = None,
    ) -> None:
        # ``store`` is an injection seam for tests (a fake exposing the four
        # async_* methods); production callers pass only (hass, entry_id).
        self._store: Store[dict[str, Any]] = (
            store if store is not None else _build_store(hass, entry_id)
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
        """Load and validate; on any problem, reset to an empty state.

        If loading migrated a v1 blob (or clamped a corrupt v2 one), the
        upgraded/repaired state is scheduled back to disk so the next load is
        already at the current schema.
        """
        raw = await self._store.async_load()
        prior_version = raw.get(_SCHEMA_KEY) if isinstance(raw, dict) else None
        self._data = validate_state(raw)
        # Persist a migration/repair so the on-disk schema advances. Only when
        # there WAS something on disk that differed from the current schema —
        # a first-ever empty load stays lazy (nothing to write yet).
        if raw is not None and prior_version != _CURRENT_SCHEMA:
            self._schedule_save()

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
        ``PAYLOAD_MIN_SAVE_INTERVAL_SECONDS`` (SPEC §4 eMMC-wear budget). The
        nightly job and the unload/HA-stop flush guarantee eventual persistence.
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
    # Forecast-as-issued ring (v1 or v2 snapshot dicts)
    # ------------------------------------------------------------------

    def record_issued(self, iso_date: str, snapshot: dict[str, Any]) -> None:
        """Store the forecast published on ``iso_date`` (idempotent per day).

        ``snapshot`` may be a v1 dict (``hourly_wh`` / ``daily_kwh`` / ``status``)
        or an ``IssuedSnapshot.to_dict()`` v2 dict (both curves + per-plane
        components). The ring stores whatever the coordinator hands it; v1 and
        v2 entries coexist (``version`` disambiguates on read).
        """
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

    # ------------------------------------------------------------------
    # Per-channel hourly actuals ring (short window; shademap trainer input)
    # ------------------------------------------------------------------

    def record_hourly_actuals(
        self, iso_date: str, per_channel: dict[str, dict[str, float]]
    ) -> None:
        """Store measured per-channel hourly Wh for ``iso_date`` (idempotent).

        ``per_channel`` maps ``{channel: {iso_hour: wh}}``. This ring is far
        heavier than the daily rings, so it is trimmed to the short
        HOURLY_ACTUALS_RING_DAYS window (SPEC §4 eMMC budget). The shademap
        trainer consumes it via :meth:`get_hourly_actuals`.
        """
        ring = self._data.setdefault(STORE_KEY_HOURLY_ACTUALS, {})
        ring[iso_date] = {
            chan: dict(hours) for chan, hours in per_channel.items()
        }
        _trim_ring(ring, HOURLY_ACTUALS_RING_DAYS)
        self._schedule_save()

    def get_hourly_actuals(
        self, iso_date: str
    ) -> dict[str, dict[str, float]] | None:
        """Per-channel hourly measured Wh for a day, or None if absent."""
        return self._data.get(STORE_KEY_HOURLY_ACTUALS, {}).get(iso_date)

    # ------------------------------------------------------------------
    # Learner state: day-ahead bias (RLS)
    # ------------------------------------------------------------------

    def get_bias_state(self) -> BiasState:
        """Return the persisted day-ahead RLS bias (neutral if absent/corrupt).

        Always passes through ``from_dict`` so an in-memory blob that was never
        validated (e.g. a hand-set test fixture) is still clamped on read.
        """
        return BiasState.from_dict(self._data.get(STORE_KEY_BIAS_STATE, {}))

    def set_bias_state(self, state: BiasState) -> None:
        """Persist the day-ahead RLS bias (schedules a bundled write)."""
        self._data[STORE_KEY_BIAS_STATE] = state.to_dict()
        self._schedule_save()

    # ------------------------------------------------------------------
    # Learner state: shademap
    # ------------------------------------------------------------------

    def get_shademap_state(self) -> ShademapState:
        """Return the persisted shademap (neutral/empty if absent/corrupt)."""
        return ShademapState.from_dict(self._data.get(STORE_KEY_SHADEMAP_STATE, {}))

    def set_shademap_state(self, state: ShademapState) -> None:
        """Persist the shademap (schedules a bundled write)."""
        self._data[STORE_KEY_SHADEMAP_STATE] = state.to_dict()
        self._schedule_save()

    # ------------------------------------------------------------------
    # Learner state: drift monitor
    # ------------------------------------------------------------------

    def get_drift_state(self) -> DriftState:
        """Return the persisted drift-monitor state (neutral if absent/corrupt)."""
        return DriftState.from_dict(self._data.get(STORE_KEY_DRIFT_STATE, {}))

    def set_drift_state(self, state: DriftState) -> None:
        """Persist the drift-monitor state (schedules a bundled write)."""
        self._data[STORE_KEY_DRIFT_STATE] = state.to_dict()
        self._schedule_save()

    # ------------------------------------------------------------------
    # Learner state: rollback snapshot ring
    # ------------------------------------------------------------------

    def push_snapshot(self, snapshot: LearnerSnapshot) -> None:
        """Append a rollback snapshot, trimming to ``LEARNER_SNAPSHOT_RING``.

        The nightly job pushes a snapshot of (bias, shademap) BEFORE it applies
        that night's training, so a drifting layer can be rolled back. Newest
        entry is last; the oldest is dropped when the ring overflows (SPEC §5).
        """
        ring: list[dict[str, Any]] = self._data[STORE_KEY_LEARNER_SNAPSHOTS]
        ring.append(snapshot.to_dict())
        if len(ring) > LEARNER_SNAPSHOT_RING:
            del ring[: len(ring) - LEARNER_SNAPSHOT_RING]
        self._schedule_save()

    def get_snapshots(self) -> list[LearnerSnapshot]:
        """Return the rollback ring, oldest first (validated/clamped)."""
        return [
            LearnerSnapshot.from_dict(e)
            for e in self._data.get(STORE_KEY_LEARNER_SNAPSHOTS, [])
        ]

    def latest_snapshot(self) -> LearnerSnapshot | None:
        """Return the newest rollback snapshot, or None if the ring is empty."""
        ring = self._data.get(STORE_KEY_LEARNER_SNAPSHOTS, [])
        if not ring:
            return None
        return LearnerSnapshot.from_dict(ring[-1])

    # ------------------------------------------------------------------
    # Trained-day idempotence markers
    # ------------------------------------------------------------------

    def is_day_trained(self, iso_date: str) -> bool:
        """True when the nightly training/guard pass already consumed the day.

        The RLS update and the drift-streak counters are not internally
        idempotent, and the restart-time catch-up re-sweeps the last processed
        day — this persisted marker is what makes re-running a night a no-op
        (verify finding 2026-07-06).
        """
        days = self._data.get(STORE_KEY_TRAINED_DAYS, [])
        return isinstance(days, list) and iso_date in days

    def mark_day_trained(self, iso_date: str) -> None:
        """Record a completed training/guard pass for ``iso_date`` (trimmed)."""
        days = self._data.get(STORE_KEY_TRAINED_DAYS)
        if not isinstance(days, list):
            days = []
        if iso_date in days:
            return
        days.append(iso_date)
        days.sort()
        if len(days) > TRAINED_DAYS_RING:
            del days[: len(days) - TRAINED_DAYS_RING]
        self._data[STORE_KEY_TRAINED_DAYS] = days
        self._schedule_save()

    # ------------------------------------------------------------------
    # Bootstrap import (backfill JSON -> learner state)
    # ------------------------------------------------------------------

    def import_bootstrap(
        self, payload: Any, *, expected_signature: str | None = None
    ) -> None:
        """Validate + clamp a backfill bootstrap and REPLACE the learner state.

        Snapshots the prior (bias, shademap) into the rollback ring first, so an
        unwanted import can be rolled back, then swaps in the clamped, n-capped
        bootstrap. Raises ``ValueError`` on a schema mismatch / non-dict payload
        / site-signature mismatch (the import service surfaces it to the
        operator); all values inside a well-formed payload are clamped, never
        rejected (SPEC §6).
        """
        from homeassistant.util import dt as dt_util

        shademap, bias = ingest_bootstrap(
            self.get_shademap_state(),
            self.get_bias_state(),
            payload,
            expected_signature=expected_signature,
        )
        # Rollback point BEFORE the swap.
        self.push_snapshot(
            LearnerSnapshot(
                taken_at=dt_util.utcnow().isoformat(),
                bias=self.get_bias_state(),
                shademap=self.get_shademap_state(),
            )
        )
        self._data[STORE_KEY_BIAS_STATE] = bias.to_dict()
        self._data[STORE_KEY_SHADEMAP_STATE] = shademap.to_dict()
        self._schedule_save()

    # ------------------------------------------------------------------
    # v3: quantile state (SPEC §6) — owner: quantiles/store
    # ------------------------------------------------------------------

    def get_quantile_state(self) -> QuantileState:
        """Return the persisted quantile ring (empty if absent/corrupt)."""
        return QuantileState.from_dict(self._data.get(STORE_KEY_QUANTILE_STATE, {}))

    def set_quantile_state(self, state: QuantileState) -> None:
        """Persist the quantile ring (schedules a bundled write)."""
        self._data[STORE_KEY_QUANTILE_STATE] = state.to_dict()
        self._schedule_save()

    # ------------------------------------------------------------------
    # v3: scoreboard state (SPEC §9/§10) — owner: scoreboard/store
    # ------------------------------------------------------------------

    def get_scoreboard_state(self) -> ScoreboardState:
        """Return the persisted scoreboard day-ring (empty if absent/corrupt)."""
        return ScoreboardState.from_dict(
            self._data.get(STORE_KEY_SCOREBOARD_STATE, {})
        )

    def set_scoreboard_state(self, state: ScoreboardState) -> None:
        """Persist the scoreboard day-ring (schedules a bundled write)."""
        self._data[STORE_KEY_SCOREBOARD_STATE] = state.to_dict()
        self._schedule_save()

    # ------------------------------------------------------------------
    # v3: comparison ring (recorder-read cache) — owner: scoreboard/store
    # ------------------------------------------------------------------

    def record_comparison(
        self, iso_date: str, per_comparison_kwh: dict[str, float]
    ) -> None:
        """Cache the per-comparison daily-kWh read from recorder for ``iso_date``.

        ``per_comparison_kwh`` maps ``{comparison_name: daily_kwh}`` — each value
        the comparison entity's own value AS IT STOOD during ``iso_date`` (read
        from recorder history, no leakage). Idempotent per day; trimmed to the
        actuals-ring window.
        """
        ring = self._data.setdefault(STORE_KEY_COMPARISON_RING, {})
        ring[iso_date] = dict(per_comparison_kwh)
        _trim_ring(ring, _ACTUALS_RING_DAYS)
        self._schedule_save()

    def get_comparison(self, iso_date: str) -> dict[str, float] | None:
        """Per-comparison daily-kWh cached for a day, or None if absent."""
        return self._data.get(STORE_KEY_COMPARISON_RING, {}).get(iso_date)
