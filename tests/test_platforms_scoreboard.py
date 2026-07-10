"""Tests for the v0.4 platform layer (SPEC §6, §9, §10).

Covers the skill-scoreboard sensors (engine daily/hourly MAE, per-comparison
MAE, engine-vs-best-baseline percent), the kill-gate binary sensor, the daily
P10/P90 quantile energy sensors, the p10/p90 wh_period band attributes on the
served energy sensor, the extended get_forecast response band blocks, the
options-flow comparison list + quantile switch, and the diagnostics scoreboard/
quantile summaries.

All read the coordinator's flat ``self.data`` v0.4 keys and must:
  * stay available where they are diagnostics (never vanish);
  * report ``None`` — never a fabricated zero — when the scoreboard/quantiles
    are absent, disabled or cold-started (SPEC §9 "no premature verdict",
    SPEC §6 "no fake spread");
  * tolerate missing / malformed values without raising.

Needs Home Assistant; skipped on the plain-core path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

from balcony_solar_forecast import sensor as sensor_mod  # noqa: E402
from balcony_solar_forecast.binary_sensor import (  # noqa: E402
    KillGatePassedSensor,
)
from balcony_solar_forecast.const import (  # noqa: E402
    ATTR_WH_PERIOD_P10,
    ATTR_WH_PERIOD_P90,
    CONF_COMPARISON_SENSORS,
    DATA_KEY_KILL_GATE_PASSED,
    DATA_KEY_QUANTILE_CURVES,
    DATA_KEY_SCOREBOARD,
    FORECAST_RESP_KEY_P10,
    FORECAST_RESP_KEY_P50,
    FORECAST_RESP_KEY_P90,
)
from balcony_solar_forecast.core.types import ComparisonConfig  # noqa: E402
from balcony_solar_forecast.diagnostics import (  # noqa: E402
    _quantile_summary,
    _scoreboard_summary,
)
from balcony_solar_forecast.sensor import (  # noqa: E402
    ComparisonDailyKwhMaeSensor,
    EnergyBandSensor,
    EnergyProductionSensor,
    EngineDailyKwhMaeSensor,
    EngineHourlyMaeSensor,
    EngineVsBestBaselinePctSensor,
    _band_blocks,
    _build_forecast_response,
    _configured_comparisons,
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
        "comparison_daily_kwh_mae": {},
        "engine_vs_best_baseline_pct": None,
        "kill_gate_passed": None,
        "window_days": 14,
        "scored_days": 0,
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


def test_engine_vs_best_baseline_pct_sign_preserved():
    # Positive == engine better.
    coord = _FakeCoordinator(
        {DATA_KEY_SCOREBOARD: _sb(engine_vs_best_baseline_pct=12.34)}
    )
    assert _bare(
        EngineVsBestBaselinePctSensor, coord
    ).native_value == pytest.approx(12.3)
    # Negative == engine worse (must not be clamped to zero).
    coord_neg = _FakeCoordinator(
        {DATA_KEY_SCOREBOARD: _sb(engine_vs_best_baseline_pct=-8.77)}
    )
    assert _bare(
        EngineVsBestBaselinePctSensor, coord_neg
    ).native_value == pytest.approx(-8.8)
    # None passes through.
    assert _bare(
        EngineVsBestBaselinePctSensor, _FakeCoordinator({})
    ).native_value is None


# ==========================================================================
# Per-comparison MAE sensors (dynamic)
# ==========================================================================


def test_comparison_mae_reads_by_name():
    cmp = ComparisonConfig(name="8-Entry Baseline", daily_entity="sensor.x")
    coord = _FakeCoordinator(
        {
            DATA_KEY_SCOREBOARD: _sb(
                comparison_daily_kwh_mae={"8-Entry Baseline": 0.812, "Other": 1.0}
            )
        }
    )
    sensor = _bare(ComparisonDailyKwhMaeSensor, coord, _comparison=cmp)
    assert sensor.native_value == pytest.approx(0.812)
    assert sensor.extra_state_attributes == {
        "comparison_name": "8-Entry Baseline",
        "daily_entity": "sensor.x",
    }


def test_comparison_mae_none_until_scored():
    cmp = ComparisonConfig(name="Alt 1600W", daily_entity="sensor.y")
    # Comparison configured but not yet in the map (no scored day) -> None.
    coord = _FakeCoordinator({DATA_KEY_SCOREBOARD: _sb(comparison_daily_kwh_mae={})})
    assert _bare(ComparisonDailyKwhMaeSensor, coord, _comparison=cmp).native_value is None
    # No scoreboard at all -> None.
    assert _bare(
        ComparisonDailyKwhMaeSensor, _FakeCoordinator({}), _comparison=cmp
    ).native_value is None


def test_comparison_sensor_unique_id_and_slug():
    cmp = ComparisonConfig(name="8-Entry Baseline!", daily_entity="sensor.x")
    coord = _FakeCoordinator({})
    sensor = ComparisonDailyKwhMaeSensor(coord, cmp)
    # Slug drops punctuation; unique id embeds it so a rename mints a new sensor.
    assert cmp.slug == "8_entry_baseline"
    assert sensor.unique_id == "abc123_comparison_daily_kwh_mae_8_entry_baseline"
    assert sensor.name == "Comparison daily kWh MAE 8-Entry Baseline!"
    # The object_id is pinned via the SUPPORTED integration-suggested path — a
    # pre-set entity_id (the former _attr_suggested_object_id does not exist in
    # HA and was silently ignored). It must equal the documented dashboard id.
    assert sensor.entity_id == (
        "sensor.balcony_solar_forecast_comparison_daily_kwh_mae_8_entry_baseline"
    )


def test_comparison_slug_is_strictly_ascii():
    """A non-ASCII label ("Süd") must slugify to ASCII: the slug is embedded in
    the unique_id AND the pre-set entity_id, where non-ASCII is invalid — and
    the documented dashboard id must name the real entity."""
    cmp = ComparisonConfig(name="PV Süd", daily_entity="sensor.z")
    assert cmp.slug == "pv_s_d"
    sensor = ComparisonDailyKwhMaeSensor(_FakeCoordinator({}), cmp)
    assert sensor.entity_id == (
        "sensor.balcony_solar_forecast_comparison_daily_kwh_mae_pv_s_d"
    )
    assert sensor.entity_id.isascii()


# ==========================================================================
# _configured_comparisons: reads merged entry config, drops malformed rows
# ==========================================================================


def test_configured_comparisons_from_options():
    entry = _FakeEntry(
        options={
            CONF_COMPARISON_SENSORS: [
                {"name": "A", "daily_entity": "sensor.a"},
                {"name": "B", "daily_entity": "sensor.b"},
                {"name": "", "daily_entity": "sensor.c"},  # dropped (no name)
                {"name": "D"},  # dropped (no entity)
                "garbage",  # dropped (not a dict)
            ]
        }
    )
    coord = _FakeCoordinator({}, entry=entry)
    cmps = _configured_comparisons(coord)
    assert [c.name for c in cmps] == ["A", "B"]


def test_configured_comparisons_options_override_data():
    entry = _FakeEntry(
        data={CONF_COMPARISON_SENSORS: [{"name": "old", "daily_entity": "sensor.o"}]},
        options={CONF_COMPARISON_SENSORS: [{"name": "new", "daily_entity": "sensor.n"}]},
    )
    coord = _FakeCoordinator({}, entry=entry)
    assert [c.name for c in _configured_comparisons(coord)] == ["new"]


def test_configured_comparisons_empty_by_default():
    coord = _FakeCoordinator({}, entry=_FakeEntry())
    assert _configured_comparisons(coord) == ()


# ==========================================================================
# Kill-gate binary sensor
# ==========================================================================


def test_kill_gate_on_off_none():
    on = _FakeCoordinator({DATA_KEY_KILL_GATE_PASSED: True})
    off = _FakeCoordinator({DATA_KEY_KILL_GATE_PASSED: False})
    unknown = _FakeCoordinator({DATA_KEY_KILL_GATE_PASSED: None})
    absent = _FakeCoordinator({})
    assert _bare(KillGatePassedSensor, on).is_on is True
    assert _bare(KillGatePassedSensor, off).is_on is False
    # None (window not full) and absent both -> unknown, never a premature pass.
    assert _bare(KillGatePassedSensor, unknown).is_on is None
    assert _bare(KillGatePassedSensor, absent).is_on is None


def test_kill_gate_always_available_and_attrs():
    coord = _FakeCoordinator(
        {
            DATA_KEY_KILL_GATE_PASSED: True,
            DATA_KEY_SCOREBOARD: _sb(
                window_days=14,
                scored_days=14,
                engine_daily_kwh_mae=0.4,
                engine_vs_best_baseline_pct=15.0,
            ),
        },
        last_update_success=False,
    )
    sensor = _bare(KillGatePassedSensor, coord)
    assert sensor.available is True  # survives an unavailable forecast
    attrs = sensor.extra_state_attributes
    assert attrs["window_days"] == 14
    assert attrs["scored_days"] == 14
    assert attrs["engine_daily_kwh_mae"] == 0.4
    assert attrs["engine_vs_best_baseline_pct"] == 15.0


def test_kill_gate_non_bool_flag_is_none():
    # A stray non-bool value must not leak through as truthy.
    coord = _FakeCoordinator({DATA_KEY_KILL_GATE_PASSED: "yes"})
    assert _bare(KillGatePassedSensor, coord).is_on is None


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

    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    curve = _band_curve(start, [100.0, 100.0, 100.0, 100.0])  # 400 Wh today
    coord = _FakeCoordinator(
        {DATA_KEY_QUANTILE_CURVES: {FORECAST_RESP_KEY_P10: curve}}
    )
    sensor = _bare(EnergyBandSensor, coord, _band=FORECAST_RESP_KEY_P10)
    assert sensor.native_value == pytest.approx(0.4)


def test_energy_band_sensor_none_when_no_band(monkeypatch):
    fixed = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: fixed)
    monkeypatch.setattr(sensor_mod.dt_util, "as_local", lambda d: d)

    # Quantiles off / cold start: no curve -> None (no fabricated spread).
    coord = _FakeCoordinator({})
    assert _bare(EnergyBandSensor, coord, _band=FORECAST_RESP_KEY_P90).native_value is None
    coord2 = _FakeCoordinator({DATA_KEY_QUANTILE_CURVES: {FORECAST_RESP_KEY_P90: {}}})
    assert _bare(EnergyBandSensor, coord2, _band=FORECAST_RESP_KEY_P90).native_value is None


def test_energy_band_sensor_none_when_all_slots_other_day(monkeypatch):
    fixed = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: fixed)
    monkeypatch.setattr(sensor_mod.dt_util, "as_local", lambda d: d)

    start = datetime(2026, 7, 6, 10, 0, tzinfo=UTC)  # tomorrow's slots
    curve = _band_curve(start, [100.0, 100.0])
    coord = _FakeCoordinator(
        {DATA_KEY_QUANTILE_CURVES: {FORECAST_RESP_KEY_P10: curve}}
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
        }
    )

    class _Hass:
        data = {DOMAIN: {"abc123": coord}}

    entry = _build_forecast_response(_Hass(), None)["entries"]["abc123"]
    assert FORECAST_RESP_KEY_P10 not in entry
    assert FORECAST_RESP_KEY_P90 not in entry


# ==========================================================================
# Diagnostics: scoreboard + quantile summaries
# ==========================================================================


def test_diagnostics_scoreboard_summary():
    data = {
        DATA_KEY_SCOREBOARD: _sb(
            engine_daily_kwh_mae=0.4,
            engine_hourly_mae=150.0,
            comparison_daily_kwh_mae={"A": 0.6},
            engine_vs_best_baseline_pct=33.3,
            kill_gate_passed=True,
            scored_days=14,
            strata={"clear": {"n": 5, "engine_daily_kwh_mae": 0.2}},
        ),
        DATA_KEY_KILL_GATE_PASSED: True,
    }
    out = _scoreboard_summary(_FakeCoordinator(data), data)
    assert out["engine_daily_kwh_mae"] == 0.4
    assert out["comparison_daily_kwh_mae"] == {"A": 0.6}
    assert out["strata"]["clear"]["n"] == 5
    assert out["kill_gate_passed_flag"] is True


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
