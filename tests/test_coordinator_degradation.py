"""Tests for the degradation ladder (SPEC §13) + learner-hook composition.

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

Shared coordinator and weather fakes live in tests/helpers/.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("homeassistant")

from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: E402

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    CORRECTION_SOURCE_BOTH,
    CORRECTION_SOURCE_DAY_AHEAD_INTRADAY,
    FAILED_FETCH_MIN_INTERVAL_SECONDS,
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
from custom_components.balcony_solar_forecast.core import (  # noqa: E402
    solpos,
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
from tests.helpers.weather import (
    FETCH_INTERVAL as FETCH_INTERVAL,
)

# Compatibility exports for external regression probes; new tests import helpers.
from tests.helpers.weather import (
    NOW as NOW,
)
from tests.helpers.weather import (
    _coord as _coord,
)
from tests.helpers.weather import (
    _FakeFetcher as _FakeFetcher,
)
from tests.helpers.weather import (
    _om_payload as _om_payload,
)
from tests.helpers.weather import (
    _PayloadStore as _PayloadStore,
)
from tests.helpers.weather import (
    _sparse_payload as _sparse_payload,
)

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
    # A failure backs off (FAILED_FETCH_MIN_INTERVAL_SECONDS) instead of
    # retrying on the very next recompute tick.
    assert c._due_for_fetch(NOW) is False
    c._last_attempt_at = NOW - timedelta(seconds=FAILED_FETCH_MIN_INTERVAL_SECONDS)
    assert c._due_for_fetch(NOW) is True  # backoff elapsed -> retry


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
    # The ATTEMPT anchor advances on failure too — it IS an attempt; without
    # the stamp the _due_for_fetch backoff never engages and a down provider
    # would be hammered every recompute tick.
    assert c._last_attempt_at == NOW
    # The payload anchor and the stored payload are untouched.
    assert c._last_fetched_at == NOW - timedelta(hours=3)
    assert store.last_payload["payload"] is old


async def test_try_fetch_success_persists_and_advances_both_anchors():
    store = _PayloadStore()
    c = _coord(store)
    fresh = _om_payload(start_iso="2026-07-05T12:15")
    c._fetcher = _FakeFetcher([fresh])

    await c._async_try_fetch(NOW)

    assert c._last_fetch_ok is True and c._last_error is None
    assert c._last_fetched_at == NOW
    assert c._last_attempt_at == NOW
    assert store.last_payload["payload"] is fresh


async def test_failed_fetch_backs_off_provider():
    """Sustained outage: at most ONE provider attempt per backoff window
    (FAILED_FETCH_MIN_INTERVAL_SECONDS), never one per recompute tick — and a
    configured fetch interval SHORTER than the backoff is not stretched."""
    store = _PayloadStore()
    store.set_last_payload(_om_payload(), "2026-07-05T09:00:00+00:00")
    c = _coord(store)
    c._fetcher = _FakeFetcher([FetchError("down", retryable=True)] * 3)

    backoff = timedelta(seconds=FAILED_FETCH_MIN_INTERVAL_SECONDS)
    await c._async_try_fetch(NOW)  # attempt 1 fails
    assert c._fetcher.calls == 1
    # Every recompute tick inside the window: NOT due (no new attempt).
    for minutes in (1, 5, 14):
        assert c._due_for_fetch(NOW + timedelta(minutes=minutes)) is False
    # Backoff elapsed: exactly one retry, which fails again and re-arms.
    t1 = NOW + backoff
    assert c._due_for_fetch(t1) is True
    await c._async_try_fetch(t1)  # attempt 2 fails
    assert c._fetcher.calls == 2
    assert c._due_for_fetch(t1 + timedelta(minutes=1)) is False

    # A fetch interval SHORTER than the backoff wins the min(): the operator's
    # faster cadence is not stretched by the failure backoff.
    c._fetch_interval = timedelta(minutes=10)
    assert c._due_for_fetch(t1 + timedelta(minutes=10)) is True


async def test_try_fetch_coverage_refusal_keeps_payload_age():
    """REGRESSION (SPEC §13): keeping the richer stored payload must not stamp
    the served weather as fresh — the payload anchor stays, only the scheduler
    anchor advances, so the age keeps climbing through the ladder."""
    store = _PayloadStore()
    rich = _om_payload(start_iso="2026-07-05T12:15")
    store.set_last_payload(rich, "2026-07-05T06:00:00+00:00")
    c = _coord(store)
    payload_age_anchor = NOW - timedelta(hours=6)
    c._last_fetched_at = payload_age_anchor
    c._fetcher = _FakeFetcher([_om_payload(n_quarters=4, start_iso="2026-07-05T12:15")])

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


def _slot_cloud_class(slot, c) -> str:
    """Classify a slot exactly as the coordinator does (A5: k_c-based).

    The live loops pass the slot GHI and the sun elevation, so a test that seeds a
    matching cell must classify with the same inputs or the class (and hence the
    cell key) diverges.
    """
    _az, elev = solpos.sun_position(slot.start, c._site.latitude, c._site.longitude)
    return bias_mod.classify_cloud(
        cloud_low=slot.cloud_low, cloud_mid=slot.cloud_mid,
        cloud_high=slot.cloud_high, visibility_m=slot.visibility_m,
        month=slot.start.month, ghi=slot.ghi, elevation_deg=elev,
    )


def _trained_bias_for(weather: WeatherSeries, c) -> BiasState:
    """A BiasState whose cell matches every slot of ``weather`` (factor 1.2)."""
    cells: dict[str, BiasCell] = {}
    for slot in weather.slots:
        cc = _slot_cloud_class(slot, c)
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
    c._bias_state = _trained_bias_for(weather, c)
    c._intraday_scalar = 1.5  # fast learner active (non-neutral)

    hooks = c._build_learner_hooks(weather, NOW)

    assert hooks.slot_factor is not None
    past, future = weather.slots[0].start, weather.slots[1].start
    # Past slot (>15 min ago): only the day-ahead factor applies.
    assert hooks.slot_factor(past) == pytest.approx(1.2)
    # Future slot: day-ahead factor PLUS the intraday factor (> day-ahead alone).
    assert hooks.slot_factor(future) > 1.2
    assert hooks.correction_source == CORRECTION_SOURCE_DAY_AHEAD_INTRADAY


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
        cc = _slot_cloud_class(slot, c)
        dp = bias_mod.day_part_for_hour(slot.start.hour)
        bins[QuantileState.bin_key(cc, dp)] = [0.8] * 60
    c._quantile_state = QuantileState(bins=bins)

    hooks = c._build_learner_hooks(weather, NOW)
    assert hooks.band_by_slot is not None
    assert set(hooks.band_by_slot) == {s.start for s in weather.slots}
