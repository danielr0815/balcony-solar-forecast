"""Archive provenance and temporal training guards through coordinator entry points."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast import (
    coordinator as coordinator_mod,  # noqa: E402
)
from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    CORRECTION_SOURCE_SHADEMAP_DAY_AHEAD,
    DAY_PART_MIDDAY,
)
from custom_components.balcony_solar_forecast.core import quantiles  # noqa: E402
from custom_components.balcony_solar_forecast.core.engine import (
    LearnerHooks,  # noqa: E402
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    InverterGroup,
    IssuedSnapshot,
    QuantileState,
    WeatherSeries,
    WeatherSlot,
)
from tests.helpers.coordinator import _make_coordinator  # noqa: E402


async def test_archive_uses_published_generation_when_cache_and_eta_advance(monkeypatch):
    coord = _make_coordinator()
    at = datetime(2026, 7, 1, 11, tzinfo=UTC)
    now = at.replace(hour=1)
    weather_a = WeatherSeries((WeatherSlot(at, 900, 700, 200, 20),))
    weather_b = WeatherSeries((WeatherSlot(at, 50, 0, 50, 20),))

    def hooks(_weather, _now):
        coord._day_factor = {at: 1.2}
        return LearnerHooks(
            beam_tau=lambda _ch, _az, _el, _doy, prior: prior * .7,
            slot_factor=lambda _slot: 1.2,
            correction_source=CORRECTION_SOURCE_SHADEMAP_DAY_AHEAD,
            inverter_efficiency=.93,
        )

    monkeypatch.setattr(coord, "_build_learner_hooks", hooks)
    coord._cached_weather = lambda: weather_a
    result = await coord._compute(weather_a, now)
    coord._last_result = result
    coord.data = coord._build_data(result, result.hourly_wh, now, "fresh", timedelta())
    expected_slow = result.hourly_wh[at.isoformat()] / 1.2
    assert expected_slow != pytest.approx(result.raw_hourly_wh[at.isoformat()])

    # The old path would recompute slow-only using cache B and the learner state
    # available during archiving instead of the actual curve issued from A.
    coord._cached_weather = lambda: weather_b
    coord._slow_active = lambda: True
    coord._effective_inverter_eta = lambda: .98

    await coord._snapshot_issued(at.date())
    snapshot = IssuedSnapshot.from_dict(coord._store.get_issued(at.date().isoformat()))
    assert snapshot.raw_hourly_wh == result.raw_hourly_wh
    assert snapshot.corrected_hourly_wh == result.hourly_wh
    assert snapshot.slow_only_hourly_wh == pytest.approx({at.isoformat(): expected_slow})
    assert snapshot.cloud_class_by_hour == {at.isoformat(): "clear"}
    assert snapshot.corrected_ac_hourly_wh == result.ac_hourly_wh
    assert snapshot.eta == pytest.approx(.93)


@pytest.mark.parametrize(
    ("issued", "computed", "target_hour"),
    [("12:00", "12:00", 11), ("00:00", "12:00", 11), ("05:30", "05:30", 5)],
)
async def test_retrospective_whole_day_archive_cannot_change_learners(monkeypatch, issued, computed, target_hour):
    coord = _make_coordinator()
    day = date(2026, 7, 1)
    iso = day.isoformat()
    h = f"{iso}T{target_hour:02d}:00:00+00:00"
    coord._store.issued[iso] = IssuedSnapshot(
        issued_at=f"{iso}T{issued}:00+00:00",
        computed_at=f"{iso}T{computed}:00+00:00",
        status="fresh", raw_hourly_wh={h: 200}, corrected_hourly_wh={h: 180},
    ).to_dict()
    coord._store.actuals[iso] = {"M1": 105, "M2": 105}
    coord._store.hourly_actuals[iso] = {"M1": {h: 105}, "M2": {h: 105}}
    calls = []
    for name in ("_is_collapse_day", "_train_day_ahead", "_train_shademap",
                 "_train_quantiles_day", "_update_drift"):
        monkeypatch.setattr(coord, name, lambda *_, step=name: calls.append(step))
    await coord._train_and_guard(day)
    assert calls == []
    assert not coord._store.is_day_trained(iso)


@pytest.mark.parametrize("modeled_zero", [False, True])
@pytest.mark.parametrize("entrypoint", ["training", "scoreboard"])
async def test_positive_measurement_preceding_early_issue_blocks_training_and_score(monkeypatch, modeled_zero, entrypoint):
    coord = _make_coordinator()
    day = date(2026, 7, 1)
    iso = day.isoformat()
    before_issue = f"{iso}T05:00:00+00:00"
    later = f"{iso}T11:00:00+00:00"
    curve = {later: 300, before_issue: 0} if modeled_zero else {later: 300}
    snap = IssuedSnapshot(
        issued_at=f"{iso}T05:30:00+00:00", status="fresh",
        raw_hourly_wh=curve, corrected_hourly_wh=curve,
    )
    coord._store.issued[iso] = snap.to_dict()
    coord._store.actuals[iso] = {"M1": 175, "M2": 150}
    coord._store.hourly_actuals[iso] = {
        "M1": {before_issue: 25, later: 150},
        "M2": {before_issue: 0, later: 150},
    }
    if entrypoint == "scoreboard":
        await coord._score_scoreboard_day(day)
        assert not coord._scoreboard_state.days
        return

    calls = []
    for name in ("_train_day_ahead", "_train_shademap", "_train_quantiles_day", "_update_drift"):
        monkeypatch.setattr(coord, name, lambda *_, step=name: calls.append(step))
    await coord._train_and_guard(day)
    assert calls == []


async def test_early_publication_remains_eligible_when_written_to_archive_late():
    coord = _make_coordinator()
    day = date(2026, 7, 1)
    iso = day.isoformat()
    h = f"{iso}T11:00:00+00:00"
    night = f"{iso}T00:00:00+00:00"
    coord._store.issued[iso] = IssuedSnapshot(
        issued_at=f"{iso}T01:00:00+00:00", computed_at=f"{iso}T01:00:00+00:00",
        archived_at=f"{iso}T14:00:00+00:00", status="fresh",
        raw_hourly_wh={night: 0, h: 200}, corrected_hourly_wh={night: 0, h: 180},
    ).to_dict()
    coord._store.actuals[iso] = {"M1": 105, "M2": 105}
    coord._store.hourly_actuals[iso] = {"M1": {night: 0, h: 105}, "M2": {night: 0, h: 105}}
    await coord._train_and_guard(day)
    assert coord._drift_state.daily_mae[iso]["raw"] == 10
    assert coord._store.is_day_trained(iso)


def test_drift_counts_missing_forecast_and_ignores_missing_measurement():
    coord = _make_coordinator()
    iso = "2026-07-01"
    match = f"{iso}T11:00:00+00:00"
    absent_forecast = f"{iso}T12:00:00+00:00"
    absent_actual = f"{iso}T13:00:00+00:00"
    forecast = {match: 100, absent_actual: 400}
    snap = IssuedSnapshot(
        issued_at=f"{iso}T01:00:00+00:00", status="fresh",
        raw_hourly_wh=forecast, slow_only_hourly_wh=forecast, corrected_hourly_wh=forecast,
    )
    coord._store.hourly_actuals[iso] = {
        channel: {match: 50, absent_forecast: 150} for channel in ("M1", "M2")
    }
    coord._update_drift(iso, snap.to_dict(), {"M1": 200, "M2": 200})
    entry = coord._drift_state.daily_mae[iso]
    assert entry["hourly_basis"] == 1
    assert (entry["raw"], entry["slow"], entry["corrected"]) == (150, 150, 150)


@pytest.mark.parametrize(
    ("day_offsets", "eligible"),
    [([-110, -109, -108, -107, -106], False),
     ([-90, -4, -3, -1, 0], True), ([1, 2, 3, 4, 5], False)],
)
def test_quantile_diagnostics_and_serving_use_same_issue_window(monkeypatch, day_offsets, eligible):
    coord = _make_coordinator()
    now = datetime(2026, 7, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(coordinator_mod.dt_util, "utcnow", lambda: now)
    key = QuantileState.bin_key("clear", DAY_PART_MIDDAY)
    coord._quantile_state = QuantileState(bins={key: [
        [(now.date() + timedelta(days=offset)).isoformat(), .8 + i * .1]
        for offset in day_offsets for i in range(4)
    ]})
    band = quantiles.bands_for_bin(
        coord._quantile_state, cloud_class="clear", day_part=DAY_PART_MIDDAY,
        as_of_date=now.date().isoformat(),
    )
    summary = coord.quantile_state_summary()
    n, days = (20, 5) if eligible else (0, 0)
    assert band.n == n
    assert summary["bins"][key] == {"n": n, "days": days, "trained": eligible}
    assert summary["dated_days"] == days


@pytest.mark.parametrize("new_source", [
    {"ac_actual_entity": None},
    {"ac_actual_entity": "sensor.replacement"},
    {"ac_actual_invert": True},
])
async def test_eta_survives_same_source_reload_but_new_curve_after_source_change_uses_config(new_source):
    coord = _make_coordinator()
    coord._site = replace(
        coord._site, ac_actual_entity="sensor.site_ac",
        groups=(InverterGroup(name="WR", plane_names=("M1", "M2"),
                              ac_limit_w=1000, inverter_efficiency=.90),),
    )
    # Train the actual source before reloading its persisted provenance.
    for day in (date(2026, 6, 29), date(2026, 6, 30)):
        iso = day.isoformat()
        hours = {f"{iso}T{hour:02d}:00:00+00:00": 250 for hour in range(8, 18)}
        coord._store.hourly_actuals[iso] = {"M1": hours, "M2": hours}

        async def read_ac(_day, labels=hours):
            return {key: 465 for key in labels}

        coord._async_read_ac_actuals = read_ac
        await coord._train_inverter_cal(day)
    coord._inverter_cal_state = coord._store.get_inverter_cal_state()
    assert coord.inverter_efficiency_learned()["effective"] == pytest.approx(.93)
    at = datetime(2026, 7, 1, 11, tzinfo=UTC)
    weather = WeatherSeries((WeatherSlot(at, 900, 700, 200, 20),))
    learned = await coord._compute(weather, at)
    assert learned.ac_watts[0] / learned.total_watts[0] == pytest.approx(.93)

    coord._site = replace(coord._site, **new_source)
    fallback = await coord._compute(weather, at)
    assert fallback.ac_watts[0] / fallback.total_watts[0] == pytest.approx(.90)
    assert coord.inverter_efficiency_learned()["effective"] is None
    assert coord._store.get_inverter_cal_state().n == 20


@pytest.mark.parametrize("dated", [False, True])
async def test_unchanged_entry_can_bind_legacy_eta_to_its_only_ac_source(dated):
    from custom_components.balcony_solar_forecast.const import (
        STORE_KEY_INVERTER_CAL_STATE,
    )
    from custom_components.balcony_solar_forecast.store import ForecastStore
    from tests.helpers.store import FakeStore

    original = {'eta': .94, 'n': 37}
    dates = ['2026-07-03', '2026-07-04'] if dated else []
    backend = FakeStore({
        'schema_version': 3,
        STORE_KEY_INVERTER_CAL_STATE: original,
        'inverter_cal_trained_days': dates,
    })
    store = ForecastStore(None, "entry", store=backend)
    await store.async_load()
    coord = _make_coordinator(store)
    coord._site = replace(coord._site, ac_actual_entity="sensor.original")
    coord.entry.created_at = coord.entry.modified_at = datetime(2026, 7, 1, tzinfo=UTC)
    coord._learner_states_loaded = False
    coord._load_learner_states()

    assert coord.inverter_efficiency_learned()["effective"] == pytest.approx(.94)
    await store.async_flush()
    restored = ForecastStore(None, "entry", store=FakeStore(backend.saved))
    await restored.async_load()
    coord._inverter_cal_state = restored.get_inverter_cal_state()
    coord._site = replace(coord._site, ac_actual_entity=None)
    assert coord.inverter_efficiency_learned()["effective"] is None
    coord._site = replace(coord._site, ac_actual_entity="sensor.original")
    assert coord.inverter_efficiency_learned()["effective"] == pytest.approx(.94)
    assert restored.inverter_trained_dates() == tuple(dates)


async def test_eta_training_dates_follow_source_including_polarity_replacement():
    coord = _make_coordinator()
    coord._site = replace(coord._site, ac_actual_entity="sensor.meter")
    day = date(2026, 7, 1)
    hour = "2026-07-01T11:00:00+00:00"
    coord._store.hourly_actuals[day.isoformat()] = {"M1": {hour: 200}, "M2": {hour: 200}}

    async def read_ac(_day):
        return {hour: 372 if coord._site.ac_actual_invert else 384}

    coord._async_read_ac_actuals = read_ac
    await coord._train_inverter_cal(day)
    assert coord._inverter_cal_state.eta == pytest.approx(.96)

    coord._site = replace(coord._site, ac_actual_invert=True)
    await coord._train_inverter_cal(day)
    assert coord._inverter_cal_state.n == 1
    assert coord._inverter_cal_state.eta == pytest.approx(.93)
    await coord._train_inverter_cal(day)
    assert coord._inverter_cal_state.n == 1


@pytest.mark.parametrize("mixed", [False, True])
async def test_configured_eta_metadata_matches_frozen_ac_calculation(mixed):
    coord = _make_coordinator()
    coord._site = replace(coord._site, groups=(
        InverterGroup("first", ("M1",), 800, .9),
        InverterGroup("second", ("M2",), 800, .96 if mixed else .9),
    ))
    at = datetime(2026, 7, 1, 11, tzinfo=UTC)
    now = at.replace(hour=1)
    weather = WeatherSeries((WeatherSlot(at, 900, 700, 200, 20),))
    result = await coord._compute(weather, now)
    coord._last_result = result
    coord.data = coord._build_data(result, result.hourly_wh, now, "fresh", timedelta())
    await coord._snapshot_issued(at.date())

    issued = IssuedSnapshot.from_dict(coord._store.get_issued(at.date().isoformat()))
    assert issued.corrected_ac_hourly_wh == result.ac_hourly_wh
    audit = coord.data["curve_audit"]
    if mixed:
        assert issued.eta is None
        assert audit["eta_effective"] is None
        assert audit["eta_source"] == "mixed_config"
    else:
        assert issued.eta == pytest.approx(.9)
        assert audit["eta_effective"] == pytest.approx(.9)
        assert audit["eta_source"] == "config"


async def test_unverified_legacy_eta_and_training_dates_survive_replacement_and_reload():
    from custom_components.balcony_solar_forecast.const import (
        STORE_KEY_INVERTER_CAL_STATE,
    )
    from custom_components.balcony_solar_forecast.store import ForecastStore
    from tests.helpers.store import FakeStore

    legacy = {'eta': .94, 'n': 37}
    # The old EMA contains samples from meter A and B. The dated ring was
    # introduced after the switch, so its sole marker only reflects meter B.
    legacy_dates = ['2026-06-29']
    backend = FakeStore({
        'schema_version': 3,
        STORE_KEY_INVERTER_CAL_STATE: legacy,
        'inverter_cal_trained_days': legacy_dates,
    })
    store = ForecastStore(None, "entry", store=backend)
    await store.async_load()
    coord = _make_coordinator(store)
    coord._site = replace(coord._site, ac_actual_entity="sensor.new_meter")
    coord.entry.created_at = datetime(2026, 5, 1, tzinfo=UTC)
    coord.entry.modified_at = datetime(2026, 6, 25, tzinfo=UTC)
    coord._learner_states_loaded = False
    coord._load_learner_states()

    summary = coord.inverter_efficiency_learned()
    assert summary["effective"] is None
    assert summary["source_status"] == "unverified_legacy"
    assert store.get_inverter_cal_state().to_dict() == legacy

    day = date(2026, 7, 1)
    hour = f"{day}T11:00:00+00:00"
    store.record_hourly_actuals(day.isoformat(), {"M1": {hour: 200}, "M2": {hour: 200}})

    async def read_ac(_day):
        return {hour: 384}

    coord._async_read_ac_actuals = read_ac
    await coord._train_inverter_cal(day)
    await store.async_flush()
    assert backend.saved.get("inverter_cal_unverified_legacy") == {
        "state": legacy, "trained_dates": legacy_dates,
    }

    restored = ForecastStore(None, "entry", store=FakeStore(backend.saved))
    await restored.async_load()
    saved = restored.get_unverified_inverter_calibration()
    assert saved is not None
    retained, dates = saved
    assert retained.to_dict() == legacy
    assert list(dates) == legacy_dates
    assert restored.inverter_trained_dates() == (day.isoformat(),)
    fresh = restored.get_inverter_cal_state()
    assert fresh.n == 1
    assert fresh.eta == pytest.approx(.96)
    coord._store = restored
    coord._inverter_cal_state = fresh
    assert coord.inverter_efficiency_learned()["effective"] is None
    assert coord.inverter_efficiency_learned()["unverified_legacy_saved"] == {
        "eta": .94, "n": 37, "trained_days": 1,
    }

    coord._site = replace(coord._site, ac_actual_entity="sensor.third_meter")
    await coord._train_inverter_cal(day)
    assert restored.get_unverified_inverter_calibration() == saved
