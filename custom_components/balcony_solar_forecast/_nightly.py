"""Nightly training / guard sweep (idempotent, date-keyed) — SPEC §4/§5.

Owner: glue (nightly trainer). Runs at ~01:30 local (and once on startup as a
catch-up): snapshot today's issued forecast, read each closed day's measured
per-module energy, take a rollback snapshot, run the collapse detector, train
the day-ahead RLS bias + the shademap under the SPEC §5 label gates, sample the
quantile ring, and drive the rolling-MAE drift monitor with its auto-disable +
repair-issue + rollback ring.

Every function takes the coordinator as ``coord`` and touches exactly the same
attributes the methods did (``coord._store`` / ``coord._site`` /
``coord._drift_state`` / ``coord._bias_state`` / ``coord._shademap_state`` / …);
the persistence, repair-issue, ``_cached_weather``, ``_slow_frozen`` and
``_read_actuals_safe`` helpers stay on the coordinator and are reached back
through ``coord``. The coordinator exposes each of these as a 1-2 line delegate
(the tests build it via ``__new__`` and call the delegates directly), and
re-imports ``_NIGHTLY_HOUR`` / ``_NIGHTLY_MINUTE`` for ``async_start_nightly_job``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from homeassistant.util import dt as dt_util

from ._glue_util import (
    _daily_kwh_from_hourly,
    _filter_hourly_to_local_day,
    _hour_key,
    _replace_drift,
)
from .const import (
    CLOUD_CLASS_CLEAR,
    COLLAPSE_FORECAST_MIN_WH,
    COLLAPSE_MEASURED_MAX_FRAC,
    DATA_KEY_CORRECTED_HOURLY_WH,
    DATA_KEY_RAW_HOURLY_WH,
    DEFAULT_INVERTER_EFFICIENCY,
    DRIFT_LOSS_MARGIN,
    DRIFT_LOSS_MIN_ABS_WH,
    DRIFT_LOSS_STREAK_DAYS,
    DRIFT_WINDOW_DAYS,
    INVERTER_CAL_CLIP_HEADROOM_FRAC,
    ISSUE_FAST_LEARNER_DISABLED,
    ISSUE_SLOW_LEARNER_DISABLED,
    LEARNER_LAYER_FAST,
    LEARNER_LAYER_SLOW,
    NIGHTLY_CATCHUP_MAX_DAYS,
    SHADEMAP_MEASURED_CLEAR_MIN_FRAC,
)
from .core import (
    IssuedSnapshot,
    LearnerSnapshot,
    PlaneHourlyModeled,
    ShademapState,
    clearsky,
    solpos,
)
from .core import bias as bias_mod
from .core import (
    inverter_cal as inverter_cal_mod,
)
from .core import (
    quantiles as quantiles_mod,
)
from .core import (
    shademap as shademap_mod,
)

_LOGGER = logging.getLogger(__name__)

# Nightly training/snapshot job local wall-clock (SPEC §4: ~01:30 local).
_NIGHTLY_HOUR = 1
_NIGHTLY_MINUTE = 30


# One nightly day-part-aggregated observation for the RLS bias. Duck-typed like
# the intraday sample the bias contract accepts (SPEC §5); the trainer only
# requires attribute access, so a frozen dataclass suffices.
@dataclass(frozen=True, slots=True)
class _DayAheadSample:
    """One nightly day-part-aggregated observation for the RLS bias."""

    cloud_class: str
    day_part: str
    measured_wh: float
    modeled_wh: float


async def async_nightly_job(coord, now: datetime | None = None) -> None:
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

    coord._load_learner_states()

    # 1) Snapshot the forecast we are issuing today (v2 dual-curve).
    await coord._snapshot_issued(today)

    # 2-6) Catch-up sweep: run the actuals-read + training/guard logic for
    # every closed day back to the last one we processed, bounded to a few
    # days (SPEC §5 idempotent/date-keyed). A missed 01:30 job (HA down at
    # night, multi-day outage) would otherwise silently lose those days'
    # training, drift and collapse detection.
    yesterday = today - timedelta(days=1)
    for day in coord._catchup_days(yesterday):
        iso = day.isoformat()
        if not coord._store.has_actuals(iso):
            read = await coord._read_actuals_safe(day)
            if read is not None:
                daily, hourly = read
                # A day that failed the frozen-channel gate returns empty;
                # do NOT record it, so a later manual re-run can fill it.
                if daily:
                    coord._store.record_actuals(iso, daily)
                if hourly:
                    coord._store.record_hourly_actuals(iso, hourly)
        try:
            await coord._train_and_guard(day)
        except Exception:  # pragma: no cover - never crash the scheduler
            _LOGGER.warning(
                "Nightly training/guard failed for %s", day, exc_info=True
            )
        # Skill scoreboard (SPEC §9/§10): score this closed day's engine
        # forecast-as-issued + each comparison AS IT STOOD that day against
        # the measured site energy, and persist it into the rolling window.
        # Independently guarded so a scoreboard failure never aborts the
        # training sweep (and vice-versa).
        try:
            await coord._score_scoreboard_day(day)
        except Exception:  # pragma: no cover - never crash the scheduler
            _LOGGER.warning(
                "Nightly scoreboard scoring failed for %s", day, exc_info=True
            )
        # Inverter DC->AC efficiency site calibration (AC-side Phase 3): fold this
        # closed day's eligible AC/DC hours into the learned eta_inv. Independently
        # guarded so an AC-meter read failure never aborts the DC nightly job (and
        # vice-versa); a no-meter / no-eligible-hours day is a silent no-op.
        try:
            await coord._train_inverter_cal(day)
        except Exception:  # pragma: no cover - never crash the scheduler
            _LOGGER.warning(
                "Nightly inverter calibration failed for %s", day, exc_info=True
            )


def catchup_days(coord, latest: date) -> list[date]:
    """Closed local days to (re)process, oldest first, bounded and idempotent.

    Sweeps from the day after the newest already-recorded actuals up to
    ``latest`` (yesterday), capped at NIGHTLY_CATCHUP_MAX_DAYS so a long
    outage does not fan out unboundedly. Every step keyed by ISO date is
    idempotent, so re-processing an already-trained day is safe (the
    date-keyed store guards make it a no-op where state already reflects it).
    """
    try:
        recorded = coord._store.actuals_dates()
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


async def snapshot_issued(coord, today: date) -> None:
    """Record today's issued forecast as a v2 dual-curve snapshot."""
    if coord.data is None or coord._store.get_issued(today.isoformat()) is not None:
        return
    # Slice the full-horizon curves to the snapshot's own LOCAL day so the
    # 90-day issued ring never carries 4 days of hours per snapshot (store
    # size / flash-wear) and every nightly consumer sees exactly one day.
    iso = today.isoformat()
    raw_hourly = _filter_hourly_to_local_day(
        coord.data.get(DATA_KEY_RAW_HOURLY_WH, {}), iso)
    corrected_hourly = _filter_hourly_to_local_day(
        coord.data.get(DATA_KEY_CORRECTED_HOURLY_WH, {}), iso)
    snapshot = IssuedSnapshot(
        issued_at=dt_util.utcnow().isoformat(),
        status=str(coord.data.get("status", "")),
        raw_hourly_wh=raw_hourly,
        corrected_hourly_wh=corrected_hourly,
        raw_daily_kwh=_daily_kwh_from_hourly(raw_hourly),
        corrected_daily_kwh=_daily_kwh_from_hourly(corrected_hourly),
        per_plane=coord._per_plane_modeled(iso),
        cloud_class_by_hour=coord._cloud_class_by_hour(iso),
        # Slow-only (shademap ∘ physics, no day-ahead) curve for the drift
        # monitor's per-layer attribution (audit #13b); {} when the slow layer
        # is inactive (slow-only == raw, so nothing extra is stored).
        slow_only_hourly_wh=coord._slow_only_hourly(iso),
    )
    coord._store.record_issued(iso, snapshot.to_dict())


