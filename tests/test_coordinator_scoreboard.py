"""HA-harness tests for the coordinator's skill-scoreboard glue (v0.4).

Owner: scoreboard. These exercise the coordinator's LEAK-FREE IO around the pure
``core/scoreboard.py`` math: reading the engine forecast AS ISSUED from the
issued ring, the measured site energy from the actuals ring, and each configured
comparison entity's value AS IT STOOD during the scored day from the recorder
history — then persisting a DayScore into the rolling window. No full HA instance
is stood up; the coordinator is built via ``__new__`` (the same pattern as
test_coordinator_learning.py) and the recorder is faked.

Import is via ``custom_components.balcony_solar_forecast`` (the real HA-importing
package), so HA must be installed; the whole module is skipped otherwise.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    CLOUD_CLASS_CLEAR,
    CLOUD_CLASS_OVERCAST,
    DEFAULT_SCOREBOARD_GATE_MARGIN,
)
from custom_components.balcony_solar_forecast.coordinator import (  # noqa: E402
    BalconySolarCoordinator,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    ComparisonConfig,
    IssuedSnapshot,
    PlaneConfig,
    QuantileState,
    ScoreboardState,
    SiteConfig,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConfig:
    time_zone = "UTC"


class _FakeHass:
    def __init__(self) -> None:
        self.config = _FakeConfig()


class _FakeStore:
    """In-memory stand-in exposing the v1 rings + v3 scoreboard/comparison API."""

    def __init__(self) -> None:
        self.issued: dict[str, dict] = {}
        self.actuals: dict[str, dict] = {}
        self.hourly_actuals: dict[str, dict[str, dict[str, float]]] = {}
        self.scoreboard: dict = ScoreboardState().to_dict()
        self.comparison_ring: dict[str, dict[str, float]] = {}
        self.quantile: dict = QuantileState().to_dict()

    # v1 rings
    def get_issued(self, iso):
        return self.issued.get(iso)

    def get_actuals(self, iso):
        return self.actuals.get(iso)

    def get_hourly_actuals(self, iso):
        return self.hourly_actuals.get(iso)

    # v3 scoreboard state — matches the REAL store: takes a ScoreboardState.
    def get_scoreboard_state(self) -> ScoreboardState:
        return ScoreboardState.from_dict(self.scoreboard)

    def set_scoreboard_state(self, state: ScoreboardState) -> None:
        self.scoreboard = state.to_dict()

    # v3 comparison ring
    def get_comparison(self, iso):
        return self.comparison_ring.get(iso)

    def record_comparison(self, iso, per_comparison_kwh):
        self.comparison_ring[iso] = dict(per_comparison_kwh)

    # v3 quantile state — matches the REAL store: takes a QuantileState.
    def get_quantile_state(self) -> QuantileState:
        return QuantileState.from_dict(self.quantile)

    def set_quantile_state(self, state: QuantileState) -> None:
        self.quantile = state.to_dict()


class _FakeRecorderInstance:
    """Stands in for get_instance(hass): just runs the executor job inline."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _FakeHistoryState:
    def __init__(self, state: str, last_updated: datetime | None = None) -> None:
        self.state = state
        # ``None`` means "let _patch_recorder stamp an in-day timestamp" (the
        # common case: the state is a fresh in-day update). A test that wants to
        # exercise the stale-carry-in path passes an explicit pre-day-start
        # ``last_updated`` (which the freshness gate then rejects).
        self.last_updated = last_updated
        self._explicit_updated = last_updated is not None


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


def _make_coordinator(
    store: _FakeStore,
    comparisons: tuple[ComparisonConfig, ...] = (),
    *,
    window_days: int = 14,
) -> BalconySolarCoordinator:
    c = BalconySolarCoordinator.__new__(BalconySolarCoordinator)
    c.hass = _FakeHass()
    c._store = store
    c._site = _site()
    c._scoreboard_enabled = True
    c._scoreboard_window_days = window_days
    c._scoreboard_gate_margin = DEFAULT_SCOREBOARD_GATE_MARGIN
    c._comparisons = comparisons
    c._scoreboard_state = store.get_scoreboard_state()
    c._quantiles_enabled = True
    c._quantile_state = store.get_quantile_state()
    return c


