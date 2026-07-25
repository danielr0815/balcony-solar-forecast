"""Learning-visibility gates (0.23.1) — SPEC §5/§7/§8.

Owner: glue (``_channel_health``). The failure these guard is a status LIE, not
a crash: ``const.DEFAULT_SITE`` ships eight of the reference plant's Hoymiles
entity ids, and ``_actuals`` discards the WHOLE day for BOTH learners as soon as
one configured channel is unusable. An install that adopts the shipped default
therefore throws away every single night forever while the entities keep
reporting ``cold_start`` and the learners keep reporting "active".

Two detectors, tested from both sides:

  * the setup-time channel-presence check — a missing ``actual_entity`` must
    raise a repair issue that NAMES the plane and the entity id, and a complete
    configuration must clear it;
  * the nightly discard streak — reaching ``LEARNING_STALLED_STREAK_DAYS``
    structurally discarded days must raise the issue matching the gate that
    fired (dead / frozen / low-coverage, each with its own remedy), one accepted
    day must reset it, and a FRESH INSTALL with no history must stay silent.

That last one is the whole design constraint: a new, correctly configured plant
legitimately has no complete long-term-statistics days yet, and the nightly
catch-up sweep reaches back into history that predates the installation. Both
sides are asserted here — the fresh install is silent, the permanently dead
channel is not.

The coordinator is built via ``__new__`` with only the attributes these paths
touch (the same pattern as ``test_coordinator_learning``); the store round-trip
goes through the REAL ``ForecastStore`` validators so the persistence claim is
tested against the shipped schema, not against a fake.
"""

from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast import (  # noqa: E402
    _channel_health,
    _nightly,
)
from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    DROPOUT_REASON_DEAD_CHANNEL,
    DROPOUT_REASON_FROZEN_CHANNEL,
    DROPOUT_REASON_LOW_COVERAGE,
    DROPOUT_REASONS,
    ISSUE_ACTUAL_ENTITY_MISSING,
    ISSUE_LEARNING_STALLED_BY_REASON,
    ISSUE_LEARNING_STALLED_DEAD_CHANNEL,
    ISSUE_LEARNING_STALLED_FROZEN_CHANNEL,
    ISSUE_LEARNING_STALLED_LOW_COVERAGE,
    LEARNING_STALLED_STREAK_DAYS,
    STORE_KEY_LEARNING_HEALTH,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    PlaneConfig,
    SiteConfig,
)

_DAY = date(2026, 7, 20)


# ---------------------------------------------------------------------------
# Fakes: only what the two detectors touch.
# ---------------------------------------------------------------------------


class _FakeStates:
    def __init__(self, known: set[str] | None = None) -> None:
        self._known = set(known or ())

    def get(self, entity_id: str):
        return object() if entity_id in self._known else None


class _FakeHass:
    def __init__(self, known: set[str] | None = None) -> None:
        self.states = _FakeStates(known)


class _HealthStore:
    """Store stand-in for the learning-health section + the issued ring."""

    def __init__(self) -> None:
        self.health = _channel_health_neutral()
        self.issued: dict[str, dict] = {}

    def get_learning_health(self) -> dict:
        return dict(self.health)

    def set_learning_health(self, health: dict) -> None:
        self.health = dict(health)

    def get_issued(self, iso: str):
        return self.issued.get(iso)


def _channel_health_neutral() -> dict:
    from custom_components.balcony_solar_forecast.store import (
        _empty_learning_health,
    )

    return _empty_learning_health()


class _Coord:
    """Minimal coordinator surface: site, store, hass, repair-issue helpers."""

    def __init__(self, *, site=None, store=None, hass=None) -> None:
        self._site = site if site is not None else _site()
        self._store = store if store is not None else _HealthStore()
        self.hass = hass if hass is not None else _FakeHass()
        self._last_actuals_dropout: dict | None = None
        self._channel_health: dict = {"available": False}
        self.raised: list[tuple[str, dict | None]] = []
        self.deleted: list[str] = []

    def _raise_repair_issue(self, issue_id, placeholders=None) -> None:
        self.raised.append((issue_id, placeholders))

    def _delete_repair_issue(self, issue_id) -> None:
        self.deleted.append(issue_id)

    # Delegates, exactly as the real coordinator wires them.
    def _record_actuals_outcome(self, day, *, accepted) -> None:
        _channel_health.record_actuals_outcome(self, day, accepted=accepted)

    def learning_health_summary(self) -> dict:
        return _channel_health.learning_health_summary(self)


