"""Tests for the degradation ladder (SPEC §7) + learner-hook composition.

Covers the previously untested fault-tolerance core of the coordinator:

  * ``_status_for_age`` at every rung boundary (fresh / cached /
    physics_fallback / unavailable, negative-age clamp);
  * ``_due_for_fetch`` scheduling on the ATTEMPT anchor;
  * ``_async_try_fetch``: failure keeps the last-good cache and records the
    error; success persists and advances both anchors; and the
    coverage-refusal branch (keep the richer stored payload when a new fetch
    has less radiation coverage) — REGRESSION: that branch must NOT advance the
    payload-age anchor, else a sustained partial Open-Meteo degradation would
    serve arbitrarily old weather at status "fresh"/age ~0 forever;
  * ``_async_update_data`` end-to-end: UpdateFailed with no cache, a cached
    curve served on fetch failure, UpdateFailed beyond the physics-fallback
    horizon;
  * ``_build_learner_hooks`` composition: the day-ahead per-slot factor map,
    the intraday in-progress-slot boundary (age_min > -15), the
    correction-source labels, and the quantile band map presence/omission.

Reuses the fake-coordinator infrastructure from tests/test_coordinator_learning
and the Open-Meteo payload shape from tests/test_fetcher_shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    CORRECTION_SOURCE_BOTH,
    CORRECTION_SOURCE_INTRADAY,
    MAX_PAYLOAD_AGE_HOURS,
    MAX_PHYSICS_FALLBACK_AGE_HOURS,
    STATUS_CACHED,
    STATUS_FRESH,
    STATUS_PHYSICS_FALLBACK,
    STATUS_UNAVAILABLE,
)
from custom_components.balcony_solar_forecast.core import (  # noqa: E402
    bias as bias_mod,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasCell,
    BiasState,
    LearnerConfig,
    QuantileState,
    WeatherSeries,
    WeatherSlot,
)
from custom_components.balcony_solar_forecast.fetcher import (  # noqa: E402
    FetchError,
)
from tests.test_coordinator_learning import (  # noqa: E402
    _FakeStore,
    _make_coordinator,
)

NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
FETCH_INTERVAL = timedelta(seconds=1800)


# ---------------------------------------------------------------------------
# Payload / fetcher fakes
# ---------------------------------------------------------------------------


def _om_payload(n_quarters: int = 8, start_iso: str = "2026-07-05T10:15") -> dict:
    """A minimal valid Open-Meteo payload (mirrors tests/test_fetcher_shapes)."""
    base = datetime.fromisoformat(start_iso)
    times = [
        (base + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M")
        for i in range(n_quarters)
    ]
    hours = max(1, n_quarters // 4 + 1)
    hbase = base.replace(minute=0)
    htimes = [
        (hbase + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M")
        for i in range(hours)
    ]
    return {
        "minutely_15": {
            "time": times,
            "shortwave_radiation": [500.0] * n_quarters,
            "direct_normal_irradiance": [600.0] * n_quarters,
            "diffuse_radiation": [150.0] * n_quarters,
            "temperature_2m": [22.0] * n_quarters,
        },
        "hourly": {
            "time": htimes,
            "cloud_cover_low": [10.0] * hours,
            "cloud_cover_mid": [0.0] * hours,
            "cloud_cover_high": [0.0] * hours,
            "visibility": [30000.0] * hours,
            "snowfall": [0.0] * hours,
            "snow_depth": [0.0] * hours,
        },
    }


def _sparse_payload(**kw) -> dict:
    """A payload with LESS radiation coverage (nulled radiation samples)."""
    p = _om_payload(**kw)
    n = len(p["minutely_15"]["time"])
    p["minutely_15"]["shortwave_radiation"] = [None] * n
    p["minutely_15"]["direct_normal_irradiance"] = [None] * n
    p["minutely_15"]["diffuse_radiation"] = [None] * n
    return p


class _PayloadStore(_FakeStore):
    """FakeStore that actually holds a last-good payload."""

    def __init__(self) -> None:
        super().__init__()
        self.last_payload: dict | None = None

    def get_last_payload(self):
        return self.last_payload

    def set_last_payload(self, payload, fetched_at_iso):
        self.last_payload = {"payload": payload, "fetched_at": fetched_at_iso}


class _FakeFetcher:
    """Scripted fetcher: pops the next behaviour per call."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls = 0

    async def async_fetch_raw(self, _lat, _lon, _days):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _coord(store: _PayloadStore | None = None):
    c = _make_coordinator(store or _PayloadStore())
    c._fetch_interval = FETCH_INTERVAL
    return c


