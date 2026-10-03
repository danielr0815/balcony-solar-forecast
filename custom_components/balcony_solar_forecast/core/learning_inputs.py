"""Shared issued-reference reduction and geometric training, without HA imports.

These functions are used by nightly production training and physical offline
replays. They consume frozen curves, never reconstruct a reference from a newer
learner state. Identity remains the plane's stable name (SPEC §9).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo

from ..const import CLOUD_CLASS_CLEAR, SLOT_HOURS, SLOT_MINUTES
from . import clearsky, solpos
from . import shademap as shademap_mod
from .bias import DayAheadSample as DayAheadSample
from .types import (
    ForecastResult,
    IssuedSnapshot,
    PlaneHourlyModeled,
    ShademapState,
    SiteConfig,
)

_LOGGER = logging.getLogger(__name__)


def _hour_key(start: datetime) -> str:
    return start.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()


def _parse_hour(key: str) -> datetime | None:
    try:
        return datetime.fromisoformat(key)
    except (TypeError, ValueError):
        return None




def per_plane_modeled(site: SiteConfig, result: ForecastResult | None, iso: str, tz: tzinfo) -> dict[str, PlaneHourlyModeled]:
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
    ``_last_result`` is absent (older cached build), returns an empty mapping
    (SPEC §12.1: attempt, not a blocker).
    """
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
        start_utc = start.astimezone(UTC)
        if start_utc.astimezone(tz).date().isoformat() != iso:
            continue
        mid = start_utc + timedelta(minutes=SLOT_MINUTES / 2)
        _az, el = solpos.sun_position(
            mid, site.latitude, site.longitude
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
        raw_wh: dict[str, float] = {}
        slow_wh: dict[str, float] = {}
        corrected_wh: dict[str, float] = {}
        raw_series = getattr(pr, "raw_watts", ())
        slow_series = getattr(pr, "slow_watts", ()) or raw_series
        corrected_series = getattr(pr, "watts", ())
        for i, start in enumerate(result.slot_starts):
            if start.astimezone(tz).date().isoformat() != iso:
                continue
            hkey = _hour_key(start)
            if i < len(pr.beam_ref_watts):
                beam_wh[hkey] = beam_wh.get(hkey, 0.0) + pr.beam_ref_watts[i] * SLOT_HOURS
            if i < len(pr.diffuse_ref_watts):
                diffuse_wh[hkey] = diffuse_wh.get(hkey, 0.0) + pr.diffuse_ref_watts[i] * SLOT_HOURS
            if i < len(raw_series):
                raw_wh[hkey] = raw_wh.get(hkey, 0.0) + raw_series[i] * SLOT_HOURS
            if i < len(slow_series):
                slow_wh[hkey] = slow_wh.get(hkey, 0.0) + slow_series[i] * SLOT_HOURS
            if i < len(corrected_series):
                corrected_wh[hkey] = (
                    corrected_wh.get(hkey, 0.0) + corrected_series[i] * SLOT_HOURS
                )
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
            raw_wh={h: round(v, 2) for h, v in raw_wh.items() if h in keep},
            slow_wh={h: round(v, 2) for h, v in slow_wh.items() if h in keep},
            corrected_wh={
                h: round(v, 2) for h, v in corrected_wh.items() if h in keep
            },
        )
    return out


