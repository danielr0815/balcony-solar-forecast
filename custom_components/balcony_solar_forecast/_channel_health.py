"""Making "this install is not learning" VISIBLE — SPEC §5.1 (gates: SPEC §5).

Owner: glue (learning visibility). Two independent detectors, both surfacing
through HA repair issues and the diagnostics dump instead of a log line nobody
reads:

  (1) **Channel presence** (:func:`check_actual_channels`). At setup and after
      every config change, does every configured plane ``actual_entity``
      actually EXIST in this Home Assistant? The shipped ``const.DEFAULT_SITE``
      is a reference example that carries EIGHT of the reference plant's
      Hoymiles entity ids; an install that adopts it (wholly or partly) has
      measurement channels that resolve to nothing. This catches that in
      seconds, at setup, before a single night is wasted.

  (2) **Discard streak** (:func:`record_actuals_outcome`). ``_actuals`` applies
      the SPEC §5 label gates and discards the WHOLE day for BOTH learners as
      soon as ONE configured channel is unusable. Repeated night after night
      that means the system NEVER learns — while the status entities keep
      reporting ``cold_start`` and the learners keep reporting "active". This
      counts consecutive structurally-discarded days, names the gate that
      fired, and raises a repair issue at ``LEARNING_STALLED_STREAK_DAYS``.

**No false alarm on a fresh install** (the reason (2) is not simply "N empty
days"): a new, correctly configured plant legitimately has no complete LTS days
yet, and the nightly catch-up sweep reaches back up to ``NIGHTLY_CATCHUP_MAX_DAYS``
into history that predates the installation entirely. A discarded day therefore
only counts when the integration ISSUED a forecast for that day (an entry in the
issued ring proves we were running and the channels should have logged). Days
from before our time never count, so the anlauf phase is silent while a
permanently dead channel still trips the streak within a working week.

The AC meter (``ac_actual_entity``) is deliberately NOT a repair issue: it is
optional, self-gating (a missing/implausible meter falls back to the configured
η and the DC learning is untouched, SPEC §5) and never blocks learning. A second
repair card next to the blocking one would dilute exactly the signal that needs
action, so a missing AC meter is reported in the diagnostics dump and logged
once — visible to whoever is debugging, silent to whoever is not.
"""

from __future__ import annotations

import logging
from datetime import date

from .const import (
    ISSUE_ACTUAL_ENTITY_MISSING,
    ISSUE_LEARNING_STALLED_BY_REASON,
    LEARNING_STALLED_STREAK_DAYS,
)

_LOGGER = logging.getLogger(__name__)


def _entity_known(hass, entity_id: str) -> bool:
    """True when ``entity_id`` exists in this HA (state OR registry entry).

    A state is the cheap, always-available signal; the entity registry catches
    the entity that exists but is currently restoring / disabled / unavailable
    without a state object. An entity that is merely ``unavailable`` counts as
    KNOWN — that is a device problem, not a configuration problem, and it is the
    discard-streak detector's job, not this one's.
    """
    try:
        if hass.states.get(entity_id) is not None:
            return True
    except Exception:  # pragma: no cover - defensive, never crash setup
        return False
    try:
        from homeassistant.helpers import entity_registry as er

        return er.async_get(hass).async_get(entity_id) is not None
    except Exception:  # pragma: no cover - registry unavailable in bare tests
        return False


def missing_actual_entities(hass, site) -> list[tuple[str, str]]:
    """``(plane name, entity id)`` for every configured channel that does NOT
    exist in this HA instance. Empty list == every measurement channel resolves.
    """
    missing: list[tuple[str, str]] = []
    for plane in getattr(site, "planes", ()) or ():
        entity_id = getattr(plane, "actual_entity", None)
        if not entity_id:
            continue
        if not _entity_known(hass, entity_id):
            missing.append((plane.name, entity_id))
    return missing


def check_actual_channels(coord) -> dict[str, object]:
    """Verify every configured measurement channel exists; raise/clear the issue.

    Returns the summary the diagnostics dump publishes. Never raises: a broken
    check must not take down setup (SPEC §5).
    """
    site = coord._site
    try:
        missing = missing_actual_entities(coord.hass, site)
    except Exception:  # pragma: no cover - defensive
        _LOGGER.debug("Measurement-channel check failed", exc_info=True)
        return {"available": False}

    configured = sum(
        1 for p in getattr(site, "planes", ()) or () if getattr(p, "actual_entity", None)
    )
    if missing:
        listed = ", ".join(f"{name} ({eid})" for name, eid in missing)
        _LOGGER.warning(
            "%d of %d configured measurement channel(s) do not exist in this "
            "Home Assistant: %s. Nightly training discards the WHOLE day as soon "
            "as one channel is unusable, so NOTHING will ever be learned until "
            "these are fixed. Enter your own inverter sensors via Reconfigure — "
            "the shipped default site is a reference example and carries a "
            "stranger's sensor ids (SPEC §5)",
            len(missing), configured, listed,
        )
        coord._raise_repair_issue(
            ISSUE_ACTUAL_ENTITY_MISSING,
            {
                "count": str(len(missing)),
                "configured": str(configured),
                "channels": listed,
            },
        )
    else:
        coord._delete_repair_issue(ISSUE_ACTUAL_ENTITY_MISSING)

    # The optional whole-site AC meter: diagnostics + one log line, no repair
    # issue (see the module docstring for why).
    ac_entity = getattr(site, "ac_actual_entity", None)
    ac_missing = bool(ac_entity) and not _entity_known(coord.hass, ac_entity)
    if ac_missing:
        _LOGGER.warning(
            "Configured AC meter %s does not exist in this Home Assistant; the "
            "inverter-efficiency calibration stays on the configured eta and "
            "measured_ac_power will not update. DC learning is unaffected",
            ac_entity,
        )

    summary: dict[str, object] = {
        "available": True,
        "configured": configured,
        "missing": [f"{name} ({eid})" for name, eid in missing],
        "ac_configured": bool(ac_entity),
        "ac_missing": ac_missing,
    }
    coord._channel_health = summary
    return summary