# ---------------------------------------------------------------------------
# _status_for_age: every rung boundary
# ---------------------------------------------------------------------------


def test_status_for_age_rungs():
    c = _coord()
    c._last_fetch_ok = True
    assert c._status_for_age(timedelta(minutes=5)) == STATUS_FRESH
    # At/after the fetch interval a good fetch no longer counts as fresh.
    assert c._status_for_age(FETCH_INTERVAL) == STATUS_CACHED
    assert c._status_for_age(timedelta(hours=MAX_PAYLOAD_AGE_HOURS)) == STATUS_CACHED
    assert (
        c._status_for_age(timedelta(hours=MAX_PAYLOAD_AGE_HOURS, seconds=1))
        == STATUS_PHYSICS_FALLBACK
    )
    assert (
        c._status_for_age(timedelta(hours=MAX_PHYSICS_FALLBACK_AGE_HOURS))
        == STATUS_PHYSICS_FALLBACK
    )
    assert (
        c._status_for_age(timedelta(hours=MAX_PHYSICS_FALLBACK_AGE_HOURS, seconds=1))
        == STATUS_UNAVAILABLE
    )


def test_status_for_age_failed_fetch_is_never_fresh_and_negative_age_clamped():
    c = _coord()
    c._last_fetch_ok = False
    assert c._status_for_age(timedelta(minutes=1)) == STATUS_CACHED
    # A clock skew (negative age) is clamped, not treated as ancient/fresh-forever.
    c._last_fetch_ok = True
    assert c._status_for_age(timedelta(minutes=-10)) == STATUS_FRESH


# ---------------------------------------------------------------------------
# _due_for_fetch: schedules on the ATTEMPT anchor
# ---------------------------------------------------------------------------


def test_due_for_fetch_attempt_anchor():
    c = _coord()
    assert c._due_for_fetch(NOW) is True  # nothing attempted yet
    c._last_attempt_at = NOW - timedelta(minutes=5)
    c._last_fetch_ok = True
    assert c._due_for_fetch(NOW) is False  # recent successful round-trip
    c._last_attempt_at = NOW - FETCH_INTERVAL
    assert c._due_for_fetch(NOW) is True  # interval elapsed
    c._last_attempt_at = NOW - timedelta(minutes=5)
    c._last_fetch_ok = False
    assert c._due_for_fetch(NOW) is True  # failure -> retry next tick


# ---------------------------------------------------------------------------
# _async_try_fetch: failure / success / coverage-refusal
# ---------------------------------------------------------------------------


async def test_try_fetch_failure_keeps_cache_and_records_error():
    store = _PayloadStore()
    old = _om_payload()
    store.set_last_payload(old, "2026-07-05T09:00:00+00:00")
    c = _coord(store)
    c._last_fetched_at = NOW - timedelta(hours=3)
    c._fetcher = _FakeFetcher([FetchError("boom", retryable=True)])

    await c._async_try_fetch(NOW)

    assert c._last_fetch_ok is False
    assert "boom" in c._last_error
    # The payload anchor and the stored payload are untouched.
    assert c._last_fetched_at == NOW - timedelta(hours=3)
    assert store.last_payload["payload"] is old