def _site(*, entities=("sensor.m1", "sensor.m2"), ac_entity=None) -> SiteConfig:
    planes = tuple(
        PlaneConfig(
            name=f"M{i + 1}",
            azimuth_deg=115.0,
            tilt_deg=70.0,
            wp=370.0,
            actual_entity=eid,
        )
        for i, eid in enumerate(entities)
    )
    return SiteConfig(
        latitude=48.5,
        longitude=12.2,
        planes=planes,
        groups=(),
        ac_actual_entity=ac_entity,
    )


def _issue_ids(coord) -> list[str]:
    return [i for i, _p in coord.raised]


def _run_streak(coord, reason: str, days: int, *, start_day: date = _DAY) -> None:
    """Discard ``days`` consecutive days for ``reason``, all of them days we ran."""
    from datetime import timedelta

    for offset in range(days):
        day = start_day + timedelta(days=offset)
        coord._store.issued[day.isoformat()] = {"issued_at": "x"}
        coord._last_actuals_dropout = {
            "reason": reason,
            "modules": ["M2"],
            "entities": ["sensor.m2"],
        }
        coord._record_actuals_outcome(day, accepted=False)


# ---------------------------------------------------------------------------
# (1) Setup-time measurement-channel presence check.
# ---------------------------------------------------------------------------


def test_missing_actual_entity_raises_issue_naming_plane_and_entity():
    """The copied reference site: the issue must name WHICH plane and WHICH id.

    "A channel is missing" is not actionable; "M2 (sensor.m2)" is, because it
    tells the operator exactly which row of the object editor to fix.
    """
    coord = _Coord(hass=_FakeHass({"sensor.m1"}))  # sensor.m2 does not exist

    summary = coord_check(coord)

    assert _issue_ids(coord) == [ISSUE_ACTUAL_ENTITY_MISSING]
    placeholders = coord.raised[0][1]
    assert placeholders["count"] == "1"
    assert placeholders["configured"] == "2"
    assert placeholders["channels"] == "M2 (sensor.m2)"
    assert summary["missing"] == ["M2 (sensor.m2)"]
    assert summary["configured"] == 2


def test_every_missing_channel_is_listed_not_just_the_first():
    """A wholesale copy of the reference site misses ALL of them at once."""
    coord = _Coord(hass=_FakeHass(set()))

    coord_check(coord)

    channels = coord.raised[0][1]["channels"]
    assert "M1 (sensor.m1)" in channels
    assert "M2 (sensor.m2)" in channels
    assert coord.raised[0][1]["count"] == "2"


def test_all_channels_present_clears_the_issue():
    """A complete configuration raises nothing and DELETES a stale issue."""
    coord = _Coord(hass=_FakeHass({"sensor.m1", "sensor.m2"}))

    summary = coord_check(coord)

    assert coord.raised == []
    assert ISSUE_ACTUAL_ENTITY_MISSING in coord.deleted
    assert summary["missing"] == []


def test_unavailable_entity_counts_as_present():
    """An entity that EXISTS but currently has no value is a device problem, not
    a configuration problem — the streak detector's job, not this check's."""
    coord = _Coord(hass=_FakeHass({"sensor.m1", "sensor.m2"}))

    coord_check(coord)

    assert coord.raised == []


def test_plane_without_actual_entity_is_not_counted():
    """A plane with no channel configured is a deliberate choice, not a fault."""
    coord = _Coord(
        site=_site(entities=("sensor.m1", None)),
        hass=_FakeHass({"sensor.m1"}),
    )

    summary = coord_check(coord)

    assert coord.raised == []
    assert summary["configured"] == 1


def test_missing_ac_meter_is_diagnostics_only_no_repair_issue():
    """The AC meter is optional and self-gating: it must NOT raise a card that
    competes with the one that actually blocks learning — but it MUST be visible
    in the diagnostics dump."""
    coord = _Coord(
        site=_site(ac_entity="sensor.ac_meter"),
        hass=_FakeHass({"sensor.m1", "sensor.m2"}),
    )

    summary = coord_check(coord)

    assert coord.raised == []
    assert summary["ac_configured"] is True
    assert summary["ac_missing"] is True


