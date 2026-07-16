"""Tests for the platform layer (sensor / binary_sensor / energy / recorder /
diagnostics).

These modules import Home Assistant; where HA (and its test harness) is not
installed the whole module is skipped. The genuinely pure logic — slot-curve
iteration, per-day attribute slicing, degradation-status mapping and the
service response shape — is exercised against the coordinator's flat
``self.data`` contract (documented on ``BalconySolarCoordinator._build_data``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

from homeassistant.components.sensor import (  # noqa: E402
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower  # noqa: E402
from homeassistant.helpers.entity import EntityCategory  # noqa: E402
from homeassistant.helpers.update_coordinator import (  # noqa: E402
    CoordinatorEntity,
)

from custom_components.balcony_solar_forecast import sensor as sensor_mod  # noqa: E402
from custom_components.balcony_solar_forecast.binary_sensor import (  # noqa: E402
    DegradedSensor,
)
from custom_components.balcony_solar_forecast.energy import (  # noqa: E402
    async_get_solar_forecast,
)
from custom_components.balcony_solar_forecast.recorder import (  # noqa: E402
    exclude_attributes,
)
from custom_components.balcony_solar_forecast.sensor import (  # noqa: E402
    EnergyBandSensor,
    EnergyProductionDcSensor,
    EnergyProductionSensor,
    LastFetchAgeSensor,
    MeasuredAcPowerSensor,
    MeasuredDcTotalSensor,
    PowerNowDcSensor,
    PowerNowSensor,
    SourceStatusSensor,
    _build_forecast_response,
)

DOMAIN = "balcony_solar_forecast"


# Served-AC is a distinct, smaller curve than DC (an inverter efficiency stand-in)
# so a test that asserts the sensor reads the AC key really proves it — the AC and
# DC values never coincide.
_AC_FACTOR = 0.9


def _curve_data(start: datetime, watts: list[float], *, status: str = "fresh"):
    """Build a coordinator-shaped flat data dict for a run of 15-min slots.

    Carries BOTH the DC keys (energy_today_kwh / power_now_w / watts / …) and the
    served-AC siblings (Phase 2: energy_today_kwh_ac / power_now_w_ac / watts_ac /
    …) at a distinct ``_AC_FACTOR`` scale, so the rewired main sensors (which read
    the AC keys) and the DC diagnostics (which read the DC keys) can each be
    pinned unambiguously.
    """
    starts = [start + timedelta(minutes=15 * i) for i in range(len(watts))]
    iso = [s.isoformat() for s in starts]
    day = start.date().isoformat()
    total_wh = sum(watts) * 0.25
    ac_watts = [w * _AC_FACTOR for w in watts]
    ac_total_wh = sum(ac_watts) * 0.25
    return {
        "status": status,
        "degraded": status != "fresh",
        "weather_age_seconds": 120,
        "last_error": None,
        "power_now_w": watts[0],
        "energy_today_kwh": total_wh / 1000.0,
        "energy_tomorrow_kwh": None,
        "energy_d2_kwh": None,
        "watts": {k: v for k, v in zip(iso, watts, strict=False)},
        "wh_period": {k: round(v * 0.25, 2) for k, v in zip(iso, watts, strict=False)},
        "hourly_wh": {start.isoformat(): total_wh},
        "daily_kwh": {day: total_wh / 1000.0},
        # --- served-AC siblings (Phase 2 operator-facing standard) ---
        "power_now_w_ac": ac_watts[0],
        "energy_today_kwh_ac": ac_total_wh / 1000.0,
        "energy_tomorrow_kwh_ac": None,
        "energy_d2_kwh_ac": None,
        "watts_ac": {k: v for k, v in zip(iso, ac_watts, strict=False)},
        "wh_period_ac": {
            k: round(v * 0.25, 2) for k, v in zip(iso, ac_watts, strict=False)
        },
        "hourly_wh_ac": {start.isoformat(): ac_total_wh},
        "daily_kwh_ac": {day: ac_total_wh / 1000.0},
        "slot_starts": iso,
        "plane_watts": {"M1": list(watts)},
        "computed_at": start.isoformat(),
    }


class _FakeEntry:
    entry_id = "abc123"


class _FakeCoordinator:
    def __init__(self, data, *, last_update_success=True):
        self.data = data
        self.entry = _FakeEntry()
        self.last_update_success = last_update_success


def _bare(cls, coordinator, **attrs):
    """Instantiate an entity bypassing HA's CoordinatorEntity.__init__."""
    obj = cls.__new__(cls)
    obj.coordinator = coordinator
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


