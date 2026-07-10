"""Tests for the coordinator's learning-layer glue (v0.2.0 + v0.3.0).

Owner: coordinator. These exercise the pure GLUE logic — live-actual label
gates, the intraday-sample builder, day-ahead aggregation, the collapse
detector, the drift monitor's auto-disable + repair issue, the rollback ring
and the learner-status / self.data additions — WITHOUT standing up a full HA
instance. The coordinator is built via ``__new__`` and only the attributes each
method touches are populated (the platform tests use the same pattern).

Import is via ``custom_components.balcony_solar_forecast`` (the real HA-importing
package), so HA must be installed; the whole module is skipped otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import State  # noqa: E402

from custom_components.balcony_solar_forecast import (
    coordinator as coord_mod,  # noqa: E402
)
from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    CLOUD_CLASS_CLEAR,
    COLLAPSE_FORECAST_MIN_WH,
    CORRECTION_SOURCE_NONE,
    CORRECTION_SOURCE_SHADEMAP,
    DATA_KEY_CORRECTED_HOURLY_WH,
    DATA_KEY_RAW_HOURLY_WH,
    DAY_AHEAD_BIAS_MIN,
    DAY_PART_AFTERNOON,
    DAY_PART_MIDDAY,
    DAY_PART_MORNING,
    DRIFT_LOSS_STREAK_DAYS,
    INTRADAY_NEUTRAL,
    ISSUE_FAST_LEARNER_DISABLED,
    LABEL_FROZEN_STALE_SECONDS,
    LEARNER_LAYER_FAST,
    LEARNER_LAYER_SLOW,
    LEARNER_SNAPSHOT_RING,
    LEARNER_STATUS_ACTIVE,
    LEARNER_STATUS_FROZEN,
    RLS_MIN_SAMPLES,
)
from custom_components.balcony_solar_forecast.coordinator import (  # noqa: E402
    BalconySolarCoordinator,
    _is_frozen_channel,
    _usable_power,
)
from custom_components.balcony_solar_forecast.core import LearnerHooks  # noqa: E402
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasState,
    DriftState,
    ForecastResult,
    IssuedSnapshot,
    LearnerConfig,
    PlaneConfig,
    PlaneResult,
    ShademapBin,
    ShademapState,
    SiteConfig,
)

DOMAIN = "balcony_solar_forecast"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStates:
    def __init__(self) -> None:
        self._d: dict[str, State] = {}

    def set(self, entity_id: str, value, last_updated: datetime | None = None) -> None:
        self._d[entity_id] = State(entity_id, str(value), last_updated=last_updated)

    def get(self, entity_id: str) -> State | None:
        return self._d.get(entity_id)


class _FakeConfig:
    time_zone = "UTC"


class _FakeHass:
    def __init__(self) -> None:
        self.states = _FakeStates()
        self.config = _FakeConfig()


class _FakeStore:
    """In-memory stand-in for the (owner: store) v2 getters/setters."""

    def __init__(self) -> None:
        self.bias = BiasState().to_dict()
        self.shademap = ShademapState().to_dict()
        self.drift = DriftState().to_dict()
        self.snapshots: list[dict] = []
        self.issued: dict[str, dict] = {}
        self.actuals: dict[str, dict] = {}
        self.hourly_actuals: dict[str, dict[str, dict[str, float]]] = {}

    # v2 learner state
    def get_bias_state(self) -> BiasState:
        return BiasState.from_dict(self.bias)

    def set_bias_state(self, state) -> None:
        self.bias = state.to_dict()

    def get_shademap_state(self) -> ShademapState:
        return ShademapState.from_dict(self.shademap)

    def set_shademap_state(self, state) -> None:
        self.shademap = state.to_dict()

    def get_drift_state(self) -> DriftState:
        return DriftState.from_dict(self.drift)

    def set_drift_state(self, state) -> None:
        self.drift = state.to_dict()

    # rollback ring (real ForecastStore API)
    def get_snapshots(self):
        from custom_components.balcony_solar_forecast.core.types import LearnerSnapshot

        return [LearnerSnapshot.from_dict(e) for e in self.snapshots]

    def push_snapshot(self, snapshot) -> None:
        self.snapshots.append(snapshot.to_dict())
        if len(self.snapshots) > LEARNER_SNAPSHOT_RING:
            del self.snapshots[: len(self.snapshots) - LEARNER_SNAPSHOT_RING]

    # v1 rings
    def get_issued(self, iso):
        return self.issued.get(iso)

    def record_issued(self, iso, snap):
        self.issued[iso] = snap

    def get_actuals(self, iso):
        return self.actuals.get(iso)

    def has_actuals(self, iso):
        return iso in self.actuals

    def record_actuals(self, iso, per_module):
        self.actuals[iso] = dict(per_module)

    def actuals_dates(self):
        return sorted(self.actuals)

    def get_last_payload(self):
        return None

    def get_hourly_actuals(self, iso):
        return self.hourly_actuals.get(iso)

    def record_hourly_actuals(self, iso, per_channel):
        self.hourly_actuals[iso] = {c: dict(h) for c, h in per_channel.items()}

    # trained-day idempotence markers (real ForecastStore API)
    def is_day_trained(self, iso):
        return iso in getattr(self, "trained_days", set())

    def mark_day_trained(self, iso):
        if not hasattr(self, "trained_days"):
            self.trained_days = set()
        self.trained_days.add(iso)


def _site() -> SiteConfig:
    return SiteConfig(
        latitude=48.5,
        longitude=12.2,
        planes=(
            PlaneConfig(name="M1", azimuth_deg=115.0, tilt_deg=70.0, wp=370.0,
                        actual_entity="sensor.m1"),
            PlaneConfig(name="M2", azimuth_deg=205.0, tilt_deg=70.0, wp=430.0,
                        actual_entity="sensor.m2"),
        ),
        groups=(),
    )


class _Entry:
    def __init__(self, data=None, options=None):
        self.entry_id = "e1"
        self.data = data or {}
        self.options = options or {}


def _make_coordinator(store: _FakeStore | None = None) -> BalconySolarCoordinator:
    """Build a bare coordinator with only the attributes the glue methods use."""
    c = BalconySolarCoordinator.__new__(BalconySolarCoordinator)
    c.hass = _FakeHass()
    c._store = store or _FakeStore()
    c._site = _site()
    c.entry = _Entry()
    c._learner_config = LearnerConfig()
    c._bias_state = BiasState()
    c._shademap_state = ShademapState()
    c._drift_state = DriftState()
    c._learner_states_loaded = True
    c._intraday_scalar = INTRADAY_NEUTRAL
    from collections import deque

    c._intraday_samples = deque()
    c._correction_source = CORRECTION_SOURCE_NONE
    c._last_result = None
    c._last_error = None
    # Fetch provenance for the degradation ladder (normally set in __init__).
    c._last_fetched_at = None
    c._last_attempt_at = None
    c._last_fetch_ok = False
    # Shade-profile diagram selection + memo (normally set in __init__).
    c._shade_profile_module = None
    c._shade_profile_date = None
    c._shade_profile_cache = None
    # v0.4 scoreboard attributes (_build_data now assembles the scoreboard
    # summary): neutral empty ring, defaults, no comparisons.
    from custom_components.balcony_solar_forecast.const import (
        DEFAULT_SCOREBOARD_GATE_MARGIN,
        DEFAULT_SCOREBOARD_WINDOW_DAYS,
    )
    from custom_components.balcony_solar_forecast.core.types import ScoreboardState

    c._scoreboard_enabled = True
    c._scoreboard_window_days = DEFAULT_SCOREBOARD_WINDOW_DAYS
    c._scoreboard_gate_margin = DEFAULT_SCOREBOARD_GATE_MARGIN
    c._comparisons = ()
    c._scoreboard_state = ScoreboardState()
    # v0.4 quantile lane: enabled by default, empty ring (cold start -> neutral).
    from custom_components.balcony_solar_forecast.core.types import QuantileState

    c._quantiles_enabled = True
    c._quantile_state = QuantileState()
    return c


def _issued_snapshot(*, raw_daily_wh: float, hours: list[tuple[str, float]]) -> dict:
    """Build a v2 issued snapshot dict with the given hourly raw curve."""
    raw_hourly = {h: v for h, v in hours}
    return IssuedSnapshot(
        issued_at="2026-01-01T00:00:00+00:00",
        status="fresh",
        raw_hourly_wh=raw_hourly,
        corrected_hourly_wh=dict(raw_hourly),
    ).to_dict()


# ---------------------------------------------------------------------------
# Live-actual label gates (_usable_power)
# ---------------------------------------------------------------------------


def test_usable_power_accepts_fresh_numeric():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    s = State("sensor.m1", "210.0", last_updated=now)
    assert _usable_power(s, now) == pytest.approx(210.0)


def test_usable_power_accepts_fresh_zero():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    s = State("sensor.m1", "0", last_updated=now)
    assert _usable_power(s, now) == 0.0


@pytest.mark.parametrize("bad", ["unknown", "unavailable", "", "none", "not-a-number"])
def test_usable_power_rejects_unusable_states(bad):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    s = State("sensor.m1", bad, last_updated=now)
    assert _usable_power(s, now) is None


def test_usable_power_rejects_missing_state():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert _usable_power(None, now) is None


def test_usable_power_rejects_frozen_stale_sensor():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    stale = now - timedelta(seconds=LABEL_FROZEN_STALE_SECONDS + 60)
    s = State("sensor.m1", "210.0", last_updated=stale)
    assert _usable_power(s, now) is None
    fresh = now - timedelta(seconds=LABEL_FROZEN_STALE_SECONDS - 60)
    s2 = State("sensor.m1", "210.0", last_updated=fresh)
    assert _usable_power(s2, now) == pytest.approx(210.0)


def test_read_live_actuals_total_sums_usable_and_skips_frozen():
    c = _make_coordinator()
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    c.hass.states.set("sensor.m1", 100.0, last_updated=now)
    # M2 frozen (stale) -> skipped
    c.hass.states.set(
        "sensor.m2", 999.0,
        last_updated=now - timedelta(seconds=LABEL_FROZEN_STALE_SECONDS + 60),
    )
    total, planes = c._read_live_actuals_total(now)
    assert total == pytest.approx(100.0)
    assert planes == {"M1"}


def test_read_live_actuals_total_none_when_all_unusable():
    c = _make_coordinator()
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    c.hass.states.set("sensor.m1", "unavailable", last_updated=now)
    c.hass.states.set("sensor.m2", "unknown", last_updated=now)
    assert c._read_live_actuals_total(now) is None


# ---------------------------------------------------------------------------
# Day-part mapping + day-ahead aggregation
# ---------------------------------------------------------------------------


def test_day_part_for_hourkey_maps_local_hours():
    c = _make_coordinator()  # tz = UTC
    assert c._day_part_for_hourkey("2026-07-01T08:00:00+00:00") == DAY_PART_MORNING
    assert c._day_part_for_hourkey("2026-07-01T12:00:00+00:00") == DAY_PART_MIDDAY
    assert c._day_part_for_hourkey("2026-07-01T16:00:00+00:00") == DAY_PART_AFTERNOON


def test_day_ahead_samples_apportion_measured_by_modeled_shape():
    """Without hourly actuals, the day's measured total apportions by shape."""
    c = _make_coordinator()
    raw_hourly = {
        "2026-07-01T08:00:00+00:00": 100.0,  # morning
        "2026-07-01T12:00:00+00:00": 300.0,  # midday
        "2026-07-01T16:00:00+00:00": 100.0,  # afternoon
    }
    actuals = {"M1": 250.0, "M2": 250.0}  # measured total 500 Wh
    snap = IssuedSnapshot.from_dict(
        _issued_snapshot(raw_daily_wh=500.0, hours=list(raw_hourly.items()))
    )
    samples = c._day_ahead_samples(raw_hourly, actuals, snap, None)
    by_part = {s.day_part: s for s in samples}
    assert set(by_part) == {DAY_PART_MORNING, DAY_PART_MIDDAY, DAY_PART_AFTERNOON}
    assert by_part[DAY_PART_MIDDAY].measured_wh == pytest.approx(300.0)
    assert by_part[DAY_PART_MIDDAY].modeled_wh == pytest.approx(300.0)
    assert by_part[DAY_PART_MORNING].measured_wh == pytest.approx(100.0)
    # No forecast cloud context -> default clear.
    assert all(s.cloud_class == CLOUD_CLASS_CLEAR for s in samples)