def coord_check(coord) -> dict:
    return _channel_health.check_actual_channels(coord)


# ---------------------------------------------------------------------------
# (2) Nightly discard streak.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "issue_id"),
    [
        (DROPOUT_REASON_DEAD_CHANNEL, ISSUE_LEARNING_STALLED_DEAD_CHANNEL),
        (DROPOUT_REASON_FROZEN_CHANNEL, ISSUE_LEARNING_STALLED_FROZEN_CHANNEL),
        (DROPOUT_REASON_LOW_COVERAGE, ISSUE_LEARNING_STALLED_LOW_COVERAGE),
    ],
)
def test_streak_at_threshold_raises_the_issue_for_that_reason(reason, issue_id):
    """Each gate gets its OWN issue because each has a different remedy."""
    coord = _Coord()

    _run_streak(coord, reason, LEARNING_STALLED_STREAK_DAYS)

    assert _issue_ids(coord) == [issue_id]
    placeholders = coord.raised[0][1]
    assert placeholders["days"] == str(LEARNING_STALLED_STREAK_DAYS)
    assert placeholders["channels"] == "M2 (sensor.m2)"
    assert coord._store.health["last_discard_reason"] == reason


def test_streak_below_threshold_stays_silent():
    """One bad night is weather/maintenance, not a broken installation."""
    coord = _Coord()

    _run_streak(coord, DROPOUT_REASON_DEAD_CHANNEL, LEARNING_STALLED_STREAK_DAYS - 1)

    assert coord.raised == []
    assert coord._store.health["discard_streak"] == LEARNING_STALLED_STREAK_DAYS - 1


def test_accepted_day_resets_streak_and_clears_every_stalled_issue():
    """The first day that passes the gates ends the alarm — that is the whole
    contract of a self-clearing issue."""
    coord = _Coord()
    _run_streak(coord, DROPOUT_REASON_DEAD_CHANNEL, LEARNING_STALLED_STREAK_DAYS)
    coord.deleted.clear()

    coord._record_actuals_outcome(date(2026, 8, 1), accepted=True)

    assert coord._store.health["discard_streak"] == 0
    assert coord._store.health["last_discard_reason"] is None
    assert coord._store.health["last_accepted_day"] == "2026-08-01"
    # Every reason-specific card goes, not just the one that happened to be up.
    for issue_id in ISSUE_LEARNING_STALLED_BY_REASON.values():
        assert issue_id in coord.deleted


def test_changed_reason_mid_streak_replaces_the_stale_card():
    """A stale card would send the operator after the wrong fix."""
    coord = _Coord()
    _run_streak(coord, DROPOUT_REASON_DEAD_CHANNEL, LEARNING_STALLED_STREAK_DAYS)
    coord.deleted.clear()
    coord.raised.clear()

    _run_streak(
        coord,
        DROPOUT_REASON_FROZEN_CHANNEL,
        1,
        start_day=date(2026, 8, 1),
    )

    assert _issue_ids(coord) == [ISSUE_LEARNING_STALLED_FROZEN_CHANNEL]
    assert ISSUE_LEARNING_STALLED_DEAD_CHANNEL in coord.deleted
    assert ISSUE_LEARNING_STALLED_FROZEN_CHANNEL not in coord.deleted


def test_fresh_install_without_history_never_raises():
    """THE false-alarm case. A brand-new install's first nightly sweep walks back
    over days that predate the installation; every one of them is empty and every
    one of them is discarded. None of that is a defect, so none of it may count.

    Modelled exactly: the dropouts are real, but the issued ring has no entry for
    those days — we were not running, so we cannot have measured them.
    """
    coord = _Coord()

    for offset in range(LEARNING_STALLED_STREAK_DAYS * 2):
        from datetime import timedelta

        coord._last_actuals_dropout = {
            "reason": DROPOUT_REASON_DEAD_CHANNEL,
            "modules": ["M1"],
            "entities": ["sensor.m1"],
        }
        coord._record_actuals_outcome(_DAY + timedelta(days=offset), accepted=False)

    assert coord.raised == []
    assert coord._store.health["discard_streak"] == 0