# --------------------------------------------------------------------------
# Energy sensors: state from the day roll-up, attribute curve sliced per day.
# --------------------------------------------------------------------------


def test_energy_sensor_state_and_curve(monkeypatch):
    fixed = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: fixed)
    monkeypatch.setattr(sensor_mod.dt_util, "as_local", lambda d: d)

    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    coord = _FakeCoordinator(_curve_data(start, [400.0, 400.0, 400.0, 400.0]))

    # Phase 2: the main energy sensor's HEADLINE reads the served-AC roll-up
    # (energy_today_kwh_ac), NOT the DC key — 0.4 kWh DC * _AC_FACTOR == 0.36.
    today = _bare(
        EnergyProductionSensor,
        coord,
        _day_offset=0,
        _energy_key="energy_today_kwh_ac",
    )
    assert today.native_value == pytest.approx(0.4 * _AC_FACTOR)
    attrs = today.extra_state_attributes
    # The 15-min curve attributes stay the DC model curve (the AC bands are
    # hourly-only, so the matching 15-min band attributes remain the DC band
    # shape); the p10/p90 band curves are empty here (no DATA_KEY_QUANTILE_CURVES).
    assert set(attrs) == {"watts", "wh_period", "wh_period_p10", "wh_period_p90"}
    assert len(attrs["watts"]) == 4
    assert attrs["wh_period"][start.isoformat()] == pytest.approx(100.0)
    assert attrs["wh_period_p10"] == {}
    assert attrs["wh_period_p90"] == {}

    # Tomorrow: coordinator AC roll-up is None and no slots fall on that date.
    tomorrow = _bare(
        EnergyProductionSensor,
        coord,
        _day_offset=1,
        _energy_key="energy_tomorrow_kwh_ac",
    )
    assert tomorrow.native_value is None
    assert tomorrow.extra_state_attributes == {
        "watts": {},
        "wh_period": {},
        "wh_period_p10": {},
        "wh_period_p90": {},
    }


def test_energy_sensor_no_data():
    coord = _FakeCoordinator(None, last_update_success=False)
    sensor = _bare(
        EnergyProductionSensor, coord, _day_offset=0, _energy_key="energy_today_kwh"
    )
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {
        "watts": {},
        "wh_period": {},
        "wh_period_p10": {},
        "wh_period_p90": {},
    }


# --------------------------------------------------------------------------
# Power-now, age and status diagnostics.
# --------------------------------------------------------------------------


def test_power_now_reads_coordinator_value():
    # Phase 2: the main power sensor reports the served-AC power (power_now_w_ac),
    # NOT the DC value — 123.45 W DC * _AC_FACTOR == 111.1 W, rounded to 111.1.
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    coord = _FakeCoordinator(_curve_data(start, [123.45, 200.0]))
    sensor = _bare(PowerNowSensor, coord)
    assert sensor.native_value == pytest.approx(round(123.45 * _AC_FACTOR, 1))


def test_last_fetch_age_converts_seconds_to_minutes():
    coord = _FakeCoordinator({"weather_age_seconds": 150})
    sensor = _bare(LastFetchAgeSensor, coord)
    assert sensor.native_value == pytest.approx(2.5)
    # Always available even on a failed update (diagnostics must not vanish).
    assert sensor.available is True


def test_source_status_maps_status_and_failure():
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    ok = _FakeCoordinator(_curve_data(start, [1.0], status="cached"))
    assert _bare(SourceStatusSensor, ok).native_value == "cached"

    failed = _FakeCoordinator(_curve_data(start, [1.0]), last_update_success=False)
    assert _bare(SourceStatusSensor, failed).native_value == "unavailable"