def cloud_class_by_hour(coord, iso: str) -> dict[str, str]:
    """Per-ISO-hour forecast cloud class for ``iso`` (day-ahead RLS input).

    Derived from the cached weather series so the nightly RLS trains the
    real (cloud class x day part) cell rather than a fixed "clear" label
    (SPEC §5). A cloudy/fog/overcast day therefore trains the correct cell,
    and a genuinely clear day is never routed to a fog-poisoned one. Best
    effort: an unparseable weather image yields an empty map.
    """
    weather = coord._cached_weather()
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


def per_plane_modeled(coord, iso: str) -> dict[str, PlaneHourlyModeled]:
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
    result = getattr(coord, "_last_result", None)
    if result is None:
        return {}

    # Site-level hourly kc via THE shared reduction (clearsky.hourly_kc):
    # the clear-sky-energy-weighted mean over the hour's slots, the same
    # estimator the offline backfill applies to its hourly data. The
    # previous per-slot last-write-wins collapsed each hour to its FINAL
    # slot — the highest-elevation slot of a morning hour but the LOWEST of
    # an evening hour — so the quasi-clear gate was azimuth-asymmetric and
    # diverged from the backfill. The slot GHI is recovered by inverting
    # the engine's unclamped kc = ghi / haurwitz(midpoint elevation).
    kc_samples: dict[str, list[tuple[float, float]]] = {}
    site_kc = result.plane_results[0].kc if result.plane_results else ()
    for i, start in enumerate(result.slot_starts):
        if i >= len(site_kc):
            break
        start_utc = dt_util.as_utc(start)
        if dt_util.as_local(start_utc).date().isoformat() != iso:
            continue
        mid = start_utc + timedelta(minutes=7, seconds=30)
        _az, el = solpos.sun_position(
            mid, coord._site.latitude, coord._site.longitude
        )
        hw = clearsky.haurwitz_ghi(el)
        kc_samples.setdefault(_hour_key(start), []).append(
            (site_kc[i] * hw, el)
        )
    kc_by_hour = {h: clearsky.hourly_kc(s) for h, s in kc_samples.items()}

    out: dict[str, PlaneHourlyModeled] = {}
    for pr in result.plane_results:
        if not pr.beam_ref_watts and not pr.diffuse_ref_watts:
            continue  # engine without the reference export: do NOT train
        beam_wh: dict[str, float] = {}
        diffuse_wh: dict[str, float] = {}
        for i, start in enumerate(result.slot_starts):
            if dt_util.as_local(dt_util.as_utc(start)).date().isoformat() != iso:
                continue
            hkey = _hour_key(start)
            if i < len(pr.beam_ref_watts):
                beam_wh[hkey] = beam_wh.get(hkey, 0.0) + pr.beam_ref_watts[i] * 0.25
            if i < len(pr.diffuse_ref_watts):
                diffuse_wh[hkey] = diffuse_wh.get(hkey, 0.0) + pr.diffuse_ref_watts[i] * 0.25
        # Store trim: the issued ring keeps 90 days of these — drop NIGHT
        # hours (all-zero, nothing to train on: the trainer skips beam<=0
        # anyway) and round to 0.01 Wh / 6-decimal kc, far below trainer
        # noise, instead of 17-significant-digit floats.
        keep = {
            h
            for h in set(beam_wh) | set(diffuse_wh) | set(kc_by_hour)
            if beam_wh.get(h, 0.0) > 0.0
            or diffuse_wh.get(h, 0.0) > 0.0
            or kc_by_hour.get(h, 0.0) > 0.0
        }
        out[pr.name] = PlaneHourlyModeled(
            beam_wh={
                h: round(v, 2) for h, v in beam_wh.items() if h in keep
            },
            diffuse_wh={
                h: round(v, 2) for h, v in diffuse_wh.items() if h in keep
            },
            ghi={},
            kc={
                h: round(v, 6)
                for h, v in kc_by_hour.items()
                if h in keep
            },
        )
    return out