def test_day_ahead_samples_use_hourly_measured_and_cloud_class():
    """With hourly actuals + cloud context, each cell carries its OWN pair."""
    c = _make_coordinator()
    raw_hourly = {
        "2026-07-01T08:00:00+00:00": 100.0,  # morning
        "2026-07-01T12:00:00+00:00": 300.0,  # midday
    }
    snap = IssuedSnapshot(
        issued_at="x", status="fresh", raw_hourly_wh=raw_hourly,
        cloud_class_by_hour={
            "2026-07-01T08:00:00+00:00": "fog",
            "2026-07-01T12:00:00+00:00": "clear",
        },
    )
    site_measured = {
        "2026-07-01T08:00:00+00:00": 40.0,   # fog morning under-produced
        "2026-07-01T12:00:00+00:00": 285.0,  # clear midday near forecast
    }
    samples = c._day_ahead_samples(raw_hourly, {"M1": 325.0}, snap, site_measured)
    by = {(s.cloud_class, s.day_part): s for s in samples}
    assert by[("fog", DAY_PART_MORNING)].measured_wh == pytest.approx(40.0)
    assert by[("clear", DAY_PART_MIDDAY)].measured_wh == pytest.approx(285.0)


def test_day_ahead_samples_empty_on_zero_energy():
    c = _make_coordinator()
    snap = IssuedSnapshot.from_dict(_issued_snapshot(raw_daily_wh=0.0, hours=[]))
    assert c._day_ahead_samples({}, {"M1": 0.0}, snap, None) == []


def test_day_ahead_training_moves_theta_up_not_to_min():
    """A near-1.0 day trained RLS_MIN_SAMPLES times pushes theta well above the
    0.5 clamp — the anti-pinned-at-DAY_AHEAD_BIAS_MIN assertion (FIX-2)."""
    c = _make_coordinator()
    # Day D forecast 2000 Wh (daylight hours), measured 1800 Wh (~0.9).
    hours = [(f"2026-07-01T{h:02d}:00:00+00:00", 250.0) for h in range(8, 16)]
    issued = _issued_snapshot(raw_daily_wh=2000.0, hours=hours)
    actuals = {"M1": 900.0, "M2": 900.0}  # 1800 total
    for _ in range(RLS_MIN_SAMPLES + 1):
        c._train_day_ahead("2026-07-01", issued, actuals)
    theta = c._bias_state.cells[BiasState.cell_key("clear", DAY_PART_MIDDAY)].theta
    assert theta > DAY_AHEAD_BIAS_MIN + 0.2


