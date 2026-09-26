"""Behavior-preserving review contracts: full results and ownership boundaries."""

from __future__ import annotations

import random
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from balcony_solar_forecast.core import bias, quantiles, shademap
from balcony_solar_forecast.core.curves import EnergyTotals
from balcony_solar_forecast.core.types import (
    BiasCell,
    BiasState,
    IssuedSnapshot,
    LearnerSnapshot,
    PlaneHourlyModeled,
    QuantileState,
    ShademapBin,
    ShademapState,
)


def _frozen_energy_rollup(slots, tz):
    """Frozen engine roll-up before EnergyTotals extraction (2026-09 review)."""
    hourly_wh = {}
    daily_kwh = {}
    for start, watts in slots:
        hkey = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0).isoformat()
        day_key = start.astimezone(tz).date().isoformat()
        wh = watts * 0.25
        hourly_wh[hkey] = hourly_wh.get(hkey, 0.0) + wh
        daily_kwh[day_key] = daily_kwh.get(day_key, 0.0) + wh / 1000.0
    return hourly_wh, daily_kwh


@pytest.mark.parametrize("zone", ["UTC", "Europe/Berlin", "Pacific/Auckland"])
def test_energy_extraction_is_bit_identical_across_calendar_boundaries(zone):
    rng = random.Random(20260926)
    tz = ZoneInfo(zone)
    start = datetime(2026, 10, 24, 17, tzinfo=UTC)
    slots = [(start + timedelta(minutes=15 * i), rng.uniform(0.0, 5000.0)) for i in range(1200)]
    expected_hourly, expected_daily = _frozen_energy_rollup(slots, tz)
    totals = EnergyTotals(tz=tz)
    for at, watts in slots:
        totals.add(at, watts)
    assert totals.hourly_wh == expected_hourly
    assert totals.daily_kwh == expected_daily


def test_rollback_snapshot_remains_independent_of_future_training_and_export_edits():
    state = LearnerSnapshot(
        taken_at="2026-06-21T00:00:00+00:00",
        bias=BiasState(cells={"clear|midday": BiasCell(theta=0.9, n=10)}),
        shademap=ShademapState(channels={"P": {"1:1:0": ShademapBin(0.6, 10)}}),
        quantile=QuantileState(bins={"clear|midday": [["2026-06-20", 0.9]]}),
    )
    before = state.to_dict()
    restored = LearnerSnapshot.from_dict(before)
    bias.train_day_ahead_bias(restored.bias, [bias.DayAheadSample("clear", "midday", 80.0, 100.0)])
    shademap.update_bin(
        restored.shademap, channel="P", sun_az=5.0, sun_el=2.5, doy=1, measured_t=0.8,
    )
    quantiles.train_quantiles(
        restored.quantile, [quantiles.QuantileSample("clear", "midday", 80.0, 100.0)],
        training_date="2026-06-21",
    )
    exported = restored.to_dict()
    exported["bias"]["cells"]["clear|midday"]["theta"] = 0.5
    exported["shademap"]["channels"]["P"]["1:1:0"]["tau"] = 0.1
    exported["quantile"]["bins"]["clear|midday"][0][1] = 0.1
    assert restored.to_dict() == before
    assert state.to_dict() == before


def test_issued_snapshot_load_and_export_own_their_nested_curves():
    hour = "2026-06-21T11:00:00+00:00"
    issued = IssuedSnapshot(
        issued_at="2026-06-21T00:00:00+00:00", status="fresh",
        corrected_hourly_wh={hour: 100.0},
        per_plane={"P": PlaneHourlyModeled(corrected_wh={hour: 100.0})},
    )
    stored = issued.to_dict()
    expected = deepcopy(stored)
    restored = IssuedSnapshot.from_dict(stored)
    stored["corrected_hourly_wh"][hour] = 0.0
    stored["per_plane"]["P"]["corrected_wh"][hour] = 0.0
    exported = restored.to_dict()
    exported["per_plane"]["P"]["corrected_wh"][hour] = 50.0
    assert issued.to_dict() == expected
    assert restored.to_dict() == expected
