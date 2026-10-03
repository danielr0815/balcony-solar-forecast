"""Nightly training / guard sweep (idempotent, date-keyed) — SPEC §9.7/§9.8.

Owner: glue (nightly trainer). Runs at ~01:30 local (and once on startup as a
catch-up): snapshot today's issued forecast, read each closed day's measured
per-module energy, take a rollback snapshot, run the collapse detector, train
the day-ahead RLS bias + the shademap under the SPEC §9.8 label gates, sample the
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
from datetime import UTC, date, datetime, timedelta

from homeassistant.util import dt as dt_util

from . import _channel_health
from ._glue_util import (
    _daily_kwh_from_hourly,
    _filter_hourly_to_local_day,
    _hour_key,
    _replace_drift,
)
from .const import (
    CLOUD_CLASS_CLEAR,
    DATA_KEY_CORRECTED_HOURLY_WH,
    DATA_KEY_RAW_HOURLY_WH,
    DAY_AHEAD_BIAS_RESEED_N,
    DRIFT_LOSS_MARGIN,
    DRIFT_LOSS_MIN_ABS_WH,
    DRIFT_LOSS_STREAK_DAYS,
    DRIFT_WINDOW_DAYS,
    INVERTER_CAL_CLIP_HEADROOM_FRAC,
    INVERTER_CAL_MAX,
    INVERTER_CAL_MIN,
    ISSUE_FAST_LEARNER_DISABLED,
    ISSUE_SLOW_LEARNER_DISABLED,
    LEARNER_LAYER_DAY_AHEAD,
    LEARNER_LAYER_FAST,
    LEARNER_LAYER_SLOW,
    NIGHTLY_CATCHUP_MAX_DAYS,
    SHADEMAP_MEASURED_CLEAR_MIN_FRAC,
)
from .core import (
    IssuedSnapshot,
    LearnerSnapshot,
    PlaneHourlyModeled,
    QuantileState,
    ShademapState,
    electrical,
    learning_inputs,
    solpos,
)
from .core import bias as bias_mod
from .core import (
    inverter_cal as inverter_cal_mod,
)
from .core import (
    quantiles as quantiles_mod,
)
from .core.learning_inputs import DayAheadSample as _DayAheadSample
from .core.measurement_quality import production_collapsed

_LOGGER = logging.getLogger(__name__)

# Nightly training/snapshot job local wall-clock (SPEC §2: ~01:30 local).
_NIGHTLY_HOUR = 1
_NIGHTLY_MINUTE = 30


# One nightly day-part-aggregated observation for the RLS bias. Duck-typed like
# the intraday sample the bias contract accepts (SPEC §9.5); the trainer only
# requires attribute access, so a frozen dataclass suffices.


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
    crashes HA (SPEC §9.7). Recorder reads run in the recorder executor.
    """
    local_now = dt_util.as_local(now or dt_util.utcnow())
    today = local_now.date()

    coord._load_learner_states()

    # 1) Snapshot the forecast we are issuing today (v2 dual-curve).
    await coord._snapshot_issued(today)

    # 2-6) Catch-up sweep: run the actuals-read + training/guard logic for
    # every closed day back to the last one we processed, bounded to a few
    # days (SPEC §9.7 idempotent/date-keyed). A missed 01:30 job (HA down at
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
                # Learning visibility (0.23.1): fold the outcome into the
                # persisted discard streak so "we ran all week and learned
                # NOTHING" becomes a repair issue naming the responsible gate,
                # instead of a warning line per night. Only reached when a read
                # actually happened — an already-recorded day was counted when
                # it was first read, and a recorder IO failure (read is None) is
                # not a label-gate verdict.
                coord._record_actuals_outcome(day, accepted=bool(daily))
        try:
            await coord._train_and_guard(day)
        except Exception:  # pragma: no cover - never crash the scheduler
            _LOGGER.warning(
                "Nightly training/guard failed for %s", day, exc_info=True
            )
        # Skill scoreboard (SPEC §15.2): score this closed day's engine
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
    """Revisit the bounded window, including holes before newer recorded days.

    Actuals, training, scoring and inverter calibration have independent
    date guards: a successful newer step must never hide an older failed one.
    """
    return [latest - timedelta(days=offset)
            for offset in reversed(range(NIGHTLY_CATCHUP_MAX_DAYS))]