def test_day_ahead_samples_filtered_to_training_day():
    """An old 4-day snapshot only contributes the training day's hours (FIX-2)."""
    c = _make_coordinator()
    hours = []
    # Day D = 2026-07-01, 2000 Wh across daylight hours.
    for h in range(8, 16):
        hours.append((f"2026-07-01T{h:02d}:00:00+00:00", 250.0))
    # D+1..D+3 each 2000 Wh — must be ignored.
    for d in (2, 3, 4):
        for h in range(8, 16):
            hours.append((f"2026-07-0{d}T{h:02d}:00:00+00:00", 250.0))
    issued = _issued_snapshot(raw_daily_wh=8000.0, hours=hours)
    actuals = {"M1": 900.0, "M2": 900.0}  # measured 1800 for day D
    samples = c._day_ahead_samples(
        c._filter_hourly(issued, "2026-07-01"), actuals, IssuedSnapshot.from_dict(issued), None
    )
    assert sum(s.modeled_wh for s in samples) == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# Collapse detector
# ---------------------------------------------------------------------------


def test_collapse_detected_when_measured_far_below_forecast():
    c = _make_coordinator()
    issued = _issued_snapshot(
        raw_daily_wh=COLLAPSE_FORECAST_MIN_WH + 500.0,
        hours=[("2026-01-15T11:00:00+00:00", COLLAPSE_FORECAST_MIN_WH + 500.0)],
    )
    actuals = {"M1": 5.0, "M2": 5.0}  # ~1% of forecast
    assert c._is_collapse_day("2026-01-15", issued, actuals) is True


def test_no_collapse_when_measured_matches():
    c = _make_coordinator()
    total = COLLAPSE_FORECAST_MIN_WH + 500.0
    issued = _issued_snapshot(
        raw_daily_wh=total, hours=[("2026-01-15T11:00:00+00:00", total)]
    )
    actuals = {"M1": total / 2, "M2": total / 2}
    assert c._is_collapse_day("2026-01-15", issued, actuals) is False


def test_no_collapse_when_forecast_trivial():
    c = _make_coordinator()
    issued = _issued_snapshot(
        raw_daily_wh=10.0, hours=[("2026-01-15T11:00:00+00:00", 10.0)]
    )
    assert c._is_collapse_day("2026-01-15", issued, {"M1": 0.0, "M2": 0.0}) is False


def test_collapse_uses_single_day_forecast():
    """A 4-day snapshot must not inflate the collapse threshold (FIX-2)."""
    c = _make_coordinator()
    hours = [("2026-01-15T11:00:00+00:00", 2000.0)]
    for d in (16, 17, 18):
        hours.append((f"2026-01-{d}T11:00:00+00:00", 2000.0))
    issued = _issued_snapshot(raw_daily_wh=8000.0, hours=hours)
    # 300 Wh > 5% of ONE day (2000) -> NOT a collapse (old 8000-based code would).
    assert c._is_collapse_day("2026-01-15", issued, {"M1": 300.0}) is False
    # 90 Wh < 5% of 2000 -> collapse.
    assert c._is_collapse_day("2026-01-15", issued, {"M1": 90.0}) is True


async def test_train_and_guard_freezes_next_day_on_collapse():
    """Yesterday's collapse freezes the geometric learners for TODAY (FIX-7)."""
    c = _make_coordinator()
    total = COLLAPSE_FORECAST_MIN_WH + 500.0
    iso = "2026-01-15"
    next_iso = "2026-01-16"
    c._store.issued[iso] = _issued_snapshot(
        raw_daily_wh=total, hours=[("2026-01-15T11:00:00+00:00", total)]
    )
    c._store.actuals[iso] = {"M1": 3.0, "M2": 3.0}
    await c._train_and_guard(
        datetime.fromisoformat(iso + "T00:00:00+00:00").date()
    )
    # The freeze is persisted in DriftState and points at the SERVED day.
    assert c._drift_state.collapse_frozen_date == next_iso


# ---------------------------------------------------------------------------
# Drift monitor: 7 losing days -> auto-disable + repair issue
# ---------------------------------------------------------------------------


def test_drift_auto_disable_after_streak(monkeypatch):
    c = _make_coordinator()
    raised: list[str] = []
    monkeypatch.setattr(c, "_raise_repair_issue", lambda issue_id: raised.append(issue_id))
    c._learner_config = LearnerConfig(fast_enabled=True, slow_enabled=False)

    base = datetime(2026, 5, 1, tzinfo=UTC).date()
    for i in range(DRIFT_LOSS_STREAK_DAYS):
        day = base + timedelta(days=i)
        iso = day.isoformat()
        issued = IssuedSnapshot(
            issued_at="x",
            status="fresh",
            raw_hourly_wh={f"{iso}T11:00:00+00:00": 1000.0},
            corrected_hourly_wh={f"{iso}T11:00:00+00:00": 2000.0},
        ).to_dict()
        actuals = {"M1": 1000.0}
        c._update_drift(iso, issued, actuals)

    assert c._drift_state.fast_disabled is True
    assert ISSUE_FAST_LEARNER_DISABLED in raised
    assert c._drift_state.slow_disabled is False


def test_drift_mae_is_one_day_energy_error():
    """The drift MAE is the ONE-day |modeled - measured|, not the 4-day sum."""
    c = _make_coordinator()
    hours = [("2026-05-01T11:00:00+00:00", 2000.0)]
    for d in (2, 3, 4):
        hours.append((f"2026-05-0{d}T11:00:00+00:00", 2000.0))
    issued = _issued_snapshot(raw_daily_wh=8000.0, hours=hours)
    c._update_drift("2026-05-01", issued, {"M1": 1800.0})
    assert c._drift_state.daily_mae["2026-05-01"]["raw"] == pytest.approx(200.0)


def test_drift_noise_level_delta_is_not_a_loss():
    """A rounding-scale corrected-vs-raw delta on a well-trained/clear day must
    NOT count as a losing day — the absolute floor guards against seven such
    coin-flips auto-disabling a layer over meaningless Wh (SPEC §5)."""
    from custom_components.balcony_solar_forecast.const import (
        DRIFT_LOSS_MIN_ABS_WH,
    )

    c = _make_coordinator()
    c._learner_config = LearnerConfig(fast_enabled=True, slow_enabled=False)
    iso = "2026-05-01"
    h = f"{iso}T11:00:00+00:00"

    # raw perfect (raw_mae 0); corrected off by < DRIFT_LOSS_MIN_ABS_WH -> the
    # relative margin is technically exceeded (0 * 1.02 == 0) but the absolute
    # floor blocks it: not a loss.
    noise = DRIFT_LOSS_MIN_ABS_WH - 10.0
    issued = IssuedSnapshot(
        issued_at="x", status="fresh",
        raw_hourly_wh={h: 1000.0},
        corrected_hourly_wh={h: 1000.0 + noise},
    ).to_dict()
    c._update_drift(iso, issued, {"M1": 1000.0})
    assert c._drift_state.fast_loss_streak == 0

    # A materially worse corrected curve (> the floor) still counts.
    iso2 = "2026-05-02"
    h2 = f"{iso2}T11:00:00+00:00"
    real = DRIFT_LOSS_MIN_ABS_WH + 100.0
    issued2 = IssuedSnapshot(
        issued_at="x", status="fresh",
        raw_hourly_wh={h2: 1000.0},
        corrected_hourly_wh={h2: 1000.0 + real},
    ).to_dict()
    c._update_drift(iso2, issued2, {"M1": 1000.0})
    assert c._drift_state.fast_loss_streak == 1


