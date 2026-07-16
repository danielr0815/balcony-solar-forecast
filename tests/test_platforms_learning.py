"""Tests for the learning-layer diagnostic entities (SPEC §5, §9).

Covers the intraday-scalar sensor, the drift-MAE sensor (state + attributes),
the per-layer learner-status ENUM sensors, and the fast/slow learner-active
binary sensors. All read the coordinator's flat ``self.data`` learner keys and
must stay available (diagnostics never vanish) and tolerate missing/unknown
values without raising.

Needs Home Assistant; skipped on the plain-core path.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

from balcony_solar_forecast.binary_sensor import (  # noqa: E402
    LearnerActiveSensor,
)
from balcony_solar_forecast.const import (  # noqa: E402
    DATA_KEY_DRIFT_MAE,
    DATA_KEY_INTRADAY_SCALAR,
    DATA_KEY_LEARNER_STATUS,
)
from balcony_solar_forecast.sensor import (  # noqa: E402
    LEARNER_LAYER_DAY_AHEAD,
    LEARNER_LAYER_FAST,
    LEARNER_LAYER_SLOW,
    LEARNER_STATUS_ACTIVE,
    LEARNER_STATUS_DISABLED_BY_DRIFT,
    LEARNER_STATUS_OFF,
    DriftMaeCorrectedSensor,
    IntradayScalarSensor,
    LearnerStatusSensor,
)


class _FakeEntry:
    entry_id = "abc123"


class _FakeCoordinator:
    def __init__(self, data, *, last_update_success=True):
        self.data = data
        self.entry = _FakeEntry()
        self.last_update_success = last_update_success


def _bare(cls, coordinator, **attrs):
    obj = cls.__new__(cls)
    obj.coordinator = coordinator
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


# --------------------------------------------------------------------------
# Intraday scalar sensor.
# --------------------------------------------------------------------------


def test_intraday_scalar_value_and_rounding():
    coord = _FakeCoordinator({DATA_KEY_INTRADAY_SCALAR: 1.23456})
    sensor = _bare(IntradayScalarSensor, coord)
    assert sensor.native_value == pytest.approx(1.235)
    assert sensor.available is True


def test_intraday_scalar_none_when_missing():
    coord = _FakeCoordinator({})
    assert _bare(IntradayScalarSensor, coord).native_value is None
    # Even with no data at all (unavailable forecast) the diagnostic survives.
    coord2 = _FakeCoordinator(None, last_update_success=False)
    assert _bare(IntradayScalarSensor, coord2).native_value is None


# --------------------------------------------------------------------------
# Drift MAE sensor: state = corrected, attributes carry raw/corrected/baseline.
# --------------------------------------------------------------------------


def test_drift_mae_state_and_attributes():
    coord = _FakeCoordinator(
        {DATA_KEY_DRIFT_MAE: {"raw": 210.4, "corrected": 150.66, "baseline": 260.1}}
    )
    sensor = _bare(DriftMaeCorrectedSensor, coord)
    assert sensor.native_value == pytest.approx(150.7)
    attrs = sensor.extra_state_attributes
    assert attrs == {
        "raw_mae": 210.4,
        "corrected_mae": 150.7,
        "baseline_mae": 260.1,
    }


def test_drift_mae_missing_is_none():
    coord = _FakeCoordinator({})
    sensor = _bare(DriftMaeCorrectedSensor, coord)
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {
        "raw_mae": None,
        "corrected_mae": None,
        "baseline_mae": None,
    }


def test_drift_mae_non_dict_tolerated():
    coord = _FakeCoordinator({DATA_KEY_DRIFT_MAE: "oops"})
    sensor = _bare(DriftMaeCorrectedSensor, coord)
    assert sensor.native_value is None


# --------------------------------------------------------------------------
# Learner status ENUM sensors (one per layer).
# --------------------------------------------------------------------------


def test_learner_status_reads_layer():
    coord = _FakeCoordinator(
        {
            DATA_KEY_LEARNER_STATUS: {
                LEARNER_LAYER_FAST: LEARNER_STATUS_ACTIVE,
                LEARNER_LAYER_SLOW: LEARNER_STATUS_DISABLED_BY_DRIFT,
                LEARNER_LAYER_DAY_AHEAD: LEARNER_STATUS_OFF,
            }
        }
    )
    fast = _bare(LearnerStatusSensor, coord, _layer=LEARNER_LAYER_FAST)
    slow = _bare(LearnerStatusSensor, coord, _layer=LEARNER_LAYER_SLOW)
    day = _bare(LearnerStatusSensor, coord, _layer=LEARNER_LAYER_DAY_AHEAD)
    assert fast.native_value == LEARNER_STATUS_ACTIVE
    assert slow.native_value == LEARNER_STATUS_DISABLED_BY_DRIFT
    assert day.native_value == LEARNER_STATUS_OFF


def test_learner_status_day_ahead_bias_cells_attrs_present_when_empty():
    """v0.19.2: with NO learned cells the day-ahead status sensor still shows
    ``bias_cells: {}`` + ``cells_n: 0`` — a deliberate reset must be
    distinguishable from a broken attribute pipeline (the attribute used to
    vanish entirely). With cells, they ride along verbatim + counted."""
    from custom_components.balcony_solar_forecast.const import DATA_KEY_BIAS_CELLS

    empty = _bare(
        LearnerStatusSensor,
        _FakeCoordinator({DATA_KEY_BIAS_CELLS: {}}),
        _layer=LEARNER_LAYER_DAY_AHEAD,
    )
    assert empty.extra_state_attributes == {"bias_cells": {}, "cells_n": 0}

    cells = {"clear|midday": {"theta": 0.9, "n": 4, "applied": 0.9}}
    trained = _bare(
        LearnerStatusSensor,
        _FakeCoordinator({DATA_KEY_BIAS_CELLS: cells}),
        _layer=LEARNER_LAYER_DAY_AHEAD,
    )
    assert trained.extra_state_attributes == {"bias_cells": cells, "cells_n": 1}

    # Other layers stay attribute-free.
    fast = _bare(
        LearnerStatusSensor,
        _FakeCoordinator({DATA_KEY_BIAS_CELLS: cells}),
        _layer=LEARNER_LAYER_FAST,
    )
    assert fast.extra_state_attributes is None


def test_learner_status_unknown_value_is_none():
    coord = _FakeCoordinator(
        {DATA_KEY_LEARNER_STATUS: {LEARNER_LAYER_FAST: "bogus"}}
    )
    sensor = _bare(LearnerStatusSensor, coord, _layer=LEARNER_LAYER_FAST)
    # Undeclared enum values are reported as unknown, never leaked to HA.
    assert sensor.native_value is None


def test_learner_status_missing_map_is_none():
    coord = _FakeCoordinator({})
    sensor = _bare(LearnerStatusSensor, coord, _layer=LEARNER_LAYER_FAST)
    assert sensor.native_value is None
    coord2 = _FakeCoordinator({DATA_KEY_LEARNER_STATUS: "not-a-dict"})
    sensor2 = _bare(LearnerStatusSensor, coord2, _layer=LEARNER_LAYER_FAST)
    assert sensor2.native_value is None


# --------------------------------------------------------------------------
# Learner-active binary sensors.
# --------------------------------------------------------------------------


def test_learner_active_on_only_when_active():
    coord = _FakeCoordinator(
        {
            DATA_KEY_LEARNER_STATUS: {
                LEARNER_LAYER_FAST: LEARNER_STATUS_ACTIVE,
                LEARNER_LAYER_SLOW: LEARNER_STATUS_OFF,
            }
        }
    )
    fast = _bare(LearnerActiveSensor, coord, _layer=LEARNER_LAYER_FAST)
    slow = _bare(LearnerActiveSensor, coord, _layer=LEARNER_LAYER_SLOW)
    assert fast.is_on is True
    assert slow.is_on is False
    assert fast.available is True
    assert fast.extra_state_attributes == {"status": LEARNER_STATUS_ACTIVE}
    assert slow.extra_state_attributes == {"status": LEARNER_STATUS_OFF}


def test_learner_active_none_when_status_missing():
    coord = _FakeCoordinator({})
    sensor = _bare(LearnerActiveSensor, coord, _layer=LEARNER_LAYER_FAST)
    assert sensor.is_on is None
    assert sensor.extra_state_attributes == {"status": None}
    assert sensor.available is True