# --------------------------------------------------------------------------
# Model-internal DC diagnostics (Phase 2): read the DC keys, diagnostic-category.
# --------------------------------------------------------------------------


def test_power_now_dc_reads_dc_key():
    # The DC diagnostic reports the DC value (power_now_w), the UN-scaled watts[0],
    # NOT the AC key the main power sensor now reads. Built via the real __init__
    # so the resolved diagnostic entity_category can be pinned.
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    coord = _FakeCoordinator(_curve_data(start, [123.45, 200.0]))
    s = PowerNowDcSensor(coord)
    assert s.native_value == pytest.approx(123.5)
    assert s.entity_category == EntityCategory.DIAGNOSTIC
    assert s.available is True  # diagnostics stay available
    assert s.device_class == SensorDeviceClass.POWER
    assert s.native_unit_of_measurement == UnitOfPower.WATT
    assert s.unique_id == "abc123_power_production_now_dc"


def test_energy_dc_diagnostic_reads_dc_key():
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    coord = _FakeCoordinator(_curve_data(start, [400.0, 400.0, 400.0, 400.0]))
    # The DC diagnostic reports the DC day roll-up (0.4 kWh), NOT the AC headline.
    s = EnergyProductionDcSensor(coord, "energy_production_today_dc", "energy_today_kwh")
    assert s.native_value == pytest.approx(0.4)
    assert s.entity_category == EntityCategory.DIAGNOSTIC
    assert s.device_class == SensorDeviceClass.ENERGY
    assert s.available is True


def test_power_now_exposes_inverter_efficiency():
    """The main AC power sensor carries the per-group eta_inv summary."""

    class _Group:
        def __init__(self, name, eta):
            self.name = name
            self.inverter_efficiency = eta

    class _Site:
        groups = (_Group("WR1", 0.965), _Group("WR2", 0.90))

    class _Coord:
        _site = _Site()
        data = {}

    s = _bare(PowerNowSensor, _Coord())
    assert s.extra_state_attributes == {
        "inverter_efficiency": {"WR1": 0.965, "WR2": 0.90},
        # v0.19.2 status honesty: without an AC-meter calibration the eta map is
        # a verbatim config echo and must say so.
        "inverter_efficiency_source": "config",
    }


# --------------------------------------------------------------------------
# Quantile band sensors now report the served-AC band (Phase 2).
# --------------------------------------------------------------------------


def test_energy_band_sensor_reads_ac_band(monkeypatch):
    from custom_components.balcony_solar_forecast.const import (
        DATA_KEY_QUANTILE_CURVES_AC,
        FORECAST_RESP_KEY_P90,
    )

    fixed = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: fixed)
    monkeypatch.setattr(sensor_mod.dt_util, "as_local", lambda d: d)

    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    # AC band curves are HOURLY Wh: two of today's hours totalling 0.5 kWh.
    data = {
        DATA_KEY_QUANTILE_CURVES_AC: {
            FORECAST_RESP_KEY_P90: {
                start.isoformat(): 300.0,
                (start + timedelta(hours=1)).isoformat(): 200.0,
            }
        }
    }
    coord = _FakeCoordinator(data)
    s = _bare(EnergyBandSensor, coord, _band=FORECAST_RESP_KEY_P90)
    assert s.native_value == pytest.approx(0.5)


def test_energy_band_sensor_none_without_ac_band():
    # No DATA_KEY_QUANTILE_CURVES_AC (quantiles off / cold start) => unknown, not
    # a fabricated spread.
    from custom_components.balcony_solar_forecast.const import FORECAST_RESP_KEY_P10

    coord = _FakeCoordinator({})
    s = _bare(EnergyBandSensor, coord, _band=FORECAST_RESP_KEY_P10)
    assert s.native_value is None
    # v0.19.2 status honesty: with NO band there is no band SOURCE either —
    # the old default labelled a non-existent band "learned".
    assert s.extra_state_attributes is None