async def snapshot_issued(coord, today: date) -> None:
    """Record today's issued forecast as a v2 dual-curve snapshot."""
    if (coord.data is None or not getattr(coord, "last_update_success", True)
            or coord._store.get_issued(today.isoformat()) is not None):
        return
    # Slice the full-horizon curves to the snapshot's own LOCAL day so the
    # 90-day issued ring never carries 4 days of hours per snapshot (store
    # size / flash-wear) and every nightly consumer sees exactly one day.
    iso = today.isoformat()
    data = coord.data
    generation = getattr(coord, "_last_result", None)
    raw_hourly = _filter_hourly_to_local_day(
        data.get(DATA_KEY_RAW_HOURLY_WH, {}), iso)
    corrected_hourly = _filter_hourly_to_local_day(
        data.get(DATA_KEY_CORRECTED_HOURLY_WH, {}), iso)
    # Freeze synchronous provenance before the executor await. If a refresh
    # overtakes this pass, retry later rather than archiving mixed generations.
    per_plane = coord._per_plane_modeled(iso)
    cloud_classes = coord._cloud_class_by_hour(iso)
    eta = coord._effective_inverter_eta()
    from .core.provenance import bounded_provenance

    provenance = bounded_provenance(data.get("provenance"))
    bands = {}
    quantile_curves = data.get("quantile_curves") or {}
    for key in ("p10", "p50", "p90"):
        if isinstance(quantile_curves.get(key), dict):
            bands[f"dc_{key}_slot_wh"] = _filter_hourly_to_local_day(quantile_curves[key], iso)
    for key in ("p10", "p90"):
        if isinstance(data.get(f"wh_period_ac_{key}"), dict):
            bands[f"ac_{key}_slot_wh"] = _filter_hourly_to_local_day(data[f"wh_period_ac_{key}"], iso)
    slow_only = await coord._slow_only_hourly(iso)
    if (coord.data is not data or getattr(coord, "_last_result", None) is not generation
            or not getattr(coord, "last_update_success", True)):
        return
    computed = data.get("computed_at")
    archived = dt_util.utcnow().isoformat()
    snapshot = IssuedSnapshot(
        issued_at=computed if isinstance(computed, str) else archived,
        computed_at=computed if isinstance(computed, str) else None,
        archived_at=archived,
        corrected_ac_hourly_wh=(
            _filter_hourly_to_local_day(data["hourly_wh_ac"], iso)
            if isinstance(data.get("hourly_wh_ac"), dict) else None
        ),
        status=str(data.get("status", "")),
        raw_hourly_wh=raw_hourly,
        corrected_hourly_wh=corrected_hourly,
        raw_daily_kwh=_daily_kwh_from_hourly(raw_hourly),
        corrected_daily_kwh=_daily_kwh_from_hourly(corrected_hourly),
        per_plane=per_plane,
        cloud_class_by_hour=cloud_classes,
        # Slow-only (shademap ∘ physics, no day-ahead) curve for the drift
        # monitor's per-layer attribution (audit #13b); {} when the slow layer
        # is inactive (slow-only == raw, so nothing extra is stored).
        slow_only_hourly_wh=slow_only,
        # Site DC->AC efficiency in effect right now, so the issued AC curve can be
        # reconstructed later without hindsight (IRC-5/SCT-4).
        eta=eta,
        provenance=provenance,
        bands=bands or None,
    )
    coord._store.record_issued(iso, snapshot.to_dict())


def cloud_class_by_hour(coord, iso: str) -> dict[str, str]:
    """Per-ISO-hour forecast cloud class for ``iso`` (day-ahead RLS input).

    Derived from the cached weather series so the nightly RLS trains the
    real (cloud class x day part) cell rather than a fixed "clear" label
    (SPEC §8). A cloudy/fog/overcast day therefore trains the correct cell,
    and a genuinely clear day is never routed to a fog-poisoned one. Best
    effort: an unparseable weather image yields an empty map.
    """
    weather = coord._cached_weather()
    if weather is None:
        return {}
    lat = coord._site.latitude
    lon = coord._site.longitude
    out: dict[str, str] = {}
    for slot in weather.slots:
        start = dt_util.as_utc(slot.start)
        if dt_util.as_local(start).date().isoformat() != iso:
            continue
        local = dt_util.as_local(start)
        # Clear-sky-index classification (A5), consistent with the live coordinator
        # loops: the nightly RLS must train the same (class x part) cell it serves.
        _az, elev = solpos.sun_position(start, lat, lon)
        cc = bias_mod.classify_cloud(
            cloud_low=slot.cloud_low, cloud_mid=slot.cloud_mid,
            cloud_high=slot.cloud_high,
            visibility_m=slot.visibility_m, month=local.month,
            ghi=slot.ghi, elevation_deg=elev,
        )
        hkey = _hour_key(start)
        # First writer per hour wins (slots within an hour share cloud data).
        out.setdefault(hkey, cc)
    return out