def test_drift_streak_resets_on_a_winning_day():
    c = _make_coordinator()
    c._learner_config = LearnerConfig(fast_enabled=True, slow_enabled=False)
    base = datetime(2026, 5, 1, tzinfo=UTC).date()
    for i in range(3):
        iso = (base + timedelta(days=i)).isoformat()
        issued = IssuedSnapshot(
            issued_at="x", status="fresh",
            raw_hourly_wh={f"{iso}T11:00:00+00:00": 1000.0},
            corrected_hourly_wh={f"{iso}T11:00:00+00:00": 2000.0},
        ).to_dict()
        c._update_drift(iso, issued, {"M1": 1000.0})
    assert c._drift_state.fast_loss_streak == 3
    iso = (base + timedelta(days=3)).isoformat()
    issued = IssuedSnapshot(
        issued_at="x", status="fresh",
        raw_hourly_wh={f"{iso}T11:00:00+00:00": 1500.0},
        corrected_hourly_wh={f"{iso}T11:00:00+00:00": 1000.0},
    ).to_dict()
    c._update_drift(iso, issued, {"M1": 1000.0})
    assert c._drift_state.fast_loss_streak == 0
    assert c._drift_state.fast_disabled is False


def test_drift_window_trimmed():
    c = _make_coordinator()
    base = datetime(2026, 5, 1, tzinfo=UTC).date()
    for i in range(20):
        iso = (base + timedelta(days=i)).isoformat()
        issued = IssuedSnapshot(
            issued_at="x", status="fresh",
            raw_hourly_wh={f"{iso}T11:00:00+00:00": 1000.0},
            corrected_hourly_wh={f"{iso}T11:00:00+00:00": 1000.0},
        ).to_dict()
        c._update_drift(iso, issued, {"M1": 1000.0})
    from custom_components.balcony_solar_forecast.const import DRIFT_WINDOW_DAYS

    assert len(c._drift_state.daily_mae) == DRIFT_WINDOW_DAYS


# ---------------------------------------------------------------------------
# Rollback ring
# ---------------------------------------------------------------------------


def test_rollback_ring_pushes_and_bounds(monkeypatch):
    c = _make_coordinator()
    for i in range(LEARNER_SNAPSHOT_RING + 3):
        day = datetime(2026, 5, 1, 1, 30, tzinfo=UTC) + timedelta(days=i)
        monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda d=day: d)
        c._maybe_push_rollback_snapshot(f"2026-04-{i + 1:02d}")
    assert len(c._store.snapshots) == LEARNER_SNAPSHOT_RING


def test_rollback_ring_idempotent_per_run_day(monkeypatch):
    c = _make_coordinator()
    day = datetime(2026, 5, 1, 1, 30, tzinfo=UTC)
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: day)
    c._maybe_push_rollback_snapshot("2026-04-29")
    c._maybe_push_rollback_snapshot("2026-04-30")
    assert len(c._store.snapshots) == 1


def test_rollback_ring_depth_exceeds_loss_streak():
    """A pre-streak good snapshot must survive an auto-disable (ring > streak)."""
    assert LEARNER_SNAPSHOT_RING > DRIFT_LOSS_STREAK_DAYS


# ---------------------------------------------------------------------------
# FIX-5: re-enable clears drift disable only on a real OFF->ON transition
# ---------------------------------------------------------------------------


def test_restart_preserves_drift_disable(monkeypatch):
    """7 losing days -> disable -> restart must NOT re-enable (FIX-5)."""
    store = _FakeStore()
    store.drift = DriftState(fast_disabled=True, fast_option_seen=True).to_dict()
    c = _make_coordinator(store)
    c._learner_states_loaded = False
    deleted: list[str] = []
    monkeypatch.setattr(c, "_delete_repair_issue", lambda i: deleted.append(i))
    c.entry = _Entry(data={"fast_learner_enabled": True})
    c._load_learner_states()
    c.rebuild_learner_config()
    assert c._drift_state.fast_disabled is True
    assert deleted == []  # not re-enabled


def test_toggle_off_on_clears_drift_disable(monkeypatch):
    c = _make_coordinator()
    deleted: list[str] = []
    monkeypatch.setattr(c, "_delete_repair_issue", lambda i: deleted.append(i))
    c._drift_state = DriftState(
        fast_disabled=True, fast_loss_streak=5, fast_option_seen=True
    )
    # Toggle OFF: flag persists, fast_option_seen -> False.
    c.entry = _Entry(options={"fast_learner_enabled": False})
    c.rebuild_learner_config()
    assert c._drift_state.fast_disabled is True
    assert c._drift_state.fast_option_seen is False
    # Toggle back ON: the OFF->ON transition clears the disable.
    c.entry = _Entry(options={"fast_learner_enabled": True})
    c.rebuild_learner_config()
    assert c._drift_state.fast_disabled is False
    assert c._drift_state.fast_loss_streak == 0
    assert ISSUE_FAST_LEARNER_DISABLED in deleted


def test_legacy_drift_state_without_option_seen_keeps_disable():
    """A legacy blob (no *_option_seen) must NOT be read as a transition."""
    ds = DriftState.from_dict({"slow_disabled": True})
    assert ds.fast_option_seen is None
    assert ds.slow_option_seen is None
    c = _make_coordinator()
    c._drift_state = ds
    c.entry = _Entry()  # all default options (slow enabled by default)
    c.rebuild_learner_config()
    assert c._drift_state.slow_disabled is True  # not cleared


# ---------------------------------------------------------------------------
# Intraday sample builder (uses RAW curve, scales modeled to usable planes)
# ---------------------------------------------------------------------------


def _forecast_at_noon(watts: float, raw_watts: float | None = None):
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    rw = watts if raw_watts is None else raw_watts
    result = ForecastResult(
        slot_starts=(start,),
        total_watts=(watts,),
        plane_results=(
            PlaneResult(name="M1", watts=(watts,), raw_watts=(rw,)),
        ),
        hourly_wh={start.isoformat(): watts * 0.25},
        raw_total_watts=(rw,),
        raw_hourly_wh={start.isoformat(): rw * 0.25},
    )
    return result, start


def test_build_intraday_sample_returns_kc_space_ratio():
    c = _make_coordinator()
    result, start = _forecast_at_noon(400.0)
    c.hass.states.set("sensor.m1", 200.0, last_updated=start)
    c.hass.states.set("sensor.m2", 0.0, last_updated=start)
    sample = c._build_intraday_sample(result, start)
    assert sample is not None
    assert sample.modeled_kc > 0.0
    # measured 200 / modeled 400 (M1 only, M2 has no plane_result) ...
    # both usable planes; M2 has no raw series so modeled restricts to M1 = 400.
    assert sample.measured_kc / sample.modeled_kc == pytest.approx(0.5, rel=1e-6)


def test_intraday_sample_uses_raw_curve():
    """The sample's modeled_kc derives from the RAW curve, not the corrected."""
    c = _make_coordinator()
    # corrected 800 W, raw 400 W at noon; measured 400 W.
    result, start = _forecast_at_noon(800.0, raw_watts=400.0)
    c.hass.states.set("sensor.m1", 400.0, last_updated=start)
    c.hass.states.set("sensor.m2", 0.0, last_updated=start)
    sample = c._build_intraday_sample(result, start)
    assert sample is not None
    # ratio == measured/raw == 400/400 == 1.0 (NOT 400/800 == 0.5).
    assert sample.measured_kc / sample.modeled_kc == pytest.approx(1.0, rel=1e-6)