def test_energy_band_sensor_band_source_present_with_band(monkeypatch):
    """While a band exists, band_source rides along (default "learned")."""
    from custom_components.balcony_solar_forecast.const import (
        DATA_KEY_QUANTILE_CURVES_AC,
        FORECAST_RESP_KEY_P90,
    )

    fixed = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(sensor_mod.dt_util, "now", lambda: fixed)
    monkeypatch.setattr(sensor_mod.dt_util, "as_local", lambda d: d)
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    coord = _FakeCoordinator({
        DATA_KEY_QUANTILE_CURVES_AC: {
            FORECAST_RESP_KEY_P90: {start.isoformat(): 300.0},
        }
    })
    s = _bare(EnergyBandSensor, coord, _band=FORECAST_RESP_KEY_P90)
    assert s.native_value == pytest.approx(0.3)
    assert s.extra_state_attributes == {"band_source": "learned"}


# --------------------------------------------------------------------------
# Measured site-total DC-power sensor (ground truth; independent of the
# coordinator's forecast cycle).
# --------------------------------------------------------------------------


class _FakeSourceState:
    def __init__(self, state):
        self.state = state


class _StatesRegistry:
    """Minimal ``hass.states`` stand-in: entity_id -> state string map."""

    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, entity_id):
        if entity_id not in self._mapping:
            return None
        return _FakeSourceState(self._mapping[entity_id])


class _StatesHass:
    def __init__(self, mapping):
        self.states = _StatesRegistry(mapping)


class _MeasuredPlane:
    def __init__(self, actual_entity, name=None):
        self.actual_entity = actual_entity
        self.name = name


class _MeasuredSite:
    def __init__(self, planes, ac_actual_entity=None):
        self.planes = tuple(planes)
        # Site-level AC meter (Phase 2); None == not configured.
        self.ac_actual_entity = ac_actual_entity


class _MeasuredEntry:
    def __init__(self):
        self.entry_id = "abc123"
        self.data: dict = {}
        self.options: dict = {}


class _MeasuredCoordinator:
    """Coordinator double exposing the ``_site.planes`` surface + entry that
    ``async_setup_entry`` reads to build (or omit) the measured-total sensor."""

    def __init__(self, actual_entities, ac_actual_entity=None):
        self.entry = _MeasuredEntry()
        # Plane names M1, M2, … aligned with the given actual_entity ids so the
        # measured-total sensor can expose ``source_names`` alongside ``sources``.
        self._site = _MeasuredSite(
            [_MeasuredPlane(a, f"M{i + 1}") for i, a in enumerate(actual_entities)],
            ac_actual_entity=ac_actual_entity,
        )
        self.data = None
        self.last_update_success = True


class _MeasuredHass:
    def __init__(self, coordinator):
        self.data = {DOMAIN: {"abc123": coordinator}}


def test_measured_total_sums_numeric_sources():
    hass = _StatesHass({"sensor.a": "120.0", "sensor.b": "80.0"})
    s = _bare(
        MeasuredDcTotalSensor,
        _FakeCoordinator(None),
        hass=hass,
        _source_ids=["sensor.a", "sensor.b"],
        _source_names=["M1", "M2"],
        _value=None,
        _reporting=0,
    )
    s._recompute()
    assert s.native_value == pytest.approx(200.0)
    assert s.available is True
    attrs = s.extra_state_attributes
    assert attrs == {
        "channels_total": 2,
        "channels_reporting": 2,
        "sources": ["sensor.a", "sensor.b"],
        # Plane names aligned index-for-index with ``sources``.
        "source_names": ["M1", "M2"],
    }


def test_measured_total_skips_unavailable_and_non_numeric():
    hass = _StatesHass(
        {
            "sensor.a": "100.0",
            "sensor.b": "unavailable",
            "sensor.c": "not-a-number",
            "sensor.d": "50.0",
            # sensor.e is absent from the state machine entirely -> skipped.
        }
    )
    s = _bare(
        MeasuredDcTotalSensor,
        _FakeCoordinator(None),
        hass=hass,
        _source_ids=["sensor.a", "sensor.b", "sensor.c", "sensor.d", "sensor.e"],
        _source_names=["M1", "M2", "M3", "M4", "M5"],
        _value=None,
        _reporting=0,
    )
    s._recompute()
    # Only the two numeric sources contribute; the count reflects it.
    assert s.native_value == pytest.approx(150.0)
    attrs = s.extra_state_attributes
    assert attrs["channels_total"] == 5
    assert attrs["channels_reporting"] == 2
    assert s.available is True