def per_plane_modeled(coord, iso: str) -> dict[str, PlaneHourlyModeled]:
    """Bind HA timezone/result to the shared pure issued-reference reducer."""
    tz = dt_util.get_time_zone(coord.hass.config.time_zone) or UTC
    return learning_inputs.per_plane_modeled(coord._site, getattr(coord, "_last_result", None), iso, tz)


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

    from .core.training_plan import TrainingContext, plan_training

    context = TrainingContext(
        day=day, already_trained=False, has_issued=bool(issued), has_actuals=bool(actuals),
        collapsed=coord._is_collapse_day(iso, issued, actuals),
        frozen_date=coord._drift_state.collapse_frozen_date,
    )
    plan = plan_training(context)
    # The pure plan exposes intended mutations. Keep the historical application
    # order: rollback first, freeze, geometry, empirical bands, drift, date marker.
    if plan.rollback_snapshot:
        coord._maybe_push_rollback_snapshot(iso)
    if plan.freeze_changed:
        coord._set_collapse_frozen_date(plan.freeze_date)
    if context.collapsed:
        _LOGGER.info("Collapse detected for %s: freezing geometric learners for %s",
                     iso, next_iso)
    if plan.train_geometric:
        coord._train_day_ahead(iso, issued, actuals)
        coord._train_shademap(iso, issued, actuals)
    if plan.train_empirical:
        coord._train_quantiles_day(day)
        coord._update_drift(iso, issued, actuals)
    if plan.mark_consumed:
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

    NO-LEAKAGE + consistent frame (SPEC §11.1): the relative error is
    ``measured_hourly / issued-CORRECTED-hourly`` — the SAME issued corrected
    curve the scoreboard scores and the bands are later applied to, RESTRICTED
    to the metered planes (:func:`metered_modeled_hourly`): the measured sum
    only ever covers planes with an ``actual_entity``, so an unmetered plane
    inside the modeled total would read as a permanent fractional deficit and
    drag every relerr (hence P50) down by the metering share. Each daylight
    hour whose corrected Wh exceeds QUANTILE_MIN_FORECAST_WH becomes
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
    # Teilmengen-Regel: modeled side restricted to the metered planes; None
    # means the comparison is impossible (no metered plane / legacy snapshot
    # on a partially metered site) -> the day is skipped, never poisoned.
    corrected_hourly = metered_modeled_hourly(
        coord, snap, corrected_hourly, layer="corrected"
    )
    if corrected_hourly is None:
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
    # date-windowed and the collapse gate can count distinct days (SPEC §11.1).
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
    datasheet-derived AC of EACH group sits below
    INVERTER_CAL_CLIP_HEADROOM_FRAC of that group's AC ceiling — gated on the
    INDEPENDENT DC side so a meter glitch cannot both pass the gate and corrupt
    the ratio). Groupless sites have no modeled clip ceiling. The eligible ratios fold
    into the EMA via ``inverter_cal.update`` (out-of-band ratios self-drop), and
    the state is persisted only when it actually changed. A day with 0 eligible
    (or only out-of-band) hours leaves the calibration untouched.
    """
    site = coord._site
    ac_entity = getattr(site, "ac_actual_entity", None)
    if not ac_entity:
        return  # no whole-site AC meter -> calibration is a pure no-op
    iso = day.isoformat()
    if coord._store.is_inverter_day_trained(iso):
        return
    # Summed per-module DC hourly actuals (already read + stored for the DC
    # learners earlier in the sweep): {iso_hour: wh}. Absent -> nothing to
    # calibrate against (a later catch-up re-runs the day once LTS is complete).
    hourly_actuals = coord._store_hourly_actuals(iso)
    if any(
        not plane.actual_entity
        or not (hourly_actuals or {}).get(plane.name)
        for plane in site.planes
    ):
        # The AC meter covers the whole site. A partial DC denominator would
        # inflate AC/DC and either poison eta or make every sample look
        # implausible; calibration therefore requires complete DC metering.
        return
    # Coverage is a per-HOUR invariant too: the shared aggregator only returns
    # the local-day intersection carried by every metered plane, so an AC/DC
    # ratio can never receive a partial denominator after a recorder gap.
    site_dc_by_hour = coord._site_measured_hourly(iso, hourly_actuals)
    dc_by_hour = site_dc_by_hour or {}
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

    ratios: list[float] = []
    for hkey, dc_wh in dc_by_hour.items():
        ac_wh = ac_by_hour.get(hkey)
        if ac_wh is None:
            continue
        dc_w = float(dc_wh)  # Wh over 1 h == mean W
        ac_w = float(ac_wh)
        # Test headroom per configured inverter, not against one summed site
        # ceiling. A heavily loaded group cannot borrow unused headroom from a
        # sibling. Ungrouped sites have no modeled clip and are therefore
        # eligible; ungrouped planes on a mixed site likewise do not consume a
        # configured group's headroom.
        clip_headroom_ok = True
        for group in site.groups:
            group_dc = sum(
                float(hourly_actuals.get(name, {}).get(hkey, 0.0))
                for name in group.plane_names
            )
            eta = electrical._clamp_eta(group.inverter_efficiency)
            if eta * group_dc >= (
                group.ac_limit_w * INVERTER_CAL_CLIP_HEADROOM_FRAC
            ):
                clip_headroom_ok = False
                break
        r = inverter_cal_mod.eligible_ratio(
            ac_w, dc_w, clip_headroom_ok=clip_headroom_ok
        )
        if r is not None:
            ratios.append(r)
    if not ratios:
        return
    # RAW-ratio diagnostic (v0.20): record the measured AC/DC ratio BEFORE the
    # plausibility band, incl. out-of-band samples the EMA will drop. When the
    # DC sensors themselves mis-scale (e.g. ports reading ~25 % high), the true
    # ratio sits OUTSIDE [INVERTER_CAL_MIN, INVERTER_CAL_MAX] — the calibration
    # then correctly refuses to fold it, but without this diagnostic the
    # operator would never SEE the evidence (n stays 0, eta unknown). Kept
    # transient on the coordinator (not persisted): it is evidence, not state,
    # and the next nightly refreshes it.
    srt = sorted(ratios)
    mid = len(srt) // 2
    raw_median = (
        srt[mid] if len(srt) % 2 else (srt[mid - 1] + srt[mid]) / 2.0
    )
    in_band = sum(
        1 for r in ratios if INVERTER_CAL_MIN <= r <= INVERTER_CAL_MAX
    )
    coord._inverter_cal_raw = {
        "date": iso,
        "median_ratio": round(raw_median, 4),
        "n": len(ratios),
        "in_band_n": in_band,
    }
    # η plausibility watchdog (SPEC §10): fold the day's median into the
    # persisted out-of-band streak — raises ISSUE_ETA_OUT_OF_BAND after
    # INVERTER_CAL_OUT_OF_BAND_STREAK_DAYS consecutive out-of-band days, clears
    # it on the first day back in band. Pure visibility, never load-bearing.
    _channel_health.record_eta_calibration_outcome(
        coord, day, median_ratio=raw_median
    )
    new_state = inverter_cal_mod.update(coord._inverter_cal_state, ratios)
    if new_state is coord._inverter_cal_state:
        return  # every ratio was out of band -> nothing folded, state unchanged
    coord._inverter_cal_state = new_state
    coord._persist_inverter_cal_state()
    coord._store.mark_inverter_day_trained(iso)


def train_day_ahead(
    coord, iso: str, issued: dict | None, actuals: dict | None
) -> None:
    """Train the day-ahead RLS bias from the issued (slow-only) vs actuals day.

    Aggregates the issued slow-only hourly curve (shademap ∘ physics; B2) and
    the measured site energy into
    (cloud class x day part) day-parts and runs one RLS step per part. The
    cloud class is derived from the issued snapshot's per-plane k_c/ghi where
    available; absent that (v0.1 issued), we fall back to CLEAR so the RLS
    still learns a coarse bias. Idempotent: a night already reflected in the
    state is guarded by the date-keyed nightly scheduling.
    """
    if not coord._learner_config.day_ahead_enabled:
        return
    if (
        coord._drift_state.day_ahead_disabled
        or coord._drift_state.fast_disabled  # legacy v1 alias
    ):
        return
    if not issued or not actuals:
        return
    snap = IssuedSnapshot.from_dict(issued)
    # The modeled side of the day-ahead RLS is the SLOW-ONLY curve (shademap ∘
    # physics, no day-ahead factor) rather than pure raw (B2/SCT-3): theta is
    # APPLIED on top of the shademap-corrected curve, so training it against pure
    # raw would double-correct the same shading error once the shademap learns.
    # slow_only is {} when the slow layer is inactive (slow-only == raw) or on a
    # legacy snapshot -> fall back to raw (then corrected) so the RLS still trains.
    # Defense-in-depth: an old-code snapshot's rings can span 4 days; slice the
    # modeled curve to the training day before aggregating (FIX-2).
    raw_hourly = _filter_hourly_to_local_day(
        snap.slow_only_hourly_wh or snap.raw_hourly_wh or snap.corrected_hourly_wh,
        iso,
    )
    if not raw_hourly:
        return
    # Prefer TRUE per-hour measured site energy (from the hourly-actuals
    # ring): it gives an independent per-part signal AND real per-part cloud
    # conditioning. Fall back to the daily-apportioned path only when hourly
    # actuals are absent (coordinator:935).
    hourly_actuals = coord._store_hourly_actuals(iso)
    site_measured_hourly = coord._site_measured_hourly(iso, hourly_actuals)
    if hourly_actuals and site_measured_hourly is None:
        # Hourly records exist, but no hour is complete across every measured
        # channel. Falling back to the daily total here would silently turn an
        # incomplete recorder day into a valid label.
        return
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
    """Sum complete per-channel hourly measured Wh into a site total.

    Only configured, metered planes participate. An hour is returned only when
    EVERY such channel has a value for that exact hour; otherwise summing the
    surviving channels would manufacture a site-wide production collapse.
    Returns ``{iso_hour: wh}`` sliced to local day ``iso``, or None when no
    complete hour exists.
    """
    if not hourly_actuals:
        return None
    metered_names = tuple(
        plane.name
        for plane in getattr(getattr(coord, "_site", None), "planes", ())
        if plane.actual_entity
    )
    if not metered_names:
        return None
    by_channel: dict[str, dict[str, float]] = {}
    for name in metered_names:
        hours = hourly_actuals.get(name)
        if not hours:
            return None
        valid: dict[str, float] = {}
        for hkey, wh in hours.items():
            dt = dt_util.parse_datetime(hkey)
            if dt is None:
                continue
            if dt_util.as_local(dt_util.as_utc(dt)).date().isoformat() != iso:
                continue
            valid[hkey] = float(wh)
        if not valid:
            return None
        by_channel[name] = valid
    common_hours = set.intersection(
        *(set(hours) for hours in by_channel.values())
    )
    if not common_hours:
        return None
    return {
        hkey: sum(by_channel[name][hkey] for name in metered_names)
        for hkey in common_hours
    }


def metered_modeled_hourly(coord, snap: IssuedSnapshot, modeled_hourly: dict[str, float], *, layer: str = "raw") -> dict[str, float] | None:
    """Preserve the HA seam; exact metered-subset reduction is HA-free."""
    site = getattr(coord, "_site", None)
    if site is None:
        return None
    return learning_inputs.metered_modeled_hourly(site, snap, modeled_hourly, layer=layer)


def day_ahead_samples(coord, raw_hourly: dict[str, float], actuals: dict, snap: IssuedSnapshot,
                      site_measured_hourly: dict[str, float] | None) -> list[_DayAheadSample]:
    """Build samples against the original slow curve with production solar bins."""
    site = getattr(coord, "_site", None)
    if site is None:
        return []
    return learning_inputs.day_ahead_samples(site, raw_hourly, actuals, snap, site_measured_hourly,
                                            day_part=coord._day_part_for_hourkey)


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
    EMA-update the matched bin (SPEC §9.1). Measured hourly per-plane energy
    comes from the store's hourly-actuals ring (populated by the nightly LTS
    read); when absent the shademap does not train that night (SPEC §12.1
    attempt-not-blocker).

    Measured-side clearness gate (coordinator:1015): the whole day must have
    measured site energy within a band of the modeled forecast, otherwise the
    forecast wrongly called it clear and every hour would write pure weather
    error into the geometric map. A day that fails this gate trains nothing.
    """
    if not coord._learner_config.slow_enabled:
        return
    if coord._drift_state.slow_disabled:
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
    # not darken a geometric bin, SPEC §9.1). Uses the RAW gated modeled total
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
    """Measured-side clearness gate for shademap training (SPEC §9.1).

    The candidate day's measured site energy must be at least
    SHADEMAP_MEASURED_CLEAR_MIN_FRAC of the modeled RAW forecast; otherwise
    the forecast over-predicted clearness (overcast reality) and training
    would write weather error into the geometry. The modeled reference is the
    gated RAW hourly total (what the engine issued), sliced to the day and
    RESTRICTED to the metered planes (:func:`metered_modeled_hourly`) — an
    unmetered plane would otherwise read as a permanent under-production and
    reject every clear day. Returns False when the restriction is impossible
    (no metered plane / legacy snapshot without the per-plane breakdown):
    an unverifiable day trains nothing.
    """
    modeled_hourly = _filter_hourly_to_local_day(
        snap.raw_hourly_wh or snap.corrected_hourly_wh, iso
    )
    metered_hourly = metered_modeled_hourly(
        coord, snap, modeled_hourly, layer="raw"
    )
    if metered_hourly is None:
        return False
    modeled = sum(metered_hourly.values())
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