def record_actuals_outcome(coord, day: date, *, accepted: bool) -> None:
    """Fold one nightly actuals read into the persisted discard streak.

    ``accepted`` is True when the day cleared every label gate (usable per-module
    energy came back). Idempotent per day: the catch-up sweep re-processes a
    discarded day every night until it is recorded, so only a day NEWER than the
    last counted one advances the streak. Never raises (SPEC §5).
    """
    try:
        _record_actuals_outcome(coord, day, accepted=accepted)
    except Exception:  # pragma: no cover - bookkeeping is never fatal
        _LOGGER.debug("Learning-health bookkeeping failed for %s", day, exc_info=True)


def _record_actuals_outcome(coord, day: date, *, accepted: bool) -> None:
    store = coord._store
    getter = getattr(store, "get_learning_health", None)
    setter = getattr(store, "set_learning_health", None)
    if getter is None or setter is None:
        return  # older store schema in flight: skip, never crash
    health = dict(getter())
    iso = day.isoformat()

    if accepted:
        if health.get("discard_streak") or health.get("last_discard_reason"):
            _LOGGER.info(
                "Nightly actuals for %s accepted again after %s discarded day(s); "
                "learning resumes",
                iso, health.get("discard_streak"),
            )
        health.update(
            discard_streak=0,
            last_discard_reason=None,
            last_discard_modules=[],
            last_discard_day=None,
            last_accepted_day=iso,
        )
        setter(health)
        _clear_stalled_issues(coord)
        return

    dropout = getattr(coord, "_last_actuals_dropout", None)
    if not isinstance(dropout, dict) or not dropout.get("reason"):
        # No gate fired (e.g. no channels configured at all, or the read never
        # reached the gates): nothing to explain, so nothing to count.
        return
    if not _day_was_ours(coord, iso):
        # Fresh-install guard: we never issued a forecast for this day, so it
        # predates the installation (the catch-up sweep reaches back up to
        # NIGHTLY_CATCHUP_MAX_DAYS). Absent history is not a defect.
        _LOGGER.debug(
            "Discarded %s (%s) predates our first issued forecast; not counted "
            "toward the learning-stalled streak", iso, dropout.get("reason"),
        )
        return

    last = health.get("last_discard_day")
    if isinstance(last, str) and iso <= last:
        return  # already counted (idempotent re-sweep of an unrecorded day)

    streak = int(health.get("discard_streak") or 0) + 1
    modules = [m for m in (dropout.get("modules") or []) if isinstance(m, str)]
    health.update(
        discard_streak=streak,
        last_discard_reason=dropout.get("reason"),
        last_discard_modules=modules,
        last_discard_day=iso,
    )
    setter(health)

    if streak < LEARNING_STALLED_STREAK_DAYS:
        return
    reason = str(dropout.get("reason"))
    issue_id = ISSUE_LEARNING_STALLED_BY_REASON.get(reason)
    if issue_id is None:  # pragma: no cover - reasons are const-enumerated
        return
    entities = [e for e in (dropout.get("entities") or []) if isinstance(e, str)]
    channels = ", ".join(
        f"{m} ({e})" for m, e in zip(modules, entities, strict=False)
    ) or ", ".join(modules)
    _LOGGER.warning(
        "Nightly training has discarded the whole day %d times in a row (last: "
        "%s, reason %s, channel(s) %s). NOTHING is being learned (SPEC §5)",
        streak, iso, reason, channels,
    )
    # Exactly one stalled-issue at a time: if the dominant cause changed
    # mid-streak, the stale card must go or the operator chases the wrong fix.
    _clear_stalled_issues(coord, keep=issue_id)
    coord._raise_repair_issue(
        issue_id,
        {"days": str(streak), "channels": channels, "last_day": iso},
    )


def _clear_stalled_issues(coord, *, keep: str | None = None) -> None:
    """Delete every learning-stalled repair issue except ``keep``."""
    for issue_id in ISSUE_LEARNING_STALLED_BY_REASON.values():
        if issue_id != keep:
            coord._delete_repair_issue(issue_id)


def _day_was_ours(coord, iso: str) -> bool:
    """True when we issued a forecast for ``iso`` (so we were running that day).

    This is the fresh-install guard: an install whose first nightly sweep walks
    back over pre-install history finds no issued snapshot for those days and
    stays silent, while a running install that discards day after day does not.
    """
    getter = getattr(coord._store, "get_issued", None)
    if not callable(getter):
        return False
    try:
        return getter(iso) is not None
    except Exception:  # pragma: no cover - defensive
        return False


def learning_health_summary(coord) -> dict[str, object]:
    """Persisted discard-streak state for the diagnostics dump (SPEC §8).

    Remote diagnosis without log access: WHY the last day was thrown away, which
    channels caused it, how many days in a row, and when a day was last accepted.
    """
    getter = getattr(coord._store, "get_learning_health", None)
    if not callable(getter):
        return {"available": False}
    try:
        health = dict(getter())
    except Exception as err:  # noqa: BLE001 -- diagnostics must not raise
        return {"error": repr(err)}
    health["available"] = True
    health["streak_threshold"] = LEARNING_STALLED_STREAK_DAYS
    return health