def test_measured_total_all_dead_is_unavailable():
    hass = _StatesHass({"sensor.a": "unavailable", "sensor.b": "unknown"})
    s = _bare(
        MeasuredDcTotalSensor,
        _FakeCoordinator(None),
        hass=hass,
        _source_ids=["sensor.a", "sensor.b"],
        _source_names=["M1", "M2"],
        _value=None,
        _reporting=0,
    )
    s._recompute()
    assert s.native_value is None
    assert s.available is False
    assert s.extra_state_attributes["channels_reporting"] == 0


def test_measured_total_available_independent_of_coordinator():
    # The forecast coordinator's last update FAILED, but a live source keeps the
    # measured sensor available (ground truth is decoupled from the forecast).
    hass = _StatesHass({"sensor.a": "42.0"})
    coord = _FakeCoordinator(None, last_update_success=False)
    s = _bare(
        MeasuredDcTotalSensor,
        coord,
        hass=hass,
        _source_ids=["sensor.a"],
        _source_names=["M1"],
        _value=None,
        _reporting=0,
    )
    s._recompute()
    assert coord.last_update_success is False
    assert s.available is True
    assert s.native_value == pytest.approx(42.0)


async def test_measured_total_state_change_event_recomputes(monkeypatch):
    """async_added_to_hass registers the state-change tracker with the source
    ids + recompute callback, seeds once, and firing the callback recomputes."""
    captured: dict = {}

    def _fake_track(hass, ids, cb):
        captured["hass"] = hass
        captured["ids"] = ids
        captured["cb"] = cb
        return lambda: captured.__setitem__("unsub_called", True)

    monkeypatch.setattr(sensor_mod, "async_track_state_change_event", _fake_track)

    async def _noop_super(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop_super)

    mapping = {"sensor.a": "100.0", "sensor.b": "unavailable"}
    hass = _StatesHass(mapping)
    written: list[bool] = []
    s = _bare(
        MeasuredDcTotalSensor,
        _FakeCoordinator(None),
        hass=hass,
        _source_ids=["sensor.a", "sensor.b"],
        _source_names=["M1", "M2"],
        _value=None,
        _reporting=0,
        async_on_remove=lambda f: None,
        async_write_ha_state=lambda: written.append(True),
    )
    await s.async_added_to_hass()
    # Tracker wired to our source ids + the recompute callback.
    assert captured["ids"] == ["sensor.a", "sensor.b"]
    assert captured["cb"] == s._handle_source_event
    assert captured["hass"] is hass
    # Seeded once on add: only sensor.a reports.
    assert s.native_value == pytest.approx(100.0)
    assert s.extra_state_attributes["channels_reporting"] == 1
    assert written == []  # the seed does not write (the platform's add does)
    # A second source comes alive -> firing the captured callback recomputes
    # and publishes.
    mapping["sensor.b"] = "50.0"
    captured["cb"](object())
    assert s.native_value == pytest.approx(150.0)
    assert written == [True]


async def test_measured_total_added_only_when_sources_configured():
    # Planes carry actual_entity ids (deduped, order preserved) -> the sensor is
    # added, wired to those source ids.
    coord = _MeasuredCoordinator(
        ["sensor.a", "sensor.b", "sensor.a", None]
    )
    added: list = []
    await sensor_mod.async_setup_entry(_MeasuredHass(coord), coord.entry, added.extend)
    measured = [e for e in added if isinstance(e, MeasuredDcTotalSensor)]
    assert len(measured) == 1
    assert measured[0]._source_ids == ["sensor.a", "sensor.b"]
    # Plane names align with the de-duplicated ids; the duplicate "sensor.a"
    # (plane M3) keeps M1's name (first-plane-wins, same order as the ids).
    assert measured[0]._source_names == ["M1", "M2"]


