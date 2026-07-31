"""Tests for the v0.4 platform layer (SPEC §11/§15).

Covers the skill-scoreboard sensors (engine daily/hourly MAE), the daily
P10/P90 quantile energy sensors, the p10/p90 wh_period band attributes on the
served energy sensor, the extended get_forecast response band blocks, the
options-flow quantile switch, and the diagnostics scoreboard/
quantile summaries.

All read the coordinator's flat ``self.data`` v0.4 keys and must:
  * stay available where they are diagnostics (never vanish);
  * report ``None`` — never a fabricated zero — when the scoreboard/quantiles
    are absent, disabled or cold-started (SPEC §11.1 "no fake spread");
  * tolerate missing / malformed values without raising.

Needs Home Assistant; skipped on the plain-core path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

from balcony_solar_forecast import sensor as sensor_mod  # noqa: E402
from balcony_solar_forecast.const import (  # noqa: E402
    ATTR_WH_PERIOD_P10,
    ATTR_WH_PERIOD_P90,
    DATA_KEY_BAND_SOURCE,
    DATA_KEY_BAND_SOURCE_BY_DAY,
    DATA_KEY_QUANTILE_CURVES,
    DATA_KEY_QUANTILE_CURVES_AC,
    DATA_KEY_SCOREBOARD,
    FORECAST_RESP_KEY_P10,
    FORECAST_RESP_KEY_P50,
    FORECAST_RESP_KEY_P90,
)
from balcony_solar_forecast.diagnostics import (  # noqa: E402
    _quantile_summary,
    _scoreboard_summary,
)
from balcony_solar_forecast.sensor import (  # noqa: E402
    EnergyBandSensor,
    EnergyProductionSensor,
    EngineDailyKwhMaeSensor,
    EngineHourlyMaeSensor,
    _band_blocks,
    _build_forecast_response,
    _hourly_from_slots,
)

DOMAIN = "balcony_solar_forecast"


class _FakeEntry:
    def __init__(self, data=None, options=None):
        self.entry_id = "abc123"
        self.data = data or {}
        self.options = options or {}


class _FakeCoordinator:
    def __init__(self, data, *, entry=None, last_update_success=True, **extra):
        self.data = data
        self.entry = entry or _FakeEntry()
        self.last_update_success = last_update_success
        for k, v in extra.items():
            setattr(self, k, v)


def _bare(cls, coordinator, **attrs):
    obj = cls.__new__(cls)
    obj.coordinator = coordinator
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


def _sb(**fields):
    """A DATA_KEY_SCOREBOARD summary dict with the given fields set."""
    base = {
        "engine_daily_kwh_mae": None,
        "engine_hourly_mae": None,
        "window_days": 14,
        "scored_days": 0,
        "newest_scored_date": None,
        "strata": {},
    }
    base.update(fields)
    return base


# ==========================================================================
# Scoreboard metric sensors
# ==========================================================================


def test_engine_daily_kwh_mae_value_and_attrs():
    coord = _FakeCoordinator(
        {
            DATA_KEY_SCOREBOARD: _sb(
                engine_daily_kwh_mae=0.4567, scored_days=14, window_days=14
            )
        }
    )
    sensor = _bare(EngineDailyKwhMaeSensor, coord)
    assert sensor.native_value == pytest.approx(0.457)
    assert sensor.available is True
    attrs = sensor.extra_state_attributes
    assert attrs == {"window_days": 14, "scored_days": 14}


def test_engine_daily_kwh_mae_none_when_absent():
    # No scoreboard at all (coordinator not yet populated / disabled).
    assert _bare(EngineDailyKwhMaeSensor, _FakeCoordinator({})).native_value is None
    # Scoreboard present but the metric is None (empty window -> no fake zero).
    coord = _FakeCoordinator({DATA_KEY_SCOREBOARD: _sb()})
    assert _bare(EngineDailyKwhMaeSensor, coord).native_value is None
    # Non-dict scoreboard tolerated.
    coord2 = _FakeCoordinator({DATA_KEY_SCOREBOARD: "oops"})
    assert _bare(EngineDailyKwhMaeSensor, coord2).native_value is None


def test_engine_hourly_mae_value_and_none():
    coord = _FakeCoordinator({DATA_KEY_SCOREBOARD: _sb(engine_hourly_mae=150.66)})
    assert _bare(EngineHourlyMaeSensor, coord).native_value == pytest.approx(150.7)
    assert _bare(EngineHourlyMaeSensor, _FakeCoordinator({})).native_value is None


async def test_setup_registers_no_kill_gate_or_comparison_entities():
    # The kill-gate binary sensor, the vs-best-baseline sensor and the
    # per-comparison MAE sensors were removed with the external-comparison
    # machinery: both platforms' async_setup_entry must not register any of
    # them (a leftover registration would recreate orphaned entities on the
    # live install).
    from balcony_solar_forecast import binary_sensor as binary_sensor_mod

    class _Plane:
        actual_entity = None
        name = "M1"

    class _Site:
        planes = (_Plane(),)
        ac_actual_entity = None

    coord = _FakeCoordinator({})
    coord._site = _Site()

    class _Hass:
        data = {DOMAIN: {"abc123": coord}}

    added: list = []
    await sensor_mod.async_setup_entry(_Hass(), coord.entry, added.extend)
    await binary_sensor_mod.async_setup_entry(_Hass(), coord.entry, added.extend)
    assert added  # sanity: the platforms did register their real entities
    for entity in added:
        uid = getattr(entity, "unique_id", "") or ""
        assert "kill_gate" not in uid
        assert "vs_best" not in uid
        assert "comparison" not in uid


# ==========================================================================
# Daily P10 / P90 energy sensors
# ==========================================================================


def _band_curve(start: datetime, wh_per_slot: list[float]) -> dict[str, float]:
    return {
        (start + timedelta(minutes=15 * i)).isoformat(): wh
        for i, wh in enumerate(wh_per_slot)
    }


def test_energy_band_sensor_sums_today(monkeypatch):
    fixed = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: fixed)
    monkeypatch.setattr(sensor_mod.dt_util, "as_local", lambda d: d)

    # Phase 2: the band sensor reports the served-AC band (DATA_KEY_QUANTILE_CURVES_AC).
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    curve = _band_curve(start, [100.0, 100.0, 100.0, 100.0])  # 400 Wh today
    coord = _FakeCoordinator(
        {DATA_KEY_QUANTILE_CURVES_AC: {FORECAST_RESP_KEY_P10: curve}}
    )
    sensor = _bare(EnergyBandSensor, coord, _band=FORECAST_RESP_KEY_P10)
    assert sensor.native_value == pytest.approx(0.4)


def test_energy_band_sensor_none_when_no_band(monkeypatch):
    fixed = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: fixed)
    monkeypatch.setattr(sensor_mod.dt_util, "as_local", lambda d: d)

    # Quantiles off / cold start: no AC curve -> None (no fabricated spread).
    coord = _FakeCoordinator({})
    assert _bare(EnergyBandSensor, coord, _band=FORECAST_RESP_KEY_P90).native_value is None
    coord2 = _FakeCoordinator({DATA_KEY_QUANTILE_CURVES_AC: {FORECAST_RESP_KEY_P90: {}}})
    assert _bare(EnergyBandSensor, coord2, _band=FORECAST_RESP_KEY_P90).native_value is None


def test_energy_band_sensor_none_when_all_slots_other_day(monkeypatch):
    fixed = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: fixed)
    monkeypatch.setattr(sensor_mod.dt_util, "as_local", lambda d: d)

    start = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)  # tomorrow's slots
    curve = _band_curve(start, [100.0, 100.0])
    coord = _FakeCoordinator(
        {DATA_KEY_QUANTILE_CURVES_AC: {FORECAST_RESP_KEY_P10: curve}}
    )
    sensor = _bare(EnergyBandSensor, coord, _band=FORECAST_RESP_KEY_P10)
    # No slot falls on today -> None, not 0.
    assert sensor.native_value is None


# ==========================================================================
# Served energy sensor: p10/p90 wh_period band attributes sliced per day
# ==========================================================================


def test_energy_sensor_band_attributes(monkeypatch):
    fixed = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: fixed)
    monkeypatch.setattr(sensor_mod.dt_util, "as_local", lambda d: d)

    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    iso = [(start + timedelta(minutes=15 * i)).isoformat() for i in range(4)]
    data = {
        "watts": {k: 400.0 for k in iso},
        "wh_period": {k: 100.0 for k in iso},
        "slot_starts": iso,
        "energy_today_kwh": 0.4,
        DATA_KEY_QUANTILE_CURVES: {
            FORECAST_RESP_KEY_P10: {k: 80.0 for k in iso},
            FORECAST_RESP_KEY_P90: {k: 120.0 for k in iso},
        },
    }
    coord = _FakeCoordinator(data)
    sensor = _bare(
        EnergyProductionSensor, coord, _day_offset=0, _energy_key="energy_today_kwh"
    )
    attrs = sensor.extra_state_attributes
    assert set(attrs) == {"watts", "wh_period", ATTR_WH_PERIOD_P10, ATTR_WH_PERIOD_P90}
    assert attrs[ATTR_WH_PERIOD_P10][iso[0]] == pytest.approx(80.0)
    assert attrs[ATTR_WH_PERIOD_P90][iso[0]] == pytest.approx(120.0)
    assert len(attrs[ATTR_WH_PERIOD_P10]) == 4


def test_energy_sensor_band_attrs_empty_without_bands(monkeypatch):
    fixed = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: fixed)
    monkeypatch.setattr(sensor_mod.dt_util, "as_local", lambda d: d)

    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    iso = [start.isoformat()]
    data = {
        "watts": {iso[0]: 400.0},
        "wh_period": {iso[0]: 100.0},
        "slot_starts": iso,
        "energy_today_kwh": 0.1,
    }
    coord = _FakeCoordinator(data)
    sensor = _bare(
        EnergyProductionSensor, coord, _day_offset=0, _energy_key="energy_today_kwh"
    )
    attrs = sensor.extra_state_attributes
    # Band attrs present but empty (no fabricated spread).
    assert attrs[ATTR_WH_PERIOD_P10] == {}
    assert attrs[ATTR_WH_PERIOD_P90] == {}


def test_energy_sensor_band_attrs_unrecorded():
    # The band curves must be excluded from the recorder like the served curve.
    excluded = EnergyProductionSensor._unrecorded_attributes
    assert ATTR_WH_PERIOD_P10 in excluded
    assert ATTR_WH_PERIOD_P90 in excluded


# ==========================================================================
# get_forecast response band blocks
# ==========================================================================


def test_band_blocks_shape():
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    iso0 = start.isoformat()
    iso1 = (start + timedelta(minutes=15)).isoformat()
    iso2 = (start + timedelta(minutes=60)).isoformat()  # next hour
    data = {
        DATA_KEY_QUANTILE_CURVES: {
            FORECAST_RESP_KEY_P10: {iso0: 50.0, iso1: 50.0, iso2: 30.0},
            FORECAST_RESP_KEY_P50: {iso0: 60.0, iso1: 60.0, iso2: 40.0},
            FORECAST_RESP_KEY_P90: {iso0: 70.0, iso1: 70.0, iso2: 50.0},
        }
    }
    blocks = _band_blocks(data)
    assert set(blocks) == {FORECAST_RESP_KEY_P10, FORECAST_RESP_KEY_P50, FORECAST_RESP_KEY_P90}
    p10 = blocks[FORECAST_RESP_KEY_P10]
    assert p10["wh_period"] == {iso0: 50.0, iso1: 50.0, iso2: 30.0}
    # Hourly roll-up buckets the two 10:00 slots into the 10:00 hour.
    hour0 = start.replace(minute=0).isoformat()
    hour1 = (start + timedelta(hours=1)).replace(minute=0).isoformat()
    assert p10["hourly"][hour0] == pytest.approx(100.0)
    assert p10["hourly"][hour1] == pytest.approx(30.0)


def test_band_blocks_empty_when_absent():
    assert _band_blocks({}) == {}
    assert _band_blocks({DATA_KEY_QUANTILE_CURVES: {}}) == {}
    assert _band_blocks({DATA_KEY_QUANTILE_CURVES: "oops"}) == {}


def test_hourly_from_slots_skips_garbage():
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    out = _hourly_from_slots(
        {start.isoformat(): 40.0, "not-a-date": 10.0, "x": "y", 5: 1.0}
    )
    assert out == {start.replace(minute=0).isoformat(): 40.0}


def test_forecast_response_includes_bands():
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    iso = start.isoformat()
    coord = _FakeCoordinator(
        {
            "slot_starts": [iso],
            "watts": {iso: 400.0},
            "plane_watts": {"M1": [400.0]},
            "hourly_wh": {iso: 100.0},
            "computed_at": iso,
            DATA_KEY_QUANTILE_CURVES: {
                FORECAST_RESP_KEY_P10: {iso: 80.0},
                FORECAST_RESP_KEY_P50: {iso: 100.0},
                FORECAST_RESP_KEY_P90: {iso: 120.0},
            },
            DATA_KEY_BAND_SOURCE: "ensemble",
            DATA_KEY_BAND_SOURCE_BY_DAY: {"2026-07-05": {"ensemble": 1}},
        }
    )

    class _Hass:
        data = {DOMAIN: {"abc123": coord}}

    resp = _build_forecast_response(_Hass(), None)
    entry = resp["entries"]["abc123"]
    # v0.1 keys still present.
    assert entry["total_15min"] == [400.0]
    # v0.4 band blocks added.
    assert entry[FORECAST_RESP_KEY_P10]["wh_period"] == {iso: 80.0}
    assert entry[FORECAST_RESP_KEY_P90]["hourly"][start.replace(minute=0).isoformat()] == pytest.approx(120.0)
    # Band provenance rides along with the band block.
    assert entry["band_source"] == "ensemble"
    assert entry["band_source_by_day"] == {"2026-07-05": {"ensemble": 1}}


def test_forecast_response_omits_bands_when_absent():
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    iso = start.isoformat()
    coord = _FakeCoordinator(
        {
            "slot_starts": [iso],
            "watts": {iso: 400.0},
            "plane_watts": {"M1": [400.0]},
            "hourly_wh": {iso: 100.0},
            "computed_at": iso,
            # The coordinator always carries a band_source (default "learned")
            # even on a quantiles-off / cold-start cycle; the response must NOT
            # surface it without an accompanying band block (no status lie).
            DATA_KEY_BAND_SOURCE: "learned",
        }
    )

    class _Hass:
        data = {DOMAIN: {"abc123": coord}}

    entry = _build_forecast_response(_Hass(), None)["entries"]["abc123"]
    assert FORECAST_RESP_KEY_P10 not in entry
    assert FORECAST_RESP_KEY_P90 not in entry
    # No band block -> no band provenance either (gated together).
    assert "band_source" not in entry
    assert "band_source_by_day" not in entry


# ==========================================================================
# Diagnostics: scoreboard + quantile summaries
# ==========================================================================


def test_diagnostics_scoreboard_summary():
    data = {
        DATA_KEY_SCOREBOARD: _sb(
            engine_daily_kwh_mae=0.4,
            engine_hourly_mae=150.0,
            scored_days=14,
            strata={"clear": {"n": 5, "engine_daily_kwh_mae": 0.2}},
        ),
    }
    out = _scoreboard_summary(_FakeCoordinator(data), data)
    assert out["engine_daily_kwh_mae"] == 0.4
    assert out["engine_hourly_mae"] == 150.0
    assert out["strata"]["clear"]["n"] == 5


def test_diagnostics_scoreboard_absent():
    out = _scoreboard_summary(_FakeCoordinator({}), {})
    assert out == {"available": False}


def test_diagnostics_quantile_summary_accessor():
    class _Coord:
        def quantile_state_summary(self):
            return {"bins": {"clear|midday": 30}, "total_samples": 30}

    out = _quantile_summary(_Coord())
    assert out["total_samples"] == 30


def test_diagnostics_quantile_summary_absent_and_raises():
    class _NoAccessor:
        pass

    assert _quantile_summary(_NoAccessor()) == {"available": False}

    class _Raises:
        def quantile_state_summary(self):
            raise RuntimeError("boom")

    assert "error" in _quantile_summary(_Raises())

    class _NonDict:
        def quantile_state_summary(self):
            return "nope"

    assert _quantile_summary(_NonDict()) == {"available": False}