async def train_and_guard(coord, day: date) -> None:
    """Steps 3-6 of the nightly job for a closed calendar ``day``."""
    iso = day.isoformat()
    # Idempotence guard (verify finding 2026-07-06): the startup catch-up
    # re-sweeps the last processed day on EVERY restart / options reload,
    # and neither the RLS update nor the drift-streak counters are
    # internally idempotent — an unguarded re-run double-counts the same
    # training sample and double-increments the loss streak (spurious
    # auto-disable after 4 restarts on a bad-weather streak).
    if coord._store.is_day_trained(iso):
        _LOGGER.debug("Training for %s already recorded; skipping", iso)
        return
    # The day whose SERVED forecast the geometric freeze protects: the day
    # AFTER the analyzed collapse (snow still on the panels the next day).
    next_iso = (day + timedelta(days=1)).isoformat()

    issued = coord._store.get_issued(iso)
    actuals = coord._store.get_actuals(iso)

    # --- 3) Rollback snapshot (pre-training) --------------------------
    # Take one snapshot per night, idempotently (date-keyed by taken-day).
    coord._maybe_push_rollback_snapshot(iso)

    # --- 4) Collapse detector -----------------------------------------
    # All channels ~0 while forecast high => snow / total dropout: freeze
    # BOTH geometric learners for the FOLLOWING served day (SPEC §5), and
    # skip training the geometric learners on the collapse day itself.
    if coord._is_collapse_day(iso, issued, actuals):
        coord._set_collapse_frozen_date(next_iso)
        _LOGGER.info(
            "Collapse detected for %s: freezing geometric learners for %s",
            iso, next_iso,
        )
        # Still run the drift monitor so a persistently bad correction is
        # caught; do NOT train the geometric learners on a collapse day.
    else:
        # A non-collapse day closes: clear any freeze it (or an earlier day)
        # set that has not been superseded by a later collapse.
        frozen = coord._drift_state.collapse_frozen_date
        if frozen is not None and frozen <= next_iso:
            coord._set_collapse_frozen_date(None)
        # --- 5) Training under label gates ----------------------------
        coord._train_day_ahead(iso, issued, actuals)
        coord._train_shademap(iso, issued, actuals)

    # --- 5b) Quantile bands (SPEC §6/§10) -----------------------------
    # Sample the day's hourly relative errors (measured vs issued-CORRECTED)
    # into the 90-day ring. Runs on every day (incl. collapse days: a
    # dropout hour's relerr is legitimately near 0), inside the same
    # date-keyed idempotence guard as the learners below.
    coord._train_quantiles_day(day)

    # --- 6) Drift monitor --------------------------------------------
    coord._update_drift(iso, issued, actuals)

    # Mark the day consumed ONLY when both inputs existed: a day whose
    # actuals arrive later (LTS lag, manual re-run) must be retried by a
    # future catch-up instead of being skipped forever.
    if issued and actuals:
        coord._store.mark_day_trained(iso)