async def test_measured_total_omitted_when_no_sources():
    # No plane has an actual_entity -> nothing to sum -> sensor not created.
    coord = _MeasuredCoordinator([None, None])
    added: list = []
    await sensor_mod.async_setup_entry(_MeasuredHass(coord), coord.entry, added.extend)
    assert not any(isinstance(e, MeasuredDcTotalSensor) for e in added)


def test_measured_total_entity_contract_pinned():
    coord = _FakeCoordinator(None)
    s = MeasuredDcTotalSensor(coord, ["sensor.a", "sensor.b"], ["M1", "M2"])
    assert s.extra_state_attributes["source_names"] == ["M1", "M2"]
    assert s.unique_id == "abc123_measured_dc_power_total"
    assert s.translation_key == "measured_dc_power_total"
    assert s.device_class == SensorDeviceClass.POWER
    assert s.native_unit_of_measurement == UnitOfPower.WATT
    assert s.state_class == SensorStateClass.MEASUREMENT
    # NOT excluded from the recorder: the class declares no unrecorded attrs
    # (its history is the whole point) — unlike the curve-bearing energy sensor.
    assert "_unrecorded_attributes" not in MeasuredDcTotalSensor.__dict__


# --------------------------------------------------------------------------
# Measured site-total AC-power sensor (Phase 2): SINGLE meter, ground truth.
# --------------------------------------------------------------------------


def test_measured_ac_reads_single_source():
    hass = _StatesHass({"sensor.ac": "615.0"})
    s = _bare(
        MeasuredAcPowerSensor,
        _FakeCoordinator(None),
        hass=hass,
        _source_id="sensor.ac",
        _value=None,
        _reporting=False,
    )
    s._recompute()
    assert s.native_value == pytest.approx(615.0)
    assert s.available is True
    # Single source, no channels_total (unlike the DC total's sum).
    assert s.extra_state_attributes == {"source": "sensor.ac"}


def test_measured_ac_unavailable_when_dead():
    hass = _StatesHass({"sensor.ac": "unavailable"})
    s = _bare(
        MeasuredAcPowerSensor,
        _FakeCoordinator(None),
        hass=hass,
        _source_id="sensor.ac",
        _value=None,
        _reporting=False,
    )
    s._recompute()
    assert s.native_value is None
    assert s.available is False


def test_measured_ac_available_independent_of_coordinator():
    # The forecast coordinator's last update FAILED, but the live AC meter keeps
    # the measured sensor available (ground truth decoupled from the forecast).
    hass = _StatesHass({"sensor.ac": "42.0"})
    coord = _FakeCoordinator(None, last_update_success=False)
    s = _bare(
        MeasuredAcPowerSensor,
        coord,
        hass=hass,
        _source_id="sensor.ac",
        _value=None,
        _reporting=False,
    )
    s._recompute()
    assert coord.last_update_success is False
    assert s.available is True
    assert s.native_value == pytest.approx(42.0)


async def test_measured_ac_added_only_when_configured():
    coord = _MeasuredCoordinator([None], ac_actual_entity="sensor.site_ac")
    added: list = []
    await sensor_mod.async_setup_entry(_MeasuredHass(coord), coord.entry, added.extend)
    ac = [e for e in added if isinstance(e, MeasuredAcPowerSensor)]
    assert len(ac) == 1
    assert ac[0]._source_id == "sensor.site_ac"


async def test_measured_ac_omitted_when_not_configured():
    # No ac_actual_entity -> nothing to read -> sensor not created.
    coord = _MeasuredCoordinator(["sensor.a"], ac_actual_entity=None)
    added: list = []
    await sensor_mod.async_setup_entry(_MeasuredHass(coord), coord.entry, added.extend)
    assert not any(isinstance(e, MeasuredAcPowerSensor) for e in added)


# --------------------------------------------------------------------------
# Degraded binary sensor.
# --------------------------------------------------------------------------