def test_dead_channel_on_a_running_install_does_raise():
    """The other side of the same coin: identical dropouts, but on days we DID
    issue a forecast for. Silence here would be the original bug."""
    coord = _Coord()

    _run_streak(coord, DROPOUT_REASON_DEAD_CHANNEL, LEARNING_STALLED_STREAK_DAYS)

    assert _issue_ids(coord) == [ISSUE_LEARNING_STALLED_DEAD_CHANNEL]


def test_resweeping_an_unrecorded_day_does_not_double_count():
    """A discarded day is never recorded, so the catch-up sweep re-reads it every
    night. Counting it again each time would fire the issue after two real days."""
    coord = _Coord()
    coord._store.issued[_DAY.isoformat()] = {"issued_at": "x"}
    dropout = {
        "reason": DROPOUT_REASON_DEAD_CHANNEL,
        "modules": ["M1"],
        "entities": ["sensor.m1"],
    }

    for _ in range(LEARNING_STALLED_STREAK_DAYS * 2):
        coord._last_actuals_dropout = dict(dropout)
        coord._record_actuals_outcome(_DAY, accepted=False)

    assert coord._store.health["discard_streak"] == 1
    assert coord.raised == []


def test_read_failure_without_a_dropout_reason_is_not_counted():
    """A recorder IO failure is not a label-gate verdict about the channels."""
    coord = _Coord()
    coord._store.issued[_DAY.isoformat()] = {"issued_at": "x"}
    coord._last_actuals_dropout = None

    coord._record_actuals_outcome(_DAY, accepted=False)

    assert coord._store.health["discard_streak"] == 0


def test_store_without_the_health_section_is_a_noop():
    """An older store schema in flight must never crash the nightly job."""

    class _Bare:
        pass

    coord = _Coord(store=_Bare())
    coord._last_actuals_dropout = {"reason": DROPOUT_REASON_DEAD_CHANNEL}

    coord._record_actuals_outcome(_DAY, accepted=False)  # must not raise

    assert coord.raised == []


# ---------------------------------------------------------------------------
# (3) Persistence across a restart, through the REAL store validators.
# ---------------------------------------------------------------------------


def test_learning_health_survives_a_restart():
    """The streak is worthless if a nightly HA restart resets it to zero."""
    from custom_components.balcony_solar_forecast.store import validate_state

    coord = _Coord()
    _run_streak(coord, DROPOUT_REASON_LOW_COVERAGE, 3)

    # Simulate the on-disk round-trip through the shipped validator.
    from custom_components.balcony_solar_forecast.store import _empty_state

    on_disk = _empty_state()
    on_disk[STORE_KEY_LEARNING_HEALTH] = coord._store.health
    reloaded = validate_state(on_disk)[STORE_KEY_LEARNING_HEALTH]

    assert reloaded["discard_streak"] == 3
    assert reloaded["last_discard_reason"] == DROPOUT_REASON_LOW_COVERAGE
    assert reloaded["last_discard_modules"] == ["M2"]
    assert reloaded["last_discard_day"] == "2026-07-22"


def test_store_predating_the_feature_reads_back_neutral():
    """ADDITIVE within v3: a store written before 0.23.1 has no such key and must
    load to the neutral section without touching any other section."""
    from custom_components.balcony_solar_forecast.store import (
        _empty_state,
        validate_state,
    )

    on_disk = _empty_state()
    del on_disk[STORE_KEY_LEARNING_HEALTH]

    health = validate_state(on_disk)[STORE_KEY_LEARNING_HEALTH]

    assert health["discard_streak"] == 0
    assert health["last_discard_reason"] is None
    assert health["last_accepted_day"] is None


def test_corrupt_health_section_degrades_to_neutral():
    """validate-and-clamp on load (SPEC §5): garbage never crashes setup."""
    from custom_components.balcony_solar_forecast.store import (
        _empty_state,
        validate_state,
    )

    on_disk = _empty_state()
    on_disk[STORE_KEY_LEARNING_HEALTH] = {
        "discard_streak": -7,
        "last_discard_reason": "not_a_reason",
        "last_discard_modules": "M1",
        "last_discard_day": 42,
    }

    health = validate_state(on_disk)[STORE_KEY_LEARNING_HEALTH]

    assert health["discard_streak"] == 0
    assert health["last_discard_reason"] is None
    assert health["last_discard_modules"] == []
    assert health["last_discard_day"] is None