def test_intraday_sample_scales_modeled_to_usable_planes():
    """Partial dropout: modeled restricts to the reporting plane (no phantom
    deficit)."""
    c = _make_coordinator()
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    result = ForecastResult(
        slot_starts=(start,),
        total_watts=(600.0,),
        plane_results=(
            PlaneResult(name="M1", watts=(300.0,), raw_watts=(300.0,)),
            PlaneResult(name="M2", watts=(300.0,), raw_watts=(300.0,)),
        ),
        hourly_wh={start.isoformat(): 150.0},
        raw_total_watts=(600.0,),
        raw_hourly_wh={start.isoformat(): 150.0},
    )
    # Only M1 reports (M2 unavailable) at 300 W == its own modeled 300 W.
    c.hass.states.set("sensor.m1", 300.0, last_updated=start)
    c.hass.states.set("sensor.m2", "unavailable", last_updated=start)
    sample = c._build_intraday_sample(result, start)
    assert sample is not None
    # measured 300 / modeled-of-M1 300 == 1.0, NOT 300/600 == 0.5.
    assert sample.measured_kc / sample.modeled_kc == pytest.approx(1.0, rel=1e-6)


def test_build_intraday_sample_none_below_min_modeled():
    c = _make_coordinator()
    result, start = _forecast_at_noon(4.0)
    c.hass.states.set("sensor.m1", 2.0, last_updated=start)
    c.hass.states.set("sensor.m2", 0.0, last_updated=start)
    assert c._build_intraday_sample(result, start) is None


def test_update_intraday_scalar_neutral_when_disabled():
    c = _make_coordinator()
    c._learner_config = LearnerConfig(fast_enabled=False)
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    c._intraday_scalar = 0.5  # stale
    c._update_intraday_scalar(now)
    assert c._intraday_scalar == INTRADAY_NEUTRAL


def test_update_intraday_scalar_neutral_when_drift_disabled():
    c = _make_coordinator()
    c._drift_state = DriftState(fast_disabled=True)
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    c._update_intraday_scalar(now)
    assert c._intraday_scalar == INTRADAY_NEUTRAL


def test_update_intraday_scalar_survives_notimplemented(monkeypatch):
    c = _make_coordinator()
    result, start = _forecast_at_noon(400.0)
    c._last_result = result
    c.hass.states.set("sensor.m1", 200.0, last_updated=start)
    c.hass.states.set("sensor.m2", 0.0, last_updated=start)

    def _boom(*a, **k):
        raise NotImplementedError

    monkeypatch.setattr(coord_mod.bias_mod, "compute_intraday_scalar", _boom)
    c._update_intraday_scalar(start)
    assert c._intraday_scalar == INTRADAY_NEUTRAL


# ---------------------------------------------------------------------------
# FIX-1: hooks wiring
# ---------------------------------------------------------------------------


def test_compute_passes_learner_hooks(monkeypatch):
    c = _make_coordinator()
    captured = {}

    def _fake_compute(site, weather, now, tz=None, *, hooks=None):
        captured["hooks"] = hooks
        captured["tz"] = tz
        return _forecast_at_noon(0.0)[0]

    monkeypatch.setattr(coord_mod, "compute_forecast", _fake_compute)

    class _W:
        slots = ()

    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    c._compute(_W(), now)
    assert isinstance(captured["hooks"], LearnerHooks)


def test_hooks_identity_when_layers_off():
    c = _make_coordinator()
    c._learner_config = LearnerConfig(
        fast_enabled=False, slow_enabled=False, day_ahead_enabled=False
    )

    class _W:
        slots = ()

    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    hooks = c._build_learner_hooks(_W(), now)
    assert hooks.beam_tau is None
    assert hooks.slot_factor is None
    assert hooks.correction_source == CORRECTION_SOURCE_NONE


def test_hooks_shademap_source_when_bins_present():
    c = _make_coordinator()
    c._shademap_state = ShademapState(
        channels={"M1": {"1:1:0": ShademapBin(tau=0.0, n=50)}}
    )

    class _W:
        slots = ()

    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    hooks = c._build_learner_hooks(_W(), now)
    assert hooks.beam_tau is not None
    assert hooks.correction_source == CORRECTION_SOURCE_SHADEMAP


def test_beam_tau_hook_delegates_to_effective_tau_pooled(monkeypatch):
    """The built beam_tau hook binds shademap.effective_tau_pooled over the state.

    Storage is per plane, so an ungrouped plane's pool is just its own channel.
    """
    c = _make_coordinator()
    c._shademap_state = ShademapState(
        channels={"M1": {"1:1:0": ShademapBin(tau=0.0, n=50)}}
    )
    seen = {}

    def _fake_eff(state, *, channels, sun_az, sun_el, doy, static_prior):
        seen["channels"] = channels
        return 0.0

    monkeypatch.setattr(coord_mod.shademap_mod, "effective_tau_pooled", _fake_eff)

    class _W:
        slots = ()

    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    hooks = c._build_learner_hooks(_W(), now)
    assert hooks.beam_tau("M1", 200.0, 40.0, 180, 1.0) == 0.0
    # Ungrouped plane -> pool is exactly its own channel.
    assert seen["channels"] == ("M1",)


def test_hooks_shademap_silenced_by_drift_disable():
    c = _make_coordinator()
    c._shademap_state = ShademapState(
        channels={"M1": {"1:1:0": ShademapBin(tau=0.0, n=50)}}
    )
    c._drift_state = DriftState(slow_disabled=True)

    class _W:
        slots = ()

    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    hooks = c._build_learner_hooks(_W(), now)
    assert hooks.beam_tau is None


# ---------------------------------------------------------------------------
# self.data additive keys + learner status
# ---------------------------------------------------------------------------


def test_build_data_carries_learner_keys():
    c = _make_coordinator()
    result, start = _forecast_at_noon(400.0)
    now = start + timedelta(minutes=1)
    c._intraday_scalar = 0.9
    c._correction_source = coord_mod.CORRECTION_SOURCE_INTRADAY
    data = c._build_data(result, dict(result.hourly_wh), now, "fresh", timedelta(minutes=1))
    assert data[DATA_KEY_RAW_HOURLY_WH] == result.raw_hourly_wh
    assert data[DATA_KEY_CORRECTED_HOURLY_WH] == result.hourly_wh
    assert data["hourly_wh"] == result.hourly_wh
    assert data["intraday_scalar"] == pytest.approx(0.9)
    status = data["learner_status"]
    assert status["fast_active"] is True
    assert status[LEARNER_LAYER_FAST] == LEARNER_STATUS_ACTIVE


def test_learner_status_layer_strings():
    """_learner_status returns the layer-keyed ENUM strings (coordinator:674)."""
    c = _make_coordinator()
    status = c._learner_status()
    assert status[LEARNER_LAYER_FAST] == LEARNER_STATUS_ACTIVE
    assert status[LEARNER_LAYER_SLOW] == LEARNER_STATUS_ACTIVE


def test_learner_status_reflects_collapse_freeze():
    c = _make_coordinator()
    today = coord_mod.dt_util.as_local(coord_mod.dt_util.utcnow()).date().isoformat()
    c._drift_state = DriftState(collapse_frozen_date=today)
    status = c._learner_status()
    assert status["slow_frozen"] is True
    assert status["slow_active"] is False
    assert status[LEARNER_LAYER_SLOW] == LEARNER_STATUS_FROZEN


# ---------------------------------------------------------------------------
# Frozen-channel gate (nightly LTS label gate)
# ---------------------------------------------------------------------------


def _lts_rows(day_hours: list[tuple[int, float]]) -> list[dict]:
    """Synthetic LTS rows: (utc_hour, mean_w) pairs on 2026-06-20."""
    return [
        {"mean": w, "start": datetime(2026, 6, 20, h, 0, tzinfo=UTC)}
        for h, w in day_hours
    ]


