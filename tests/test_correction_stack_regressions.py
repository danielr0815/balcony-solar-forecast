"""Regression tests spanning Home-Assistant glue correction boundaries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast import _actuals  # noqa: E402
from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    DAY_AHEAD_BIAS_RESEED_N,
    DRIFT_LOSS_STREAK_DAYS,
    LEARNER_LAYER_DAY_AHEAD,
    LEARNER_LAYER_FAST,
    LEARNER_LAYER_SLOW,
    LEARNER_STATUS_COLD_START,
    RLS_INIT_COVARIANCE,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasCell,
    BiasState,
    DriftState,
    LearnerConfig,
    PlaneConfig,
    QuantileState,
    ShademapState,
    SiteConfig,
)
from tests.test_coordinator_learning import _Entry, _make_coordinator  # noqa: E402


def _site(*, partial: bool = False, ac_meter: bool = False) -> SiteConfig:
    return SiteConfig(
        latitude=51.0,
        longitude=10.0,
        planes=(
            PlaneConfig(
                name="P1",
                azimuth_deg=180.0,
                tilt_deg=70.0,
                wp=400.0,
                actual_entity="sensor.p1",
            ),
            PlaneConfig(
                name="P2",
                azimuth_deg=90.0,
                tilt_deg=70.0,
                wp=400.0,
                actual_entity=None if partial else "sensor.p2",
            ),
        ),
        groups=(),
        ac_actual_entity="sensor.site_ac" if ac_meter else None,
    )


def _issued(
    iso: str,
    *,
    raw: float,
    slow: float,
    corrected: float,
    per_plane: dict[str, tuple[float, float, float]] | None = None,
) -> dict:
    hkey = f"{iso}T11:00:00+00:00"
    out = {
        "version": 2,
        "issued_at": f"{iso}T00:30:00+00:00",
        "status": "fresh",
        "raw_hourly_wh": {hkey: raw},
        "slow_only_hourly_wh": {hkey: slow},
        "corrected_hourly_wh": {hkey: corrected},
        "raw_daily_kwh": {iso: raw / 1000.0},
        "corrected_daily_kwh": {iso: corrected / 1000.0},
        "cloud_class_by_hour": {hkey: "clear"},
        "per_plane": {},
    }
    for name, (p_raw, p_slow, p_corrected) in (per_plane or {}).items():
        out["per_plane"][name] = {
            "beam_wh": {hkey: p_raw},
            "diffuse_wh": {},
            "kc": {hkey: 1.0},
            "raw_wh": {hkey: p_raw},
            "slow_wh": {hkey: p_slow},
            "corrected_wh": {hkey: p_corrected},
        }
    return out


def test_intraday_references_slow_only_curve_not_raw() -> None:
    """A learned shade loss must not be estimated again by Intraday."""
    c = _make_coordinator()
    start = datetime(2026, 6, 21, 11, 0, tzinfo=UTC)
    c._day_factor = {start: 1.0}
    result = SimpleNamespace(
        slot_starts=(start,),
        raw_total_watts=(100.0,),
        plane_results=(
            SimpleNamespace(
                name="P1",
                raw_watts=(100.0,),
                slow_watts=(70.0,),
                watts=(70.0,),
            ),
        ),
    )

    assert c._modeled_power_for_planes(result, start, {"P1"}) == 70.0


def test_disabled_persisted_learners_do_not_train(monkeypatch) -> None:
    """Rollback state stays immutable while its layer is auto-disabled."""
    c = _make_coordinator()
    c._drift_state = DriftState(fast_disabled=True, slow_disabled=True)
    iso = "2026-06-21"
    snap = _issued(iso, raw=100.0, slow=100.0, corrected=100.0)
    called = {"day": 0, "slow": 0}

    monkeypatch.setattr(
        c,
        "_day_ahead_samples",
        lambda *_args: [
            SimpleNamespace(
                cloud_class="clear",
                day_part="midday",
                measured_wh=80.0,
                modeled_wh=100.0,
            )
        ],
    )
    monkeypatch.setattr(
        "custom_components.balcony_solar_forecast._nightly.bias_mod.train_day_ahead_bias",
        lambda state, samples: called.__setitem__("day", called["day"] + 1)
        or state,
    )
    c._store.hourly_actuals[iso] = {
        "P1": {f"{iso}T11:00:00+00:00": 80.0}
    }
    snap["per_plane"] = {
        "P1": {
            "beam_wh": {f"{iso}T11:00:00+00:00": 100.0},
            "kc": {f"{iso}T11:00:00+00:00": 1.0},
        }
    }
    monkeypatch.setattr(c, "_day_is_measured_clear", lambda *_args: True)
    monkeypatch.setattr(
        c,
        "_train_channel",
        lambda state, *_args: (
            called.__setitem__("slow", called["slow"] + 1) or state,
            True,
        ),
    )

    c._train_day_ahead(iso, snap, {"P1": 80.0})
    c._train_shademap(iso, snap, {"P1": 80.0})

    assert called == {"day": 0, "slow": 0}


def test_day_ahead_drift_is_independent_of_intraday_switch() -> None:
    c = _make_coordinator()
    c._site = SiteConfig(
        latitude=51.0,
        longitude=10.0,
        planes=(_site().planes[0],),
        groups=(),
    )
    c._learner_config = LearnerConfig(
        fast_enabled=False, slow_enabled=False, day_ahead_enabled=True
    )
    iso = "2026-06-21"
    hkey = f"{iso}T11:00:00+00:00"
    c._store.hourly_actuals[iso] = {"P1": {hkey: 1000.0}}
    snap = _issued(iso, raw=1000.0, slow=1000.0, corrected=1300.0)

    c._update_drift(iso, snap, {"P1": 1000.0})

    assert getattr(c._drift_state, "day_ahead_loss_streak", 0) == 1


def test_partial_metering_uses_exact_issued_plane_curve_for_drift() -> None:
    c = _make_coordinator()
    c._site = _site(partial=True)
    iso = "2026-06-21"
    hkey = f"{iso}T11:00:00+00:00"
    c._store.hourly_actuals[iso] = {"P1": {hkey: 100.0}}
    snap = _issued(
        iso,
        raw=200.0,
        slow=200.0,
        corrected=200.0,
        per_plane={"P1": (100.0, 100.0, 100.0), "P2": (100.0, 100.0, 100.0)},
    )

    c._update_drift(iso, snap, {"P1": 100.0})

    assert c._drift_state.daily_mae[iso]["raw"] == 0.0
    assert c._drift_state.daily_mae[iso]["corrected"] == 0.0


def test_drift_mae_detects_hourly_shape_error_that_daily_total_hides() -> None:
    """Equal daily kWh must not cancel opposite hourly forecast errors."""
    c = _make_coordinator()
    iso = "2026-06-21"
    h1 = f"{iso}T10:00:00+00:00"
    h2 = f"{iso}T11:00:00+00:00"
    c._store.hourly_actuals[iso] = {
        "M1": {h1: 50.0, h2: 150.0},
        "M2": {h1: 0.0, h2: 0.0},
    }
    snap = {
        "version": 2,
        "issued_at": f"{iso}T00:30:00+00:00",
        "status": "fresh",
        "raw_hourly_wh": {h1: 100.0, h2: 100.0},
        "corrected_hourly_wh": {h1: 50.0, h2: 150.0},
        "cloud_class_by_hour": {h1: "clear", h2: "clear"},
        "per_plane": {},
    }

    c._update_drift(iso, snap, {"M1": 200.0, "M2": 0.0})

    entry = c._drift_state.daily_mae[iso]
    assert entry["raw"] == 50.0
    assert entry["corrected"] == 0.0
    assert entry["raw_daily_abs"] == 0.0
    assert entry["hourly_basis"] == 1.0


def test_drift_dashboard_keeps_error_magnitude_and_direction_separate() -> None:
    """Daily bias may cancel across days; the hourly MAE must not."""
    c = _make_coordinator()
    c._drift_state = DriftState(
        daily_mae={
            "2026-06-20": {
                "raw": 120.0,
                "corrected": 80.0,
                "baseline": 140.0,
                "corrected_daily_bias": 100.0,
            },
            "2026-06-21": {
                "raw": 180.0,
                "corrected": 120.0,
                "baseline": 220.0,
                "corrected_daily_bias": -40.0,
            },
            # A legacy entry contributes to MAE, but has no signed-bias field.
            "2026-06-22": {
                "raw": 150.0,
                "corrected": 100.0,
                "baseline": 180.0,
            },
        }
    )

    assert c._latest_drift_mae() == {
        "raw": 150.0,
        "corrected": 100.0,
        "baseline": 180.0,
        "corrected_bias": 30.0,
    }


def test_drift_does_not_fallback_when_hourly_channels_do_not_overlap() -> None:
    """Present-but-incomplete hourly data is not a legacy daily-only store."""
    c = _make_coordinator()
    iso = "2026-06-21"
    h10 = f"{iso}T10:00:00+00:00"
    h11 = f"{iso}T11:00:00+00:00"
    c._store.hourly_actuals[iso] = {
        "M1": {h10: 100.0},
        "M2": {h11: 100.0},
    }
    snap = {
        "version": 2,
        "issued_at": f"{iso}T00:30:00+00:00",
        "status": "fresh",
        "raw_hourly_wh": {h10: 100.0, h11: 100.0},
        "corrected_hourly_wh": {h10: 80.0, h11: 80.0},
        "cloud_class_by_hour": {},
        "per_plane": {},
    }

    c._update_drift(iso, snap, {"M1": 100.0, "M2": 100.0})

    assert c._drift_state.daily_mae == {}


def test_drift_mae_counts_production_completely_missed_by_forecast() -> None:
    """Positive measured daylight is evidence even if both curves are zero."""
    c = _make_coordinator()
    iso = "2026-06-21"
    hkey = f"{iso}T10:00:00+00:00"
    c._store.hourly_actuals[iso] = {
        "M1": {hkey: 100.0},
        "M2": {hkey: 0.0},
    }
    snap = {
        "version": 2,
        "issued_at": f"{iso}T00:30:00+00:00",
        "status": "fresh",
        "raw_hourly_wh": {hkey: 0.0},
        "corrected_hourly_wh": {hkey: 0.0},
        "cloud_class_by_hour": {},
        "per_plane": {},
    }

    c._update_drift(iso, snap, {"M1": 100.0, "M2": 0.0})

    entry = c._drift_state.daily_mae[iso]
    assert entry["raw"] == 100.0
    assert entry["corrected"] == 100.0
    assert entry["hourly_basis"] == 1.0


def test_auto_disable_invalidates_states_dependent_on_removed_layers() -> None:
    """Drift shutdown changes the serving basis like an option transition."""
    c = _make_coordinator()
    iso = "2026-06-21"
    hkey = f"{iso}T10:00:00+00:00"
    c._bias_state = BiasState(
        cells={
            "clear|midday": BiasCell(theta=0.8, covariance=0.01, n=40)
        }
    )
    c._quantile_state = QuantileState(
        bins={"clear|midday": [["2026-06-20", 0.9]]}
    )
    c._drift_state = DriftState(
        slow_loss_streak=DRIFT_LOSS_STREAK_DAYS - 1,
        day_ahead_loss_streak=DRIFT_LOSS_STREAK_DAYS - 1,
        fast_loss_streak=DRIFT_LOSS_STREAK_DAYS - 1,
    )
    snap = {
        "version": 2,
        "issued_at": f"{iso}T00:30:00+00:00",
        "status": "fresh",
        "raw_hourly_wh": {hkey: 1000.0},
        "slow_only_hourly_wh": {hkey: 1300.0},
        "corrected_hourly_wh": {hkey: 1700.0},
        "cloud_class_by_hour": {},
        "per_plane": {},
    }

    c._update_drift(iso, snap, {"M1": 1000.0, "M2": 0.0})

    assert c._drift_state.slow_disabled is True
    assert c._drift_state.day_ahead_disabled is True
    assert c._quantile_state == QuantileState()
    cell = c._bias_state.cells["clear|midday"]
    assert cell.theta == 0.8
    assert cell.covariance == RLS_INIT_COVARIANCE
    assert cell.n == DAY_AHEAD_BIAS_RESEED_N


def test_partial_metering_does_not_trigger_false_collapse() -> None:
    c = _make_coordinator()
    c._site = _site(partial=True)
    iso = "2026-06-21"
    snap = _issued(
        iso,
        raw=2000.0,
        slow=2000.0,
        corrected=2000.0,
        per_plane={"P1": (600.0, 600.0, 600.0), "P2": (1400.0, 1400.0, 1400.0)},
    )

    assert not c._is_collapse_day(iso, snap, {"P1": 40.0})


async def test_partial_metering_scoreboard_scores_metered_curve() -> None:
    c = _make_coordinator()
    c._site = _site(partial=True)
    c._persist_scoreboard_state = lambda: None
    iso = "2026-06-21"
    hkey = f"{iso}T11:00:00+00:00"
    c._store.issued[iso] = _issued(
        iso,
        raw=200.0,
        slow=200.0,
        corrected=200.0,
        per_plane={"P1": (100.0, 100.0, 100.0), "P2": (100.0, 100.0, 100.0)},
    )
    c._store.actuals[iso] = {"P1": 100.0}
    c._store.hourly_actuals[iso] = {"P1": {hkey: 100.0}}

    await c._score_scoreboard_day(date.fromisoformat(iso))

    assert c._scoreboard_state.days[iso].engine_kwh == pytest.approx(0.1)
    assert c._scoreboard_state.days[iso].engine_daily_abs_err == 0.0


def test_night_rows_do_not_satisfy_daylight_coverage() -> None:
    day = date(2026, 6, 21)
    rows = [
        {
            "start": datetime(2026, 6, 21, hour, tzinfo=UTC).timestamp(),
            "mean": float(hour),
        }
        for hour in range(6)
    ]
    daylight = {
        datetime(2026, 6, 21, hour, tzinfo=UTC)
        .replace(minute=0, second=0, microsecond=0)
        .isoformat()
        for hour in range(10, 16)
    }

    daily, hourly = _actuals._actuals_from_stats(
        {"sensor.p1": rows},
        {"P1": "sensor.p1"},
        expected_daylight_hours=len(daylight),
        expected_daylight_hour_keys=daylight,
        day=day,
    )

    assert daily == {}
    assert hourly == {}


async def test_collapse_day_is_quarantined_from_quantiles_and_drift(
    monkeypatch,
) -> None:
    c = _make_coordinator()
    iso = "2026-06-21"
    c._store.issued[iso] = _issued(
        iso, raw=1000.0, slow=1000.0, corrected=1000.0
    )
    c._store.actuals[iso] = {"M1": 0.0, "M2": 0.0}
    called = {"q": 0, "drift": 0}
    monkeypatch.setattr(
        c, "_train_quantiles_day", lambda *_args: called.__setitem__("q", 1)
    )
    monkeypatch.setattr(
        c, "_update_drift", lambda *_args: called.__setitem__("drift", 1)
    )

    await c._train_and_guard(date.fromisoformat(iso))

    assert called == {"q": 0, "drift": 0}


async def test_inverter_calibration_without_groups_can_learn(monkeypatch) -> None:
    c = _make_coordinator()
    c._site = SiteConfig(
        latitude=51.0,
        longitude=10.0,
        planes=(_site(ac_meter=True).planes[0],),
        groups=(),
        ac_actual_entity="sensor.site_ac",
    )
    iso = "2026-06-21"
    hkey = f"{iso}T11:00:00+00:00"
    c._store.hourly_actuals[iso] = {"P1": {hkey: 200.0}}

    async def _ac(_day):
        return {hkey: 190.0}

    monkeypatch.setattr(c, "_async_read_ac_actuals", _ac)
    monkeypatch.setattr(c, "_persist_inverter_cal_state", lambda: None)

    await c._train_inverter_cal(date.fromisoformat(iso))

    assert c._inverter_cal_state.n == 1
    assert c._inverter_cal_state.eta == pytest.approx(0.95)


async def test_inverter_calibration_rejects_partial_dc_denominator(
    monkeypatch,
) -> None:
    """A whole-site AC numerator must never be divided by one DC channel."""
    c = _make_coordinator()
    c._site = _site(ac_meter=True)
    iso = "2026-06-21"
    hkey = f"{iso}T11:00:00+00:00"
    c._store.hourly_actuals[iso] = {"P1": {hkey: 200.0}}
    called = False

    async def _ac(_day):
        nonlocal called
        called = True
        return {hkey: 190.0}

    monkeypatch.setattr(c, "_async_read_ac_actuals", _ac)

    await c._train_inverter_cal(date.fromisoformat(iso))

    assert c._inverter_cal_state.n == 0
    assert called is False


async def test_inverter_calibration_requires_complete_dc_per_hour(
    monkeypatch,
) -> None:
    """A one-hour port gap must not become a partial whole-site denominator."""
    c = _make_coordinator()
    c._site = _site(ac_meter=True)
    iso = "2026-06-21"
    h10 = f"{iso}T10:00:00+00:00"
    h11 = f"{iso}T11:00:00+00:00"
    c._store.hourly_actuals[iso] = {
        "P1": {h10: 100.0, h11: 100.0},
        "P2": {h10: 100.0},
    }

    async def _ac(_day):
        return {h10: 190.0, h11: 95.0}

    monkeypatch.setattr(c, "_async_read_ac_actuals", _ac)
    monkeypatch.setattr(c, "_persist_inverter_cal_state", lambda: None)

    await c._train_inverter_cal(date.fromisoformat(iso))

    assert c._inverter_cal_state.n == 1
    assert c._inverter_cal_state.eta == pytest.approx(0.95)


def test_learner_status_never_calls_empty_layers_active() -> None:
    c = _make_coordinator()

    status = c._learner_status()

    assert status[LEARNER_LAYER_FAST] == LEARNER_STATUS_COLD_START
    assert status[LEARNER_LAYER_SLOW] == LEARNER_STATUS_COLD_START
    assert status[LEARNER_LAYER_DAY_AHEAD] == LEARNER_STATUS_COLD_START

    c._shademap_state = ShademapState(channels={"P1": {}})
    assert c._learner_status()[LEARNER_LAYER_SLOW] == LEARNER_STATUS_COLD_START


def test_correction_fingerprint_tracks_layer_dependencies() -> None:
    c = _make_coordinator()
    c._site = _site()
    base = c._config_fingerprint()

    p1, p2 = c._site.planes
    c._site = SiteConfig(
        latitude=c._site.latitude,
        longitude=c._site.longitude,
        planes=(
            PlaneConfig(
                name=p1.name,
                azimuth_deg=p1.azimuth_deg,
                tilt_deg=p1.tilt_deg,
                wp=p1.wp,
                actual_entity=p1.actual_entity,
                shade_group="shared",
            ),
            PlaneConfig(
                name=p2.name,
                azimuth_deg=p2.azimuth_deg,
                tilt_deg=p2.tilt_deg,
                wp=p2.wp,
                actual_entity=p2.actual_entity,
                shade_group="shared",
            ),
        ),
        groups=(),
    )

    assert c._config_fingerprint() != base


def test_correction_fingerprint_ignores_semantic_order() -> None:
    """Reordering planes alone must not reopen a correctly trained model."""
    c = _make_coordinator()
    c._site = _site()
    base = c._config_fingerprint()

    c._site = SiteConfig(
        latitude=c._site.latitude,
        longitude=c._site.longitude,
        planes=tuple(reversed(c._site.planes)),
        groups=tuple(reversed(c._site.groups)),
    )

    assert c._config_fingerprint() == base


def test_correction_fingerprint_ignores_shade_pool_label() -> None:
    """Renaming a pool without changing its members is semantically neutral."""
    c = _make_coordinator()
    p1, p2 = c._site.planes
    c._site = replace(
        c._site,
        planes=(
            replace(p1, shade_group="old-label"),
            replace(p2, shade_group="old-label"),
        ),
    )
    before = c._config_fingerprint()
    c._site = replace(
        c._site,
        planes=(
            replace(p1, shade_group="new-label"),
            replace(p2, shade_group="new-label"),
        ),
    )

    assert c._config_fingerprint() == before


def test_layer_basis_transition_reseeds_dependents() -> None:
    """Turning Slow off changes both theta's input and quantile residual basis."""
    c = _make_coordinator()
    c._bias_state = BiasState(
        cells={
            "clear|midday": BiasCell(theta=0.8, covariance=0.01, n=40)
        }
    )
    c._quantile_state = QuantileState(
        bins={"clear|midday": [["2026-06-20", 0.9]]}
    )
    c._drift_state = DriftState(
        slow_loss_streak=4,
        day_ahead_loss_streak=5,
        fast_loss_streak=5,
        fast_option_seen=True,
        slow_option_seen=True,
        day_ahead_option_seen=True,
    )
    c.entry = _Entry(options={"slow_learner_enabled": False})

    c.rebuild_learner_config()

    cell = c._bias_state.cells["clear|midday"]
    assert cell.theta == 0.8
    assert cell.covariance == RLS_INIT_COVARIANCE
    assert c._quantile_state == QuantileState()
    assert c._drift_state.slow_loss_streak == 0
    assert c._drift_state.day_ahead_loss_streak == 0