def set_collapse_frozen_date(coord, iso: str | None) -> None:
    """Persist the collapse-freeze date into DriftState (survives restart)."""
    if coord._drift_state.collapse_frozen_date == iso:
        return
    coord._drift_state = _replace_drift(
        coord._drift_state, collapse_frozen_date=iso
    )
    coord._persist_drift_state()


def train_quantiles_day(coord, day: date) -> None:
    """Sample one closed ``day`` into the quantile relative-error ring.

    NO-LEAKAGE + consistent frame (SPEC §6): the relative error is
    ``measured_hourly / issued-CORRECTED-hourly`` — the SAME issued corrected
    curve the scoreboard scores and the bands are later applied to. Each
    daylight hour whose corrected Wh exceeds QUANTILE_MIN_FORECAST_WH becomes
    one sample, classed by the issued snapshot's forecast cloud class for that
    hour (``cloud_class_by_hour``) x the local day part — the identical
    (class x part) taxonomy the day-ahead bias and the applier use. Gated on
    the quantiles kill switch; needs both the issued snapshot and hourly
    actuals for the day, else it is a no-op (retried by a later catch-up).
    Idempotence is provided by the same ``is_day_trained`` marker as the
    learners (see :meth:`_train_and_guard`).
    """
    if not coord._quantiles_enabled:
        return
    iso = day.isoformat()
    issued = coord._store.get_issued(iso)
    if not issued:
        return
    snap = IssuedSnapshot.from_dict(issued)
    corrected_hourly = _filter_hourly_to_local_day(
        snap.corrected_hourly_wh or snap.raw_hourly_wh, iso
    )
    if not corrected_hourly:
        return
    hourly_actuals = coord._store_hourly_actuals(iso)
    measured_hourly = coord._site_measured_hourly(iso, hourly_actuals)
    if not measured_hourly:
        return

    samples: list[quantiles_mod.QuantileSample] = []
    for hkey, corrected_wh in corrected_hourly.items():
        if hkey not in measured_hourly:
            continue
        part = coord._day_part_for_hourkey(hkey)
        if part is None:
            continue
        cc = snap.cloud_class_by_hour.get(hkey, CLOUD_CLASS_CLEAR)
        samples.append(
            quantiles_mod.QuantileSample(
                cloud_class=cc,
                day_part=part,
                measured_wh=float(measured_hourly[hkey]),
                corrected_wh=float(corrected_wh),
            )
        )
    if not samples:
        return
    # Date-stamp every sample with the trained day's ISO date so the ring is
    # date-windowed and the collapse gate can count distinct days (SPEC §6).
    coord._quantile_state = quantiles_mod.train_quantiles(
        coord._quantile_state, samples, training_date=iso
    )
    coord._persist_quantile_state()