async def test_try_fetch_success_persists_and_advances_both_anchors():
    store = _PayloadStore()
    c = _coord(store)
    fresh = _om_payload()
    c._fetcher = _FakeFetcher([fresh])

    await c._async_try_fetch(NOW)

    assert c._last_fetch_ok is True and c._last_error is None
    assert c._last_fetched_at == NOW
    assert c._last_attempt_at == NOW
    assert store.last_payload["payload"] is fresh


async def test_try_fetch_coverage_refusal_keeps_payload_age():
    """REGRESSION (SPEC §7): keeping the richer stored payload must not stamp
    the served weather as fresh — the payload anchor stays, only the scheduler
    anchor advances, so the age keeps climbing through the ladder."""
    store = _PayloadStore()
    rich = _om_payload()
    store.set_last_payload(rich, "2026-07-05T06:00:00+00:00")
    c = _coord(store)
    payload_age_anchor = NOW - timedelta(hours=6)
    c._last_fetched_at = payload_age_anchor
    c._fetcher = _FakeFetcher([_sparse_payload()])

    await c._async_try_fetch(NOW)

    # Round-trip succeeded: scheduler satisfied, no error, richer payload kept.
    assert c._last_fetch_ok is True and c._last_error is None
    assert c._last_attempt_at == NOW
    assert store.last_payload["payload"] is rich
    # But the PAYLOAD age anchor did not move: the served weather keeps aging.
    assert c._last_fetched_at == payload_age_anchor
    # And the ladder sees it: 6h > fetch interval -> no longer "fresh".
    age = NOW - c._last_fetched_at
    assert c._status_for_age(age) == STATUS_CACHED


# ---------------------------------------------------------------------------
# _async_update_data: the ladder end-to-end
# ---------------------------------------------------------------------------


async def test_update_data_without_any_cache_raises():
    c = _coord()
    c._fetcher = _FakeFetcher([FetchError("down", retryable=True)])
    with pytest.raises(UpdateFailed):
        await c._async_update_data()


async def test_update_data_serves_cached_curve_on_fetch_failure(monkeypatch):
    import custom_components.balcony_solar_forecast.coordinator as coord_mod

    store = _PayloadStore()
    store.set_last_payload(_om_payload(), "irrelevant")
    c = _coord(store)
    c._fetcher = _FakeFetcher([FetchError("down", retryable=True)])
    c._last_fetched_at = NOW - timedelta(hours=3)  # within the 24h cache window
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: NOW)

    data = await c._async_update_data()

    assert data["status"] == STATUS_CACHED
    assert data["degraded"] is True
    assert data["weather_age_seconds"] == pytest.approx(3 * 3600, abs=5)
    # A curve was actually computed from the cached weather image.
    assert data["watts"], "expected a served curve from the cached payload"


async def test_update_data_beyond_fallback_horizon_raises(monkeypatch):
    import custom_components.balcony_solar_forecast.coordinator as coord_mod

    store = _PayloadStore()
    store.set_last_payload(_om_payload(), "irrelevant")
    c = _coord(store)
    c._fetcher = _FakeFetcher([FetchError("down", retryable=True)])
    c._last_fetched_at = NOW - timedelta(
        hours=MAX_PHYSICS_FALLBACK_AGE_HOURS, minutes=1
    )
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda: NOW)

    with pytest.raises(UpdateFailed):
        await c._async_update_data()


# ---------------------------------------------------------------------------
# _build_learner_hooks: composition
# ---------------------------------------------------------------------------


def _weather_two_slots() -> WeatherSeries:
    """One past slot (1h ago) and one future slot (1h ahead), clear sky."""
    mk = lambda start: WeatherSlot(  # noqa: E731 - tiny local factory
        start=start, ghi=500.0, dni=600.0, dhi=150.0, temp_c=20.0,
        cloud_low=0.0, cloud_mid=0.0, cloud_high=0.0,
        visibility_m=30000.0, snowfall_cm=0.0, snow_depth_m=0.0,
    )
    return WeatherSeries(slots=(mk(NOW - timedelta(hours=1)), mk(NOW + timedelta(hours=1))))