# ---------------------------------------------------------------------------
# (4) Diagnostics surface.
# ---------------------------------------------------------------------------


def test_learning_health_summary_carries_cause_modules_streak_and_threshold():
    """Remote diagnosis without log access is the whole point (SPEC §8)."""
    coord = _Coord()
    _run_streak(coord, DROPOUT_REASON_FROZEN_CHANNEL, 2)

    summary = coord.learning_health_summary()

    assert summary["available"] is True
    assert summary["discard_streak"] == 2
    assert summary["last_discard_reason"] == DROPOUT_REASON_FROZEN_CHANNEL
    assert summary["last_discard_modules"] == ["M2"]
    assert summary["last_discard_day"] == "2026-07-21"
    assert summary["streak_threshold"] == LEARNING_STALLED_STREAK_DAYS


def test_diagnostics_blocks_carry_learning_health_and_channel_presence():
    """The two accessors the diagnostics dump already uses must carry the new
    fields — no third code path (SPEC §8)."""
    from custom_components.balcony_solar_forecast.coordinator import (
        BalconySolarCoordinator,
    )

    c = BalconySolarCoordinator.__new__(BalconySolarCoordinator)
    c._store = _HealthStore()
    c._site = _site()
    c.hass = _FakeHass({"sensor.m1"})
    c._last_actuals_dropout = None
    c._channel_health = {"available": False}
    c.raised = []
    c.deleted = []
    c._raise_repair_issue = lambda i, p=None: c.raised.append(i)
    c._delete_repair_issue = lambda i: c.deleted.append(i)
    c._bias_state = None
    c._quantile_state = None
    c._shademap_state = None

    c.async_check_actual_channels()

    stats = c.store_stats()
    assert stats["learning_health"]["available"] is True
    assert stats["learning_health"]["streak_threshold"] == LEARNING_STALLED_STREAK_DAYS

    learners = c.learner_state_summary()
    assert learners["actual_channels"]["missing"] == ["M2 (sensor.m2)"]


# ---------------------------------------------------------------------------
# (5) The reason plumbing in _actuals: the gate must SAY which one fired.
# ---------------------------------------------------------------------------


def _rows(hours: int, value: float = 100.0, *, start_hour: int = 6) -> list[dict]:
    """Hourly LTS rows with epoch-SECOND ``start`` values (in-process format)."""
    from datetime import UTC, datetime

    return [
        {
            "start": datetime(2026, 7, 20, start_hour + h, tzinfo=UTC).timestamp(),
            "mean": value + h,  # varying, so the frozen gate does not fire
        }
        for h in range(hours)
    ]


@pytest.mark.parametrize(
    ("stats", "reason", "module"),
    [
        ({"sensor.m1": _rows(12)}, DROPOUT_REASON_DEAD_CHANNEL, "M2"),
        (
            {"sensor.m1": _rows(12), "sensor.m2": [
                {"start": r["start"], "mean": 42.0} for r in _rows(12)
            ]},
            DROPOUT_REASON_FROZEN_CHANNEL,
            "M2",
        ),
        (
            {"sensor.m1": _rows(12), "sensor.m2": _rows(2)},
            DROPOUT_REASON_LOW_COVERAGE,
            "M2",
        ),
    ],
)
def test_actuals_gate_reports_which_gate_discarded_the_day(stats, reason, module):
    """Without this the streak detector could only say "nothing again" — which is
    the log line that was already there and that nobody acted on."""
    from custom_components.balcony_solar_forecast._actuals import _actuals_from_stats

    dropout: dict = {}
    daily, hourly = _actuals_from_stats(
        stats,
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=_DAY,
        dropout_out=dropout,
    )

    assert (daily, hourly) == ({}, {})
    assert dropout["reason"] == reason
    assert dropout["modules"] == [module]
    assert dropout["entities"] == ["sensor.m2"]