async def train_inverter_cal(coord, day: date) -> None:
    """Calibrate the site inverter DC->AC efficiency for a closed ``day``.

    NEVER load-bearing (AC-side Phase 3). Gated + degrading at every step:
      * no ``ac_actual_entity`` configured -> no-op (no whole-site AC meter);
      * no stored per-module DC hourly actuals for the day -> no-op (the DC
        learners read + persisted them earlier in the sweep);
      * an empty / failed AC-meter read -> no-op.

    For each hour present in BOTH the summed per-module DC hourly actuals AND the
    whole-site AC meter, form ``(p_ac_w, p_dc_w) = (ac_wh, dc_wh)`` (Wh over one
    hour == mean W) and build an eligible ratio: the DC must clear
    INVERTER_CAL_MIN_LOAD_W and the hour must be UNCLIPPED (clip-headroom proxy:
    the datasheet-derived AC sits below INVERTER_CAL_CLIP_HEADROOM_FRAC of the
    summed group AC ceiling — gated on the INDEPENDENT DC side so a meter glitch
    cannot both pass the gate and corrupt the ratio). The eligible ratios fold
    into the EMA via ``inverter_cal.update`` (out-of-band ratios self-drop), and
    the state is persisted only when it actually changed. A day with 0 eligible
    (or only out-of-band) hours leaves the calibration untouched.
    """
    site = coord._site
    ac_entity = getattr(site, "ac_actual_entity", None)
    if not ac_entity:
        return  # no whole-site AC meter -> calibration is a pure no-op
    iso = day.isoformat()
    # Summed per-module DC hourly actuals (already read + stored for the DC
    # learners earlier in the sweep): {iso_hour: wh}. Absent -> nothing to
    # calibrate against (a later catch-up re-runs the day once LTS is complete).
    hourly_actuals = coord._store_hourly_actuals(iso)
    dc_by_hour = coord._site_measured_hourly(iso, hourly_actuals)
    if not dc_by_hour:
        return
    # Whole-site AC hourly energy from the meter (sign-corrected at the read
    # boundary). A recorder read failure is contained HERE (defense-in-depth with
    # the sweep-level guard) so an AC-read hiccup never aborts the DC nightly job.
    try:
        ac_by_hour = await coord._async_read_ac_actuals(day)
    except Exception:  # pragma: no cover - recorder is best-effort
        _LOGGER.warning(
            "Inverter-cal AC-meter read failed for %s; calibration untouched",
            iso, exc_info=True,
        )
        return
    if not ac_by_hour:
        return

    ceiling = sum(g.ac_limit_w for g in site.groups)
    ratios: list[float] = []
    for hkey, dc_wh in dc_by_hour.items():
        ac_wh = ac_by_hour.get(hkey)
        if ac_wh is None:
            continue
        dc_w = float(dc_wh)  # Wh over 1 h == mean W
        ac_w = float(ac_wh)
        # Clip-headroom gate on the INDEPENDENT DC side: the inverter clips AC at
        # ``ceiling``, so a datasheet-derived AC comfortably below it means the
        # hour is unclipped and its ratio does not understate eta.
        dc_derived_ac = DEFAULT_INVERTER_EFFICIENCY * dc_w
        clip_headroom_ok = (
            ceiling > 0.0
            and dc_derived_ac < ceiling * INVERTER_CAL_CLIP_HEADROOM_FRAC
        )
        r = inverter_cal_mod.eligible_ratio(
            ac_w, dc_w, clip_headroom_ok=clip_headroom_ok
        )
        if r is not None:
            ratios.append(r)
    if not ratios:
        return
    new_state = inverter_cal_mod.update(coord._inverter_cal_state, ratios)
    if new_state is coord._inverter_cal_state:
        return  # every ratio was out of band -> nothing folded, state unchanged
    coord._inverter_cal_state = new_state
    coord._persist_inverter_cal_state()


