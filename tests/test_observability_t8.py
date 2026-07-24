"""Observability accessors — forensik tranche T8 (B3/B4/B5).

Covers the diagnostic surfaces the 7-day forensik added:

  * ``coordinator.store_stats()`` / ``learner_state_summary()`` return real
    counts (SPEC-2) so diagnostics no longer report a false ``available: false``;
  * ``quantile_state_summary()`` gates ``trained`` on BOTH the sample count AND
    the effective-day count (QRC-4): 20 samples from only 3 days -> not trained;
  * ``_bias_cells_summary()`` flags a cell whose theta sits at the day-ahead band
    edge with ``clamped: true`` (SCT-4).

The coordinator module imports Home Assistant, so this module is skipped on the
plain-core path. The methods under test are pure state readers, so a bare
``__new__``-built coordinator with the relevant state attributes set is enough.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    DAY_AHEAD_BIAS_MAX,
    DAY_AHEAD_BIAS_MIN,
    DAY_PART_MIDDAY,
    DAY_PART_MORNING,
    LEARNER_SNAPSHOT_RING,
    QUANTILE_MIN_DAYS,
    QUANTILE_MIN_SAMPLES,
)
from custom_components.balcony_solar_forecast.coordinator import (  # noqa: E402
    BalconySolarCoordinator,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasCell,
    BiasState,
    QuantileState,
    ShademapBin,
    ShademapState,
)


def _bare_coordinator() -> BalconySolarCoordinator:
    c = BalconySolarCoordinator.__new__(BalconySolarCoordinator)
    c._bias_state = BiasState()
    c._quantile_state = QuantileState()
    c._shademap_state = ShademapState()
    c._quantiles_enabled = True
    return c


# ---------------------------------------------------------------------------
# B5b — quantile_state_summary trained gate (n AND days)
# ---------------------------------------------------------------------------


def test_quantile_trained_needs_enough_days_not_just_samples():
    """20 samples from only 3 distinct days -> not trained (QRC-4)."""
    assert QUANTILE_MIN_SAMPLES <= 20
    assert QUANTILE_MIN_DAYS > 3
    key = QuantileState.bin_key("clear", DAY_PART_MIDDAY)
    # 20 dated samples spread over exactly 3 calendar days.
    dates = ["2026-07-01", "2026-07-02", "2026-07-03"]
    ring = [[dates[i % 3], 1.0] for i in range(20)]
    c = _bare_coordinator()
    c._quantile_state = QuantileState(bins={key: ring})

    summary = c.quantile_state_summary()
    assert summary["available"] is True
    bin_stat = summary["bins"][key]
    assert bin_stat["n"] == 20
    assert bin_stat["days"] == 3
    # Enough samples but too few days -> the serving band collapses, so trained
    # must report False (it previously lied True on the sample count alone).
    assert bin_stat["trained"] is False


def test_quantile_trained_true_with_enough_days():
    key = QuantileState.bin_key("clear", DAY_PART_MIDDAY)
    n = max(QUANTILE_MIN_SAMPLES, QUANTILE_MIN_DAYS)
    ring = [[f"2026-07-{(i % QUANTILE_MIN_DAYS) + 1:02d}", 1.0] for i in range(n)]
    c = _bare_coordinator()
    c._quantile_state = QuantileState(bins={key: ring})

    bin_stat = c.quantile_state_summary()["bins"][key]
    assert bin_stat["n"] == n
    assert bin_stat["days"] >= QUANTILE_MIN_DAYS
    assert bin_stat["trained"] is True


# ---------------------------------------------------------------------------
# B4b — day-ahead bias clamped flag
# ---------------------------------------------------------------------------


def test_bias_cells_summary_flags_clamped_at_band_edge():
    c = _bare_coordinator()
    # One cell pinned to the ceiling (theta above MAX clamps to MAX), one well
    # inside the band, one at the floor.
    c._bias_state = BiasState(
        cells={
            BiasState.cell_key("clear", DAY_PART_MORNING): BiasCell(
                theta=DAY_AHEAD_BIAS_MAX + 0.5, n=99
            ),
            BiasState.cell_key("clear", DAY_PART_MIDDAY): BiasCell(
                theta=1.05, n=99
            ),
            BiasState.cell_key("overcast", DAY_PART_MIDDAY): BiasCell(
                theta=DAY_AHEAD_BIAS_MIN - 0.5, n=99
            ),
        }
    )
    out = c._bias_cells_summary()
    assert out["clear|morning"]["clamped"] is True
    assert out["clear|morning"]["theta"] == round(DAY_AHEAD_BIAS_MAX, 4)
    assert out["clear|midday"]["clamped"] is False
    assert out["overcast|midday"]["clamped"] is True


# ---------------------------------------------------------------------------
# B5a — store_stats + learner_state_summary
# ---------------------------------------------------------------------------


class _StatsStore:
    """Store stand-in exposing the read accessors store_stats() consults."""

    def __init__(self):
        self._issued = ["2026-07-01", "2026-07-02", "2026-07-03"]
        self._actuals = ["2026-07-01", "2026-07-02"]
        self._hourly = ["2026-07-02"]
        self._snaps = [object(), object()]

    def issued_dates(self):
        return list(self._issued)

    def actuals_dates(self):
        return list(self._actuals)

    def hourly_actuals_dates(self):
        return list(self._hourly)

    def get_snapshots(self):
        return list(self._snaps)

    def schema_version(self):
        return 3


def test_store_stats_reports_real_counts():
    c = _bare_coordinator()
    c._store = _StatsStore()
    stats = c.store_stats()
    assert stats["available"] is True
    assert stats["issued_days"] == 3
    assert stats["actuals_days"] == 2
    assert stats["hourly_actuals_days"] == 1
    assert stats["snapshot_ring"] == 2
    assert stats["snapshot_ring_capacity"] == LEARNER_SNAPSHOT_RING
    assert stats["schema_version"] == 3


def test_store_stats_degrades_missing_accessor_to_none_not_unavailable():
    class _Legacy:
        def issued_dates(self):
            return ["2026-07-01"]

    c = _bare_coordinator()
    c._store = _Legacy()
    stats = c.store_stats()
    # The block stays available (the pre-fix lie was available: false); only the
    # unknown fields degrade to None.
    assert stats["available"] is True
    assert stats["issued_days"] == 1
    assert stats["actuals_days"] is None
    assert stats["schema_version"] is None


def test_learner_state_summary_counts_cells_bins_channels():
    c = _bare_coordinator()
    c._bias_state = BiasState(
        cells={
            "clear|morning": BiasCell(theta=1.2, n=5),
            "clear|midday": BiasCell(theta=0.9, n=5),
        }
    )
    c._quantile_state = QuantileState(
        bins={"clear|midday": [["2026-07-01", 1.0]]}
    )
    c._shademap_state = ShademapState(
        channels={
            "M2": {"3:4:0": ShademapBin(tau=0.5, n=10)},
            "M3": {
                "3:4:0": ShademapBin(tau=0.5, n=10),
                "3:5:0": ShademapBin(tau=0.6, n=8),
            },
        }
    )
    out = c.learner_state_summary()
    assert out["available"] is True
    assert out["bias_cells"] == 2
    assert out["quantile_bins"] == 1
    assert out["shademap_channels"] == 2
    assert out["shademap_bins"] == {"M2": 1, "M3": 2}


# ---------------------------------------------------------------------------
# B5a/B5c — diagnostics dump wiring (no false available: false; DC/AC split)
# ---------------------------------------------------------------------------


def test_diagnostics_store_and_learners_no_longer_report_unavailable():
    from custom_components.balcony_solar_forecast.diagnostics import (
        _learner_summary,
        _store_stats,
    )

    c = _bare_coordinator()
    c._store = _StatsStore()
    c._bias_state = BiasState(cells={"clear|midday": BiasCell(theta=1.1, n=5)})

    store_block = _store_stats(c)
    assert store_block["available"] is True
    assert store_block["issued_days"] == 3
    assert store_block["schema_version"] == 3

    learners = _learner_summary(c, {})
    assert learners["state"]["available"] is True
    assert learners["state"]["bias_cells"] == 1


def test_diagnostics_forecast_summary_splits_dc_and_ac():
    from custom_components.balcony_solar_forecast.diagnostics import (
        _forecast_summary,
    )

    data = {
        "slot_starts": ["2026-07-01T10:00:00+00:00"],
        "plane_watts": {"M2": [1.0]},
        "daily_kwh": {"2026-07-01": 13.44},
        "daily_kwh_ac": {"2026-07-01": 12.37},
        "hourly_wh": {"2026-07-01T10:00:00+00:00": 500.0},
    }
    out = _forecast_summary(data)
    # The old conflated ``daily_kwh`` is gone; the DC/AC split names which is which
    # (explains the 12.37-vs-13.44 confusion, SPEC-4).
    assert "daily_kwh" not in out
    assert out["daily_kwh_dc"] == {"2026-07-01": 13.44}
    assert out["daily_kwh_ac"] == {"2026-07-01": 12.37}