def test_accepted_day_leaves_the_dropout_slot_empty():
    """A clean day must not look like a discard to the streak detector."""
    from custom_components.balcony_solar_forecast._actuals import _actuals_from_stats

    dropout: dict = {}
    daily, _hourly = _actuals_from_stats(
        {"sensor.m1": _rows(12), "sensor.m2": _rows(12, 50.0)},
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=_DAY,
        dropout_out=dropout,
    )

    assert daily  # the day was accepted
    assert dropout == {}


def test_every_dropout_reason_has_a_repair_issue():
    """A new gate that forgets its issue id would stall learning invisibly again
    — exactly the class of bug this whole tranche exists to end."""
    assert set(ISSUE_LEARNING_STALLED_BY_REASON) == set(DROPOUT_REASONS)


# ---------------------------------------------------------------------------
# (6) The nightly job actually calls the detector.
# ---------------------------------------------------------------------------


def test_nightly_records_the_outcome_of_every_actuals_read(monkeypatch):
    """Wiring guard: the detector is worthless if the nightly never feeds it."""
    calls: list[tuple[date, bool]] = []

    class _Coordinator:
        def __init__(self) -> None:
            self._store = _NightlyStore()
            self.data = None

        def _load_learner_states(self):
            pass

        async def _snapshot_issued(self, today):
            pass

        def _catchup_days(self, latest):
            return [latest]

        async def _read_actuals_safe(self, day):
            return {"M1": 1000.0}, {"M1": {"h": 1.0}}

        def _record_actuals_outcome(self, day, *, accepted):
            calls.append((day, accepted))

        async def _train_and_guard(self, day):
            pass

        async def _score_scoreboard_day(self, day):
            pass

        async def _train_inverter_cal(self, day):
            pass

    class _NightlyStore:
        def __init__(self) -> None:
            self.actuals: dict[str, dict] = {}
            self.hourly: dict[str, dict] = {}

        def has_actuals(self, iso):
            return iso in self.actuals

        def record_actuals(self, iso, daily):
            self.actuals[iso] = daily

        def record_hourly_actuals(self, iso, hourly):
            self.hourly[iso] = hourly

    import asyncio
    from datetime import UTC, datetime

    coord = _Coordinator()
    asyncio.run(
        _nightly.async_nightly_job(coord, datetime(2026, 7, 21, 1, 30, tzinfo=UTC))
    )

    assert calls == [(date(2026, 7, 20), True)]


# ---------------------------------------------------------------------------
# (7) Entry setup actually schedules the channel check — both branches.
#
# Without these, replacing the ``async_schedule_channel_health_check()`` call in
# ``async_setup_entry`` with ``pass`` left the whole suite green: detector (1)
# would run in NO real installation and nothing would notice. The deferral to
# EVENT_HOMEASSISTANT_STARTED is the load-bearing part (it is the reason the
# check does not hang off ``_reconcile_config_fingerprint``), so both branches
# and the unsubscribe bookkeeping are pinned here.
# ---------------------------------------------------------------------------


class _OnceBus:
    """``async_listen_once`` with HA's real removal semantics.

    HA's ``_OneTimeListener`` removes itself BEFORE invoking the callback, so
    the unsub handed back to the caller is already spent once the event fired;
    calling it again reaches ``EventBus._async_remove_listener``, which logs
    "Unable to remove unknown job listener" as an ERROR with traceback. This
    fake raises on that second removal so the guard is tested, not assumed.
    """

    def __init__(self) -> None:
        self.listeners: dict[str, list] = {}
        self.unsub_calls = 0

    def async_listen_once(self, event_type, callback_):
        self.listeners.setdefault(event_type, []).append(callback_)

        def _unsub() -> None:
            self.unsub_calls += 1
            self.listeners[event_type].remove(callback_)  # ValueError if spent

        return _unsub

    def fire(self, event_type, event=None) -> None:
        for callback_ in list(self.listeners.get(event_type, ())):
            self.listeners[event_type].remove(callback_)  # HA removes first
            callback_(event)


class _StartupHass:
    def __init__(self, state) -> None:
        self.state = state
        self.bus = _OnceBus()


class _StartupEntry:
    def __init__(self) -> None:
        self.unloads: list = []

    def async_on_unload(self, func) -> None:
        self.unloads.append(func)

    def run_unloads(self) -> None:
        for func in self.unloads:
            func()