def _issued_for_day(
    iso: str,
    *,
    corrected_hourly: dict[str, float],
    cloud_class_by_hour: dict[str, str] | None = None,
) -> dict:
    return IssuedSnapshot(
        issued_at=f"{iso}T00:00:00+00:00",
        status="fresh",
        raw_hourly_wh=dict(corrected_hourly),
        corrected_hourly_wh=dict(corrected_hourly),
        cloud_class_by_hour=cloud_class_by_hour or {},
    ).to_dict()


def _patch_recorder(monkeypatch, history_by_entity: dict[str, list]) -> None:
    """Patch the recorder get_instance + state_changes_during_period lookups.

    ``history_by_entity`` maps ``entity_id -> [_FakeHistoryState, ...]`` (or an
    empty list / absent key for a comparison with no usable state that day).
    """
    import homeassistant.components.recorder as rec_mod
    import homeassistant.components.recorder.history as hist_mod

    monkeypatch.setattr(
        rec_mod, "get_instance", lambda hass: _FakeRecorderInstance()
    )

    def _fake_changes(hass, start, end, entity_id, **kwargs):
        states = history_by_entity.get(entity_id, [])
        # Stamp any state without an explicit last_updated with an in-day time
        # (``start`` == the issue_at horizon, which is inside the scored day), so
        # the coordinator's freshness gate accepts a normal fresh update.
        for st in states:
            if not getattr(st, "_explicit_updated", False):
                st.last_updated = start
        return {entity_id: states}

    monkeypatch.setattr(hist_mod, "state_changes_during_period", _fake_changes)


# ---------------------------------------------------------------------------
# Comparison history is read and scored
# ---------------------------------------------------------------------------


def test_score_day_reads_comparison_history_and_scores(monkeypatch):
    store = _FakeStore()
    iso = "2026-07-01"
    day = date(2026, 7, 1)
    # Engine issued 10 kWh (corrected) across two clear hours; measured 9 kWh.
    hours = {
        "2026-07-01T10:00:00+00:00": 4000.0,
        "2026-07-01T11:00:00+00:00": 6000.0,
    }
    store.issued[iso] = _issued_for_day(
        iso,
        corrected_hourly=hours,
        cloud_class_by_hour={h: CLOUD_CLASS_CLEAR for h in hours},
    )
    store.actuals[iso] = {"M1": 4000.0, "M2": 5000.0}  # 9 kWh measured

    comparisons = (
        ComparisonConfig(name="8-Entry Baseline", daily_entity="sensor.base"),
        ComparisonConfig(name="Alt 1600W", daily_entity="sensor.alt"),
    )
    # Matched horizon (SPEC §9): the comparison is read at the engine's ~01:30
    # issue horizon = the FIRST usable state at/after the issue time, not the
    # settled end-of-day value. base's first usable row is 6.0 (the leading
    # 'unknown' is skipped, the later 12.0 is a mid-day refresh we must NOT
    # score); alt is 8.0.
    _patch_recorder(
        monkeypatch,
        {
            "sensor.base": [
                _FakeHistoryState("unknown"),
                _FakeHistoryState("6.0"),
                _FakeHistoryState("12.0"),
            ],
            "sensor.alt": [_FakeHistoryState("8.0")],
        },
    )

    c = _make_coordinator(store, comparisons)
    asyncio.run(c._score_scoreboard_day(day))

    st = store.get_scoreboard_state()
    assert iso in st.days
    ds = st.days[iso]
    assert ds.engine_kwh == pytest.approx(10.0)
    assert ds.measured_kwh == pytest.approx(9.0)
    assert ds.engine_daily_abs_err == pytest.approx(1.0)
    assert ds.weather_class == CLOUD_CLASS_CLEAR
    # Comparisons scored against measured (9): base |6-9|=3, alt |8-9|=1.
    assert ds.comparison_kwh["8-Entry Baseline"] == pytest.approx(6.0)
    assert ds.comparison_daily_abs_err["8-Entry Baseline"] == pytest.approx(3.0)
    assert ds.comparison_daily_abs_err["Alt 1600W"] == pytest.approx(1.0)
    # The read was cached in the comparison ring.
    assert store.comparison_ring[iso]["8-Entry Baseline"] == pytest.approx(6.0)