def train_channel(coord, state: ShademapState, channel: str, modeled: PlaneHourlyModeled,
                  measured_by_hour: dict[str, float]) -> tuple[ShademapState, bool]:
    """Production and replay share the identical issued beam-reference trainer."""
    return learning_inputs.train_channel(coord._site, state, channel, modeled, measured_by_hour)


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
    (SPEC §9.8). The modeled total is sliced to the training LOCAL day so an
    old 4-day snapshot cannot inflate the threshold (FIX-2). Absent either
    side, not a collapse (can't tell).
    """
    if not issued or not actuals:
        return False
    snap = IssuedSnapshot.from_dict(issued)
    raw_hourly = _filter_hourly_to_local_day(
        snap.raw_hourly_wh or snap.corrected_hourly_wh, iso
    )
    metered_hourly = metered_modeled_hourly(
        coord, snap, raw_hourly, layer="raw"
    )
    if metered_hourly is None:
        return False
    forecast_wh = sum(metered_hourly.values())
    measured_wh = sum(
        float(v) for v in actuals.values() if isinstance(v, (int, float))
    )
    return production_collapsed(forecast_wh, measured_wh)


def update_drift(
    coord, iso: str, issued: dict | None, actuals: dict | None
) -> None:
    """Rolling daylight-MAE drift monitor with per-layer auto-disable (SPEC §9.8).

    Decomposes the persisted stack as ``corrected = slow ∘ day-ahead`` and attributes a
    "losing" day to the GUILTY layer only, so an innocent layer is never
    auto-disabled and rolled back alongside a drifting sibling (audit #13b). A
    layer is "losing" when its challenger daylight-hour MAE beats its reference by
    more than DRIFT_LOSS_MARGIN (relative) AND by more than DRIFT_LOSS_MIN_ABS_WH
    (absolute) — the absolute floor keeps a rounding-scale delta on a
    well-trained/clear day from counting as a loss:
      * SLOW (shademap): slow-only MAE vs raw physics MAE — the shademap made
        pure physics worse;
      * DAY-AHEAD: corrected MAE vs slow-only MAE — the day-ahead factor
        made the slow-only curve worse.
    The two streaks advance INDEPENDENTLY from their own signal (a non-losing
    leg resets only that layer's streak). DRIFT_LOSS_STREAK_DAYS consecutive
    losing days auto-disables that layer, raises a repair issue and rolls it
    back; the flag stays until the user re-enables in the options flow. The
    window is trimmed to DRIFT_WINDOW_DAYS.

    LEGACY fallback: without a slow-only curve the monitor can still judge the
    day-ahead leg against RAW, but does not invent a slow-layer verdict.

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
    raw_hourly = metered_modeled_hourly(
        coord, snap, raw_hourly, layer="raw"
    )
    corrected_hourly = metered_modeled_hourly(
        coord, snap, corrected_hourly, layer="corrected"
    )
    if raw_hourly is None or corrected_hourly is None:
        return
    raw_total = sum(raw_hourly.values())
    corrected_total = sum(corrected_hourly.values())
    if raw_total <= 0.0 and corrected_total <= 0.0 and measured_wh <= 0.0:
        return
    # Slow-only curve (shademap ∘ physics, no day-ahead) sliced the SAME way.
    # Empty on a legacy snapshot / slow-inactive day / failed compute: slow-only
    # cannot be judged independently; only day-ahead-vs-raw remains attributable.
    slow_site_hourly = _filter_hourly_to_local_day(
        snap.slow_only_hourly_wh, iso
    )
    slow_hourly = (
        metered_modeled_hourly(
            coord, snap, slow_site_hourly, layer="slow"
        )
        if slow_site_hourly
        else None
    )
    has_slow = bool(slow_hourly)
    slow_total = sum(slow_hourly.values()) if has_slow else raw_total

    hourly_actuals = coord._store_hourly_actuals(iso)
    measured_hourly = coord._site_measured_hourly(iso, hourly_actuals)
    if hourly_actuals and not measured_hourly:
        # Hourly data exists but has no complete cross-channel hour. This is
        # not a legacy daily-only store and must not silently downgrade to a
        # coarser metric that can hide the recorder gap.
        return

    def _mae(curve: dict[str, float]) -> float | None:
        if not measured_hourly:
            return None
        keys = [
            hkey
            for hkey in measured_hourly
            if hkey in curve and (
                curve.get(hkey, 0.0) > 0.0
                or raw_hourly.get(hkey, 0.0) > 0.0
                or measured_hourly.get(hkey, 0.0) > 0.0
            )
        ]
        if not keys:
            return None
        return sum(
            abs(float(curve[h]) - float(measured_hourly[h])) for h in keys
        ) / len(keys)

    # Current stores always carry hourly actuals. A legacy store without them
    # degrades to the old daily absolute-energy error rather than fabricating an
    # hourly shape; diagnostics expose that fallback via ``hourly_basis``.
    raw_hourly_mae = _mae(raw_hourly)
    corrected_hourly_mae = _mae(corrected_hourly)
    slow_hourly_mae = _mae(slow_hourly) if has_slow else raw_hourly_mae
    hourly_basis = raw_hourly_mae is not None and corrected_hourly_mae is not None
    raw_mae = (
        raw_hourly_mae if hourly_basis else abs(raw_total - measured_wh)
    )
    corrected_mae = (
        corrected_hourly_mae
        if hourly_basis
        else abs(corrected_total - measured_wh)
    )
    slow_mae = (
        slow_hourly_mae
        if hourly_basis and slow_hourly_mae is not None
        else abs(slow_total - measured_wh)
    )
    baseline_mae = raw_mae  # pure physics is the baseline comparison here

    entry = {
        "raw": round(raw_mae, 2),
        "corrected": round(corrected_mae, 2),
        "baseline": round(baseline_mae, 2),
        "hourly_basis": 1.0 if hourly_basis else 0.0,
        "raw_daily_abs": round(abs(raw_total - measured_wh), 2),
        "corrected_daily_abs": round(
            abs(corrected_total - measured_wh), 2
        ),
        "corrected_daily_bias": round(corrected_total - measured_wh, 2),
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
        relative margin AND the absolute Wh floor (SPEC §9.8)."""
        return (
            challenger_mae > reference_mae * (1.0 + DRIFT_LOSS_MARGIN)
            and (challenger_mae - reference_mae) > DRIFT_LOSS_MIN_ABS_WH
        )

    day_ahead_streak = (
        coord._drift_state.day_ahead_loss_streak
        or coord._drift_state.fast_loss_streak
    )
    slow_streak = coord._drift_state.slow_loss_streak
    day_ahead_on = (
        coord._learner_config.day_ahead_enabled
        and not coord._drift_state.day_ahead_disabled
        and not coord._drift_state.fast_disabled
    )
    slow_on = coord._learner_config.slow_enabled and not coord._drift_state.slow_disabled
    if has_slow:
        # Per-layer decomposition (corrected = slow ∘ day-ahead): each active layer's
        # streak advances or resets from ITS OWN leg — the slow layer on
        # slow-only-vs-physics, Day-ahead on corrected-vs-slow-only.
        slow_losing = _losing(slow_mae, raw_mae)
        day_ahead_losing = _losing(corrected_mae, slow_mae)
        if day_ahead_on:
            day_ahead_streak = (
                day_ahead_streak + 1 if day_ahead_losing else 0
            )
        if slow_on:
            slow_streak = slow_streak + 1 if slow_losing else 0
    else:
        # Without slow-only attribution, only day-ahead can be judged safely.
        losing = _losing(corrected_mae, raw_mae)
        if day_ahead_on:
            day_ahead_streak = day_ahead_streak + 1 if losing else 0

    day_ahead_disabled = (
        coord._drift_state.day_ahead_disabled
        or coord._drift_state.fast_disabled
    )
    slow_disabled = coord._drift_state.slow_disabled
    day_ahead_just_disabled = False
    slow_just_disabled = False
    if day_ahead_on and day_ahead_streak >= DRIFT_LOSS_STREAK_DAYS:
        day_ahead_disabled = True
        day_ahead_just_disabled = True
        day_ahead_streak = 0
        coord._restore_layer_snapshot(LEARNER_LAYER_DAY_AHEAD)
        coord._raise_repair_issue(ISSUE_FAST_LEARNER_DISABLED)
        _LOGGER.warning(
            "Day-ahead learner auto-disabled after %d losing days",
            DRIFT_LOSS_STREAK_DAYS,
        )
    if slow_on and slow_streak >= DRIFT_LOSS_STREAK_DAYS:
        slow_disabled = True
        slow_just_disabled = True
        slow_streak = 0
        coord._restore_layer_snapshot(LEARNER_LAYER_SLOW)
        coord._raise_repair_issue(ISSUE_SLOW_LEARNER_DISABLED)
        _LOGGER.warning("Slow learner auto-disabled after %d losing days", DRIFT_LOSS_STREAK_DAYS)

    if slow_just_disabled:
        # Day-ahead theta is fitted on Slow-only. Removing Slow changes that
        # feature basis immediately, so reopen RLS adaptation while retaining
        # theta as a bounded starting estimate.
        coord._bias_state = bias_mod.reseed_day_ahead_bias(
            coord._bias_state, n_cap=DAY_AHEAD_BIAS_RESEED_N
        )
        coord._persist_bias_state()
    if day_ahead_just_disabled or slow_just_disabled:
        # Quantile residuals are relative to the issued corrected stack. Once a
        # layer is removed, the old population no longer describes served P50.
        coord._quantile_state = QuantileState()
        coord._persist_quantile_state()

    # Preserve the option-seen + collapse-freeze fields (replace, not
    # reconstruct, so the FIX-5 transition memory + FIX-7 freeze survive).
    coord._drift_state = _replace_drift(
        coord._drift_state,
        daily_mae=daily,
        day_ahead_loss_streak=day_ahead_streak,
        # Keep v1 aliases synchronized for old dashboards/store readers.
        fast_loss_streak=day_ahead_streak,
        slow_loss_streak=slow_streak,
        day_ahead_disabled=day_ahead_disabled,
        fast_disabled=day_ahead_disabled,
        slow_disabled=slow_disabled,
        version=2,
    )
    coord._persist_drift_state()


def restore_layer_snapshot(coord, layer: str) -> str | None:
    """Roll the auto-disabled layer back to its pre-streak state (SPEC §9.8).

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
    if layer in (LEARNER_LAYER_FAST, LEARNER_LAYER_DAY_AHEAD):
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
    auto-disable, SPEC §9.8) via the store's ``push_snapshot`` /
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
        quantile=coord._quantile_state,
    )
    try:
        coord._store.push_snapshot(snapshot)
    except Exception:  # pragma: no cover - defensive
        _LOGGER.debug("Could not push rollback snapshot", exc_info=True)
