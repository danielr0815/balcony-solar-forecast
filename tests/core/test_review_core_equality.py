"""Ensure the historical physics oracle checks the full current result schema."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from balcony_solar_forecast.core.types import ForecastResult, PlaneResult

from .test_engine_split_equivalence import _assert_result_bit_equal


@pytest.mark.parametrize("field", ["ac_watts", "slow_watts"])
def test_whole_result_equivalence_guard_includes_new_curves(field):
    at = datetime(2026, 6, 21, 11, tzinfo=UTC)
    original = ForecastResult(
        slot_starts=(at,), total_watts=(100.0,), hourly_wh={},
        plane_results=(PlaneResult(name="P", watts=(100.0,), slow_watts=(90.0,)),),
        ac_watts=(96.0,),
    )
    if field == "ac_watts":
        changed = replace(original, ac_watts=(95.0,))
    else:
        changed_plane = replace(original.plane_results[0], slow_watts=(89.0,))
        changed = replace(original, plane_results=(changed_plane,))
    with pytest.raises(AssertionError):
        _assert_result_bit_equal(original, changed)
