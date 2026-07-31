"""HA-harness tests for the coordinator's skill-scoreboard glue (v0.4).

Owner: scoreboard. These exercise the coordinator's LEAK-FREE IO around the pure
``core/scoreboard.py`` math: reading the engine forecast AS ISSUED from the
issued ring and the measured site energy from the actuals ring — then persisting
a DayScore into the rolling window. No full HA instance is stood up; the
coordinator is built via ``__new__`` (the same pattern as
test_coordinator_learning.py).

Import is via ``custom_components.balcony_solar_forecast`` (the real HA-importing
package), so HA must be installed; the whole module is skipped otherwise.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    CLOUD_CLASS_CLEAR,
    CLOUD_CLASS_OVERCAST,
)
from custom_components.balcony_solar_forecast.coordinator import (  # noqa: E402
    BalconySolarCoordinator,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
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
    """In-memory stand-in exposing the v1 rings + v3 scoreboard/quantile API."""

    def __init__(self) -> None:
        self.issued: dict[str, dict] = {}
        self.actuals: dict[str, dict] = {}
        self.hourly_actuals: dict[str, dict[str, dict[str, float]]] = {}
        self.scoreboard: dict = ScoreboardState().to_dict()
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

    # v3 quantile state — matches the REAL store: takes a QuantileState.
    def get_quantile_state(self) -> QuantileState:
        return QuantileState.from_dict(self.quantile)

    def set_quantile_state(self, state: QuantileState) -> None:
        self.quantile = state.to_dict()


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
    *,
    window_days: int = 14,
) -> BalconySolarCoordinator:
    c = BalconySolarCoordinator.__new__(BalconySolarCoordinator)
    c.hass = _FakeHass()
    c._store = store
    c._site = _site()
    c._scoreboard_enabled = True
    c._scoreboard_window_days = window_days
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


# ---------------------------------------------------------------------------
# Engine scoring (issued vs measured)
# ---------------------------------------------------------------------------


def test_score_day_scores_engine_against_measured():
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

    c = _make_coordinator(store)
    asyncio.run(c._score_scoreboard_day(day))

    st = store.get_scoreboard_state()
    assert iso in st.days
    ds = st.days[iso]
    assert ds.engine_kwh == pytest.approx(10.0)
    assert ds.measured_kwh == pytest.approx(9.0)
    assert ds.engine_daily_abs_err == pytest.approx(1.0)
    assert ds.weather_class == CLOUD_CLASS_CLEAR


# ---------------------------------------------------------------------------
# Idempotence + window trimming + guards
# ---------------------------------------------------------------------------


def test_score_day_idempotent_rescore():
    store = _FakeStore()
    iso = "2026-07-05"
    day = date(2026, 7, 5)
    hours = {"2026-07-05T11:00:00+00:00": 7000.0}
    store.issued[iso] = _issued_for_day(iso, corrected_hourly=hours)
    store.actuals[iso] = {"M1": 7000.0}

    c = _make_coordinator(store)
    asyncio.run(c._score_scoreboard_day(day))
    asyncio.run(c._score_scoreboard_day(day))  # re-run must be a stable no-op

    st = store.get_scoreboard_state()
    assert list(st.days) == [iso]
    assert st.days[iso].engine_kwh == pytest.approx(7.0)


def test_score_day_skips_when_actuals_missing():
    store = _FakeStore()
    iso = "2026-07-06"
    day = date(2026, 7, 6)
    store.issued[iso] = _issued_for_day(
        iso, corrected_hourly={"2026-07-06T11:00:00+00:00": 5000.0}
    )
    # No actuals recorded for the day.

    c = _make_coordinator(store)
    asyncio.run(c._score_scoreboard_day(day))
    assert store.get_scoreboard_state().days == {}


def test_score_day_garbage_numbers_leave_day_unscored():
    """A non-finite measured (or engine) number must NOT enter the ring:
    score_day returns None and the glue writes nothing — the old 0.0 clamp
    fabricated |engine - 0| as the engine's worst day into the scoreboard
    window (SPEC §15.2)."""
    store = _FakeStore()
    iso = "2026-07-08"
    day = date(2026, 7, 8)
    store.issued[iso] = _issued_for_day(
        iso, corrected_hourly={"2026-07-08T11:00:00+00:00": 5000.0}
    )
    store.actuals[iso] = {"M1": float("nan"), "M2": 5000.0}

    c = _make_coordinator(store)
    asyncio.run(c._score_scoreboard_day(day))
    assert store.get_scoreboard_state().days == {}
    # A NaN in the ISSUED curve is equally unscorable.
    store.issued[iso] = _issued_for_day(
        iso, corrected_hourly={"2026-07-08T11:00:00+00:00": float("nan")}
    )
    store.actuals[iso] = {"M1": 5000.0}
    asyncio.run(c._score_scoreboard_day(day))
    assert store.get_scoreboard_state().days == {}


def test_score_day_disabled_is_noop():
    store = _FakeStore()
    iso = "2026-07-07"
    day = date(2026, 7, 7)
    store.issued[iso] = _issued_for_day(
        iso, corrected_hourly={"2026-07-07T11:00:00+00:00": 5000.0}
    )
    store.actuals[iso] = {"M1": 5000.0}

    c = _make_coordinator(store)
    c._scoreboard_enabled = False
    asyncio.run(c._score_scoreboard_day(day))
    assert store.get_scoreboard_state().days == {}


def test_window_trims_scoreboard_ring():
    store = _FakeStore()
    c = _make_coordinator(store, window_days=2)
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


def test_engine_hourly_mae_from_hourly_actuals():
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
    c = _make_coordinator(store)
    asyncio.run(c._score_scoreboard_day(day))

    ds = store.get_scoreboard_state().days[iso]
    # site hourly: 10:00 -> 3600 (issued 4000, |400|); 11:00 -> 5400 (issued 6000, |600|)
    assert ds.engine_hourly_mae == pytest.approx((400.0 + 600.0) / 2.0)


# ---------------------------------------------------------------------------
# Dominant weather class
# ---------------------------------------------------------------------------


def test_dominant_weather_class_is_the_mode():
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
    c = _make_coordinator(store)
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

    c = _make_coordinator(store)
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


# ---------------------------------------------------------------------------
# Leakage guard + weather stratification
# ---------------------------------------------------------------------------


def test_snapshot_issued_after_cutoff_stays_unscored():
    """A snapshot issued at mid-day (startup catch-up recompute that already
    assimilated the day's weather) is a hindcast, not a day-ahead forecast:
    the day stays UNSCORED so it never flatters the engine (SPEC §15.2)."""
    store = _FakeStore()
    iso = "2026-07-05"
    day = date(2026, 7, 5)
    store.issued[iso] = _issued_for_day(
        iso, corrected_hourly={f"{iso}T11:00:00+00:00": 8000.0}
    )
    # Issued at 12:00 local — long after the 06:00 day-ahead cutoff.
    store.issued[iso]["issued_at"] = f"{iso}T12:00:00+00:00"
    store.actuals[iso] = {"M1": 8000.0}

    c = _make_coordinator(store)
    asyncio.run(c._score_scoreboard_day(day))

    assert store.get_scoreboard_state().days == {}


def test_issued_after_cutoff_treats_unparseable_stamp_as_valid():
    store = _FakeStore()
    c = _make_coordinator(store)
    snap = IssuedSnapshot(
        issued_at="", status="fresh",
        raw_hourly_wh={}, corrected_hourly_wh={},
    )
    assert c._issued_after_cutoff(snap, date(2026, 7, 5)) is False


def test_dominant_weather_class_skips_garbage_and_foreign_day():
    store = _FakeStore()
    iso = "2026-07-06"
    c = _make_coordinator(store)
    snap = IssuedSnapshot(
        issued_at=f"{iso}T00:00:00+00:00",
        status="fresh",
        raw_hourly_wh={f"{iso}T11:00:00+00:00": 8000.0},
        corrected_hourly_wh={f"{iso}T11:00:00+00:00": 8000.0},
        cloud_class_by_hour={
            "not-a-date": CLOUD_CLASS_OVERCAST,            # garbage: skipped
            "2026-07-07T11:00:00+00:00": CLOUD_CLASS_OVERCAST,  # other day
            f"{iso}T11:00:00+00:00": CLOUD_CLASS_CLEAR,
        },
    )
    assert c._dominant_weather_class(snap, iso) == CLOUD_CLASS_CLEAR


def test_dominant_weather_class_wh_less_snapshot_uses_hour_count():
    """A legacy snapshot without Wh curves still stratifies: the unweighted
    per-hour vote decides (never left unstratified, SPEC §15.2)."""
    store = _FakeStore()
    iso = "2026-07-06"
    c = _make_coordinator(store)
    snap = IssuedSnapshot(
        issued_at=f"{iso}T00:00:00+00:00",
        status="fresh",
        raw_hourly_wh={},
        corrected_hourly_wh={},
        cloud_class_by_hour={
            f"{iso}T08:00:00+00:00": CLOUD_CLASS_OVERCAST,
            f"{iso}T09:00:00+00:00": CLOUD_CLASS_OVERCAST,
            f"{iso}T10:00:00+00:00": CLOUD_CLASS_CLEAR,
        },
    )
    assert c._dominant_weather_class(snap, iso) == CLOUD_CLASS_OVERCAST


def test_dominant_weather_class_noncanonical_class_still_returned():
    store = _FakeStore()
    iso = "2026-07-06"
    c = _make_coordinator(store)
    snap = IssuedSnapshot(
        issued_at=f"{iso}T00:00:00+00:00",
        status="fresh",
        raw_hourly_wh={f"{iso}T11:00:00+00:00": 100.0},
        corrected_hourly_wh={f"{iso}T11:00:00+00:00": 100.0},
        cloud_class_by_hour={f"{iso}T11:00:00+00:00": "hail"},
    )
    assert c._dominant_weather_class(snap, iso) == "hail"
