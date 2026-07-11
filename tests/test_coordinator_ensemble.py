"""Coordinator glue tests for the ensemble-weather bands (v0.16, SPEC §6).

Import is via ``custom_components.balcony_solar_forecast`` (the real HA-importing
package), so Home Assistant is required; skipped on the plain-core path. The
coordinator is built via ``__new__`` with only the attributes the ensemble glue
touches (mirrors tests/test_coordinator_learning.py). Covers:

  * OFF (or factors-None) => band_by_slot is bit-identical to the learned-only
    build, band_source "learned";
  * cold-start win: an empty ring + ensemble factors yields the ensemble band on
    covered hours (all four 15-min slots of an hour share it), learned neutral
    elsewhere, band_source "ensemble";
  * envelope: bands widen ONLY where the ensemble is wider; a slot beyond the
    ensemble horizon keeps the learned band; band_source "envelope";
  * fetch-failure resilience (exception => no ensemble state, no raise);
  * staleness scheduling (no refetch within the interval; refetch when stale);
  * _det_ghi_by_hour aggregation + full fetch->parse->factor pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast import (
    coordinator as coord_mod,  # noqa: E402
)
from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    BAND_SOURCE_ENSEMBLE,
    BAND_SOURCE_ENVELOPE,
    BAND_SOURCE_LEARNED,
    ENSEMBLE_FETCH_INTERVAL_S,
    INTRADAY_NEUTRAL,
)
from custom_components.balcony_solar_forecast.coordinator import (  # noqa: E402
    BalconySolarCoordinator,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasState,
    DriftState,
    LearnerConfig,
    PlaneConfig,
    QuantileBands,
    QuantileState,
    ShademapState,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)
from custom_components.balcony_solar_forecast.fetcher import FetchError  # noqa: E402


class _FakeConfig:
    time_zone = "UTC"


class _FakeHass:
    def __init__(self) -> None:
        self.config = _FakeConfig()


class _Entry:
    entry_id = "e1"
    data: dict = {}
    options: dict = {}


class _FakeFetcher:
    def __init__(self, payload=None, exc=None) -> None:
        self.payload = payload
        self.exc = exc
        self.calls = 0

    async def async_fetch_ensemble_raw(self, lat, lon):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.payload


def _site() -> SiteConfig:
    return SiteConfig(
        latitude=48.5,
        longitude=12.2,
        planes=(
            PlaneConfig(name="M1", azimuth_deg=115.0, tilt_deg=70.0, wp=370.0),
        ),
        groups=(),
    )


def _make_coordinator(
    *, ensemble_enabled=True, quantile_bins=None, fetcher=None
) -> BalconySolarCoordinator:
    c = BalconySolarCoordinator.__new__(BalconySolarCoordinator)
    c.hass = _FakeHass()
    c._site = _site()
    c.entry = _Entry()
    c._learner_config = LearnerConfig(
        fast_enabled=False, slow_enabled=False, day_ahead_enabled=False
    )
    c._bias_state = BiasState()
    c._shademap_state = ShademapState()
    c._drift_state = DriftState()
    c._intraday_scalar = INTRADAY_NEUTRAL
    c._quantiles_enabled = True
    c._quantile_state = QuantileState(bins=quantile_bins or {})
    # Ensemble state (normally set in __init__).
    c._ensemble_enabled = ensemble_enabled
    c._ensemble_raw = None
    c._ensemble_fetched_at = None
    c._ensemble_cache = None
    c._ensemble_factors = None
    c._band_source = BAND_SOURCE_LEARNED
    c._fetcher = fetcher
    return c


def _slot(start: datetime, ghi: float = 500.0) -> WeatherSlot:
    return WeatherSlot(start=start, ghi=ghi, dni=0.0, dhi=0.0, temp_c=15.0)


def _weather(starts, ghi=500.0) -> WeatherSeries:
    return WeatherSeries(slots=tuple(_slot(s, ghi) for s in starts))


def _hourkey(dt: datetime) -> str:
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


def _ens_raw(stamp: str, base: float, members: list[float]) -> dict:
    hourly = {"time": [stamp], "shortwave_radiation": [base]}
    for i, v in enumerate(members, start=1):
        hourly[f"shortwave_radiation_member{i:02d}"] = [v]
    return {"hourly": hourly}


_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# OFF path is bit-identical to the learned-only build
# ---------------------------------------------------------------------------


def test_off_path_bit_identical_to_learned(monkeypatch):
    """Feature flag OFF (and factors-None) => band_by_slot equals learned-only."""
    learned = QuantileBands(p10=0.85, p50=1.0, p90=1.15, n=40)
    monkeypatch.setattr(
        coord_mod.quantiles_mod, "bands_for_bin",
        lambda *a, **k: learned,
    )
    starts = [_NOW + timedelta(minutes=15 * i) for i in range(4)]
    weather = _weather(starts)

    # OFF, but with factors present that WOULD have widened had the flag been on.
    off = _make_coordinator(ensemble_enabled=False, quantile_bins={"x": [["", 1.0]]})
    off._ensemble_factors = {_hourkey(_NOW): (0.5, 1.5)}
    hooks_off = off._build_learner_hooks(weather, _NOW)

    # ON but no factors available => same code path, must match OFF exactly.
    none = _make_coordinator(ensemble_enabled=True, quantile_bins={"x": [["", 1.0]]})
    none._ensemble_factors = None
    hooks_none = none._build_learner_hooks(weather, _NOW)

    assert hooks_off.band_by_slot == hooks_none.band_by_slot
    # And every slot carries the pure learned band (the ensemble was ignored).
    for start in starts:
        assert hooks_off.band_by_slot[start] == learned
    assert off._band_source == BAND_SOURCE_LEARNED


# ---------------------------------------------------------------------------
# Cold-start win: empty ring + ensemble => ensemble band on covered hours
# ---------------------------------------------------------------------------


def test_cold_start_ensemble_win_and_15min_mapping():
    c = _make_coordinator(ensemble_enabled=True, quantile_bins={})  # empty ring
    # Four 15-min slots share hour 12:00 (covered); two share hour 13:00 (not).
    covered = [_NOW + timedelta(minutes=15 * i) for i in range(4)]
    uncovered = [
        _NOW + timedelta(hours=1),
        _NOW + timedelta(hours=1, minutes=15),
    ]
    weather = _weather(covered + uncovered)
    c._ensemble_factors = {_hourkey(_NOW): (0.8, 1.3)}

    hooks = c._build_learner_hooks(weather, _NOW)
    band = hooks.band_by_slot

    # All four covered slots share the SAME cold-start ensemble band.
    for start in covered:
        assert (band[start].p10, band[start].p50, band[start].p90) == (0.8, 1.0, 1.3)
    # Uncovered slots stay on the neutral learned band -> omitted from the dict.
    for start in uncovered:
        assert start not in band
    # Learned collapsed everywhere today, ensemble supplied the spread.
    assert c._band_source == BAND_SOURCE_ENSEMBLE


# ---------------------------------------------------------------------------
# Envelope: widen only where the ensemble is wider
# ---------------------------------------------------------------------------


def test_envelope_widens_only_where_wider(monkeypatch):
    learned = QuantileBands(p10=0.9, p50=1.0, p90=1.1, n=40)
    monkeypatch.setattr(
        coord_mod.quantiles_mod, "bands_for_bin",
        lambda *a, **k: learned,
    )
    c = _make_coordinator(ensemble_enabled=True, quantile_bins={"x": [["", 1.0]]})
    wide = _NOW                       # ensemble wider -> widens
    narrow = _NOW + timedelta(hours=1)  # ensemble tighter -> learned kept
    absent = _NOW + timedelta(hours=2)  # beyond ensemble horizon -> learned kept
    weather = _weather([wide, narrow, absent])
    c._ensemble_factors = {
        _hourkey(wide): (0.7, 1.3),
        _hourkey(narrow): (0.95, 1.05),
    }

    band = c._build_learner_hooks(weather, _NOW).band_by_slot

    assert (band[wide].p10, band[wide].p50, band[wide].p90) == (0.7, 1.0, 1.3)
    assert (band[narrow].p10, band[narrow].p90) == (0.9, 1.1)  # never narrows
    assert (band[absent].p10, band[absent].p90) == (0.9, 1.1)  # learned unchanged
    assert c._band_source == BAND_SOURCE_ENVELOPE


# ---------------------------------------------------------------------------
# Fetch-failure resilience
# ---------------------------------------------------------------------------


async def test_fetch_failure_leaves_no_ensemble_state():
    fetcher = _FakeFetcher(exc=FetchError("boom", retryable=True))
    c = _make_coordinator(ensemble_enabled=True, fetcher=fetcher)
    c._ensemble_factors = {"stale": (0.5, 1.5)}  # will be dropped
    weather = _weather([_NOW])

    # Must not raise; the ladder is untouched.
    await c._async_update_ensemble(_NOW, weather)

    assert fetcher.calls == 1
    assert c._ensemble_factors is None
    assert c._ensemble_fetched_at is None  # not advanced on failure
    assert c._ensemble_raw is None


# ---------------------------------------------------------------------------
# Staleness scheduling + full fetch->parse->factor pipeline
# ---------------------------------------------------------------------------


def test_due_for_ensemble_fetch_interval():
    c = _make_coordinator(ensemble_enabled=True)
    assert c._due_for_ensemble_fetch(_NOW) is True  # never fetched
    c._ensemble_fetched_at = _NOW - timedelta(seconds=ENSEMBLE_FETCH_INTERVAL_S - 60)
    assert c._due_for_ensemble_fetch(_NOW) is False  # within interval
    c._ensemble_fetched_at = _NOW - timedelta(seconds=ENSEMBLE_FETCH_INTERVAL_S + 60)
    assert c._due_for_ensemble_fetch(_NOW) is True  # stale


async def test_no_refetch_within_interval_but_recomputes_from_cache():
    fetcher = _FakeFetcher(payload=None)  # would be used only if a fetch fired
    c = _make_coordinator(ensemble_enabled=True, fetcher=fetcher)
    # A fresh (not-due) cached raw payload covering hour 12:00 (stamp 13:00).
    c._ensemble_raw = _ens_raw(
        "2026-07-01T13:00", 500.0, [400.0 + 20.0 * i for i in range(11)]
    )
    c._ensemble_fetched_at = _NOW  # not due
    weather = _weather([_NOW + timedelta(minutes=15 * i) for i in range(4)])

    await c._async_update_ensemble(_NOW, weather)

    assert fetcher.calls == 0  # NOT refetched within the interval
    # Factors recomputed from the cached payload for the covered hour.
    assert _hourkey(_NOW) in c._ensemble_factors
    f10, f90 = c._ensemble_factors[_hourkey(_NOW)]
    assert f10 <= 1.0 <= f90


async def test_stale_triggers_refetch_and_computes_factors():
    payload = _ens_raw(
        "2026-07-01T13:00", 500.0, [400.0 + 20.0 * i for i in range(11)]
    )
    fetcher = _FakeFetcher(payload=payload)
    c = _make_coordinator(ensemble_enabled=True, fetcher=fetcher)
    c._ensemble_fetched_at = None  # due
    weather = _weather([_NOW + timedelta(minutes=15 * i) for i in range(4)])

    await c._async_update_ensemble(_NOW, weather)

    assert fetcher.calls == 1
    assert c._ensemble_raw is payload
    assert c._ensemble_fetched_at == _NOW
    assert _hourkey(_NOW) in c._ensemble_factors


def test_det_ghi_by_hour_aggregates_slot_means():
    c = _make_coordinator(ensemble_enabled=True)
    # Two slots in hour 12:00 (400, 600 -> mean 500), one in 13:00 (300).
    weather = _weather(
        [_NOW, _NOW + timedelta(minutes=15), _NOW + timedelta(hours=1)]
    )
    weather = WeatherSeries(
        slots=(
            _slot(_NOW, 400.0),
            _slot(_NOW + timedelta(minutes=15), 600.0),
            _slot(_NOW + timedelta(hours=1), 300.0),
        )
    )
    det = c._det_ghi_by_hour(weather)
    assert det[_hourkey(_NOW)] == pytest.approx(500.0)
    assert det[_hourkey(_NOW + timedelta(hours=1))] == pytest.approx(300.0)


def test_ensemble_state_summary_shape():
    c = _make_coordinator(ensemble_enabled=True)
    c._ensemble_cache = (object(), {"h1": [1.0] * 12, "h2": [1.0] * 12})
    c._ensemble_factors = {"h1": (0.9, 1.1)}
    c._ensemble_fetched_at = _NOW
    summary = c.ensemble_state_summary()
    assert summary["enabled"] is True
    assert summary["member_count"] == 12
    assert summary["hours_covered"] == 1
    assert summary["payload_age_seconds"] is not None