def test_degraded_sensor_off_when_fresh():
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    coord = _FakeCoordinator(_curve_data(start, [1.0], status="fresh"))
    sensor = _bare(DegradedSensor, coord)
    assert sensor.is_on is False
    assert sensor.available is True
    assert sensor.extra_state_attributes["source_status"] == "fresh"


def test_degraded_sensor_on_when_cached():
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    coord = _FakeCoordinator(_curve_data(start, [1.0], status="cached"))
    sensor = _bare(DegradedSensor, coord)
    assert sensor.is_on is True
    assert sensor.extra_state_attributes["source_status"] == "cached"


def test_degraded_sensor_on_when_update_failed():
    coord = _FakeCoordinator(None, last_update_success=False)
    sensor = _bare(DegradedSensor, coord)
    assert sensor.is_on is True
    assert sensor.available is True
    assert sensor.extra_state_attributes["source_status"] == "unavailable"


# --------------------------------------------------------------------------
# Service response shape.
# --------------------------------------------------------------------------


def test_build_forecast_response_shape():
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    coord = _FakeCoordinator(_curve_data(start, [100.0, 200.0]))

    class _Hass:
        data = {DOMAIN: {"abc123": coord}}

    resp = _build_forecast_response(_Hass(), None)
    assert set(resp) == {"entries"}
    entry = resp["entries"]["abc123"]
    assert entry["total_15min"] == [100.0, 200.0]
    assert entry["planes"]["M1"] == [100.0, 200.0]
    assert entry["issued_at"] == start.isoformat()
    assert entry["slot_starts"][0] == start.isoformat()
    assert entry["total_hourly"]


def test_build_forecast_response_no_data():
    coord = _FakeCoordinator(None)

    class _Hass:
        data = {DOMAIN: {"abc123": coord}}

    resp = _build_forecast_response(_Hass(), None)
    assert resp["entries"]["abc123"] == {
        "planes": {},
        "slot_starts": [],
        "total_15min": [],
        "total_hourly": {},
        "issued_at": None,
    }


def test_build_forecast_response_entry_filter():
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    coordA = _FakeCoordinator(_curve_data(start, [50.0]))
    coordB = _FakeCoordinator(_curve_data(start, [50.0]))

    class _Hass:
        data = {DOMAIN: {"A": coordA, "B": coordB}}

    resp = _build_forecast_response(_Hass(), "B")
    assert list(resp["entries"]) == ["B"]


# --------------------------------------------------------------------------
# Recorder + energy hook.
# --------------------------------------------------------------------------


def test_recorder_excludes_curve_attributes():
    assert exclude_attributes(object()) == {
        "watts",
        "wh_period",
        # Shade-profile diagram curve arrays (SPEC §5) — bulky, per-selection.
        "time",
        "azimuth",
        "sun_elevation",
        "transmittance",
        "transmittance_individual",
        "sample_n",
        "horizon_azimuth",
        "static_horizon",
        "shade_horizon",
    }


async def test_energy_hook_returns_wh_hours():
    # Phase 2: the Energy dashboard hook returns the served-AC hourly curve
    # (hourly_wh_ac) — 400 W * 4 slots * 0.25 h == 400 Wh DC, * _AC_FACTOR AC.
    start = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
    coord = _FakeCoordinator(_curve_data(start, [400.0, 400.0, 400.0, 400.0]))

    class _Hass:
        data = {DOMAIN: {"eid": coord}}

    out = await async_get_solar_forecast(_Hass(), "eid")
    assert set(out) == {"wh_hours"}
    assert out["wh_hours"][start.isoformat()] == pytest.approx(400.0 * _AC_FACTOR)


async def test_energy_hook_unknown_entry_returns_none():
    class _Hass:
        data = {DOMAIN: {}}

    assert await async_get_solar_forecast(_Hass(), "nope") is None


async def test_energy_hook_no_forecast_returns_none():
    # No served-AC hourly curve => no overlay (Phase 2 reads hourly_wh_ac).
    coord = _FakeCoordinator({"hourly_wh_ac": {}})

    class _Hass:
        data = {DOMAIN: {"eid": coord}}

    assert await async_get_solar_forecast(_Hass(), "eid") is None
