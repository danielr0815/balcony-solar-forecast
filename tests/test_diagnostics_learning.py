"""Tests for the learner-state diagnostics summary (SPEC §5).

The diagnostics dump must carry a compact learner summary (per-layer status,
intraday scalar, drift MAE, correction source) plus the optional persisted-
state counts from ``coordinator.learner_state_summary()`` — tolerating its
absence or a raise — and must route the whole thing through the coordinate
redactor.

Needs Home Assistant; skipped on the plain-core path.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from balcony_solar_forecast.const import (  # noqa: E402
    DATA_KEY_CORRECTION_SOURCE,
    DATA_KEY_DRIFT_MAE,
    DATA_KEY_INTRADAY_SCALAR,
    DATA_KEY_LEARNER_STATUS,
)
from balcony_solar_forecast.diagnostics import _learner_summary  # noqa: E402


class _Coord:
    def __init__(self, data, *, summary=None, summary_raises=False):
        self.data = data
        self._summary = summary
        self._raises = summary_raises

    def learner_state_summary(self):
        if self._raises:
            raise RuntimeError("boom")
        return self._summary


class _CoordNoSummary:
    def __init__(self, data):
        self.data = data


def _data():
    return {
        DATA_KEY_LEARNER_STATUS: {"fast": "active", "slow": "off"},
        DATA_KEY_INTRADAY_SCALAR: 1.1,
        DATA_KEY_DRIFT_MAE: {"raw": 200.0, "corrected": 150.0, "baseline": 250.0},
        DATA_KEY_CORRECTION_SOURCE: "intraday",
    }


def test_learner_summary_live_fields():
    coord = _Coord(_data(), summary={"bias_cells": 5, "shademap_channels": 3})
    out = _learner_summary(coord, coord.data)
    assert out["status"] == {"fast": "active", "slow": "off"}
    assert out["intraday_scalar"] == 1.1
    assert out["drift_mae"]["corrected"] == 150.0
    assert out["correction_source"] == "intraday"
    assert out["state"] == {"bias_cells": 5, "shademap_channels": 3}


def test_learner_summary_absent_accessor():
    coord = _CoordNoSummary(_data())
    out = _learner_summary(coord, coord.data)
    assert out["state"] == {"available": False}
    assert out["status"] == {"fast": "active", "slow": "off"}


def test_learner_summary_accessor_raises_is_caught():
    coord = _Coord(_data(), summary_raises=True)
    out = _learner_summary(coord, coord.data)
    assert "error" in out["state"]


def test_learner_summary_accessor_non_dict():
    coord = _Coord(_data(), summary="not-a-dict")
    out = _learner_summary(coord, coord.data)
    assert out["state"] == {"available": False}


def test_learner_summary_empty_data():
    coord = _CoordNoSummary({})
    out = _learner_summary(coord, {})
    assert out["status"] is None
    assert out["intraday_scalar"] is None
    assert out["drift_mae"] is None
    assert out["state"] == {"available": False}