def test_actuals_from_stats_happy_path():
    from custom_components.balcony_solar_forecast.coordinator import (
        _actuals_from_stats,
    )

    hours = [(h, 100.0 + 10.0 * h) for h in range(6, 18)]  # 12 daylight hours
    stats = {"sensor.m1": _lts_rows(hours), "sensor.m2": _lts_rows(hours)}
    daily, hourly = _actuals_from_stats(
        stats,
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=datetime(2026, 6, 20).date(),
    )
    assert set(daily) == {"M1", "M2"}
    assert daily["M1"] == pytest.approx(sum(w for _h, w in hours), abs=0.1)
    assert len(hourly["M1"]) == 12


def test_actuals_from_stats_zero_row_module_discards_day():
    """A configured module with NO LTS rows is a channel dropout: the whole day
    is discarded — a partial-site measurement must never train against the
    full-site model (SPEC §5: Messkanal-Dropout => ganzen Tag verwerfen)."""
    from custom_components.balcony_solar_forecast.coordinator import (
        _actuals_from_stats,
    )

    hours = [(h, 150.0 + h) for h in range(6, 18)]
    stats = {"sensor.m1": _lts_rows(hours)}  # sensor.m2 absent entirely
    daily, hourly = _actuals_from_stats(
        stats,
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=datetime(2026, 6, 20).date(),
    )
    assert daily == {} and hourly == {}


def test_actuals_from_stats_meanless_rows_discard_day():
    from custom_components.balcony_solar_forecast.coordinator import (
        _actuals_from_stats,
    )

    hours = [(h, 150.0 + h) for h in range(6, 18)]
    stats = {
        "sensor.m1": _lts_rows(hours),
        # Rows exist but carry no usable mean (unavailable sensor).
        "sensor.m2": [
            {"mean": None, "start": datetime(2026, 6, 20, h, 0, tzinfo=UTC)}
            for h in range(6, 18)
        ],
    }
    daily, hourly = _actuals_from_stats(
        stats,
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=datetime(2026, 6, 20).date(),
    )
    assert daily == {} and hourly == {}


def test_actuals_from_stats_frozen_channel_discards_day():
    from custom_components.balcony_solar_forecast.const import (
        LABEL_FROZEN_MIN_REPEATS,
    )
    from custom_components.balcony_solar_forecast.coordinator import (
        _actuals_from_stats,
    )

    good = [(h, 100.0 + h) for h in range(6, 18)]
    frozen = [(h, 180.0) for h in range(6, 6 + LABEL_FROZEN_MIN_REPEATS + 1)]
    stats = {"sensor.m1": _lts_rows(good), "sensor.m2": _lts_rows(frozen)}
    daily, hourly = _actuals_from_stats(
        stats,
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=datetime(2026, 6, 20).date(),
    )
    assert daily == {} and hourly == {}


def test_actuals_from_stats_partial_module_discards_day():
    """A module dying MID-DAY (few covered hours) must discard the day even when
    a healthy sibling covers everything — max-coverage masking was the bug."""
    from custom_components.balcony_solar_forecast.coordinator import (
        _actuals_from_stats,
    )

    full = [(h, 100.0 + h) for h in range(6, 18)]   # 12 hours
    partial = [(h, 100.0 + h) for h in range(6, 9)]  # 3 of 12 hours (< 75%)
    stats = {"sensor.m1": _lts_rows(full), "sensor.m2": _lts_rows(partial)}
    daily, hourly = _actuals_from_stats(
        stats,
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=datetime(2026, 6, 20).date(),
    )
    assert daily == {} and hourly == {}


def test_actuals_from_stats_unknown_daylight_skips_coverage_gate():
    from custom_components.balcony_solar_forecast.coordinator import (
        _actuals_from_stats,
    )

    short = [(h, 100.0 + h) for h in range(6, 9)]
    stats = {"sensor.m1": _lts_rows(short)}
    daily, _hourly = _actuals_from_stats(
        stats,
        {"M1": "sensor.m1"},
        expected_daylight_hours=0,  # unknown span -> coverage gate skipped
        day=datetime(2026, 6, 20).date(),
    )
    assert daily["M1"] == pytest.approx(sum(w for _h, w in short), abs=0.1)


def test_is_frozen_channel_detects_held_value():
    # Four identical non-zero hours in a row -> frozen.
    assert _is_frozen_channel([10.0, 180.0, 180.0, 180.0, 180.0]) is True
    # Varying values -> not frozen.
    assert _is_frozen_channel([10.0, 120.0, 200.0, 150.0, 60.0]) is False
    # A run of zeros (night) never trips the gate.
    assert _is_frozen_channel([0.0, 0.0, 0.0, 0.0, 0.0, 120.0]) is False


# ---------------------------------------------------------------------------
# FIX-3: shademap trains true transmittance (beam_referenced_t), not unity
# ---------------------------------------------------------------------------


def test_shademap_trains_true_transmittance_not_unity(monkeypatch):
    from custom_components.balcony_solar_forecast.core.types import PlaneHourlyModeled

    c = _make_coordinator()
    hkey = "2026-07-01T11:00:00+00:00"
    modeled = PlaneHourlyModeled(
        beam_wh={hkey: 100.0}, diffuse_wh={hkey: 20.0}, kc={hkey: 0.9}
    )
    measured_by_hour = {hkey: 65.0}  # (65 - 20) / 100 = 0.45
    # Force the quasi-clear gate to accept and pin sun position.
    monkeypatch.setattr(coord_mod.shademap_mod, "is_quasi_clear", lambda **k: True)
    monkeypatch.setattr(coord_mod.solpos, "sun_position", lambda *a: (200.0, 40.0))
    captured = {}
    real_update = coord_mod.shademap_mod.update_bin

    def _spy(state, **kw):
        captured["measured_t"] = kw["measured_t"]
        return real_update(state, **kw)

    monkeypatch.setattr(coord_mod.shademap_mod, "update_bin", _spy)
    state, changed = c._train_channel(
        c._shademap_state, "M1", modeled, measured_by_hour
    )
    assert changed is True
    assert captured["measured_t"] == pytest.approx(0.45)


def test_wall_bin_trains_full_occlusion(monkeypatch):
    from custom_components.balcony_solar_forecast.const import SHADEMAP_TAU_MIN
    from custom_components.balcony_solar_forecast.core.types import PlaneHourlyModeled

    c = _make_coordinator()
    hkey = "2026-07-01T11:00:00+00:00"
    # Ungated beam 100 Wh, diffuse floor 20 Wh, measured == floor -> T == 0.
    modeled = PlaneHourlyModeled(
        beam_wh={hkey: 100.0}, diffuse_wh={hkey: 20.0}, kc={hkey: 0.9}
    )
    measured_by_hour = {hkey: 20.0}
    monkeypatch.setattr(coord_mod.shademap_mod, "is_quasi_clear", lambda **k: True)
    monkeypatch.setattr(coord_mod.solpos, "sun_position", lambda *a: (210.0, 45.0))
    captured = {}
    real_update = coord_mod.shademap_mod.update_bin

    def _spy(state, **kw):
        captured["measured_t"] = kw["measured_t"]
        return real_update(state, **kw)

    monkeypatch.setattr(coord_mod.shademap_mod, "update_bin", _spy)
    state, changed = c._train_channel(
        c._shademap_state, "M1", modeled, measured_by_hour
    )
    assert changed is True
    assert captured["measured_t"] == pytest.approx(SHADEMAP_TAU_MIN)