def test_missing_comparison_is_skipped_not_whole_day(monkeypatch):
    store = _FakeStore()
    iso = "2026-07-02"
    day = date(2026, 7, 2)
    hours = {"2026-07-02T11:00:00+00:00": 8000.0}
    store.issued[iso] = _issued_for_day(
        iso, corrected_hourly=hours,
        cloud_class_by_hour={h: CLOUD_CLASS_OVERCAST for h in hours},
    )
    store.actuals[iso] = {"M1": 8000.0}  # 8 kWh

    comparisons = (
        ComparisonConfig(name="present", daily_entity="sensor.present"),
        ComparisonConfig(name="gone", daily_entity="sensor.gone"),
    )
    # 'present' has a value; 'gone' has only unusable states -> skipped.
    _patch_recorder(
        monkeypatch,
        {
            "sensor.present": [_FakeHistoryState("7.5")],
            "sensor.gone": [
                _FakeHistoryState("unavailable"),
                _FakeHistoryState("unknown"),
            ],
        },
    )

    c = _make_coordinator(store, comparisons)
    asyncio.run(c._score_scoreboard_day(day))

    ds = store.get_scoreboard_state().days[iso]
    # The day is still scored (engine + measured present).
    assert ds.engine_kwh == pytest.approx(8.0)
    assert ds.weather_class == CLOUD_CLASS_OVERCAST
    # 'present' scored; 'gone' is ABSENT (skipped), never a fabricated zero.
    assert "present" in ds.comparison_daily_abs_err
    assert "gone" not in ds.comparison_daily_abs_err
    assert "gone" not in ds.comparison_kwh


def test_non_numeric_comparison_state_is_skipped(monkeypatch):
    store = _FakeStore()
    iso = "2026-07-03"
    day = date(2026, 7, 3)
    hours = {"2026-07-03T11:00:00+00:00": 5000.0}
    store.issued[iso] = _issued_for_day(iso, corrected_hourly=hours)
    store.actuals[iso] = {"M1": 5000.0}

    comparisons = (ComparisonConfig(name="c", daily_entity="sensor.c"),)
    _patch_recorder(
        monkeypatch, {"sensor.c": [_FakeHistoryState("not-a-number")]}
    )
    c = _make_coordinator(store, comparisons)
    asyncio.run(c._score_scoreboard_day(day))

    ds = store.get_scoreboard_state().days[iso]
    assert "c" not in ds.comparison_kwh


# ---------------------------------------------------------------------------
# Comparison ring caching (idempotence / no double recorder read)
# ---------------------------------------------------------------------------


def test_cached_comparison_ring_avoids_recorder(monkeypatch):
    store = _FakeStore()
    iso = "2026-07-04"
    day = date(2026, 7, 4)
    hours = {"2026-07-04T11:00:00+00:00": 6000.0}
    store.issued[iso] = _issued_for_day(iso, corrected_hourly=hours)
    store.actuals[iso] = {"M1": 6000.0}
    # Pre-seed the comparison ring so the recorder must NOT be consulted.
    store.comparison_ring[iso] = {"c": 5.5}

    comparisons = (ComparisonConfig(name="c", daily_entity="sensor.c"),)

    def _boom(*a, **k):
        raise AssertionError("recorder should not be read when ring is cached")

    import homeassistant.components.recorder as rec_mod

    monkeypatch.setattr(rec_mod, "get_instance", _boom)

    c = _make_coordinator(store, comparisons)
    asyncio.run(c._score_scoreboard_day(day))

    ds = store.get_scoreboard_state().days[iso]
    assert ds.comparison_kwh["c"] == pytest.approx(5.5)