def train_day_ahead(
    coord, iso: str, issued: dict | None, actuals: dict | None
) -> None:
    """Train the day-ahead RLS bias from the issued (raw) vs actuals day.

    Aggregates the issued raw hourly curve and the measured site energy into
    (cloud class x day part) day-parts and runs one RLS step per part. The
    cloud class is derived from the issued snapshot's per-plane k_c/ghi where
    available; absent that (v0.1 issued), we fall back to CLEAR so the RLS
    still learns a coarse bias. Idempotent: a night already reflected in the
    state is guarded by the date-keyed nightly scheduling.
    """
    if not coord._learner_config.day_ahead_enabled:
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
    hourly_actuals = coord._store_hourly_actuals(iso)
    site_measured_hourly = coord._site_measured_hourly(iso, hourly_actuals)
    samples = coord._day_ahead_samples(
        raw_hourly, actuals, snap, site_measured_hourly
    )
    if not samples:
        return
    try:
        coord._bias_state = bias_mod.train_day_ahead_bias(coord._bias_state, samples)
    except NotImplementedError:
        return
    except Exception:  # pragma: no cover - defensive
        _LOGGER.debug("train_day_ahead_bias failed", exc_info=True)
        return
    coord._persist_bias_state()


def site_measured_hourly(
    coord, iso: str, hourly_actuals: dict[str, dict[str, float]] | None
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


def day_ahead_samples(
    coord,
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
        part = coord._day_part_for_hourkey(hkey)
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


def day_part_for_hourkey(coord, hkey: str) -> str | None:
    """SOLAR day part for an ISO-UTC hour key (core/bias.day_part_for_solar).

    Bins by APPARENT SOLAR time (solpos.hours_from_solar_noon at the hour
    START, longitude from the site), NOT the wall clock — so training uses the
    SAME solar boundary the coordinator applies (v0.19). If the site longitude
    is somehow unavailable, falls back to the legacy local-clock binning so
    training still runs rather than dropping every sample.
    """
    dt = dt_util.parse_datetime(hkey)
    if dt is None:
        return None
    lon = getattr(getattr(coord, "_site", None), "longitude", None)
    if lon is not None:
        hfn = solpos.hours_from_solar_noon(dt_util.as_utc(dt), lon)
        return bias_mod.day_part_for_solar(hfn)
    return bias_mod.day_part_for_hour(dt_util.as_local(dt).hour)


def train_shademap(
    coord, iso: str, issued: dict | None, actuals: dict | None
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
    if not coord._learner_config.slow_enabled:
        return
    if coord._slow_frozen():
        return  # collapse freeze silences the geometric learner today/next
    if not issued:
        return
    snap = IssuedSnapshot.from_dict(issued)
    if not snap.per_plane:
        return  # v0.1 issued or engine breakdown absent: nothing to train
    hourly_actuals = coord._store_hourly_actuals(iso)
    if not hourly_actuals:
        return
    # Measured-side clearness gate at the DAY level: reject days the forecast
    # called clear but reality was overcast (a transient weather bust must
    # not darken a geometric bin, SPEC §5). Uses the RAW gated modeled total
    # (the forecast the engine issued) vs the measured site total.
    if not coord._day_is_measured_clear(iso, snap, hourly_actuals):
        return
    state = coord._shademap_state
    trained = False
    for channel, modeled in snap.per_plane.items():
        measured_by_hour = hourly_actuals.get(channel)
        if not measured_by_hour:
            continue
        state, changed = coord._train_channel(
            state, channel, modeled, measured_by_hour
        )
        trained = trained or changed
    if trained:
        coord._shademap_state = state
        coord._persist_shademap_state()


def day_is_measured_clear(
    coord,
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


def train_channel(
    coord,
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
    plane = coord._site.plane_by_name(channel)
    if plane is None:
        return state, False
    # Storage is ALWAYS per plane (SPEC §5): each plane's learning is stored under
    # its OWN measurement channel (the plane name) forever. Grouping is applied
    # only at READ time (coordinator._build_shade_pool_map + effective_tau_pooled),
    # so it stays fully reversible — a dissolved group instantly reads each plane's
    # own channel again, with no data lost.
    store_channel = channel
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
        beam_share = beam_wh / (plane.wp) if plane.wp else 0.0
        dt = dt_util.parse_datetime(hkey)
        if dt is None:
            continue
        mid = dt + timedelta(minutes=30)
        sun_az, sun_el = solpos.sun_position(
            mid, coord._site.latitude, coord._site.longitude
        )
        # Neighbour-slot stability on the MEASURED/modeled ratio: the smooth
        # forecast k_c cannot see a real cloud fluctuation, so the gate keys
        # on this slot's ratio vs the previous slot's (shared with backfill).
        this_ratio = ratio_by_hour.get(hkey)
        neighbour_ratio = (
            ratio_by_hour.get(hkeys[idx - 1]) if idx > 0 else None
        )
        try:
            if not shademap_mod.is_quasi_clear(
                kc=modeled.kc.get(hkey, 0.0),
                sun_el=sun_el,
                beam_share=beam_share,
                stability_ratio=this_ratio,
                neighbour_ratio=neighbour_ratio,
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
                channel=store_channel,
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


def store_hourly_actuals(coord, iso: str) -> dict[str, dict[str, float]] | None:
    """Per-plane hourly measured energy for a day from the store ring."""
    try:
        return coord._store.get_hourly_actuals(iso)
    except Exception:  # pragma: no cover - defensive
        return None


def is_collapse_day(
    coord, iso: str, issued: dict | None, actuals: dict | None
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


def update_drift(
    coord, iso: str, issued: dict | None, actuals: dict | None
) -> None:
    """Rolling daylight-MAE drift monitor with per-layer auto-disable (SPEC §5).

    Decomposes the served curve as ``corrected = slow ∘ fast`` and attributes a
    "losing" day to the GUILTY layer only, so an innocent layer is never
    auto-disabled and rolled back alongside a drifting sibling (audit #13b). A
    layer is "losing" when its challenger daily-kWh MAE beats its reference by
    more than DRIFT_LOSS_MARGIN (relative) AND by more than DRIFT_LOSS_MIN_ABS_WH
    (absolute) — the absolute floor keeps a rounding-scale delta on a
    well-trained/clear day from counting as a loss:
      * SLOW (shademap): slow-only MAE vs raw physics MAE — the shademap made
        pure physics worse;
      * FAST (day-ahead): corrected MAE vs slow-only MAE — the day-ahead factor
        made the slow-only curve worse.
    The two streaks advance INDEPENDENTLY from their own signal (a non-losing
    leg resets only that layer's streak). DRIFT_LOSS_STREAK_DAYS consecutive
    losing days auto-disables that layer, raises a repair issue and rolls it
    back; the flag stays until the user re-enables in the options flow. The
    window is trimmed to DRIFT_WINDOW_DAYS.

    LEGACY fallback: when the snapshot carries NO slow-only curve (a pre-upgrade
    snapshot, or a day the slow layer was inactive so slow-only == raw), the
    decomposition has no independent slow signal — the monitor keeps exactly the
    original single corrected-vs-raw signal driving BOTH streaks.

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
    # Slow-only curve (shademap ∘ physics, no day-ahead) sliced the SAME way.
    # Empty on a legacy snapshot / slow-inactive day / failed compute: slow-only
    # == raw, so the per-layer decomposition degrades to the legacy shared
    # signal below.
    slow_hourly = _filter_hourly_to_local_day(snap.slow_only_hourly_wh, iso)
    has_slow = bool(slow_hourly)
    slow_total = sum(slow_hourly.values()) if has_slow else raw_total
    # Daily-kWh absolute error as the MAE proxy (the operator's primary
    # metric is daily kWh, SPEC §10/B9; the issued ring stores hourly so a
    # true daylight-hour MAE is available to a future finer implementation).
    raw_mae = abs(raw_total - measured_wh)
    corrected_mae = abs(corrected_total - measured_wh)
    slow_mae = abs(slow_total - measured_wh)
    baseline_mae = raw_mae  # pure physics is the baseline comparison here

    entry = {
        "raw": round(raw_mae, 2),
        "corrected": round(corrected_mae, 2),
        "baseline": round(baseline_mae, 2),
    }
    # Record the slow-only leg's MAE only when the snapshot carried a slow-only
    # curve (keep the dict shape stable on legacy/slow-inactive days).
    if has_slow:
        entry["slow"] = round(slow_mae, 2)
    daily = dict(coord._drift_state.daily_mae)
    daily[iso] = entry
    # Trim to the window (ISO date order == chronological).
    for stale in sorted(daily)[:-DRIFT_WINDOW_DAYS]:
        daily.pop(stale, None)

    def _losing(challenger_mae: float, reference_mae: float) -> bool:
        """A materially worse challenger: beats the reference by both the
        relative margin AND the absolute Wh floor (SPEC §5)."""
        return (
            challenger_mae > reference_mae * (1.0 + DRIFT_LOSS_MARGIN)
            and (challenger_mae - reference_mae) > DRIFT_LOSS_MIN_ABS_WH
        )

    fast_streak = coord._drift_state.fast_loss_streak
    slow_streak = coord._drift_state.slow_loss_streak
    fast_on = coord._learner_config.fast_enabled and not coord._drift_state.fast_disabled
    slow_on = coord._learner_config.slow_enabled and not coord._drift_state.slow_disabled
    if has_slow:
        # Per-layer decomposition (corrected = slow ∘ fast): each active layer's
        # streak advances or resets from ITS OWN leg — the slow layer on
        # slow-only-vs-physics, the fast layer on corrected-vs-slow-only.
        slow_losing = _losing(slow_mae, raw_mae)
        fast_losing = _losing(corrected_mae, slow_mae)
        if fast_on:
            fast_streak = fast_streak + 1 if fast_losing else 0
        if slow_on:
            slow_streak = slow_streak + 1 if slow_losing else 0
    else:
        # LEGACY fallback (no slow-only curve): the original single
        # corrected-vs-raw signal drives BOTH layers' streaks in lockstep, so
        # pre-upgrade snapshots and slow-inactive days behave exactly as before.
        losing = _losing(corrected_mae, raw_mae)
        if losing:
            if fast_on:
                fast_streak += 1
            if slow_on:
                slow_streak += 1
        else:
            fast_streak = 0
            slow_streak = 0

    fast_disabled = coord._drift_state.fast_disabled
    slow_disabled = coord._drift_state.slow_disabled
    if fast_on and fast_streak >= DRIFT_LOSS_STREAK_DAYS:
        fast_disabled = True
        fast_streak = 0
        coord._restore_layer_snapshot(LEARNER_LAYER_FAST)
        coord._raise_repair_issue(ISSUE_FAST_LEARNER_DISABLED)
        _LOGGER.warning("Fast learner auto-disabled after %d losing days", DRIFT_LOSS_STREAK_DAYS)
    if slow_on and slow_streak >= DRIFT_LOSS_STREAK_DAYS:
        slow_disabled = True
        slow_streak = 0
        coord._restore_layer_snapshot(LEARNER_LAYER_SLOW)
        coord._raise_repair_issue(ISSUE_SLOW_LEARNER_DISABLED)
        _LOGGER.warning("Slow learner auto-disabled after %d losing days", DRIFT_LOSS_STREAK_DAYS)

    # Preserve the option-seen + collapse-freeze fields (replace, not
    # reconstruct, so the FIX-5 transition memory + FIX-7 freeze survive).
    coord._drift_state = _replace_drift(
        coord._drift_state,
        daily_mae=daily,
        fast_loss_streak=fast_streak,
        slow_loss_streak=slow_streak,
        fast_disabled=fast_disabled,
        slow_disabled=slow_disabled,
    )
    coord._persist_drift_state()


def restore_layer_snapshot(coord, layer: str) -> str | None:
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
        snaps = coord._store.get_snapshots()
    except Exception:  # pragma: no cover - defensive
        snaps = []
    if not snaps:
        _LOGGER.warning(
            "No rollback snapshot available for %s layer restore", layer
        )
        return None
    snap = snaps[max(0, len(snaps) - DRIFT_LOSS_STREAK_DAYS)]
    if layer == LEARNER_LAYER_FAST:
        coord._bias_state = snap.bias
        coord._persist_bias_state()
    else:
        coord._shademap_state = snap.shademap
        coord._persist_shademap_state()
    _LOGGER.warning(
        "Rolled %s learner state back to pre-streak snapshot %s",
        layer, snap.taken_at,
    )
    return snap.taken_at


def maybe_push_rollback_snapshot(coord, iso: str) -> None:
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
        existing = coord._store.get_snapshots()
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
        bias=coord._bias_state,
        shademap=coord._shademap_state,
    )
    try:
        coord._store.push_snapshot(snapshot)
    except Exception:  # pragma: no cover - defensive
        _LOGGER.debug("Could not push rollback snapshot", exc_info=True)