def test_shademap_day_gate_rejects_overcast_bust():
    """A day the forecast called clear but measured far under is not trained."""
    c = _make_coordinator()
    iso = "2026-07-01"
    hkey = "2026-07-01T11:00:00+00:00"
    snap = IssuedSnapshot(
        issued_at="x", status="fresh",
        raw_hourly_wh={hkey: 1000.0},
    )
    hourly_actuals = {"M1": {hkey: 100.0}}  # 100 << 0.8 * 1000 -> reject
    assert c._day_is_measured_clear(iso, snap, hourly_actuals) is False
    hourly_actuals = {"M1": {hkey: 900.0}}  # 900 >= 800 -> accept
    assert c._day_is_measured_clear(iso, snap, hourly_actuals) is True


# ---------------------------------------------------------------------------
# FIX-2: snapshot stores only the target LOCAL day
# ---------------------------------------------------------------------------


async def test_snapshot_issued_stores_only_target_day():
    c = _make_coordinator()
    # self.data hourly spanning 4 days (UTC == local here, tz=UTC).
    raw = {}
    for d in range(1, 5):
        raw[f"2026-07-0{d}T11:00:00+00:00"] = 1000.0
    c.data = {
        DATA_KEY_RAW_HOURLY_WH: raw,
        DATA_KEY_CORRECTED_HOURLY_WH: dict(raw),
        "status": "fresh",
    }
    from datetime import date as _date

    await c._snapshot_issued(_date(2026, 7, 1))
    stored = IssuedSnapshot.from_dict(c._store.get_issued("2026-07-01"))
    assert set(stored.raw_hourly_wh) == {"2026-07-01T11:00:00+00:00"}


# ---------------------------------------------------------------------------
# Helper on the coordinator used by the day-ahead filter test
# ---------------------------------------------------------------------------


def _filter_hourly(self, issued, iso):
    snap = IssuedSnapshot.from_dict(issued)
    from custom_components.balcony_solar_forecast.coordinator import (
        _filter_hourly_to_local_day,
    )

    return _filter_hourly_to_local_day(snap.raw_hourly_wh, iso)


# Bind the helper so the day-ahead filter test can call it.
BalconySolarCoordinator._filter_hourly = _filter_hourly


# ---------------------------------------------------------------------------
# Shade-profile diagram: the learned shademap is blended into the diagram ONLY
# when the slow learner is active, matching what the served forecast applies
# (review finding — the diagram must not paint shading the forecast is not using).
# ---------------------------------------------------------------------------


def test_build_shade_profile_gates_on_slow_active():
    from custom_components.balcony_solar_forecast.core import shademap as sm

    c = _make_coordinator()
    day = datetime(2026, 6, 21).date()  # doy 172 -> half-year 1
    doy = day.timetuple().tm_yday
    # Train a fully-occluded bin for the front plane M1 (half-year 1).
    state = ShademapState()
    for _ in range(300):
        state = sm.update_bin(
            state, channel="M1", sun_az=115.0, sun_el=30.0, doy=doy, measured_t=0.0
        )
    c._shademap_state = state
    c._shade_profile_module = "M1"
    c._shade_profile_date = day

    # Slow learner ON -> the diagram blends the learned bin.
    c._learner_config = LearnerConfig(slow_enabled=True)
    on = c.build_shade_profile()
    assert on["has_learned_data"] is True
    on_cache = c._shade_profile_cache

    # Slow learner OFF -> the forecast applies static shading only; so must the
    # diagram (slow_active is part of the cache key, so this recomputes).
    c._learner_config = LearnerConfig(slow_enabled=False)
    off = c.build_shade_profile()
    assert off["has_learned_data"] is False
    assert off["learned_bins"] == 0
    assert c._shade_profile_cache is not on_cache


# ---------------------------------------------------------------------------
# _per_plane_modeled: the hourly kc must be the shared energy-weighted mean,
# not last-slot-wins (which made the quasi-clear gate azimuth-asymmetric and
# diverged from the backfill's estimator).
# ---------------------------------------------------------------------------


def test_per_plane_modeled_hourly_kc_is_energy_weighted():
    from custom_components.balcony_solar_forecast.core import (
        clearsky,
        solpos,
    )

    c = _make_coordinator()
    # Four 15-min slots of one summer UTC hour at the test site (sun well up).
    base = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    starts = tuple(base + timedelta(minutes=15 * i) for i in range(4))
    # Clear hour except the LAST slot, which drops to kc 0.2 — the old
    # last-write-wins code reported 0.2 for the whole hour.
    kc_series = (1.0, 1.0, 1.0, 0.2)
    pr = PlaneResult(
        name="M1",
        watts=(100.0,) * 4,
        beam_ref_watts=(80.0,) * 4,
        diffuse_ref_watts=(20.0,) * 4,
        kc=kc_series,
    )
    c._last_result = ForecastResult(
        slot_starts=starts,
        total_watts=(100.0,) * 4,
        plane_results=(pr,),
        hourly_wh={},
    )

    modeled = c._per_plane_modeled("2026-06-20")
    hkey = base.isoformat()
    got = modeled["M1"].kc[hkey]

    # Expected: the shared reduction over the reconstructed (ghi, el) samples,
    # using the same slot-midpoint convention as the engine.
    samples = []
    for start, kc in zip(starts, kc_series, strict=True):
        _az, el = solpos.sun_position(
            start + timedelta(minutes=7, seconds=30), 48.5, 12.2
        )
        samples.append((kc * clearsky.haurwitz_ghi(el), el))
    assert got == pytest.approx(clearsky.hourly_kc(samples))
    # Regression: NOT the last slot's value, and close to the clear majority.
    assert got > 0.6


def test_per_plane_modeled_trims_night_hours_and_rounds():
    """Store trim: all-zero night hours are dropped from the issued snapshot's
    per-plane curves and values are rounded (the 90-day ring dominated the
    store with night zeros and 17-digit floats)."""
    c = _make_coordinator()
    day = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)     # daylight hour
    night = datetime(2026, 6, 20, 22, 0, tzinfo=UTC)   # after sunset
    starts = (day, night)
    pr = PlaneResult(
        name="M1",
        watts=(100.0, 0.0),
        beam_ref_watts=(80.123456789, 0.0),
        diffuse_ref_watts=(20.987654321, 0.0),
        kc=(0.9123456789, 0.0),
    )
    c._last_result = ForecastResult(
        slot_starts=starts, total_watts=(100.0, 0.0),
        plane_results=(pr,), hourly_wh={},
    )

    modeled = c._per_plane_modeled("2026-06-20")["M1"]
    # The night hour is gone from every curve; the day hour is rounded.
    assert set(modeled.beam_wh) == {day.isoformat()}
    assert set(modeled.kc) == {day.isoformat()}
    assert modeled.beam_wh[day.isoformat()] == round(80.123456789 * 0.25, 2)
    # And the vestigial ghi dict no longer serializes at all.
    assert "ghi" not in modeled.to_dict()


# ---------------------------------------------------------------------------
# Shade groups (SPEC §5): READ-TIME pooling. Storage is ALWAYS per plane; a
# grouped plane's forecast/diagram POOLS its group siblings only at read time,
# so grouping/dissolution is fully reversible and lossless.
# ---------------------------------------------------------------------------


def _grouped_site() -> SiteConfig:
    """M1 (N) + M2 (S) sharing shade group 'south'."""
    return SiteConfig(
        latitude=48.5,
        longitude=12.2,
        planes=(
            PlaneConfig(name="M1", azimuth_deg=115.0, tilt_deg=70.0, wp=370.0,
                        actual_entity="sensor.m1", shade_group="south"),
            PlaneConfig(name="M2", azimuth_deg=205.0, tilt_deg=70.0, wp=430.0,
                        actual_entity="sensor.m2", shade_group="south"),
        ),
        groups=(),
    )