# ---------------------------------------------------------------------------
# Idempotence + window trimming + guards
# ---------------------------------------------------------------------------


def test_score_day_idempotent_rescore(monkeypatch):
    store = _FakeStore()
    iso = "2026-07-05"
    day = date(2026, 7, 5)
    hours = {"2026-07-05T11:00:00+00:00": 7000.0}
    store.issued[iso] = _issued_for_day(iso, corrected_hourly=hours)
    store.actuals[iso] = {"M1": 7000.0}
    _patch_recorder(monkeypatch, {})

    c = _make_coordinator(store, ())
    asyncio.run(c._score_scoreboard_day(day))
    asyncio.run(c._score_scoreboard_day(day))  # re-run must be a stable no-op

    st = store.get_scoreboard_state()
    assert list(st.days) == [iso]
    assert st.days[iso].engine_kwh == pytest.approx(7.0)


def test_score_day_skips_when_actuals_missing(monkeypatch):
    store = _FakeStore()
    iso = "2026-07-06"
    day = date(2026, 7, 6)
    store.issued[iso] = _issued_for_day(
        iso, corrected_hourly={"2026-07-06T11:00:00+00:00": 5000.0}
    )
    # No actuals recorded for the day.
    _patch_recorder(monkeypatch, {})

    c = _make_coordinator(store, ())
    asyncio.run(c._score_scoreboard_day(day))
    assert store.get_scoreboard_state().days == {}


def test_score_day_disabled_is_noop(monkeypatch):
    store = _FakeStore()
    iso = "2026-07-07"
    day = date(2026, 7, 7)
    store.issued[iso] = _issued_for_day(
        iso, corrected_hourly={"2026-07-07T11:00:00+00:00": 5000.0}
    )
    store.actuals[iso] = {"M1": 5000.0}
    _patch_recorder(monkeypatch, {})

    c = _make_coordinator(store, ())
    c._scoreboard_enabled = False
    asyncio.run(c._score_scoreboard_day(day))
    assert store.get_scoreboard_state().days == {}


def test_window_trims_scoreboard_ring(monkeypatch):
    store = _FakeStore()
    _patch_recorder(monkeypatch, {})
    c = _make_coordinator(store, (), window_days=2)
    for d in range(1, 5):
        iso = f"2026-07-0{d}"
        day = date(2026, 7, d)
        store.issued[iso] = _issued_for_day(
            iso, corrected_hourly={f"{iso}T11:00:00+00:00": 5000.0}
        )
        store.actuals[iso] = {"M1": 5000.0}
        asyncio.run(c._score_scoreboard_day(day))
    # Only the newest 2 days survive the window trim.
    assert sorted(store.get_scoreboard_state().days) == ["2026-07-03", "2026-07-04"]


def test_engine_hourly_mae_from_hourly_actuals(monkeypatch):
    store = _FakeStore()
    iso = "2026-07-08"
    day = date(2026, 7, 8)
    hours = {
        "2026-07-08T10:00:00+00:00": 4000.0,
        "2026-07-08T11:00:00+00:00": 6000.0,
    }
    store.issued[iso] = _issued_for_day(iso, corrected_hourly=hours)
    store.actuals[iso] = {"M1": 4500.0, "M2": 4500.0}  # 9 kWh
    # Per-channel hourly actuals summing to a site hourly curve.
    store.hourly_actuals[iso] = {
        "M1": {
            "2026-07-08T10:00:00+00:00": 1800.0,
            "2026-07-08T11:00:00+00:00": 2700.0,
        },
        "M2": {
            "2026-07-08T10:00:00+00:00": 1800.0,
            "2026-07-08T11:00:00+00:00": 2700.0,
        },
    }
    _patch_recorder(monkeypatch, {})
    c = _make_coordinator(store, ())
    asyncio.run(c._score_scoreboard_day(day))

    ds = store.get_scoreboard_state().days[iso]
    # site hourly: 10:00 -> 3600 (issued 4000, |400|); 11:00 -> 5400 (issued 6000, |600|)
    assert ds.engine_hourly_mae == pytest.approx((400.0 + 600.0) / 2.0)


