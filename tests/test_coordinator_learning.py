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

import asyncio
from datetime import UTC, date, datetime, timedelta

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
    DAY_AHEAD_BIAS_RESEED_N,
    DAY_PART_AFTERNOON,
    DAY_PART_MIDDAY,
    DAY_PART_MORNING,
    DRIFT_LOSS_STREAK_DAYS,
    INTRADAY_MIN_TRAILING_MINUTES,
    INTRADAY_NEUTRAL,
    INTRADAY_TRAILING_WINDOW_MINUTES,
    INVERTER_CAL_MIN_SAMPLES,
    ISSUE_CONFIG_CHANGED_BIAS_RESEED,
    ISSUE_FAST_LEARNER_DISABLED,
    LABEL_FROZEN_STALE_SECONDS,
    LEARNER_LAYER_FAST,
    LEARNER_LAYER_SLOW,
    LEARNER_SNAPSHOT_RING,
    LEARNER_STATUS_ACTIVE,
    LEARNER_STATUS_FROZEN,
    RLS_INIT_COVARIANCE,
    RLS_MIN_SAMPLES,
    STATUS_FRESH,
    STATUS_PHYSICS_FALLBACK,
)
from custom_components.balcony_solar_forecast.coordinator import (  # noqa: E402
    BalconySolarCoordinator,
    _is_frozen_channel,
    _measured_power_rows,
    _usable_power,
)
from custom_components.balcony_solar_forecast.core import (  # noqa: E402
    LearnerHooks,
    solpos,
)
from custom_components.balcony_solar_forecast.core.bias import (  # noqa: E402
    compute_intraday_scalar,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasCell,
    BiasState,
    DriftState,
    ForecastResult,
    HorizonRow,
    InverterCalState,
    InverterGroup,
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

    async def async_add_executor_job(self, func, *args):
        """Run inline: the engine pass is pure CPU with no loop interaction,
        and the executor indirection must not change test-visible results."""
        return func(*args)


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

    # learning-health bookkeeping (SPEC §10): plain validated dict, no dataclass
    def get_learning_health(self) -> dict:
        return dict(getattr(self, "learning_health", {}))

    def set_learning_health(self, health: dict) -> None:
        self.learning_health = dict(health)

    def get_drift_state(self) -> DriftState:
        return DriftState.from_dict(self.drift)

    def set_drift_state(self, state) -> None:
        self.drift = state.to_dict()

    # inverter DC->AC efficiency calibration (AC-side Phase 3)
    def get_inverter_cal_state(self) -> InverterCalState:
        return InverterCalState.from_dict(getattr(self, "inverter_cal", {}))

    def set_inverter_cal_state(self, state) -> None:
        self.inverter_cal = state.to_dict()

    # config fingerprint the day-ahead bias was learned against (A4)
    def get_config_fingerprint(self):
        return getattr(self, "config_fingerprint", None)

    def set_config_fingerprint(self, fp) -> None:
        self.config_fingerprint = fp

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
    # summary): neutral empty ring, defaults.
    from custom_components.balcony_solar_forecast.const import (
        DEFAULT_SCOREBOARD_WINDOW_DAYS,
    )
    from custom_components.balcony_solar_forecast.core.types import ScoreboardState

    c._scoreboard_enabled = True
    c._scoreboard_window_days = DEFAULT_SCOREBOARD_WINDOW_DAYS
    c._scoreboard_state = ScoreboardState()
    # v0.4 quantile lane: enabled by default, empty ring (cold start -> neutral).
    from custom_components.balcony_solar_forecast.core.types import QuantileState

    c._quantiles_enabled = True
    c._quantile_state = QuantileState()
    # AC-side Phase 3 inverter calibration: neutral (untrusted) by default.
    c._inverter_cal_state = InverterCalState()
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


# ---------------------------------------------------------------------------
# Partial metering (SPEC §9.5/§9.1 Teilmengen-Regel, nightly path)
# ---------------------------------------------------------------------------


def _partial_metered_site() -> SiteConfig:
    """Two planes; only M1 carries a meter (actual_entity)."""
    return SiteConfig(
        latitude=48.5,
        longitude=12.2,
        planes=(
            PlaneConfig(name="M1", azimuth_deg=115.0, tilt_deg=70.0, wp=370.0,
                        actual_entity="sensor.m1"),
            PlaneConfig(name="M2", azimuth_deg=115.0, tilt_deg=70.0, wp=370.0),
        ),
        groups=(),
    )


def test_day_ahead_samples_restrict_modeled_to_metered_planes():
    """An unmetered plane must not inflate the modeled side of the RLS pair —
    otherwise theta learns the metering SHARE (~0.5 here) instead of the
    forecast error, pinned at the clamp floor every morning."""
    from custom_components.balcony_solar_forecast.core.types import (
        PlaneHourlyModeled,
    )

    c = _make_coordinator()
    c._site = _partial_metered_site()
    hkey = "2026-07-01T12:00:00+00:00"
    raw_hourly = {hkey: 200.0}  # site total: M1 100 + M2 (unmetered) 100
    snap = IssuedSnapshot(
        issued_at="x", status="fresh",
        raw_hourly_wh=raw_hourly,
        per_plane={
            "M1": PlaneHourlyModeled(
                beam_wh={hkey: 80.0}, diffuse_wh={hkey: 20.0}
            ),
            "M2": PlaneHourlyModeled(
                beam_wh={hkey: 80.0}, diffuse_wh={hkey: 20.0}
            ),
        },
    )
    samples = c._day_ahead_samples(
        raw_hourly, {"M1": 110.0}, snap, {hkey: 110.0}
    )
    assert len(samples) == 1
    assert samples[0].modeled_wh == pytest.approx(100.0)  # metered half only
    assert samples[0].measured_wh == pytest.approx(110.0)


def test_day_ahead_samples_skip_unmetered_without_per_plane_breakdown():
    """A partially metered site with a LEGACY snapshot (no per-plane
    breakdown) cannot scale the unmetered share out: skip the day rather
    than train the metering gap into theta."""
    c = _make_coordinator()
    c._site = _partial_metered_site()
    hkey = "2026-07-01T12:00:00+00:00"
    raw_hourly = {hkey: 200.0}
    snap = IssuedSnapshot(issued_at="x", status="fresh", raw_hourly_wh=raw_hourly)
    samples = c._day_ahead_samples(
        raw_hourly, {"M1": 100.0}, snap, {hkey: 100.0}
    )
    assert samples == []


def test_train_quantiles_day_restricts_modeled_to_metered_planes():
    """The quantile relerr is measured vs issued-CORRECTED — and the corrected
    side must be restricted to the SAME metered planes the measured sum covers
    (Teilmengen-Regel, SPEC §11.1/§9.1). An unmetered plane inside the modeled
    total otherwise reads as a permanent fractional deficit: every relerr (and
    with it P50) sinks by the metering share (here 0.5)."""
    from custom_components.balcony_solar_forecast.core.types import (
        PlaneHourlyModeled,
        QuantileState,
    )

    store = _FakeStore()
    iso = "2026-07-01"
    hkey = "2026-07-01T12:00:00+00:00"
    # Site total 200 Wh: M1 (metered) 100 + M2 (unmetered) 100.
    store.issued[iso] = IssuedSnapshot(
        issued_at="x", status="fresh",
        corrected_hourly_wh={hkey: 200.0},
        cloud_class_by_hour={hkey: CLOUD_CLASS_CLEAR},
        per_plane={
            "M1": PlaneHourlyModeled(
                beam_wh={hkey: 80.0}, diffuse_wh={hkey: 20.0}
            ),
            "M2": PlaneHourlyModeled(
                beam_wh={hkey: 80.0}, diffuse_wh={hkey: 20.0}
            ),
        },
    ).to_dict()
    store.record_hourly_actuals(iso, {"M1": {hkey: 90.0}})
    c = _make_coordinator(store)
    c._site = _partial_metered_site()

    c._train_quantiles_day(date(2026, 7, 1))

    assert c._quantile_state != QuantileState()
    (bin_key, entries), = c._quantile_state.bins.items()
    assert bin_key.startswith("clear|")
    assert entries[0][0] == iso
    # relerr = measured / METERED corrected = 90/100, not 90/200.
    assert entries[0][1] == pytest.approx(0.9)


def test_train_quantiles_day_skips_unmetered_without_per_plane_breakdown():
    """A partially metered site with a LEGACY snapshot (no per-plane
    breakdown) cannot scale the unmetered share out: skip the day rather than
    train the metering gap into the relerr ring — the same treatment
    ``day_ahead_samples`` gives it."""
    from custom_components.balcony_solar_forecast.core.types import (
        QuantileState,
    )

    store = _FakeStore()
    iso = "2026-07-01"
    hkey = "2026-07-01T12:00:00+00:00"
    store.issued[iso] = IssuedSnapshot(
        issued_at="x", status="fresh",
        corrected_hourly_wh={hkey: 200.0},
        cloud_class_by_hour={hkey: CLOUD_CLASS_CLEAR},
    ).to_dict()
    store.record_hourly_actuals(iso, {"M1": {hkey: 100.0}})
    c = _make_coordinator(store)
    c._site = _partial_metered_site()

    c._train_quantiles_day(date(2026, 7, 1))

    assert c._quantile_state == QuantileState()


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


# ---------------------------------------------------------------------------
# B2 (SCT-3): day-ahead trains on the SLOW-ONLY curve (fallback raw)
# ---------------------------------------------------------------------------


def test_day_ahead_trains_on_slow_only_not_raw():
    """The modeled side of the RLS is snap.slow_only_hourly_wh, not raw: a first
    RLS step from P0 lands theta ≈ measured/modeled, so measured==slow_only pins
    theta to ~1.0 (using raw 1000 would land it near 0.8)."""
    c = _make_coordinator()
    h = "2026-07-01T11:00:00+00:00"
    issued = IssuedSnapshot(
        issued_at="x", status="fresh",
        raw_hourly_wh={h: 1000.0},
        slow_only_hourly_wh={h: 800.0},  # shademap trimmed the raw curve
    ).to_dict()
    c._train_day_ahead("2026-07-01", issued, {"M1": 800.0})
    cell = next(iter(c._bias_state.cells.values()))
    assert cell.theta == pytest.approx(1.0, abs=0.05)


def test_day_ahead_falls_back_to_raw_when_slow_only_absent():
    """A legacy / slow-inactive snapshot has no slow_only curve -> raw is used, so
    the same measured 800 vs raw 1000 lands theta near 0.8."""
    c = _make_coordinator()
    h = "2026-07-01T11:00:00+00:00"
    issued = IssuedSnapshot(
        issued_at="x", status="fresh",
        raw_hourly_wh={h: 1000.0},
        slow_only_hourly_wh={},  # slow layer inactive / old snapshot
    ).to_dict()
    c._train_day_ahead("2026-07-01", issued, {"M1": 800.0})
    cell = next(iter(c._bias_state.cells.values()))
    assert cell.theta == pytest.approx(0.8, abs=0.05)


# ---------------------------------------------------------------------------
# A4 (FOR-4): config-fingerprint re-seed of the day-ahead bias cells
# ---------------------------------------------------------------------------


def _learned_bias_state() -> BiasState:
    """A bias state that looks steady-state: high n, tiny (converged) covariance."""
    return BiasState(
        cells={
            BiasState.cell_key("clear", DAY_PART_MIDDAY): BiasCell(
                theta=0.7, covariance=2e-8, n=100
            ),
            BiasState.cell_key("clear", DAY_PART_MORNING): BiasCell(
                theta=1.4, covariance=2e-8, n=90
            ),
        }
    )


def test_config_fingerprint_change_reseeds_cells(monkeypatch):
    """A differing stored fingerprint caps every cell's n, re-opens covariance to
    P0 and keeps theta; the new fingerprint is persisted and a repair issue fires."""
    store = _FakeStore()
    store.config_fingerprint = "stale000000000000"
    c = _make_coordinator(store)
    c._bias_state = _learned_bias_state()
    raised: list[str] = []
    monkeypatch.setattr(c, "_raise_repair_issue", lambda i: raised.append(i))

    c._reconcile_config_fingerprint()

    for cell in c._bias_state.cells.values():
        assert cell.n <= DAY_AHEAD_BIAS_RESEED_N
        assert cell.covariance == pytest.approx(RLS_INIT_COVARIANCE)
    # theta (the learned estimate) is preserved as the re-adaptation start point.
    mid = c._bias_state.cells[BiasState.cell_key("clear", DAY_PART_MIDDAY)]
    assert mid.theta == pytest.approx(0.7)
    # New fingerprint persisted; matches the live config.
    assert store.config_fingerprint == c._config_fingerprint()
    assert ISSUE_CONFIG_CHANGED_BIAS_RESEED in raised


def test_config_fingerprint_first_start_records_without_reseed(monkeypatch):
    """No stored fingerprint (fresh install / feature just landed): record the live
    one, DO NOT touch the cells, DO NOT raise an issue."""
    store = _FakeStore()  # config_fingerprint attribute absent -> getter None
    c = _make_coordinator(store)
    c._bias_state = _learned_bias_state()
    raised: list[str] = []
    monkeypatch.setattr(c, "_raise_repair_issue", lambda i: raised.append(i))

    c._reconcile_config_fingerprint()

    mid = c._bias_state.cells[BiasState.cell_key("clear", DAY_PART_MIDDAY)]
    assert mid.n == 100  # untouched
    assert mid.covariance == pytest.approx(2e-8)  # untouched
    assert store.config_fingerprint == c._config_fingerprint()
    assert raised == []


def test_config_fingerprint_unchanged_is_noop(monkeypatch):
    """A matching stored fingerprint leaves the cells and the store alone."""
    store = _FakeStore()
    c = _make_coordinator(store)
    store.config_fingerprint = c._config_fingerprint()
    c._bias_state = _learned_bias_state()
    raised: list[str] = []
    monkeypatch.setattr(c, "_raise_repair_issue", lambda i: raised.append(i))

    c._reconcile_config_fingerprint()

    mid = c._bias_state.cells[BiasState.cell_key("clear", DAY_PART_MIDDAY)]
    assert mid.n == 100
    assert mid.covariance == pytest.approx(2e-8)
    assert raised == []


def test_config_fingerprint_tracks_location():
    """SPEC §7.2/§7.7: lat/lon ARE forecast-relevant (they set the whole sun
    geometry the bias cells are conditioned on), so a location reconfigure must
    flip the fingerprint; a same-coordinates rebuild must not."""
    from dataclasses import replace

    c = _make_coordinator()
    base = c._config_fingerprint()

    c._site = replace(c._site, latitude=52.52)  # moved north
    assert c._config_fingerprint() != base

    c._site = _site()
    c._site = replace(c._site, longitude=13.4)  # moved east
    assert c._config_fingerprint() != base

    # Same coordinates, fresh object -> identical fingerprint, no reseed.
    c._site = _site()
    assert c._config_fingerprint() == base


def test_config_fingerprint_location_change_reseeds(monkeypatch):
    """End to end (SPEC §7.7): a fingerprint stored at the OLD location differs
    after a location reconfigure, so the reconcile re-seeds the bias cells,
    persists the new fingerprint and raises the repair issue; storing again at
    the SAME location is a no-op."""
    from dataclasses import replace

    store = _FakeStore()
    c = _make_coordinator(store)
    store.config_fingerprint = c._config_fingerprint()  # learned at old location
    c._bias_state = _learned_bias_state()
    raised: list[str] = []
    monkeypatch.setattr(c, "_raise_repair_issue", lambda i: raised.append(i))

    c._site = replace(c._site, latitude=52.52, longitude=13.4)  # reconfigure
    c._reconcile_config_fingerprint()

    mid = c._bias_state.cells[BiasState.cell_key("clear", DAY_PART_MIDDAY)]
    assert mid.n <= DAY_AHEAD_BIAS_RESEED_N
    assert store.config_fingerprint == c._config_fingerprint()
    assert ISSUE_CONFIG_CHANGED_BIAS_RESEED in raised

    # Reconcile again at the SAME location: nothing more happens.
    raised.clear()
    c._bias_state = _learned_bias_state()
    c._reconcile_config_fingerprint()
    mid = c._bias_state.cells[BiasState.cell_key("clear", DAY_PART_MIDDAY)]
    assert mid.n == 100
    assert raised == []


def test_config_fingerprint_tracks_relevant_fields_only():
    """The fingerprint moves for a forecast-relevant edit (azimuth, albedo, AC
    limit) but is INVARIANT to benign edits (entity id, shade group)."""
    from dataclasses import replace

    c = _make_coordinator()
    base = c._config_fingerprint()

    # Azimuth change -> different geometry -> different fingerprint.
    c._site = replace(
        c._site,
        planes=(replace(c._site.planes[0], azimuth_deg=118.0), c._site.planes[1]),
    )
    assert c._config_fingerprint() != base

    # Albedo change -> different diffuse term -> different fingerprint.
    c._site = _site()
    c._site = replace(c._site, albedo=0.3)
    assert c._config_fingerprint() != base

    # AC-limit change (a group) -> different clamp -> different fingerprint.
    c._site = _site()
    c._site = replace(
        c._site,
        groups=(InverterGroup(name="g1", plane_names=("M1", "M2"), ac_limit_w=800.0),),
    )
    assert c._config_fingerprint() != base

    # tau-only horizon edit (the operator's commonest A1 action: raise a screen's
    # transmittance instead of lowering elevation) reshapes the modeled beam and
    # MUST move the fingerprint even though az/elevation are unchanged.
    c._site = _site()
    hz_row = HorizonRow(azimuth_deg=150.0, elevation_deg=20.0, tau=0.0)
    c._site = replace(
        c._site,
        planes=(replace(c._site.planes[0], horizon=(hz_row,)), c._site.planes[1]),
    )
    tau0 = c._config_fingerprint()
    assert tau0 != base
    c._site = replace(
        c._site,
        planes=(
            replace(c._site.planes[0], horizon=(replace(hz_row, tau=0.4),)),
            c._site.planes[1],
        ),
    )
    assert c._config_fingerprint() != tau0

    # v0.22 inline elevation profile: ADDING tau_points to a row (the 0.22
    # tau_points migration, and every later knot edit) reshapes the modeled
    # beam +50-150 Wh/day mornings (ADR-2) while az/elevation/tau/seasonal stay
    # put, so it MUST flip the fingerprint or the automatic n-Deckelung never
    # fires and the learned bias goes stale against a shifted raw curve.
    c._site = _site()
    bare = HorizonRow(azimuth_deg=52.0, elevation_deg=10.0, tau=0.0)
    c._site = replace(
        c._site,
        planes=(replace(c._site.planes[0], horizon=(bare,)), c._site.planes[1]),
    )
    no_pts = c._config_fingerprint()
    profiled = replace(
        bare,
        tau_points=((4.5, 0.0), (5.5, 0.25), (6.5, 0.45), (8.0, 0.85), (9.5, 1.0)),
    )
    c._site = replace(
        c._site,
        planes=(replace(c._site.planes[0], horizon=(profiled,)), c._site.planes[1]),
    )
    with_pts = c._config_fingerprint()
    assert with_pts != no_pts
    # A knot-value edit (operator re-measures the crown) flips it again.
    edited = replace(
        profiled,
        tau_points=((4.5, 0.0), (5.5, 0.30), (6.5, 0.45), (8.0, 0.85), (9.5, 1.0)),
    )
    c._site = replace(
        c._site,
        planes=(replace(c._site.planes[0], horizon=(edited,)), c._site.planes[1]),
    )
    assert c._config_fingerprint() != with_pts
    # Adding the winter tau_points_bare profile is a distinct raw-curve change.
    seasonal = replace(
        profiled,
        seasonal=True,
        tau_bare=0.0,
        tau_leafed=0.0,
        tau_points_bare=((4.5, 0.1), (5.5, 0.4), (6.5, 0.6), (8.0, 0.9), (9.5, 1.0)),
    )
    c._site = replace(
        c._site,
        planes=(replace(c._site.planes[0], horizon=(seasonal,)), c._site.planes[1]),
    )
    seasonal_fp = c._config_fingerprint()
    seasonal_no_bare = replace(seasonal, tau_points_bare=None)
    c._site = replace(
        c._site,
        planes=(
            replace(c._site.planes[0], horizon=(seasonal_no_bare,)),
            c._site.planes[1],
        ),
    )
    assert c._config_fingerprint() != seasonal_fp

    # v0.22 per-row diffuse override: setting diffuse_tau on a wall row (the
    # ADR-3.5 campaign) lifts the modeled iso-diffuse floor +0.1-0.2 kWh/day
    # site-wide while az/elevation/tau are byte-identical -> it MUST flip the
    # fingerprint, or the automatic A4 n-Deckelung never fires and every
    # follower without a manual reset_day_ahead_bias runs stale bias against the
    # new raw physics. It is exactly the field class tau_points was added for.
    c._site = _site()
    wall = HorizonRow(azimuth_deg=195.0, elevation_deg=90.0, tau=0.0)
    c._site = replace(
        c._site,
        planes=(replace(c._site.planes[0], horizon=(wall,)), c._site.planes[1]),
    )
    no_diff = c._config_fingerprint()
    walled = replace(wall, diffuse_tau=0.5)
    c._site = replace(
        c._site,
        planes=(replace(c._site.planes[0], horizon=(walled,)), c._site.planes[1]),
    )
    with_diff = c._config_fingerprint()
    assert with_diff != no_diff
    # A diffuse_tau value edit (0.5 -> 0.4) is a distinct raw-curve change.
    c._site = replace(
        c._site,
        planes=(
            replace(c._site.planes[0], horizon=(replace(walled, diffuse_tau=0.4),)),
            c._site.planes[1],
        ),
    )
    assert c._config_fingerprint() != with_diff

    # bifacial beam-gain change (the A1 1.0->1.25 rollout via the options flow)
    # scales the direct-POA share site-wide -> different fingerprint.
    c._site = _site()
    c._site = replace(c._site, bifacial_beam_gain=1.25)
    assert c._config_fingerprint() != base

    # Benign edits (measured entity id, shade grouping) do NOT move it.
    c._site = _site()
    c._site = replace(
        c._site,
        planes=(
            replace(c._site.planes[0], actual_entity="sensor.renamed",
                    shade_group="balcony"),
            replace(c._site.planes[1], shade_group="balcony"),
        ),
    )
    assert c._config_fingerprint() == base


def _legacy_v021_site() -> SiteConfig:
    """A representative PRE-0.22 site: every horizon field the fingerprint hashed
    at v0.21.0 (azimuth/elevation/tau/seasonal/tau_leafed/tau_bare), plus albedo,
    bifacial_beam_gain, ross_coeff and a group — but NONE of the v0.22 additions
    (tau_points / tau_points_bare / diffuse_tau). Its fingerprint is the golden
    invariant below.
    """
    return SiteConfig(
        latitude=48.13,
        longitude=11.57,
        albedo=0.2,
        bifacial_beam_gain=1.0,
        planes=(
            PlaneConfig(
                name="M2", azimuth_deg=115.0, tilt_deg=70.0, wp=430.0,
                efficiency=0.96, ross_coeff=0.045,
                horizon=(
                    HorizonRow(azimuth_deg=52.0, elevation_deg=10.0, tau=0.0),
                    HorizonRow(
                        azimuth_deg=205.0, elevation_deg=90.0, tau=0.2,
                        seasonal=True, tau_leafed=0.2, tau_bare=0.5,
                    ),
                ),
                actual_entity="sensor.m2",
            ),
            PlaneConfig(
                name="M4", azimuth_deg=205.0, tilt_deg=80.0, wp=430.0,
                efficiency=0.96,
                horizon=(
                    HorizonRow(azimuth_deg=195.0, elevation_deg=90.0, tau=0.0),
                ),
                actual_entity="sensor.m4",
            ),
        ),
        groups=(
            InverterGroup(name="g1", plane_names=("M2", "M4"), ac_limit_w=800.0),
        ),
    )


def test_config_fingerprint_legacy_config_is_byte_stable():
    """GOLDEN: a config WITHOUT any v0.22 field hashes to the exact pinned value.

    The v0.22 fields (tau_points / tau_points_bare / diffuse_tau) are appended to
    the fingerprint's row segment ONLY when set (mirroring the nur-wenn-gesetzt
    to_dict rule), so a legacy row's hash input string is byte-for-byte the
    pre-0.22 string. This pins that: if the base format ever silently shifts, an
    upgrade would spontaneously re-seed every follower's day-ahead bias against an
    UNCHANGED raw curve (ADR §1: 'kein Spontan-Reseed beim Upgrade'). The golden
    was computed from the ``_config_fingerprint`` algorithm; it must only
    change on a DELIBERATE fingerprint-format bump (CLASSIFIER_VERSION or a
    documented schema change), never as a side effect of adding an optional field.

    Bumped 48f218a3ca86ee54 -> 4639a8c404bf8df6 exactly once, deliberately: the
    0.23.x review put lat/lon INTO the fingerprint (SPEC §7.7 — a location
    reconfigure must re-seed). That format bump re-seeds every existing install
    exactly once on upgrade — accepted and documented, the reseed keeps theta
    and only re-opens covariance (bias.reseed_day_ahead_bias).
    """
    c = _make_coordinator()
    c._site = _legacy_v021_site()
    assert c._config_fingerprint() == "4639a8c404bf8df6"


def test_config_fingerprint_legacy_bytes_ignore_v022_none_fields():
    """A legacy row and a row that merely DEFAULTS the v0.22 fields to None hash
    identically — proof the 'only-when-set' appends never fire for a None field
    (the mechanism the golden above relies on)."""
    from dataclasses import replace

    c = _make_coordinator()
    c._site = _legacy_v021_site()
    golden = c._config_fingerprint()
    # Re-materialise every row through ``replace`` (all v0.22 fields stay None):
    # the fingerprint must be unchanged.
    c._site = replace(
        c._site,
        planes=tuple(
            replace(p, horizon=tuple(replace(r) for r in p.horizon))
            for p in c._site.planes
        ),
    )
    assert c._config_fingerprint() == golden


async def test_async_reset_day_ahead_bias_clears_persists_and_refreshes():
    """The reset service backend clears every cell, persists the empty state and
    requests a recompute so the correction disappears at once (v0.19)."""
    store = _FakeStore()
    c = _make_coordinator(store)
    c._bias_state = BiasState(
        cells={
            BiasState.cell_key("clear", DAY_PART_MIDDAY): BiasCell(
                theta=0.6, covariance=1.0, n=8
            ),
            BiasState.cell_key("clear", DAY_PART_MORNING): BiasCell(
                theta=1.4, covariance=1.0, n=8
            ),
        }
    )
    refreshed: list[bool] = []

    async def _fake_refresh() -> None:
        refreshed.append(True)

    c.async_request_refresh = _fake_refresh  # type: ignore[method-assign]

    result = await c.async_reset_day_ahead_bias()

    assert result == {"cleared_cells": 2}
    assert c._bias_state.cells == {}
    assert store.get_bias_state().cells == {}  # persisted empty
    assert refreshed == [True]  # recompute requested once


async def test_async_reset_day_ahead_bias_empty_is_safe():
    """Resetting an already-empty state clears nothing and never raises."""
    c = _make_coordinator()

    async def _fake_refresh() -> None:
        return None

    c.async_request_refresh = _fake_refresh  # type: ignore[method-assign]
    assert await c.async_reset_day_ahead_bias() == {"cleared_cells": 0}


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
    coin-flips auto-disabling a layer over meaningless Wh (SPEC §9.8)."""
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
    """The sample's modeled_kc derives from the RAW curve, not the served one.

    With θ neutral (no ``_day_factor``) the modeled side is pure raw: it must
    NEVER read the served ``watts`` (which already carries beam_tau + the scalar).
    """
    c = _make_coordinator()
    # corrected 800 W, raw 400 W at noon; measured 400 W.
    result, start = _forecast_at_noon(800.0, raw_watts=400.0)
    c.hass.states.set("sensor.m1", 400.0, last_updated=start)
    c.hass.states.set("sensor.m2", 0.0, last_updated=start)
    sample = c._build_intraday_sample(result, start)
    assert sample is not None
    # ratio == measured/raw == 400/400 == 1.0 (NOT 400/800 == 0.5).
    assert sample.measured_kc / sample.modeled_kc == pytest.approx(1.0, rel=1e-6)


def test_intraday_sample_theta_referenced_no_double_correction():
    """A2: when the day-ahead θ fully explains the error (measured == raw × θ),
    the intraday ratio stays ~1.0 — θ and the scalar must not double-correct.

    Raw 400 W, θ 1.4 for the slot, measured 560 W == raw × θ. The modeled side is
    raw × θ == 560, so the ratio (the scalar's numerator/denominator) is 1.0. With
    the pre-A2 raw-only modeled side it would have been 560/400 == 1.4, stacking a
    second 1.4 on top of the θ already in the served curve.
    """
    c = _make_coordinator()
    result, start = _forecast_at_noon(400.0, raw_watts=400.0)
    c._day_factor = {start: 1.4}
    c.hass.states.set("sensor.m1", 560.0, last_updated=start)  # == 400 × 1.4
    c.hass.states.set("sensor.m2", 0.0, last_updated=start)
    sample = c._build_intraday_sample(result, start)
    assert sample is not None
    assert sample.measured_kc / sample.modeled_kc == pytest.approx(1.0, rel=1e-6)


def test_intraday_sample_theta_referenced_keeps_real_weather_signal():
    """A2: a REAL deviation on top of θ survives — measured == 1.4 × raw × θ makes
    the ratio 1.4, so a genuine under-forecast (e.g. 21.07.) is still caught.
    """
    c = _make_coordinator()
    result, start = _forecast_at_noon(400.0, raw_watts=400.0)
    c._day_factor = {start: 1.4}
    # measured = 1.4 × (raw 400 × θ 1.4) = 784 W.
    c.hass.states.set("sensor.m1", 784.0, last_updated=start)
    c.hass.states.set("sensor.m2", 0.0, last_updated=start)
    sample = c._build_intraday_sample(result, start)
    assert sample is not None
    assert sample.measured_kc / sample.modeled_kc == pytest.approx(1.4, rel=1e-6)


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
# SPEC §9.4 robustness: sun-elevation gate at sample creation
# ---------------------------------------------------------------------------


def _low_sun_slot_start(c) -> datetime:
    """A 15-min slot start whose MIDPOINT sun elevation is in (1.0, 4.5) deg:
    the sun IS up (clear-sky reference > 0, so pre-gate code WOULD build a
    sample here) but below the 5 deg INTRADAY_MIN_SUN_ELEVATION_DEG gate.
    solpos is pure, so the scan is deterministic; the slot-midpoint
    convention matches the coordinator's clear-sky reference.
    """
    day = datetime(2026, 7, 1, tzinfo=UTC)
    for minute in range(24 * 60):
        cand = day + timedelta(minutes=minute)
        _az, el = solpos.sun_position(
            cand + timedelta(minutes=7, seconds=30),
            c._site.latitude, c._site.longitude,
        )
        if 1.0 < el < 4.5:
            return cand
    raise AssertionError("no low-sun slot found on 2026-07-01")


def test_build_intraday_sample_none_below_min_sun_elevation():
    """SPEC §9.4: no sample while the sun is under 5 deg elevation — even with
    the sun up and the modeled energy far above the Wh gate."""
    c = _make_coordinator()
    start = _low_sun_slot_start(c)
    result = ForecastResult(
        slot_starts=(start,),
        total_watts=(400.0,),
        plane_results=(
            PlaneResult(name="M1", watts=(400.0,), raw_watts=(400.0,)),
        ),
        hourly_wh={start.isoformat(): 100.0},
        raw_total_watts=(400.0,),
        raw_hourly_wh={start.isoformat(): 100.0},
    )
    c.hass.states.set("sensor.m1", 200.0, last_updated=start)
    c.hass.states.set("sensor.m2", 0.0, last_updated=start)
    assert c._build_intraday_sample(result, start) is None


# ---------------------------------------------------------------------------
# SPEC §9.4 robustness: per-tick rate limit on the served scalar
# ---------------------------------------------------------------------------


def _prefill_intraday_ring(c, now: datetime, *, ratio: float) -> None:
    """Constant-ratio samples over the trailing window (coverage >= 120 min)."""
    for i in range(13):
        c._intraday_samples.append(
            coord_mod._IntradaySample(
                at=now - timedelta(minutes=15 * i),
                measured_kc=ratio,
                modeled_kc=1.0,
                modeled_wh=200.0,
            )
        )


def test_update_intraday_scalar_rate_limited_from_neutral_start():
    """SPEC §9.4: the served scalar moves at most 0.15 per tick (literal =
    INTRADAY_MAX_STEP_PER_TICK, so this file still imports against pre-limiter
    code for the rule-6 check); the reload start value is INTRADAY_NEUTRAL."""
    c = _make_coordinator()
    assert c._intraday_scalar == INTRADAY_NEUTRAL  # neutral start after reload
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    _prefill_intraday_ring(c, now, ratio=1.5)
    c._update_intraday_scalar(now)  # _last_result is None -> no new sample
    assert c._intraday_scalar == pytest.approx(1.15, rel=1e-9)


def test_update_intraday_scalar_rate_limited_down_step():
    c = _make_coordinator()
    c._intraday_scalar = 1.5  # served scalar from earlier ticks
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    _prefill_intraday_ring(c, now, ratio=1.0)
    c._update_intraday_scalar(now)
    assert c._intraday_scalar == pytest.approx(1.35, rel=1e-9)


def test_update_intraday_scalar_rate_limit_step_sequence():
    """Tick for tick: 1.15, 1.30, 1.45, then the full 1.5 — the limiter slows
    the correction but never sticks."""
    c = _make_coordinator()
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    _prefill_intraday_ring(c, now, ratio=1.5)
    seen = []
    for _ in range(4):
        c._update_intraday_scalar(now)
        seen.append(c._intraday_scalar)
    assert seen == pytest.approx([1.15, 1.30, 1.45, 1.5])


def test_update_intraday_scalar_small_delta_not_clipped():
    """A delta inside the 0.15 step passes whole — the limiter must not round
    small corrections away."""
    c = _make_coordinator()
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    _prefill_intraday_ring(c, now, ratio=1.1)
    c._update_intraday_scalar(now)
    assert c._intraday_scalar == pytest.approx(1.1, rel=1e-9)


# ---------------------------------------------------------------------------
# A7/SCT-2: intraday sample-ring re-arm after a restart/reload
# ---------------------------------------------------------------------------


def _forecast_window(now: datetime, n_slots: int, raw_w_per_plane: float):
    """A ForecastResult with ``n_slots`` back-to-back 15-min slots ending at
    ``now`` (the last slot starts at ``now``), each plane holding a constant raw
    watt curve so the modeled site total per slot is ``2 * raw_w_per_plane``.
    """
    slot = timedelta(minutes=15)
    starts = tuple(now - slot * (n_slots - 1 - i) for i in range(n_slots))
    m1 = tuple(raw_w_per_plane for _ in starts)
    m2 = tuple(raw_w_per_plane for _ in starts)
    total = tuple(a + b for a, b in zip(m1, m2, strict=True))
    result = ForecastResult(
        slot_starts=starts,
        total_watts=total,
        plane_results=(
            PlaneResult(name="M1", watts=m1, raw_watts=m1),
            PlaneResult(name="M2", watts=m2, raw_watts=m2),
        ),
        hourly_wh={},
        raw_total_watts=total,
        raw_hourly_wh={},
    )
    return result


def _stat_rows_seconds_epoch(start: datetime, end: datetime, mean_w: float):
    """Synthetic 5-min recorder stat rows with SECONDS-epoch ``start`` floats —
    exactly what the in-process ``statistics_during_period`` API returns (the
    historical epoch bug is seconds vs. milliseconds)."""
    rows = []
    step = timedelta(minutes=5)
    t = start
    while t < end:
        rows.append({"start": t.timestamp(), "mean": mean_w})  # epoch SECONDS
        t += step
    return rows


def test_measured_power_rows_parses_seconds_epoch():
    """The re-arm stat parser must read epoch SECONDS (not treat them as ms)."""
    t0 = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    rows = _stat_rows_seconds_epoch(t0, t0 + timedelta(minutes=15), 500.0)
    parsed = _measured_power_rows(rows)
    assert [w for _, w in parsed] == [500.0, 500.0, 500.0]
    # Timestamps land in 2026, not 1970 (the seconds-as-ms collapse bug).
    assert parsed[0][0] == t0
    assert all(dt.year == 2026 for dt, _ in parsed)


def test_measured_power_rows_skips_unusable():
    t0 = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    rows = [
        {"start": t0.timestamp(), "mean": None},          # no mean
        {"start": None, "mean": 100.0},                    # unparseable start
        {"start": t0.timestamp(), "mean": 250.0},          # good
    ]
    assert _measured_power_rows(rows) == [(t0, 250.0)]


def test_rearm_samples_from_seconds_epoch_rows_fill_ring_nonneutral():
    """Reconstruction from synthetic SECONDS-epoch stat rows fills the ring and
    yields an immediate NON-neutral scalar (measured == 1.5 × modeled)."""
    c = _make_coordinator()
    now = datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    # 17 slots => 08:00..11:00; modeled site total per slot = 2 × 400 = 800 W.
    result = _forecast_window(now, n_slots=17, raw_w_per_plane=400.0)
    c._last_result = result
    # Measured site total = 1200 W == 1.5 × 800 across the whole window.
    stat_rows = _stat_rows_seconds_epoch(
        now - timedelta(minutes=INTRADAY_TRAILING_WINDOW_MINUTES), now, 1200.0,
    )
    rows = _measured_power_rows(stat_rows)
    samples = c._rearm_samples_from_rows(result, rows, now)
    assert samples, "expected reconstructed samples"
    # Coverage spans at least the minimum trailing window (else scalar is gated).
    span = (max(s.at for s in samples) - min(s.at for s in samples))
    assert span.total_seconds() / 60.0 >= INTRADAY_MIN_TRAILING_MINUTES
    for s in samples:
        c._intraday_samples.append(s)
    scalar = compute_intraday_scalar(list(c._intraday_samples), now=now)
    assert scalar != INTRADAY_NEUTRAL
    assert scalar == pytest.approx(1.5, rel=1e-6)


def test_rearm_samples_drop_low_elevation_slots():
    """The re-arm mirrors the live elevation gate: the sub-5-deg slot never
    enters the ring, the later above-gate slots of the same morning do."""
    c = _make_coordinator()
    low = _low_sun_slot_start(c)
    now = low + timedelta(minutes=90)
    # 8 slots ending at ``now``: low-15 .. low+90, modeled site 2 x 400 W.
    result = _forecast_window(now, n_slots=8, raw_w_per_plane=400.0)
    c._last_result = result
    rows = _measured_power_rows(
        _stat_rows_seconds_epoch(
            now - timedelta(minutes=INTRADAY_TRAILING_WINDOW_MINUTES), now, 800.0,
        )
    )
    samples = c._rearm_samples_from_rows(result, rows, now)
    assert samples, "expected the above-gate morning slots to be reconstructed"
    assert low not in {s.at for s in samples}


def test_rearm_skips_current_slot_no_double_sample():
    """The slot CONTAINING ``now`` is not reconstructed, so a subsequent live
    tick (which samples ``now``) never duplicates it — all sample times unique."""
    c = _make_coordinator()
    now = datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    result = _forecast_window(now, n_slots=17, raw_w_per_plane=400.0)
    c._last_result = result
    rows = _measured_power_rows(
        _stat_rows_seconds_epoch(
            now - timedelta(minutes=INTRADAY_TRAILING_WINDOW_MINUTES), now, 1200.0,
        )
    )
    samples = c._rearm_samples_from_rows(result, rows, now)
    # No reconstructed sample carries the current slot's start.
    assert now not in {s.at for s in samples}
    for s in samples:
        c._intraday_samples.append(s)
    # A live tick then appends the current slot exactly once.
    c.hass.states.set("sensor.m1", 600.0, last_updated=now)
    c.hass.states.set("sensor.m2", 600.0, last_updated=now)
    c._update_intraday_scalar(now)
    ats = [s.at for s in c._intraday_samples]
    assert len(ats) == len(set(ats)), "duplicate sample timestamps after re-arm"
    assert ats.count(now) == 1


def test_rearm_scales_modeled_to_metered_planes_only():
    """On a PARTIALLY metered site the reconstructed modeled side must match the
    metered subset the site-total sensor actually sums. M1 metered, M2 not; a
    PERFECT forecast (measured == M1's modeled) must yield scalar 1.0, not 0.5
    (the whole-site modeled would halve it, floored at INTRADAY_SCALAR_MIN and
    served for hours after every reload)."""
    c = _make_coordinator()
    c._site = SiteConfig(
        latitude=48.5,
        longitude=12.2,
        planes=(
            PlaneConfig(name="M1", azimuth_deg=115.0, tilt_deg=70.0, wp=370.0,
                        actual_entity="sensor.m1"),
            PlaneConfig(name="M2", azimuth_deg=205.0, tilt_deg=70.0, wp=430.0,
                        actual_entity=None),  # NOT metered
        ),
        groups=(),
    )
    now = datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    # Each plane models 400 W; only M1 is metered, so a perfect forecast means
    # the site-total sensor reads M1's 400 W (not both planes' 800 W).
    result = _forecast_window(now, n_slots=17, raw_w_per_plane=400.0)
    c._last_result = result
    rows = _measured_power_rows(
        _stat_rows_seconds_epoch(
            now - timedelta(minutes=INTRADAY_TRAILING_WINDOW_MINUTES), now, 400.0,
        )
    )
    samples = c._rearm_samples_from_rows(result, rows, now)
    assert samples, "expected reconstructed samples"
    for s in samples:
        c._intraday_samples.append(s)
    scalar = compute_intraday_scalar(list(c._intraday_samples), now=now)
    assert scalar == pytest.approx(1.0, rel=1e-6)


def test_async_rearm_fills_ring_from_stubs():
    """End-to-end orchestration: fresh weather + resolvable sensor + stats ->
    ring populated (recorder/registry IO stubbed)."""
    c = _make_coordinator()
    now = datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    result = _forecast_window(now, n_slots=17, raw_w_per_plane=400.0)
    c._last_result = result
    c._measured_total_stat_id = lambda: "sensor.total"

    async def _fake_read(entity_id, when):
        assert entity_id == "sensor.total"
        return _measured_power_rows(
            _stat_rows_seconds_epoch(
                when - timedelta(minutes=INTRADAY_TRAILING_WINDOW_MINUTES),
                when, 1200.0,
            )
        )

    c._async_read_measured_total_stats = _fake_read
    asyncio.run(c._async_rearm_intraday_ring(now, STATUS_FRESH))
    assert c._intraday_samples
    scalar = compute_intraday_scalar(list(c._intraday_samples), now=now)
    assert scalar == pytest.approx(1.5, rel=1e-6)


def test_async_rearm_neutral_when_no_stats():
    """Missing recorder data -> ring stays empty -> scalar stays neutral."""
    c = _make_coordinator()
    now = datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    c._last_result = _forecast_window(now, n_slots=17, raw_w_per_plane=400.0)
    c._measured_total_stat_id = lambda: "sensor.total"

    async def _empty(entity_id, when):
        return []

    c._async_read_measured_total_stats = _empty
    asyncio.run(c._async_rearm_intraday_ring(now, STATUS_FRESH))
    assert not c._intraday_samples
    assert compute_intraday_scalar(list(c._intraday_samples), now=now) == (
        INTRADAY_NEUTRAL
    )


def test_async_rearm_neutral_when_sensor_missing():
    """No site-total sensor registered -> no reconstruction."""
    c = _make_coordinator()
    now = datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    c._last_result = _forecast_window(now, n_slots=17, raw_w_per_plane=400.0)
    c._measured_total_stat_id = lambda: None
    called = {"read": False}

    async def _read(entity_id, when):
        called["read"] = True
        return []

    c._async_read_measured_total_stats = _read
    asyncio.run(c._async_rearm_intraday_ring(now, STATUS_FRESH))
    assert not c._intraday_samples
    assert called["read"] is False


def test_async_rearm_skipped_when_weather_stale():
    """A physics-fallback (stale) weather image blocks reconstruction."""
    c = _make_coordinator()
    now = datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    c._last_result = _forecast_window(now, n_slots=17, raw_w_per_plane=400.0)
    called = {"stat_id": False}

    def _stat_id():
        called["stat_id"] = True
        return "sensor.total"

    c._measured_total_stat_id = _stat_id
    asyncio.run(c._async_rearm_intraday_ring(now, STATUS_PHYSICS_FALLBACK))
    assert not c._intraday_samples
    assert called["stat_id"] is False


def test_async_rearm_skipped_when_fast_disabled():
    """Fast learner off (drift-disabled) -> no reconstruction."""
    c = _make_coordinator()
    c._drift_state = DriftState(fast_disabled=True)
    now = datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    c._last_result = _forecast_window(now, n_slots=17, raw_w_per_plane=400.0)
    c._measured_total_stat_id = lambda: "sensor.total"
    asyncio.run(c._async_rearm_intraday_ring(now, STATUS_FRESH))
    assert not c._intraday_samples


def test_async_rearm_skipped_when_ring_already_primed():
    """A ring already carrying live samples is never re-filled (no double-fill)."""
    c = _make_coordinator()
    now = datetime(2026, 7, 1, 11, 0, tzinfo=UTC)
    c._last_result = _forecast_window(now, n_slots=17, raw_w_per_plane=400.0)
    c._measured_total_stat_id = lambda: "sensor.total"
    existing = _IntradaySampleForTest(now - timedelta(minutes=30))
    c._intraday_samples.append(existing)
    called = {"read": False}

    async def _read(entity_id, when):
        called["read"] = True
        return []

    c._async_read_measured_total_stats = _read
    asyncio.run(c._async_rearm_intraday_ring(now, STATUS_FRESH))
    assert list(c._intraday_samples) == [existing]
    assert called["read"] is False


class _IntradaySampleForTest:
    """Minimal stand-in with the attributes the ring/scalar read."""

    def __init__(self, at: datetime) -> None:
        self.at = at
        self.measured_kc = 1.0
        self.modeled_kc = 1.0
        self.modeled_wh = 100.0


# ---------------------------------------------------------------------------
# FIX-1: hooks wiring
# ---------------------------------------------------------------------------


async def test_compute_passes_learner_hooks(monkeypatch):
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
    await c._compute(_W(), now)
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


def test_build_learner_hooks_caches_day_factor(monkeypatch):
    """A2: the per-slot θ factor is cached on ``_day_factor`` (keyed by slot.start)
    so the intraday sampler can reference the θ-corrected modeled curve.
    """
    c = _make_coordinator()
    c._bias_state = BiasState(cells={"clear|midday": BiasCell(theta=1.4, n=99)})
    monkeypatch.setattr(
        coord_mod.bias_mod, "classify_cloud", lambda **kw: CLOUD_CLASS_CLEAR
    )
    monkeypatch.setattr(
        coord_mod.bias_mod, "day_ahead_factor_solar", lambda *a, **kw: 1.4
    )
    slot_start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

    class _Slot:
        start = slot_start
        ghi = 500.0
        cloud_low = cloud_mid = cloud_high = 0.0
        visibility_m = 20000.0

    class _W:
        slots = (_Slot(),)

    c._build_learner_hooks(_W(), slot_start)
    assert c._day_factor.get(slot_start) == pytest.approx(1.4)


def test_build_learner_hooks_day_factor_empty_when_inactive():
    """No bias cells -> day-ahead layer inactive -> cached θ is empty (sampler
    then falls back to raw, matching the served curve)."""
    c = _make_coordinator()
    c._day_factor = {datetime(2026, 1, 1, tzinfo=UTC): 9.9}  # stale, must clear

    class _W:
        slots = ()

    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    c._build_learner_hooks(_W(), now)
    assert c._day_factor == {}


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


# ---------------------------------------------------------------------------
# MED-1: energy_today headline vs the AC re-clamp (_dayahead_today_kwh)
# ---------------------------------------------------------------------------


def _one_slot_result(
    *,
    total_w: float,
    prereclamp_w: float | None,
    start: datetime,
    ceiling_w: float | None = None,
) -> ForecastResult:
    """A single-slot ForecastResult with an explicit pre-re-clamp total.

    ``prereclamp_w`` is ``corrected_unclamped_watts[0]``; pass None to leave the
    field empty (the legacy / older-cached case). ``ceiling_w`` is
    ``slot_ceilings[0]``; None leaves it empty (older result -> a clamped slot
    keeps the served ceiling).
    """
    return ForecastResult(
        slot_starts=(start,),
        total_watts=(total_w,),
        plane_results=(PlaneResult(name="M1", watts=(total_w,)),),
        hourly_wh={start.isoformat(): total_w * 0.25},
        corrected_unclamped_watts=() if prereclamp_w is None else (prereclamp_w,),
        slot_ceilings=() if ceiling_w is None else (ceiling_w,),
    )


def test_dayahead_today_clamped_slot_uses_ceiling_not_divided():
    """A slot where the re-clamp bit contributes the served ceiling UNCHANGED —
    the intraday factor never reached it, so dividing it out would understate."""
    c = _make_coordinator()
    c._intraday_scalar = 1.3  # fast learner active; factor at age 0 == 1.3
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    # Served 800 W (the ceiling); pre-re-clamp 1040 W == the factor really bit.
    result = _one_slot_result(total_w=800.0, prereclamp_w=1040.0, start=start)
    energy = c._dayahead_today_kwh(result, now=start)
    # Contributes the ceiling: 800 W * 0.25 h / 1000 == 0.2 kWh.
    assert energy == pytest.approx(0.2)
    # NOT the understated divided value (800 / 1.3 * 0.25 / 1000 == 0.154).
    assert energy != pytest.approx(0.154)


def test_dayahead_today_unclamped_slot_divides_factor_out():
    """An unclamped slot (no re-clamp gap) still strips the intraday factor by
    dividing, exactly as before MED-1."""
    c = _make_coordinator()
    c._intraday_scalar = 1.3
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    # prereclamp == served -> the factor reached the served value -> divide it out.
    result = _one_slot_result(total_w=800.0, prereclamp_w=800.0, start=start)
    energy = c._dayahead_today_kwh(result, now=start)
    assert energy == pytest.approx(round(800.0 / 1.3 * 0.25 / 1000.0, 3))


def test_dayahead_today_down_factor_unchanged():
    """factor <= 1 never lifts past the ceiling, so prereclamp == served every
    slot -> the divide path applies, headline unchanged from the legacy code."""
    c = _make_coordinator()
    c._intraday_scalar = 0.9  # down-correction; factor at age 0 == 0.9
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    result = _one_slot_result(total_w=800.0, prereclamp_w=800.0, start=start)
    energy = c._dayahead_today_kwh(result, now=start)
    assert energy == pytest.approx(round(800.0 / 0.9 * 0.25 / 1000.0, 3))


def test_dayahead_today_empty_prereclamp_falls_back_to_divide():
    """An older cached result with no corrected_unclamped_watts cannot tell a
    clamped slot apart, so it falls back to the legacy divide-always path."""
    c = _make_coordinator()
    c._intraday_scalar = 1.3
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    # total looks like a ceiling but the field is empty -> divide anyway.
    result = _one_slot_result(total_w=800.0, prereclamp_w=None, start=start)
    energy = c._dayahead_today_kwh(result, now=start)
    assert energy == pytest.approx(round(800.0 / 1.3 * 0.25 / 1000.0, 3))


def test_dayahead_today_no_groups_bit_identical_to_legacy():
    """A site with no inverter groups never clamps (prereclamp == served), so its
    headline equals the pre-MED-1 divide-always result bit-for-bit."""
    c = _make_coordinator()
    c._intraday_scalar = 1.3
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    served = 617.3
    result = _one_slot_result(total_w=served, prereclamp_w=served, start=start)
    energy = c._dayahead_today_kwh(result, now=start)
    # Legacy behaviour: divide the factor out of every current-day slot.
    assert energy == pytest.approx(round(served / 1.3 * 0.25 / 1000.0, 3))


def test_dayahead_today_clamped_high_scalar_uses_prereclamp_not_ceiling():
    """IRC-4/FOR-7: a re-clamped slot under a LARGE scalar whose day-ahead value
    lies BELOW the ceiling contributes ``prereclamp/factor``, not the served
    ceiling — the pre-fix keep-ceiling path ballooned the headline by the whole
    factor headroom (20.07. +3.27 kWh at scalar 2.355)."""
    c = _make_coordinator()
    c._intraday_scalar = 2.355  # factor at age 0 == 2.355 (within band)
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    # Day-ahead (scalar-free) power 500 W; the 2.355x scalar lifted it to 1177.5
    # W, re-clamped to the 800 W ceiling. The true day-ahead value is 500 W.
    result = _one_slot_result(
        total_w=800.0, prereclamp_w=1177.5, start=start, ceiling_w=800.0,
    )
    energy = c._dayahead_today_kwh(result, now=start)
    # Uses prereclamp/factor == 500 W -> 0.125 kWh (NOT the ballooned 0.2 kWh).
    assert energy == pytest.approx(round(500.0 * 0.25 / 1000.0, 3))
    assert energy != pytest.approx(0.2)


def test_dayahead_today_clamped_dayahead_curve_still_delivers_ceiling():
    """When the day-ahead curve ALONE would clamp (prereclamp/factor >= ceiling),
    the re-clamped slot still contributes the ceiling — the scalar-free value is
    capped there, so a genuinely clamped clear midday is unchanged."""
    c = _make_coordinator()
    c._intraday_scalar = 2.355
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    # Day-ahead value 800 W == the ceiling; scalar lifted it to 1884 W.
    # prereclamp/factor == 800 == ceiling -> min() keeps the ceiling.
    result = _one_slot_result(
        total_w=800.0, prereclamp_w=1884.0, start=start, ceiling_w=800.0,
    )
    energy = c._dayahead_today_kwh(result, now=start)
    assert energy == pytest.approx(round(800.0 * 0.25 / 1000.0, 3))  # 0.2


# ---------------------------------------------------------------------------
# AC-side headline: _dayahead_today_kwh_ac strips the factor identically
# ---------------------------------------------------------------------------


def _one_slot_ac_result(
    *,
    ac_w: float,
    ac_prereclamp_w: float | None,
    start: datetime,
    ac_ceiling_w: float | None = None,
    ac_p10_w: float | None = None,
) -> ForecastResult:
    """A single-slot ForecastResult carrying an explicit AC pre-clamp total.

    ``ac_prereclamp_w`` is ``ac_corrected_unclamped_watts[0]``; None leaves it
    empty (the legacy / older-cached case). ``ac_ceiling_w`` is
    ``ac_slot_ceilings[0]``; ``ac_p10_w`` is ``ac_p10_watts[0]`` (the served AC
    P10 band watts) — None leaves each field empty. The DC fields are filler.
    """
    return ForecastResult(
        slot_starts=(start,),
        total_watts=(ac_w,),
        plane_results=(PlaneResult(name="M1", watts=(ac_w,)),),
        hourly_wh={start.isoformat(): ac_w * 0.25},
        ac_watts=(ac_w,),
        ac_corrected_unclamped_watts=(
            () if ac_prereclamp_w is None else (ac_prereclamp_w,)
        ),
        ac_slot_ceilings=() if ac_ceiling_w is None else (ac_ceiling_w,),
        ac_p10_watts=() if ac_p10_w is None else (ac_p10_w,),
    )


def test_dayahead_today_ac_clamped_slot_uses_ceiling():
    """AC-side MED-1: a slot where the inverter AC clamp bit contributes the
    served ceiling UNCHANGED (the intraday factor never reached it)."""
    c = _make_coordinator()
    c._intraday_scalar = 1.3
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    # Served AC 800 W (the AC limit); pre-clamp 1040 W == the factor really bit.
    result = _one_slot_ac_result(ac_w=800.0, ac_prereclamp_w=1040.0, start=start)
    energy = c._dayahead_today_kwh_ac(result, now=start)
    assert energy == pytest.approx(0.2)  # 800 W * 0.25 h / 1000
    assert energy != pytest.approx(0.154)  # NOT the understated 800/1.3 divide


def test_dayahead_today_ac_unclamped_slot_divides_factor_out():
    """An unclamped AC slot strips the intraday factor by dividing, as before."""
    c = _make_coordinator()
    c._intraday_scalar = 1.3
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    result = _one_slot_ac_result(ac_w=720.0, ac_prereclamp_w=720.0, start=start)
    energy = c._dayahead_today_kwh_ac(result, now=start)
    assert energy == pytest.approx(round(720.0 / 1.3 * 0.25 / 1000.0, 3))


def test_dayahead_today_ac_empty_prereclamp_falls_back_to_divide():
    """No ac_corrected_unclamped_watts (older cached result) -> divide-always."""
    c = _make_coordinator()
    c._intraday_scalar = 1.3
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    result = _one_slot_ac_result(ac_w=800.0, ac_prereclamp_w=None, start=start)
    energy = c._dayahead_today_kwh_ac(result, now=start)
    assert energy == pytest.approx(round(800.0 / 1.3 * 0.25 / 1000.0, 3))


def test_dayahead_today_ac_clamped_high_scalar_uses_prereclamp_not_ceiling():
    """AC-side IRC-4/FOR-7: a clamped slot under a large scalar contributes the
    scalar-free ``prereclamp/factor`` capped at the AC ceiling, not the ballooned
    served ceiling."""
    c = _make_coordinator()
    c._intraday_scalar = 2.355
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    # Day-ahead AC 500 W, scalar -> 1177.5 W, AC-clamped to the 800 W limit.
    result = _one_slot_ac_result(
        ac_w=800.0, ac_prereclamp_w=1177.5, start=start, ac_ceiling_w=800.0,
    )
    energy = c._dayahead_today_kwh_ac(result, now=start)
    assert energy == pytest.approx(round(500.0 * 0.25 / 1000.0, 3))  # 0.125
    assert energy != pytest.approx(0.2)


# ---------------------------------------------------------------------------
# FOR-7 (B1): the DAILY AC P10 must not RISE under a high intraday scalar
# ---------------------------------------------------------------------------


def test_dayahead_ac_p10_strips_high_scalar():
    """A high scalar lifts the whole served band; the daily P10 aggregate divides
    the transient factor back out so it stays at its scalar-free value."""
    c = _make_coordinator()
    c._intraday_scalar = 2.0  # factor at age 0 == 2.0
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    # Central AC served 800 W (unclamped: prereclamp == served), P10 band 600 W
    # (both carry the 2.0x scalar). Scalar-free central == 400 W, so the P10
    # strip ratio is 0.5 -> daily P10 == 600 * 0.5 == 300 W.
    result = _one_slot_ac_result(
        ac_w=800.0, ac_prereclamp_w=800.0, start=start, ac_p10_w=600.0,
    )
    p10 = c._dayahead_today_kwh_ac_p10(result, now=start)
    assert p10 == pytest.approx(round(300.0 * 0.25 / 1000.0, 3))  # 0.075
    # NOT the scalar-inflated served band (600 W -> 0.15 kWh).
    assert p10 != pytest.approx(round(600.0 * 0.25 / 1000.0, 3))


def test_dayahead_ac_p10_keeps_down_correction():
    """A down-correction (factor < 1) keeps the served, scaled-down P10 band —
    min(1, scalar_free/served) == 1, so the conservative low band is preserved."""
    c = _make_coordinator()
    c._intraday_scalar = 0.8  # factor at age 0 == 0.8
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    result = _one_slot_ac_result(
        ac_w=640.0, ac_prereclamp_w=640.0, start=start, ac_p10_w=480.0,
    )
    p10 = c._dayahead_today_kwh_ac_p10(result, now=start)
    # ratio = (640/0.8)/640 == 1.25, min(1, 1.25) == 1 -> served band kept.
    assert p10 == pytest.approx(round(480.0 * 0.25 / 1000.0, 3))  # 0.12


def test_dayahead_ac_p10_none_without_band():
    """No AC band issued (ac_p10_watts empty) -> daily P10 is None."""
    c = _make_coordinator()
    c._intraday_scalar = 2.0
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    result = _one_slot_ac_result(ac_w=800.0, ac_prereclamp_w=800.0, start=start)
    assert c._dayahead_today_kwh_ac_p10(result, now=start) is None


def test_build_data_carries_ac_keys():
    """_build_data mirrors every DC headline/curve/roll-up key on the AC side."""
    c = _make_coordinator()  # tz = UTC, fast learner neutral (factor 1.0)
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    now = start + timedelta(minutes=1)
    result = ForecastResult(
        slot_starts=(start,),
        total_watts=(400.0,),
        plane_results=(PlaneResult(name="M1", watts=(400.0,)),),
        hourly_wh={start.isoformat(): 100.0},
        ac_watts=(360.0,),
        ac_hourly_wh={start.isoformat(): 90.0},
        ac_corrected_unclamped_watts=(360.0,),
    )
    data = c._build_data(result, dict(result.hourly_wh), now, "fresh", timedelta(minutes=1))
    # DC keys unchanged (the model-internal truth).
    assert data["power_now_w"] == pytest.approx(400.0)
    assert data["energy_today_kwh"] == pytest.approx(0.1)
    # AC keys mirror them from the ac_* fields.
    assert data["power_now_w_ac"] == pytest.approx(360.0)
    assert data["energy_today_kwh_ac"] == pytest.approx(0.09)
    assert data["watts_ac"] == {start.isoformat(): 360.0}
    assert data["wh_period_ac"] == {start.isoformat(): 90.0}
    assert data["hourly_wh_ac"] == {start.isoformat(): 90.0}
    assert data["daily_kwh_ac"] == {start.date().isoformat(): pytest.approx(0.09)}
    # Tomorrow / d2 AC roll-ups: no slots there.
    assert data["energy_tomorrow_kwh_ac"] is None
    assert data["energy_d2_kwh_ac"] is None


def test_learner_status_layer_strings():
    """_learner_status returns the layer-keyed ENUM strings (coordinator:674)."""
    c = _make_coordinator()
    status = c._learner_status()
    assert status[LEARNER_LAYER_FAST] == LEARNER_STATUS_ACTIVE
    assert status[LEARNER_LAYER_SLOW] == LEARNER_STATUS_ACTIVE


def test_learner_status_day_ahead_cold_start_without_cells():
    """Day-ahead with NO learned cells reports cold_start, not active: the
    correction hook is gated on ``bool(cells)`` and applies nothing, so
    "active" would claim a correction that does not exist (v0.19.2). With a
    trained cell present it flips to active."""
    from custom_components.balcony_solar_forecast.const import (
        LEARNER_STATUS_COLD_START,
    )

    c = _make_coordinator()  # _bias_state = BiasState() (no cells)
    assert c._learner_status()["day_ahead"] == LEARNER_STATUS_COLD_START

    c._bias_state = BiasState(
        cells={BiasState.cell_key("clear", DAY_PART_MIDDAY): BiasCell(theta=0.9, n=5)}
    )
    assert c._learner_status()["day_ahead"] == LEARNER_STATUS_ACTIVE


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


def test_actuals_from_stats_accepts_float_epoch_seconds_rows():
    """REGRESSION (v0.19.2): the in-process ``statistics_during_period`` API
    returns row ``start`` as float epoch SECONDS. Parsing them as epoch-ms
    collapsed all 24 hourly rows of a day onto ONE 1970 hour key, so
    ``covered_hours == 1`` and the completeness gate discarded EVERY day —
    silently starving all nightly learning (live incident 2026-07-12..15:
    "module M1 covers only 1 of ~16 daylight hours"). Float-seconds rows must
    yield one distinct hour key per row and pass the gate."""
    from custom_components.balcony_solar_forecast.coordinator import (
        _actuals_from_stats,
    )

    hours = [(h, 100.0 + 10.0 * h) for h in range(6, 18)]  # 12 daylight hours
    rows = [
        {"mean": w, "start": datetime(2026, 6, 20, h, 0, tzinfo=UTC).timestamp()}
        for h, w in hours
    ]  # float SECONDS — the real in-process recorder format
    daily, hourly = _actuals_from_stats(
        {"sensor.m1": rows, "sensor.m2": rows},
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=datetime(2026, 6, 20).date(),
    )
    assert set(daily) == {"M1", "M2"}
    assert len(hourly["M1"]) == 12  # 12 DISTINCT hour keys, not 1
    # Keys are real 2026 hours, not 1970 artifacts.
    assert all(k.startswith("2026-06-20T") for k in hourly["M1"])
    assert daily["M1"] == pytest.approx(sum(w for _h, w in hours), abs=0.1)


def test_actuals_from_stats_accepts_epoch_ms_rows():
    """Epoch-MILLISECOND numeric rows (the WebSocket wire format) keep working
    via the magnitude heuristic (> 1e11 ⇒ ms)."""
    from custom_components.balcony_solar_forecast.coordinator import (
        _actuals_from_stats,
    )

    hours = [(h, 100.0 + 10.0 * h) for h in range(6, 18)]
    rows = [
        {
            "mean": w,
            "start": datetime(2026, 6, 20, h, 0, tzinfo=UTC).timestamp() * 1000.0,
        }
        for h, w in hours
    ]
    daily, hourly = _actuals_from_stats(
        {"sensor.m1": rows, "sensor.m2": rows},
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=datetime(2026, 6, 20).date(),
    )
    assert len(hourly["M1"]) == 12
    assert all(k.startswith("2026-06-20T") for k in hourly["M1"])


def test_stat_row_hour_key_seconds_ms_datetime_agree():
    """One instant, three formats (aware datetime / float s / float ms) — all
    must map to the SAME ISO-UTC hour key."""
    from custom_components.balcony_solar_forecast._actuals import (
        _stat_row_hour_key,
    )

    instant = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
    expected = "2026-06-20T09:00:00+00:00"
    assert _stat_row_hour_key(instant) == expected
    assert _stat_row_hour_key(instant.timestamp()) == expected
    assert _stat_row_hour_key(instant.timestamp() * 1000.0) == expected
    assert _stat_row_hour_key(None) is None
    assert _stat_row_hour_key("garbage") is None


def test_actuals_from_stats_zero_row_module_discards_day():
    """A configured module with NO LTS rows is a channel dropout: the whole day
    is discarded — a partial-site measurement must never train against the
    full-site model (SPEC §9.8: Messkanal-Dropout => ganzen Tag verwerfen)."""
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


def test_actuals_from_stats_kw_scaled_channel_discards_day():
    """SPEC §10 plausibility gate: a sustained hourly mean above
    CHANNEL_PLAUSIBILITY_MAX_WP_FRAC x the module's configured Wp is physically
    impossible — the channel is mis-scaled (the classic kW-instead-of-W
    sensor) and the whole day is discarded for learning AND scoring, with the
    reason recorded for diagnostics (mirrors the bootstrap-side gate)."""
    from custom_components.balcony_solar_forecast.coordinator import (
        _actuals_from_stats,
    )

    good = [(h, 100.0 + h) for h in range(6, 18)]
    poisoned = [(h, 100.0 + h) for h in range(6, 18)]
    poisoned[5] = (11, 5000.0)  # 5 kW sustained on a 400 Wp module
    stats = {"sensor.m1": _lts_rows(good), "sensor.m2": _lts_rows(poisoned)}
    dropout: dict = {}
    daily, hourly = _actuals_from_stats(
        stats,
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=datetime(2026, 6, 20).date(),
        dropout_out=dropout,
        wp_by_module={"M1": 400.0, "M2": 400.0},
    )
    assert daily == {} and hourly == {}
    assert dropout["reason"] == "implausible_channel"
    assert dropout["modules"] == ["M2"]


def test_actuals_from_stats_reading_at_plausibility_bound_accepted():
    """The gate fires ABOVE 1.25 x Wp, not at it (cloud-edge headroom)."""
    from custom_components.balcony_solar_forecast.coordinator import (
        _actuals_from_stats,
    )

    good = [(h, 100.0 + h) for h in range(6, 18)]
    at_bound = [(h, 100.0 + h) for h in range(6, 18)]
    at_bound[5] = (11, 500.0)  # exactly 1.25 x 400 Wp
    stats = {"sensor.m1": _lts_rows(good), "sensor.m2": _lts_rows(at_bound)}
    daily, hourly = _actuals_from_stats(
        stats,
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=datetime(2026, 6, 20).date(),
        wp_by_module={"M1": 400.0, "M2": 400.0},
    )
    assert set(daily) == {"M1", "M2"}
    assert len(hourly["M2"]) == 12


def test_actuals_from_stats_plausibility_gate_inert_without_wp():
    """Legacy callers without ``wp_by_module`` keep the pre-gate behaviour —
    the gate is opt-in via the site-aware caller (``async_read_actuals``)."""
    from custom_components.balcony_solar_forecast.coordinator import (
        _actuals_from_stats,
    )

    good = [(h, 100.0 + h) for h in range(6, 18)]
    poisoned = [(h, 100.0 + h) for h in range(6, 18)]
    poisoned[5] = (11, 5000.0)  # implausible, but no Wp context given
    stats = {"sensor.m1": _lts_rows(good), "sensor.m2": _lts_rows(poisoned)}
    daily, hourly = _actuals_from_stats(
        stats,
        {"M1": "sensor.m1", "M2": "sensor.m2"},
        expected_daylight_hours=12,
        day=datetime(2026, 6, 20).date(),
    )
    assert set(daily) == {"M1", "M2"}
    assert len(hourly["M2"]) == 12


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


def test_day_is_measured_clear_compares_metered_subset():
    """On a partially metered site the gate must compare the measured sum
    against the modeled energy of the METERED planes only — an unmetered
    plane contributing >20 % of the modeled total must not read as an
    overcast bust and block shademap training."""
    from custom_components.balcony_solar_forecast.core.types import (
        PlaneHourlyModeled,
    )

    c = _make_coordinator()
    c._site = _partial_metered_site()
    iso = "2026-07-01"
    hkey = "2026-07-01T11:00:00+00:00"
    snap = IssuedSnapshot(
        issued_at="x", status="fresh",
        raw_hourly_wh={hkey: 1000.0},  # site total: M1 400 + M2 (unmetered) 600
        per_plane={
            "M1": PlaneHourlyModeled(
                beam_wh={hkey: 320.0}, diffuse_wh={hkey: 80.0}
            ),
            "M2": PlaneHourlyModeled(
                beam_wh={hkey: 480.0}, diffuse_wh={hkey: 120.0}
            ),
        },
    )
    # 380 >= 0.8 * 400 (metered modeled) -> accept; against the FULL 1000 the
    # gate would reject (380 < 800).
    hourly_actuals = {"M1": {hkey: 380.0}}
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
# Shade groups (SPEC §9.2): READ-TIME pooling. Storage is ALWAYS per plane; a
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


# ---------------------------------------------------------------------------
# AC-side Phase 3: inverter DC->AC efficiency site calibration
# ---------------------------------------------------------------------------


def _ac_meter_site(*, invert: bool = False) -> SiteConfig:
    """A grouped site with a whole-site AC meter (ceiling 800 W)."""
    return SiteConfig(
        latitude=48.5,
        longitude=12.2,
        planes=(
            PlaneConfig(name="M1", azimuth_deg=115.0, tilt_deg=70.0, wp=370.0,
                        actual_entity="sensor.m1"),
            PlaneConfig(name="M2", azimuth_deg=205.0, tilt_deg=70.0, wp=430.0,
                        actual_entity="sensor.m2"),
        ),
        groups=(
            InverterGroup(name="WR", plane_names=("M1", "M2"), ac_limit_w=800.0),
        ),
        ac_actual_entity="sensor.site_ac",
        ac_actual_invert=invert,
    )


# --- coordinator hook binding ----------------------------------------------


def test_hooks_carry_learned_eta_when_trusted():
    c = _make_coordinator()
    c._inverter_cal_state = InverterCalState(eta=0.93, n=INVERTER_CAL_MIN_SAMPLES)

    class _W:
        slots = ()

    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    hooks = c._build_learner_hooks(_W(), now)
    assert hooks.inverter_efficiency == pytest.approx(0.93)


def test_hooks_no_learned_eta_when_untrusted():
    c = _make_coordinator()
    c._inverter_cal_state = InverterCalState(
        eta=0.93, n=INVERTER_CAL_MIN_SAMPLES - 1
    )

    class _W:
        slots = ()

    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    hooks = c._build_learner_hooks(_W(), now)
    assert hooks.inverter_efficiency is None


# --- nightly calibration sweep ---------------------------------------------


async def test_nightly_inverter_cal_folds_eligible_hours():
    c = _make_coordinator()
    c._site = _ac_meter_site()
    iso = "2026-07-05"
    day = datetime(2026, 7, 5).date()
    # Summed per-module DC hourly actuals (one channel carrying the site total).
    c._store.hourly_actuals[iso] = {
        "M1": {
            "2026-07-05T10:00:00+00:00": 500.0,  # eligible
            "2026-07-05T11:00:00+00:00": 520.0,  # eligible
            "2026-07-05T12:00:00+00:00": 900.0,  # clipped (dc-derived AC > 720)
            "2026-07-05T13:00:00+00:00": 50.0,   # below the 100 W min load
        }
    }
    ac_hourly = {
        "2026-07-05T10:00:00+00:00": 480.0,   # ratio 0.96
        "2026-07-05T11:00:00+00:00": 499.2,   # ratio 0.96
        "2026-07-05T12:00:00+00:00": 760.0,
        "2026-07-05T13:00:00+00:00": 48.0,
    }

    async def _fake_read(_day):
        return ac_hourly

    c._async_read_ac_actuals = _fake_read
    await c._train_inverter_cal(day)

    # Only the two eligible (unclipped, above min-load) hours folded.
    assert c._inverter_cal_state.n == 2
    assert c._inverter_cal_state.eta == pytest.approx(0.96)
    # Persisted through the store setter.
    assert c._store.get_inverter_cal_state().n == 2


async def test_nightly_inverter_cal_noop_without_ac_entity():
    c = _make_coordinator()  # _site() has no ac_actual_entity
    day = datetime(2026, 7, 5).date()
    before = c._inverter_cal_state
    called = {"read": False}

    async def _fake_read(_day):
        called["read"] = True
        return {"2026-07-05T10:00:00+00:00": 480.0}

    c._async_read_ac_actuals = _fake_read
    await c._train_inverter_cal(day)
    assert c._inverter_cal_state is before   # unchanged
    assert called["read"] is False           # short-circuited before the read


async def test_nightly_inverter_cal_noop_when_all_hours_ineligible():
    c = _make_coordinator()
    c._site = _ac_meter_site()
    iso = "2026-07-05"
    day = datetime(2026, 7, 5).date()
    # Both hours below the min load -> no eligible ratio -> state unchanged.
    c._store.hourly_actuals[iso] = {
        "M1": {
            "2026-07-05T10:00:00+00:00": 40.0,
            "2026-07-05T11:00:00+00:00": 30.0,
        }
    }

    async def _fake_read(_day):
        return {
            "2026-07-05T10:00:00+00:00": 38.0,
            "2026-07-05T11:00:00+00:00": 28.0,
        }

    c._async_read_ac_actuals = _fake_read
    before = c._inverter_cal_state
    await c._train_inverter_cal(day)
    assert c._inverter_cal_state is before


async def test_nightly_inverter_cal_records_raw_ratio_when_out_of_band():
    """v0.20: gated but OUT-OF-BAND ratios (e.g. DC ports reading ~25 % high →
    true AC/DC ≈ 0.77 < INVERTER_CAL_MIN) fold NOTHING into the EMA, but the
    raw-ratio diagnostic records the evidence so the operator can see WHY the
    calibration refuses — the mis-scaled-sensor smoking gun."""
    c = _make_coordinator()
    c._site = _ac_meter_site()
    iso = "2026-07-05"
    day = datetime(2026, 7, 5).date()
    c._store.hourly_actuals[iso] = {
        "M1": {
            "2026-07-05T10:00:00+00:00": 500.0,
            "2026-07-05T11:00:00+00:00": 520.0,
        }
    }

    async def _fake_read(_day):
        return {
            "2026-07-05T10:00:00+00:00": 385.0,   # ratio 0.77
            "2026-07-05T11:00:00+00:00": 400.4,   # ratio 0.77
        }

    c._async_read_ac_actuals = _fake_read
    before = c._inverter_cal_state
    await c._train_inverter_cal(day)
    # EMA untouched (both ratios outside [INVERTER_CAL_MIN, INVERTER_CAL_MAX])...
    assert c._inverter_cal_state is before
    # ...but the raw evidence is recorded and rides the learned summary.
    raw = c._inverter_cal_raw
    assert raw["date"] == iso
    assert raw["median_ratio"] == pytest.approx(0.77, abs=1e-3)
    assert raw["n"] == 2
    assert raw["in_band_n"] == 0
    summ = c.inverter_efficiency_learned()
    assert summ["n"] == 0
    assert summ["raw"]["median_ratio"] == pytest.approx(0.77, abs=1e-3)


async def test_nightly_inverter_cal_read_exception_is_contained():
    c = _make_coordinator()
    c._site = _ac_meter_site()
    iso = "2026-07-05"
    day = datetime(2026, 7, 5).date()
    c._store.hourly_actuals[iso] = {
        "M1": {"2026-07-05T10:00:00+00:00": 500.0}
    }
    before = c._inverter_cal_state

    async def _boom(_day):
        raise RuntimeError("recorder down")

    c._async_read_ac_actuals = _boom
    # Must NOT raise; calibration left untouched.
    await c._train_inverter_cal(day)
    assert c._inverter_cal_state is before


async def test_nightly_eta_out_of_band_streak_raises_then_clears_issue():
    """η plausibility watchdog (SPEC §10): the nightly MEDIAN raw AC/DC ratio
    sitting outside [INVERTER_CAL_MIN, INVERTER_CAL_MAX] for three nights in a
    row raises the persistent ``eta_out_of_band`` repair issue — the
    mis-scaled-metering smoking gun the silent EMA-drop never shows — and the
    first in-band night clears it. Pure visibility: the calibration itself is
    untouched either way (out-of-band folds keep being refused)."""
    c = _make_coordinator()
    c._site = _ac_meter_site()
    raised: list[tuple[str, dict | None]] = []
    deleted: list[str] = []
    c._raise_repair_issue = lambda iid, ph=None: raised.append((iid, ph))
    c._delete_repair_issue = deleted.append

    async def _run_night(iso: str, ac10: float, ac11: float) -> None:
        c._store.hourly_actuals[iso] = {
            "M1": {
                f"{iso}T10:00:00+00:00": 500.0,
                f"{iso}T11:00:00+00:00": 520.0,
            }
        }
        c._store.issued[iso] = {"status": "ok"}  # a day we RAN (streak guard)
        ac = {f"{iso}T10:00:00+00:00": ac10, f"{iso}T11:00:00+00:00": ac11}

        async def _read(_day, _ac=ac):
            return _ac

        c._async_read_ac_actuals = _read
        await c._train_inverter_cal(datetime.fromisoformat(iso).date())

    # ratio 0.77 — DC ports reading ~25 % high — outside [0.90, 0.99].
    await _run_night("2026-07-05", 385.0, 400.4)
    await _run_night("2026-07-06", 385.0, 400.4)
    assert raised == []  # below the streak threshold: silent
    await _run_night("2026-07-07", 385.0, 400.4)
    assert [i for i, _ in raised] == ["eta_out_of_band"]
    assert raised[0][1]["days"] == "3"
    assert raised[0][1]["last_day"] == "2026-07-07"

    # Idempotent: re-training the same night (catch-up re-sweep) counts once.
    await _run_night("2026-07-07", 385.0, 400.4)
    assert len(raised) == 1

    # The first in-band night (ratio 0.96) resets the streak and clears the card.
    await _run_night("2026-07-08", 480.0, 499.2)
    assert deleted == ["eta_out_of_band"]


# --- AC-meter reader: sign inversion ---------------------------------------


def test_ac_hourly_from_stats_negates_when_inverted():
    from custom_components.balcony_solar_forecast._actuals import (
        _ac_hourly_from_stats,
    )

    rows = [
        {"start": "2026-07-05T10:00:00+00:00", "mean": -480.0},
        {"start": "2026-07-05T11:00:00+00:00", "mean": -500.0},
    ]
    got = _ac_hourly_from_stats(rows, invert=True)
    assert got["2026-07-05T10:00:00+00:00"] == pytest.approx(480.0)
    assert got["2026-07-05T11:00:00+00:00"] == pytest.approx(500.0)
    # Default: sign preserved.
    plain = _ac_hourly_from_stats(rows, invert=False)
    assert plain["2026-07-05T10:00:00+00:00"] == pytest.approx(-480.0)


async def test_async_read_ac_actuals_empty_entity_returns_empty():
    from custom_components.balcony_solar_forecast._actuals import (
        async_read_ac_actuals,
    )

    day = datetime(2026, 7, 5).date()
    assert await async_read_ac_actuals(None, None, day, UTC) == {}
    assert await async_read_ac_actuals(None, "", day, UTC) == {}


# --- SiteConfig round-trip: ac_actual_invert -------------------------------


def test_siteconfig_roundtrip_ac_actual_invert_true():
    from custom_components.balcony_solar_forecast.const import (
        CONF_AC_ACTUAL_INVERT,
    )

    d = _ac_meter_site(invert=True).to_dict()
    assert d[CONF_AC_ACTUAL_INVERT] is True
    assert SiteConfig.from_dict(d).ac_actual_invert is True


def test_siteconfig_default_invert_emits_no_key():
    from custom_components.balcony_solar_forecast.const import (
        CONF_AC_ACTUAL_INVERT,
    )

    d = _ac_meter_site(invert=False).to_dict()
    assert CONF_AC_ACTUAL_INVERT not in d  # only-when-set convention
    assert SiteConfig.from_dict(d).ac_actual_invert is False


# --- MeasuredAcPowerSensor: dashboard sign flip ----------------------------


def _measured_ac_sensor(coordinator, source_id: str):
    from custom_components.balcony_solar_forecast.sensor import (
        MeasuredAcPowerSensor,
    )

    s = MeasuredAcPowerSensor.__new__(MeasuredAcPowerSensor)
    s.hass = coordinator.hass
    s.coordinator = coordinator
    s._source_id = source_id
    s._value = None
    s._reporting = False
    return s


def test_measured_ac_sensor_negates_when_inverted():
    c = _make_coordinator()
    c._site = _ac_meter_site(invert=True)
    c.hass.states.set("sensor.site_ac", -480.0)
    s = _measured_ac_sensor(c, "sensor.site_ac")
    s._recompute()
    assert s.native_value == pytest.approx(480.0)  # negated to positive


def test_measured_ac_sensor_keeps_sign_when_not_inverted():
    c = _make_coordinator()
    c._site = _ac_meter_site(invert=False)
    c.hass.states.set("sensor.site_ac", 480.0)
    s = _measured_ac_sensor(c, "sensor.site_ac")
    s._recompute()
    assert s.native_value == pytest.approx(480.0)


# --- diagnostic summary ----------------------------------------------------


def test_inverter_efficiency_learned_summary():
    c = _make_coordinator()
    # Untrusted -> effective is None.
    c._inverter_cal_state = InverterCalState(eta=0.94, n=5)
    summ = c.inverter_efficiency_learned()
    assert summ == {"eta": 0.94, "n": 5, "effective": None}
    # Trusted -> effective is the clamped eta.
    c._inverter_cal_state = InverterCalState(
        eta=0.94, n=INVERTER_CAL_MIN_SAMPLES
    )
    summ = c.inverter_efficiency_learned()
    assert summ["effective"] == pytest.approx(0.94)