def test_bind_beam_tau_pools_grouped_planes():
    """A dark bin under ONE plane's own channel is read by BOTH grouped planes."""
    from custom_components.balcony_solar_forecast.core import shademap as sm

    c = _make_coordinator()
    c._site = _grouped_site()
    az, el, doy = 200.0, 40.0, 172
    # Storage is PER PLANE: seed the occluded bin under M1's OWN channel only.
    state = ShademapState()
    for _ in range(400):  # large n -> shrinkage weight ~1, learned tau dominates
        state = sm.update_bin(
            state, channel="M1", sun_az=az, sun_el=el, doy=doy, measured_t=0.0
        )
    c._shademap_state = state

    beam_tau = c._bind_beam_tau()
    prior = 1.0
    tau_m1 = beam_tau("M1", az, el, doy, prior)
    tau_m2 = beam_tau("M2", az, el, doy, prior)
    # M2 has no OWN bin, but its pool includes M1: both read the same dark tau,
    # well below the static prior — proof the read POOLED ('M1','M2').
    assert tau_m1 < 0.2
    assert tau_m1 == pytest.approx(tau_m2)


def test_bind_beam_tau_ungrouped_is_bit_identical():
    """With no groups the hook reads each plane's own channel only (default)."""
    from custom_components.balcony_solar_forecast.core import shademap as sm

    c = _make_coordinator()  # default _site: M1, M2, ungrouped
    az, el, doy = 200.0, 40.0, 172
    state = ShademapState()
    for _ in range(400):
        state = sm.update_bin(
            state, channel="M1", sun_az=az, sun_el=el, doy=doy, measured_t=0.0
        )
    c._shademap_state = state
    beam_tau = c._bind_beam_tau()
    # M1 sees its learned bin; M2's pool is just ('M2',) -> exact prior.
    assert beam_tau("M1", az, el, doy, 1.0) < 0.2
    assert beam_tau("M2", az, el, doy, 1.0) == pytest.approx(1.0)


def test_bind_beam_tau_includes_legacy_group_channel():
    """A leftover v0.12.0 'south' group channel is pooled as a LEGACY source."""
    from custom_components.balcony_solar_forecast.core import shademap as sm

    c = _make_coordinator()
    c._site = _grouped_site()
    az, el, doy = 200.0, 40.0, 172
    # Only the LEGACY group channel 'south' carries the dark bin (no per-plane
    # data) — as a store already merged by the removed v0.12.0 migration would.
    state = ShademapState()
    for _ in range(400):
        state = sm.update_bin(
            state, channel="south", sun_az=az, sun_el=el, doy=doy, measured_t=0.0
        )
    c._shademap_state = state
    beam_tau = c._bind_beam_tau()
    # 'south' is present in state and is not a plane name -> folded into the pool
    # of BOTH members, so its evidence keeps counting.
    assert beam_tau("M1", az, el, doy, 1.0) < 0.2
    assert beam_tau("M2", az, el, doy, 1.0) < 0.2


def test_dissolution_reads_own_channel_only():
    """Ungrouping reads each plane's OWN channel again — the data is intact."""
    from custom_components.balcony_solar_forecast.core import shademap as sm

    c = _make_coordinator()  # ungrouped (dissolved) site: M1, M2
    az, el, doy = 200.0, 40.0, 172
    # Per-plane learning survived the (former) grouping untouched: M1 dark, M2
    # bright — because storage was always per plane, nothing was ever merged.
    state = ShademapState()
    for _ in range(400):
        state = sm.update_bin(
            state, channel="M1", sun_az=az, sun_el=el, doy=doy, measured_t=0.0
        )
    for _ in range(400):
        state = sm.update_bin(
            state, channel="M2", sun_az=az, sun_el=el, doy=doy, measured_t=1.0
        )
    c._shademap_state = state
    beam_tau = c._bind_beam_tau()
    # Each plane reads ONLY its own channel now: M1 dark, M2 bright.
    assert beam_tau("M1", az, el, doy, 0.5) < 0.2
    assert beam_tau("M2", az, el, doy, 0.5) > 0.8


def test_train_channel_writes_to_own_plane_channel(monkeypatch):
    """Nightly training of plane M1 stores under 'M1' (per plane), not 'south'."""
    from custom_components.balcony_solar_forecast.core.types import PlaneHourlyModeled

    c = _make_coordinator()
    c._site = _grouped_site()
    hkey = "2026-07-01T11:00:00+00:00"
    modeled = PlaneHourlyModeled(
        beam_wh={hkey: 100.0}, diffuse_wh={hkey: 20.0}, kc={hkey: 0.9}
    )
    measured_by_hour = {hkey: 65.0}
    monkeypatch.setattr(coord_mod.shademap_mod, "is_quasi_clear", lambda **k: True)
    monkeypatch.setattr(coord_mod.solpos, "sun_position", lambda *a: (200.0, 40.0))
    state, changed = c._train_channel(
        ShademapState(), "M1", modeled, measured_by_hour
    )
    assert changed is True
    assert set(state.channels) == {"M1"}


def test_train_shademap_writes_each_plane_own_channel(monkeypatch):
    """Both M1 and M2 training the same night land under their OWN channels."""
    from custom_components.balcony_solar_forecast.core.types import PlaneHourlyModeled

    c = _make_coordinator()
    c._site = _grouped_site()
    iso = "2026-07-01"
    hkey = "2026-07-01T11:00:00+00:00"
    modeled = PlaneHourlyModeled(
        beam_wh={hkey: 100.0}, diffuse_wh={hkey: 20.0}, kc={hkey: 0.9}
    )
    snap = IssuedSnapshot(
        issued_at="x", status="fresh",
        raw_hourly_wh={hkey: 1000.0},
        per_plane={"M1": modeled, "M2": modeled},
    ).to_dict()
    c._store.issued[iso] = snap
    # Measured >= 0.8 * modeled site total so the day-clear gate passes; each
    # module measures its own hour.
    c._store.hourly_actuals[iso] = {"M1": {hkey: 900.0}, "M2": {hkey: 900.0}}
    monkeypatch.setattr(coord_mod.shademap_mod, "is_quasi_clear", lambda **k: True)
    monkeypatch.setattr(coord_mod.solpos, "sun_position", lambda *a: (200.0, 40.0))

    c._train_shademap(iso, snap, c._store.actuals.get(iso))
    # Storage is per plane: two channels, each with its own single sample.
    assert set(c._shademap_state.channels) == {"M1", "M2"}
    assert next(iter(c._shademap_state.channels["M1"].values())).n == 1
    assert next(iter(c._shademap_state.channels["M2"].values())).n == 1


def test_build_shade_profile_passes_channel_and_pool(monkeypatch):
    """build_shade_profile renders the module's OWN channel + its read pool."""
    from custom_components.balcony_solar_forecast.core import shademap as sm

    c = _make_coordinator()
    c._site = _grouped_site()
    # Non-empty shademap so the slow layer is active (pool applies regardless).
    c._shademap_state = sm.update_bin(
        ShademapState(), channel="M1", sun_az=200.0, sun_el=40.0, doy=172,
        measured_t=0.0,
    )
    c._shade_profile_module = "M1"
    c._shade_profile_date = datetime(2026, 6, 21).date()
    captured = {}

    def _spy(**kw):
        captured["channel"] = kw["channel"]
        captured["pool"] = kw["pool"]
        return {}

    monkeypatch.setattr(coord_mod.shadeprofile_mod, "compute_shade_profile", _spy)
    c.build_shade_profile()
    # The MAIN curve uses the module's own channel; the pool adds its sibling.
    assert captured["channel"] == "M1"
    assert set(captured["pool"]) == {"M1", "M2"}