# ---------------------------------------------------------------------------
# Dominant weather class
# ---------------------------------------------------------------------------


def test_dominant_weather_class_is_the_mode(monkeypatch):
    store = _FakeStore()
    iso = "2026-07-09"
    day = date(2026, 7, 9)
    hours = {
        "2026-07-09T09:00:00+00:00": 1000.0,
        "2026-07-09T10:00:00+00:00": 1000.0,
        "2026-07-09T11:00:00+00:00": 1000.0,
    }
    # 2 overcast hours, 1 clear -> dominant overcast.
    store.issued[iso] = _issued_for_day(
        iso,
        corrected_hourly=hours,
        cloud_class_by_hour={
            "2026-07-09T09:00:00+00:00": CLOUD_CLASS_OVERCAST,
            "2026-07-09T10:00:00+00:00": CLOUD_CLASS_OVERCAST,
            "2026-07-09T11:00:00+00:00": CLOUD_CLASS_CLEAR,
        },
    )
    store.actuals[iso] = {"M1": 3000.0}
    _patch_recorder(monkeypatch, {})
    c = _make_coordinator(store, ())
    asyncio.run(c._score_scoreboard_day(day))
    assert store.get_scoreboard_state().days[iso].weather_class == CLOUD_CLASS_OVERCAST


# ---------------------------------------------------------------------------
# Quantile lane: nightly training populates the ring and yields a real band
# ---------------------------------------------------------------------------


def test_train_quantiles_day_populates_ring_and_yields_spread():
    from custom_components.balcony_solar_forecast.const import (
        QUANTILE_MIN_SAMPLES,
    )
    from custom_components.balcony_solar_forecast.core import quantiles as q
    from custom_components.balcony_solar_forecast.core.types import QuantileState

    store = _FakeStore()
    iso = "2026-07-10"
    day = date(2026, 7, 10)
    # One issued CORRECTED hour in the clear|midday bin (12:00 local == midday),
    # with per-plane hourly measured actuals for the same hour.
    hkey = "2026-07-10T12:00:00+00:00"
    store.issued[iso] = _issued_for_day(
        iso,
        corrected_hourly={hkey: 1000.0},
        cloud_class_by_hour={hkey: CLOUD_CLASS_CLEAR},
    )
    store.hourly_actuals[iso] = {"M1": {hkey: 1300.0}}  # relerr 1.3

    c = _make_coordinator(store, ())
    # Seed the same bin one sample short of the spread threshold with a spread of
    # distinct values on distinct PRIOR days, so the day's new sample crosses BOTH
    # QUANTILE_MIN_SAMPLES and the day-diversity gate (QUANTILE_MIN_DAYS) and the
    # band becomes non-collapsed (P10 != P90). Dates are inside the ring window.
    dp = q.QuantileState.bin_key(CLOUD_CLASS_CLEAR, "midday")
    seed = [
        [f"2026-06-{i + 1:02d}", 0.6 + 0.02 * i]
        for i in range(QUANTILE_MIN_SAMPLES - 1)
    ]
    c._quantile_state = QuantileState(bins={dp: list(seed)})

    c._train_quantiles_day(day)

    trained = store.get_quantile_state()
    assert len(trained.bins[dp]) == QUANTILE_MIN_SAMPLES  # the new sample landed
    band = q.bands_for_bin(
        trained, cloud_class=CLOUD_CLASS_CLEAR, day_part="midday"
    )
    assert band.n == QUANTILE_MIN_SAMPLES
    assert not band.collapsed
    assert band.p10 < band.p50 < band.p90  # a real, data-backed spread