def metered_modeled_hourly(
    site: SiteConfig,
    snap: IssuedSnapshot,
    modeled_hourly: dict[str, float],
    *,
    layer: str = "raw",
) -> dict[str, float] | None:
    """Restrict a SITE-total modeled hourly curve to the METERED planes.

    Teilmengen-Regel (SPEC §9.5/§9.1, mirrors the live
    ``coordinator._rearm_samples_from_rows`` subset rule): learners compare
    against the measured side, which only ever sums planes with an
    ``actual_entity``. An unmetered plane inside the modeled total reads as a
    permanent production deficit — the RLS theta would learn the METERING
    SHARE instead of the forecast error. Snapshot v2 stores the exact RAW,
    SLOW-only and CORRECTED curve for each plane; ``layer`` selects the one
    matching the site curve. No beam-share approximation is allowed because
    group clipping and learned factors make that share layer-dependent.

    Returns None when the comparison is impossible or meaningless: no metered
    plane at all (no measured side exists), or unmetered planes present but
    no per-plane breakdown to scale them out (legacy v0.1 snapshot) — the
    caller then SKIPS the day rather than training the metering gap.
    """
    planes = site.planes
    metered = {p.name for p in planes if p.actual_entity}
    if not metered:
        return None
    if len(metered) == len(planes):
        return dict(modeled_hourly)
    if not snap.per_plane or layer not in {"raw", "slow", "corrected"}:
        return None
    attr = f"{layer}_wh"
    exact: dict[str, dict[str, float]] = {}
    for name in metered:
        pm = snap.per_plane.get(name)
        curve = getattr(pm, attr, None) if pm is not None else None
        if not curve:
            # Legacy snapshot: exact subset attribution is impossible. Skip the
            # day instead of manufacturing a metering-share correction.
            return None
        exact[name] = curve
    out: dict[str, float] = {}
    for hkey in modeled_hourly:
        out[hkey] = sum(curve.get(hkey, 0.0) for curve in exact.values())
    return out


def day_ahead_samples(
    site: SiteConfig,
    raw_hourly: dict[str, float],
    actuals: dict,
    snap: IssuedSnapshot,
    site_measured_hourly: dict[str, float] | None,
    *, day_part: Callable[[str], str | None],
) -> list[DayAheadSample]:
    """Build (cloud class x day part) RLS training samples for one day.

    Modeled Wh per part comes from the issued SLOW-ONLY hourly curve (shademap ∘
    physics; ``raw_hourly`` here is that curve, raw only as a legacy fallback —
    see :func:`train_day_ahead`), RESTRICTED to the metered planes
    (:func:`metered_modeled_hourly`); the cloud class is the forecast cloud
    class of each hour (snap.cloud_class_by_hour,
    SPEC §8) so a fog/overcast day trains its own cell, not a fixed "clear"
    one. When TRUE per-hour measured site energy is available
    (``site_measured_hourly``) each (class, part) cell carries its OWN
    measured/modeled pair — a real independent per-part signal. Otherwise the
    day's measured total is apportioned by the modeled shape (coarse
    fallback, daily ring only).
    """
    # Teilmengen-Regel: modeled side restricted to the metered planes; None
    # means the comparison is impossible (no metered plane / legacy snapshot
    # on a partially metered site) -> the day is skipped, never poisoned.
    metered_hourly = metered_modeled_hourly(
        site, snap, raw_hourly, layer="slow"
    )
    if metered_hourly is None:
        return []
    raw_hourly = metered_hourly
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
        if (
            site_measured_hourly is not None
            and hkey not in site_measured_hourly
        ):
            # A recorder gap is absence of evidence, never a zero-production
            # label. Keep modeled and measured sides on the same hour set.
            continue
        part = day_part(hkey)
        if part is None:
            continue
        cc = snap.cloud_class_by_hour.get(hkey, CLOUD_CLASS_CLEAR)
        key = (cc, part)
        cell_modeled[key] = cell_modeled.get(key, 0.0) + float(wh)
        if site_measured_hourly is not None:
            cell_measured[key] = cell_measured.get(key, 0.0) + float(
                site_measured_hourly[hkey]
            )

    samples: list[DayAheadSample] = []
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
            DayAheadSample(
                cloud_class=cc,
                day_part=part,
                measured_wh=measured_wh,
                modeled_wh=modeled_wh,
            )
        )
    return samples


def train_channel(
    site: SiteConfig,
    state: ShademapState,
    channel: str,
    modeled: PlaneHourlyModeled,
    measured_by_hour: dict[str, float],
) -> tuple[ShademapState, bool]:
    """EMA-update one channel's bins from its quasi-clear hourly samples.

    The neighbour-stability leg of the gate is applied to the MEASURED/
    modeled ratio sequence (not the smooth forecast kc): a
    lone bright measured hour between shaded ones is a fluctuation and is
    rejected.
    """
    plane = site.plane_by_name(channel)
    if plane is None:
        return state, False
    # Storage is ALWAYS per plane (SPEC §9.2): each plane's learning is stored under
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
        dt = _parse_hour(hkey)
        if dt is None:
            continue
        mid = dt + timedelta(minutes=30)
        sun_az, sun_el = solpos.sun_position(
            mid, site.latitude, site.longitude
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