def _trained_bias_for(weather: WeatherSeries) -> BiasState:
    """A BiasState whose cell matches every slot of ``weather`` (factor 1.2)."""
    cells: dict[str, BiasCell] = {}
    for slot in weather.slots:
        cc = bias_mod.classify_cloud(
            cloud_low=slot.cloud_low, cloud_mid=slot.cloud_mid,
            cloud_high=slot.cloud_high, visibility_m=slot.visibility_m,
            month=slot.start.month,
        )
        dp = bias_mod.day_part_for_hour(slot.start.hour)
        cells[BiasState.cell_key(cc, dp)] = BiasCell(theta=1.2, covariance=1.0, n=10)
    return BiasState(cells=cells)


def test_hooks_day_factor_and_intraday_boundary(monkeypatch):
    import custom_components.balcony_solar_forecast.coordinator as coord_mod

    monkeypatch.setattr(coord_mod.dt_util, "as_local", lambda d: d)
    weather = _weather_two_slots()
    c = _coord()
    c._learner_config = LearnerConfig(
        fast_enabled=True, slow_enabled=False, day_ahead_enabled=True
    )
    c._bias_state = _trained_bias_for(weather)
    c._intraday_scalar = 1.5  # fast learner active (non-neutral)

    hooks = c._build_learner_hooks(weather, NOW)

    assert hooks.slot_factor is not None
    past, future = weather.slots[0].start, weather.slots[1].start
    # Past slot (>15 min ago): only the day-ahead factor applies.
    assert hooks.slot_factor(past) == pytest.approx(1.2)
    # Future slot: day-ahead factor PLUS the intraday factor (> day-ahead alone).
    assert hooks.slot_factor(future) > 1.2
    assert hooks.correction_source == CORRECTION_SOURCE_INTRADAY


def test_hooks_correction_source_both_with_shademap(monkeypatch):
    import custom_components.balcony_solar_forecast.coordinator as coord_mod
    from custom_components.balcony_solar_forecast.core import (
        shademap as shademap_mod,
    )

    monkeypatch.setattr(coord_mod.dt_util, "as_local", lambda d: d)
    weather = _weather_two_slots()
    c = _coord()
    c._learner_config = LearnerConfig(
        fast_enabled=True, slow_enabled=True, day_ahead_enabled=False
    )
    c._intraday_scalar = 1.3
    c._shademap_state = shademap_mod.update_bin(
        c._shademap_state, channel="M1", sun_az=180.0, sun_el=40.0,
        doy=186, measured_t=0.5,
    )

    hooks = c._build_learner_hooks(weather, NOW)

    assert hooks.beam_tau is not None
    assert hooks.slot_factor is not None
    assert hooks.correction_source == CORRECTION_SOURCE_BOTH


def test_hooks_band_by_slot_presence(monkeypatch):
    import custom_components.balcony_solar_forecast.coordinator as coord_mod

    monkeypatch.setattr(coord_mod.dt_util, "as_local", lambda d: d)
    weather = _weather_two_slots()
    c = _coord()

    # Cold start: empty quantile ring -> no band map at all.
    hooks = c._build_learner_hooks(weather, NOW)
    assert hooks.band_by_slot is None

    # A trained (non-neutral) bin for every slot -> bands keyed by slot start.
    bins: dict[str, list[float]] = {}
    for slot in weather.slots:
        cc = bias_mod.classify_cloud(
            cloud_low=slot.cloud_low, cloud_mid=slot.cloud_mid,
            cloud_high=slot.cloud_high, visibility_m=slot.visibility_m,
            month=slot.start.month,
        )
        dp = bias_mod.day_part_for_hour(slot.start.hour)
        bins[QuantileState.bin_key(cc, dp)] = [0.8] * 60
    c._quantile_state = QuantileState(bins=bins)

    hooks = c._build_learner_hooks(weather, NOW)
    assert hooks.band_by_slot is not None
    assert set(hooks.band_by_slot) == {s.start for s in weather.slots}