def _scheduling_coordinator(state):
    """Real ``async_schedule_channel_health_check`` on a bare coordinator."""
    from custom_components.balcony_solar_forecast.coordinator import (
        BalconySolarCoordinator,
    )

    coord = BalconySolarCoordinator.__new__(BalconySolarCoordinator)
    coord.hass = _StartupHass(state)
    coord.entry = _StartupEntry()
    checks: list[int] = []
    coord.async_check_actual_channels = lambda: checks.append(1)
    return coord, checks


def test_setup_entry_schedules_the_channel_health_check(monkeypatch):
    """Wiring guard: detector (1) is inert unless entry setup arms it."""
    import asyncio

    from custom_components import balcony_solar_forecast as integration

    calls: list[str] = []

    class _StubCoordinator:
        def __init__(self, hass, entry, *, fetcher, store) -> None:
            self.hass = hass

        async def async_prime_from_store(self) -> None:
            pass

        async def async_config_entry_first_refresh(self) -> None:
            pass

        def async_start_nightly_job(self) -> None:
            calls.append("nightly")

        def async_schedule_channel_health_check(self) -> None:
            calls.append("channel_health")

        async def async_startup_catchup(self) -> None:
            pass

    class _StubStore:
        def __init__(self, hass, entry_id) -> None:
            pass

        async def async_load(self) -> None:
            pass

    class _ConfigEntries:
        async def async_forward_entry_setups(self, entry, platforms) -> None:
            pass

    class _Hass:
        def __init__(self) -> None:
            self.data: dict = {}
            self.bus = _OnceBus()
            self.config_entries = _ConfigEntries()

    class _Entry(_StartupEntry):
        entry_id = "abc"

        def async_create_background_task(self, hass, coro, name=None) -> None:
            coro.close()  # never scheduled here; do not warn about it

        def add_update_listener(self, listener):
            return lambda: None

    monkeypatch.setattr(integration, "OpenMeteoFetcher", lambda session: object())
    monkeypatch.setattr(integration, "ForecastStore", _StubStore)
    monkeypatch.setattr(integration, "BalconySolarCoordinator", _StubCoordinator)
    monkeypatch.setattr(
        integration, "async_get_clientsession", lambda hass: object()
    )

    assert asyncio.run(integration.async_setup_entry(_Hass(), _Entry())) is True
    assert "channel_health" in calls


def test_channel_check_runs_at_once_when_home_assistant_is_running():
    from homeassistant.core import CoreState

    coord, checks = _scheduling_coordinator(CoreState.running)
    coord.async_schedule_channel_health_check()

    assert checks == [1]
    assert coord.hass.bus.listeners == {}  # nothing deferred
    assert coord.entry.unloads == []


def test_channel_check_is_deferred_until_home_assistant_started():
    """Cold boot: checking before the inverter integration registered its
    entities would raise a false 'channel missing' alarm on every restart."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState

    coord, checks = _scheduling_coordinator(CoreState.starting)
    coord.async_schedule_channel_health_check()

    assert checks == []  # not yet — HA is still starting
    assert len(coord.hass.bus.listeners[EVENT_HOMEASSISTANT_STARTED]) == 1

    coord.hass.bus.fire(EVENT_HOMEASSISTANT_STARTED)

    assert checks == [1]


def test_unload_after_the_deferred_check_fired_does_not_unsubscribe_again():
    """A spent one-time unsub called again logs an ERROR + traceback — on the
    exact path the repair card recommends (boot -> reconfigure -> reload)."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState

    coord, checks = _scheduling_coordinator(CoreState.starting)
    coord.async_schedule_channel_health_check()
    coord.hass.bus.fire(EVENT_HOMEASSISTANT_STARTED)

    coord.entry.run_unloads()  # must not raise

    assert checks == [1]
    assert coord.hass.bus.unsub_calls == 0


def test_unload_before_start_detaches_the_pending_listener():
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState

    coord, checks = _scheduling_coordinator(CoreState.starting)
    coord.async_schedule_channel_health_check()

    coord.entry.run_unloads()

    assert coord.hass.bus.unsub_calls == 1
    assert coord.hass.bus.listeners[EVENT_HOMEASSISTANT_STARTED] == []

    coord.hass.bus.fire(EVENT_HOMEASSISTANT_STARTED)
    assert checks == []  # detached: a dead entry never runs the check
