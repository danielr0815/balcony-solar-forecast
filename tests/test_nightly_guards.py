"""Guard-branch tests for the nightly training glue + coordinator delegates.

The nightly pipeline's EARLY-EXIT and skip branches are its safety contract
(SPEC §9.1/§9.8/§11.1/§12.1): a disabled layer, a missing issued snapshot,
foreign-day hours, an unmetered site, a garbage hour key or a non-quasi-clear
sample must each train NOTHING rather than poisoning a learner — and the
orchestration had no direct coverage for most of them. These tests drive the
coordinator delegates (thin wrappers over ``_nightly.py``) against the
learning-test scaffolds (``_make_coordinator`` / ``_FakeStore``), asserting
the state stays byte-untouched on every guard path and changes exactly where
the happy path demands it.

Needs Home Assistant; skipped on the plain-core path.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast import (
    _nightly as nightly_mod,  # noqa: E402
)
from custom_components.balcony_solar_forecast.core import (  # noqa: E402
    bias as bias_mod,
)
from custom_components.balcony_solar_forecast.core import (  # noqa: E402
    shademap as shademap_mod,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasState,
    DriftState,
    ForecastResult,
    InverterCalState,
    InverterGroup,
    IssuedSnapshot,
    LearnerConfig,
    LearnerSnapshot,
    PlaneConfig,
    PlaneHourlyModeled,
    PlaneResult,
    QuantileState,
    ShademapBin,
    ShademapState,
    SiteConfig,
)
from custom_components.balcony_solar_forecast.fetcher import (  # noqa: E402
    parse_weather,
)
from tests.test_coordinator_learning import (  # noqa: E402
    _FakeStore,
    _make_coordinator,
)
from tests.test_fetcher_shapes import (  # noqa: E402
    _hourly_block,
    _minutely_block,
)

_DAY = date(2026, 7, 1)
_ISO = _DAY.isoformat()


def _h(hour: int, day: date = _DAY) -> str:
    """ISO-UTC hour key (mid-day hours: the local date is unambiguous)."""
    return f"{day.isoformat()}T{hour:02d}:00:00+00:00"


def _issued(
    *,
    raw: dict[str, float] | None = None,
    corrected: dict[str, float] | None = None,
    slow_only: dict[str, float] | None = None,
    per_plane: dict | None = None,
    cloud: dict[str, str] | None = None,
    issued_at: str | None = None,
) -> dict:
    snap = IssuedSnapshot(
        issued_at=issued_at or f"{_ISO}T00:00:00+00:00",
        status="fresh",
        raw_hourly_wh=dict(raw or {}),
        corrected_hourly_wh=dict(corrected if corrected is not None else (raw or {})),
        per_plane=per_plane or {},
        cloud_class_by_hour=dict(cloud or {}),
        slow_only_hourly_wh=dict(slow_only or {}),
    )
    return snap.to_dict()


# ---------------------------------------------------------------------------
# snapshot_issued: never double-record, never fabricate
# ---------------------------------------------------------------------------


async def test_snapshot_issued_without_data_records_nothing():
    store = _FakeStore()
    c = _make_coordinator(store)
    c.data = None
    await c._snapshot_issued(_DAY)
    assert store.issued == {}


async def test_snapshot_issued_keeps_the_existing_snapshot():
    store = _FakeStore()
    store.issued[_ISO] = _issued(raw={_h(10): 100.0})
    c = _make_coordinator(store)
    c.data = {"status": "fresh"}  # a live forecast exists …
    await c._snapshot_issued(_DAY)
    # … but the day is already archived: the first issued stand wins.
    assert store.issued[_ISO]["raw_hourly_wh"] == {_h(10): 100.0}


# ---------------------------------------------------------------------------
# cloud_class_by_hour: real weather -> per-hour classes, first writer wins
# ---------------------------------------------------------------------------


def _weather_series(day: date = _DAY, n_slots: int = 20):
    """5 h of 15-min slots (08:00–12:45 UTC) + matching hourly context."""
    payload = {
        "minutely_15": _minutely_block(n_slots, start_iso=f"{day.isoformat()}T08:15"),
        "hourly": _hourly_block(6, start_iso=f"{day.isoformat()}T08:00"),
    }
    return parse_weather(payload)


def test_cloud_class_by_hour_maps_every_slot_hour_once():
    c = _make_coordinator()
    c._cached_weather = lambda: _weather_series()
    out = c._cloud_class_by_hour(_ISO)
    # 20 slots across the hours 08..12 (UTC == local in the test harness),
    # four slots per hour -> five hour keys, one class each (setdefault:
    # the first slot of an hour wins, later slots of that hour are dropped).
    assert sorted(out) == [_h(8), _h(9), _h(10), _h(11), _h(12)]
    # The fixture's cloud rows (low 80 / mid 10 / high 0, good visibility)
    # classify deterministically — not a blanket "clear" default.
    assert set(out.values()) == {"overcast"}


def test_cloud_class_by_hour_without_weather_is_empty():
    c = _make_coordinator()
    c._cached_weather = lambda: None
    assert c._cloud_class_by_hour(_ISO) == {}


# ---------------------------------------------------------------------------
# per_plane_modeled: local-day slicing, kc estimation, no-ref skip
# ---------------------------------------------------------------------------


def test_per_plane_modeled_slices_to_day_and_skips_planes_without_ref():
    c = _make_coordinator()
    starts = (
        datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 1, 11, 0, tzinfo=UTC),
        datetime(2026, 7, 2, 10, 0, tzinfo=UTC),   # foreign day: sliced away
    )
    with_ref = PlaneResult(
        name="M1",
        watts=(400.0, 400.0, 400.0),
        kc=(0.8, 0.8, 0.8),
        beam_ref_watts=(400.0, 400.0, 400.0),
        diffuse_ref_watts=(100.0, 100.0, 100.0),
    )
    no_ref = PlaneResult(name="M2", watts=(1.0, 1.0, 1.0))  # no reference export
    c._last_result = ForecastResult(
        slot_starts=starts,
        total_watts=(401.0, 401.0, 401.0),
        plane_results=(with_ref, no_ref),
        hourly_wh={},
    )
    out = c._per_plane_modeled(_ISO)
    # The no-reference plane is NOT trained (no fallback to the gated series).
    assert list(out) == ["M1"]
    modeled = out["M1"]
    # Only the two target-day slots survive; each contributes w * 0.25 Wh.
    assert all(k.startswith(_ISO) for k in modeled.beam_wh)
    assert all(v == pytest.approx(100.0) for v in modeled.beam_wh.values())
    assert all(v == pytest.approx(25.0) for v in modeled.diffuse_wh.values())
    # kc recovered through the shared hourly reduction, sane and finite.
    assert all(0.0 < v < 1.5 for v in modeled.kc.values())


def test_per_plane_modeled_without_result_is_empty():
    c = _make_coordinator()
    c._last_result = None
    assert c._per_plane_modeled(_ISO) == {}


# ---------------------------------------------------------------------------
# train_and_guard / collapse freeze bookkeeping
# ---------------------------------------------------------------------------


async def test_train_and_guard_clears_frozen_date_on_a_non_collapse_day():
    """A closed NON-collapse day lifts an earlier collapse freeze (the freeze
    protects only the day after the dropout, SPEC §9.8)."""
    store = _FakeStore()
    store.issued[_ISO] = _issued(raw={_h(10): 500.0})
    store.actuals[_ISO] = {"M1": 450.0}
    c = _make_coordinator(store)
    c._drift_state = replace(DriftState(), collapse_frozen_date=_ISO)

    def _noop_sync(*args, **kwargs):
        return None

    c._maybe_push_rollback_snapshot = lambda iso: None
    c._is_collapse_day = lambda iso, issued, actuals: False
    c._train_day_ahead = _noop_sync
    c._train_shademap = _noop_sync
    c._train_quantiles_day = _noop_sync
    c._update_drift = _noop_sync

    await c._train_and_guard(_DAY)

    assert c._drift_state.collapse_frozen_date is None
    # The cleared freeze was persisted (a restart must not resurrect it).
    assert store.drift["collapse_frozen_date"] is None


def test_set_collapse_frozen_date_no_change_is_a_noop():
    store = _FakeStore()
    c = _make_coordinator(store)
    c._drift_state = replace(DriftState(), collapse_frozen_date=_ISO)
    before = store.drift
    c._set_collapse_frozen_date(_ISO)
    assert store.drift is before  # no pointless write (eMMC budget)
    # A real change DOES persist.
    c._set_collapse_frozen_date(None)
    assert store.drift["collapse_frozen_date"] is None


# ---------------------------------------------------------------------------
# train_quantiles_day guards + happy path
# ---------------------------------------------------------------------------


def _quantile_issued() -> dict:
    return _issued(
        corrected={_h(10): 500.0, _h(11): 600.0},
        cloud={_h(10): "clear", _h(11): "clear"},
    )


def test_train_quantiles_disabled_is_a_noop():
    store = _FakeStore()
    store.issued[_ISO] = _quantile_issued()
    c = _make_coordinator(store)
    c._quantiles_enabled = False
    c._train_quantiles_day(_DAY)
    assert c._quantile_state == QuantileState()


def test_train_quantiles_without_issued_is_a_noop():
    c = _make_coordinator()
    c._train_quantiles_day(_DAY)
    assert c._quantile_state == QuantileState()


def test_train_quantiles_foreign_day_issued_is_a_noop():
    """A snapshot whose hours all belong to ANOTHER local day yields no
    corrected curve for the training day (legacy 4-day ring guard)."""
    store = _FakeStore()
    store.issued[_ISO] = _issued(corrected={_h(10, _DAY + timedelta(days=1)): 500.0})
    c = _make_coordinator(store)
    c._train_quantiles_day(_DAY)
    assert c._quantile_state == QuantileState()


def test_train_quantiles_without_overlapping_measured_hours_is_a_noop():
    """Hours with no measured counterpart are skipped; when nothing overlaps,
    no sample is folded at all."""
    store = _FakeStore()
    store.issued[_ISO] = _quantile_issued()
    store.record_hourly_actuals(_ISO, {"M1": {_h(13): 700.0}})  # no overlap
    c = _make_coordinator(store)
    c._train_quantiles_day(_DAY)
    assert c._quantile_state == QuantileState()


def test_train_quantiles_folds_only_measured_hours():
    store = _FakeStore()
    store.issued[_ISO] = _quantile_issued()
    # Only the 10:00 hour has measured energy; 11:00 is skipped.
    store.record_hourly_actuals(_ISO, {"M1": {_h(10): 450.0}})
    c = _make_coordinator(store)
    c._train_quantiles_day(_DAY)
    assert c._quantile_state != QuantileState()
    bins = c._quantile_state.bins
    assert len(bins) == 1
    (bin_key, entries), = bins.items()
    # Same (class x part) taxonomy as the day-ahead bias, date-stamped with
    # the trained day; relerr = measured / issued-corrected = 450/500.
    assert bin_key.startswith("clear|")
    assert entries[0][0] == _ISO
    assert entries[0][1] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# train_inverter_cal guards + happy path (η plausibility watchdog feeds)
# ---------------------------------------------------------------------------


def _ac_site() -> SiteConfig:
    return SiteConfig(
        latitude=48.5,
        longitude=12.2,
        planes=(
            PlaneConfig(name="M1", azimuth_deg=115.0, tilt_deg=70.0, wp=370.0,
                        actual_entity="sensor.m1"),
        ),
        groups=(InverterGroup(name="WR1", plane_names=("M1",), ac_limit_w=800.0),),
        ac_actual_entity="sensor.ac",
    )


async def test_train_inverter_cal_without_dc_actuals_is_a_noop():
    c = _make_coordinator()
    c._site = _ac_site()
    await c._train_inverter_cal(_DAY)
    assert c._inverter_cal_state == InverterCalState()


async def test_train_inverter_cal_without_ac_read_is_a_noop():
    store = _FakeStore()
    store.record_hourly_actuals(_ISO, {"M1": {_h(10): 500.0}})
    c = _make_coordinator(store)
    c._site = _ac_site()

    async def _empty(day):
        return {}

    c._async_read_ac_actuals = _empty
    await c._train_inverter_cal(_DAY)
    assert c._inverter_cal_state == InverterCalState()


async def test_train_inverter_cal_folds_only_overlapping_hours():
    store = _FakeStore()
    store.record_hourly_actuals(
        _ISO, {"M1": {_h(10): 500.0, _h(11): 500.0}}
    )
    c = _make_coordinator(store)
    c._site = _ac_site()

    async def _ac(day):
        return {_h(10): 460.0}  # 11:00 has no AC read -> skipped

    c._async_read_ac_actuals = _ac
    await c._train_inverter_cal(_DAY)
    # One eligible in-band ratio (460/500 = 0.92) folded into the EMA.
    assert c._inverter_cal_state.n == 1
    assert c._inverter_cal_state.eta == pytest.approx(0.92)
    # The raw-ratio diagnostic recorded the evidence (incl. band verdict).
    assert c._inverter_cal_raw["n"] == 1
    assert c._inverter_cal_raw["in_band_n"] == 1
    assert c._inverter_cal_raw["median_ratio"] == pytest.approx(0.92)


# ---------------------------------------------------------------------------
# train_day_ahead guards
# ---------------------------------------------------------------------------


def test_train_day_ahead_disabled_is_a_noop():
    store = _FakeStore()
    store.issued[_ISO] = _issued(raw={_h(10): 500.0})
    store.actuals[_ISO] = {"M1": 450.0}
    c = _make_coordinator(store)
    c._learner_config = replace(LearnerConfig(), day_ahead_enabled=False)
    c._train_day_ahead(_ISO, store.issued[_ISO], store.actuals[_ISO])
    assert c._bias_state.cells == {}


def test_train_day_ahead_foreign_day_issued_is_a_noop():
    store = _FakeStore()
    store.issued[_ISO] = _issued(raw={_h(10, _DAY + timedelta(days=1)): 500.0})
    store.actuals[_ISO] = {"M1": 450.0}
    c = _make_coordinator(store)
    c._train_day_ahead(_ISO, store.issued[_ISO], store.actuals[_ISO])
    assert c._bias_state.cells == {}


def test_train_day_ahead_zero_modeled_is_a_noop():
    store = _FakeStore()
    store.issued[_ISO] = _issued(raw={_h(10): 0.0})
    store.actuals[_ISO] = {"M1": 450.0}
    c = _make_coordinator(store)
    c._train_day_ahead(_ISO, store.issued[_ISO], store.actuals[_ISO])
    assert c._bias_state.cells == {}


def test_train_day_ahead_not_implemented_is_contained(monkeypatch):
    """A trainer refusing the sample shape (NotImplementedError) leaves the
    bias state untouched — the nightly job must not die on it."""
    store = _FakeStore()
    store.issued[_ISO] = _issued(
        raw={_h(10): 500.0}, cloud={_h(10): "clear"}
    )
    store.actuals[_ISO] = {"M1": 450.0}
    store.record_hourly_actuals(_ISO, {"M1": {_h(10): 450.0}})
    c = _make_coordinator(store)

    def _refuse(state, samples):
        raise NotImplementedError("sample shape not trainable")

    monkeypatch.setattr(bias_mod, "train_day_ahead_bias", _refuse)
    c._train_day_ahead(_ISO, store.issued[_ISO], store.actuals[_ISO])
    assert c._bias_state.cells == {}


# ---------------------------------------------------------------------------
# site_measured_hourly / metered_modeled_hourly / day_ahead_samples skips
# ---------------------------------------------------------------------------


def test_site_measured_hourly_skips_garbage_and_foreign_day_keys():
    c = _make_coordinator()
    out = c._site_measured_hourly(
        _ISO,
        {"M1": {"not-a-date": 5.0, _h(10, _DAY + timedelta(days=1)): 3.0}},
    )
    assert out is None  # nothing usable left for the training day


def test_metered_modeled_hourly_without_metered_plane_is_none():
    """No metered plane at all -> no measured side exists -> the comparison
    is impossible and the caller skips the day (never trains a gap)."""
    c = _make_coordinator()
    c._site = SiteConfig(
        latitude=48.5,
        longitude=12.2,
        planes=(
            PlaneConfig(name="M1", azimuth_deg=115.0, tilt_deg=70.0, wp=370.0),
        ),
        groups=(),
    )
    snap = IssuedSnapshot.from_dict(_issued(raw={_h(10): 500.0}))
    assert nightly_mod.metered_modeled_hourly(c, snap, {_h(10): 500.0}) is None


def test_day_ahead_samples_skips_unbinable_and_zero_cells():
    c = _make_coordinator()
    snap = IssuedSnapshot.from_dict(_issued(raw={_h(10): 500.0}))
    samples = c._day_ahead_samples(
        {"not-a-date": 100.0, _h(10): 0.0},  # unbinable + zero-modeled hours
        {"M1": 450.0},
        snap,
        None,
    )
    assert samples == []


def test_day_part_for_hourkey_garbage_is_none():
    c = _make_coordinator()
    assert c._day_part_for_hourkey("not-a-date") is None


def test_day_part_for_hourkey_falls_back_to_clock_without_longitude():
    c = _make_coordinator()
    c._site = SimpleNamespace(longitude=None)
    hkey = _h(10)
    from homeassistant.util import dt as dt_util

    expected = bias_mod.day_part_for_hour(
        dt_util.as_local(dt_util.parse_datetime(hkey)).hour
    )
    assert c._day_part_for_hourkey(hkey) == expected


# ---------------------------------------------------------------------------
# train_shademap guards
# ---------------------------------------------------------------------------


def _shademap_issued() -> dict:
    per_plane = {
        "M1": PlaneHourlyModeled(
            beam_wh={_h(10): 200.0},
            diffuse_wh={_h(10): 50.0},
            kc={_h(10): 0.9},
        ),
    }
    return _issued(raw={_h(10): 500.0}, per_plane=per_plane)


def test_train_shademap_disabled_is_a_noop():
    store = _FakeStore()
    c = _make_coordinator(store)
    c._learner_config = replace(LearnerConfig(), slow_enabled=False)
    c._train_shademap(_ISO, _shademap_issued(), {"M1": 450.0})
    assert c._shademap_state.channels == {}


def test_train_shademap_frozen_is_a_noop():
    store = _FakeStore()
    c = _make_coordinator(store)
    from homeassistant.util import dt as dt_util

    today = dt_util.as_local(dt_util.utcnow()).date().isoformat()
    c._drift_state = replace(DriftState(), collapse_frozen_date=today)
    c._train_shademap(_ISO, _shademap_issued(), {"M1": 450.0})
    assert c._shademap_state.channels == {}


def test_train_shademap_without_issued_or_breakdown_is_a_noop():
    c = _make_coordinator()
    c._train_shademap(_ISO, None, {"M1": 450.0})               # no snapshot
    c._train_shademap(_ISO, _issued(raw={_h(10): 500.0}), {"M1": 450.0})
    assert c._shademap_state.channels == {}                    # no per_plane


def test_train_shademap_without_hourly_actuals_is_a_noop():
    store = _FakeStore()
    store.issued[_ISO] = _shademap_issued()
    c = _make_coordinator(store)
    c._train_shademap(_ISO, store.issued[_ISO], {"M1": 450.0})
    assert c._shademap_state.channels == {}


def test_train_shademap_overcast_day_is_rejected_by_the_clear_gate():
    """Measured 10 % of modeled: the forecast wrongly called it clear — the
    weather bust must not darken a geometric bin (SPEC §9.1)."""
    store = _FakeStore()
    store.issued[_ISO] = _shademap_issued()
    store.record_hourly_actuals(_ISO, {"M1": {_h(10): 20.0}})
    c = _make_coordinator(store)
    c._train_shademap(_ISO, store.issued[_ISO], {"M1": 20.0})
    assert c._shademap_state.channels == {}


def test_train_shademap_skips_channels_without_measured_hours():
    store = _FakeStore()
    issued = _issued(
        raw={_h(10): 500.0},
        per_plane={
            "M1": PlaneHourlyModeled(
                beam_wh={_h(10): 200.0}, diffuse_wh={_h(10): 50.0},
                kc={_h(10): 0.05},
            ),
            "M2": PlaneHourlyModeled(
                beam_wh={_h(10): 200.0}, diffuse_wh={_h(10): 50.0},
                kc={_h(10): 0.9},
            ),
        },
    )
    store.issued[_ISO] = issued
    # Measured energy only for M1 (its kc ~ 0.05 hour then fails the
    # quasi-clear gate), nothing at all for M2 -> its channel is skipped.
    store.record_hourly_actuals(_ISO, {"M1": {_h(10): 10.0}})
    c = _make_coordinator(store)
    # Make the day pass the measured-clear gate so the channel loop runs.
    c._day_is_measured_clear = lambda iso, snap, hourly: True
    c._train_shademap(_ISO, store.issued[_ISO], {"M1": 10.0})
    # M1's overcast hour is gate-rejected; M2 was never visited:
    # nothing trained, nothing persisted.
    assert c._shademap_state.channels == {}
    assert store.shademap == ShademapState().to_dict()


# ---------------------------------------------------------------------------
# day_is_measured_clear guards
# ---------------------------------------------------------------------------


def test_day_is_measured_clear_unmetered_site_is_false():
    c = _make_coordinator()
    c._site = SiteConfig(
        latitude=48.5,
        longitude=12.2,
        planes=(PlaneConfig(name="M1", azimuth_deg=115.0, tilt_deg=70.0,
                            wp=370.0),),
        groups=(),
    )
    snap = IssuedSnapshot.from_dict(_issued(raw={_h(10): 500.0}))
    assert c._day_is_measured_clear(_ISO, snap, {"M1": {_h(10): 500.0}}) is False


def test_day_is_measured_clear_zero_modeled_is_false():
    c = _make_coordinator()
    snap = IssuedSnapshot.from_dict(_issued(raw={_h(10): 0.0}))
    assert c._day_is_measured_clear(_ISO, snap, {"M1": {_h(10): 500.0}}) is False


def test_day_is_measured_clear_garbage_keys_count_nothing():
    c = _make_coordinator()
    snap = IssuedSnapshot.from_dict(_issued(raw={_h(10): 500.0}))
    assert c._day_is_measured_clear(_ISO, snap, {"M1": {"bad": 500.0}}) is False


# ---------------------------------------------------------------------------
# train_channel guards + happy path
# ---------------------------------------------------------------------------


def _modeled(kc: float = 0.9, *, hkeys=None) -> PlaneHourlyModeled:
    hkeys = hkeys or (_h(10),)
    return PlaneHourlyModeled(
        beam_wh={k: 200.0 for k in hkeys},
        diffuse_wh={k: 50.0 for k in hkeys},
        kc={k: kc for k in hkeys},
    )


def test_train_channel_unknown_plane_is_a_noop():
    c = _make_coordinator()
    state, changed = c._train_channel(
        ShademapState(), "NOPE", _modeled(), {_h(10): 300.0}
    )
    assert changed is False
    assert state.channels == {}


def test_train_channel_skips_hours_without_beam_or_valid_stamp():
    c = _make_coordinator()
    modeled = PlaneHourlyModeled(
        beam_wh={_h(10): 0.0, "not-a-date": 200.0},  # no beam / bad stamp
        diffuse_wh={_h(10): 50.0, "not-a-date": 50.0},
        kc={_h(10): 0.9, "not-a-date": 0.9},
    )
    state, changed = c._train_channel(
        ShademapState(), "M1", modeled, {_h(10): 300.0, "not-a-date": 300.0}
    )
    assert changed is False
    assert state.channels == {}


def test_train_channel_rejects_a_non_quasi_clear_hour():
    """kc ~ 0.05 (deep overcast) fails the quasi-clear gate: a weather-bust
    hour never writes into the geometric map."""
    c = _make_coordinator()
    state, changed = c._train_channel(
        ShademapState(), "M1", _modeled(kc=0.05), {_h(10): 300.0}
    )
    assert changed is False
    assert state.channels == {}


def test_train_channel_drops_an_undefined_transmittance(monkeypatch):
    c = _make_coordinator()
    monkeypatch.setattr(shademap_mod, "is_quasi_clear", lambda **kw: True)
    monkeypatch.setattr(
        shademap_mod, "beam_referenced_t", lambda *a, **kw: None
    )
    state, changed = c._train_channel(
        ShademapState(), "M1", _modeled(), {_h(10): 300.0}
    )
    assert changed is False
    assert state.channels == {}


def test_train_channel_not_implemented_is_contained(monkeypatch):
    c = _make_coordinator()
    monkeypatch.setattr(shademap_mod, "is_quasi_clear", lambda **kw: True)
    monkeypatch.setattr(shademap_mod, "beam_referenced_t", lambda *a: 0.5)

    def _refuse(state, **kw):
        raise NotImplementedError("bin shape not trainable")

    monkeypatch.setattr(shademap_mod, "update_bin", _refuse)
    state, changed = c._train_channel(
        ShademapState(), "M1", _modeled(), {_h(10): 300.0}
    )
    assert changed is False
    assert state.channels == {}


def test_train_channel_happy_path_updates_the_own_channel(monkeypatch):
    """A quasi-clear hour EMA-updates exactly one bin under the plane's OWN
    channel (storage is per plane, SPEC §9.2) and persists it."""
    store = _FakeStore()
    c = _make_coordinator(store)
    monkeypatch.setattr(shademap_mod, "is_quasi_clear", lambda **kw: True)
    monkeypatch.setattr(shademap_mod, "beam_referenced_t", lambda *a: 0.5)
    state, changed = c._train_channel(
        ShademapState(), "M1", _modeled(), {_h(10): 300.0}
    )
    assert changed is True
    assert list(state.channels) == ["M1"]
    assert len(state.channels["M1"]) == 1
    bin_ = next(iter(state.channels["M1"].values()))
    assert bin_.tau == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# update_drift guards + slow-layer auto-disable with rollback
# ---------------------------------------------------------------------------


def test_update_drift_zero_totals_is_a_noop():
    store = _FakeStore()
    c = _make_coordinator(store)
    c._update_drift(
        _ISO, _issued(raw={_h(10): 0.0}, corrected={_h(10): 0.0}), {"M1": 0.0}
    )
    assert c._drift_state.daily_mae == {}


def test_update_drift_auto_disables_slow_layer_and_rolls_back():
    """The 7th consecutive slow-layer losing day auto-disables the layer,
    rolls it back to the pre-streak snapshot and raises the repair card
    (SPEC §9.8: never degrade silently)."""
    from custom_components.balcony_solar_forecast.const import (
        DRIFT_LOSS_STREAK_DAYS,
    )

    store = _FakeStore()
    restored = ShademapState(
        channels={"M1": {"1:2:0": ShademapBin(tau=0.77, n=5)}}
    )
    store.push_snapshot(
        LearnerSnapshot(
            taken_at="2026-06-24T01:30:00+00:00",
            bias=BiasState(),
            shademap=restored,
        )
    )
    c = _make_coordinator(store)
    c._drift_state = replace(
        DriftState(), slow_loss_streak=DRIFT_LOSS_STREAK_DAYS - 1
    )
    # Physics matches measured (raw_mae ~ 0); slow-only and corrected are
    # materially worse -> the SLOW layer owns the loss.
    issued = _issued(
        raw={_h(10): 5000.0, _h(11): 5000.0},
        corrected={_h(10): 10000.0, _h(11): 10000.0},
        slow_only={_h(10): 10000.0, _h(11): 10000.0},
    )
    c._update_drift(_ISO, issued, {"M1": 5000.0, "M2": 5000.0})

    assert c._drift_state.slow_disabled is True
    assert c._drift_state.slow_loss_streak == 0
    # Rolled back to the pre-streak shademap, and the verdict persisted.
    assert c._shademap_state.channels["M1"]["1:2:0"].tau == pytest.approx(0.77)
    assert store.drift["slow_disabled"] is True
    assert store.shademap["channels"]["M1"]["1:2:0"]["tau"] == pytest.approx(0.77)


# ---------------------------------------------------------------------------
# Coordinator delegates: rebuild_learner_config / shade profile / fingerprint
# ---------------------------------------------------------------------------


def test_rebuild_learner_config_clears_slow_disable_on_reenable():
    """A drift auto-disable lifts ONLY on a real OFF->ON option transition
    (SPEC §9.8): the flag + streak clear and the state persists."""
    store = _FakeStore()
    c = _make_coordinator(store)
    c._drift_state = replace(
        DriftState(),
        slow_disabled=True,
        slow_loss_streak=3,
        slow_option_seen=False,   # option was OFF when the drift fired
        fast_option_seen=True,
    )
    c.rebuild_learner_config()  # options now say slow_enabled=True again
    assert c._drift_state.slow_disabled is False
    assert c._drift_state.slow_loss_streak == 0
    assert c._drift_state.slow_option_seen is True
    assert store.drift["slow_disabled"] is False


def test_build_shade_profile_without_planes_returns_empty():
    c = _make_coordinator()
    c._site = SiteConfig(latitude=48.5, longitude=12.2, planes=(), groups=())
    assert c.build_shade_profile() == {}


def test_reconcile_fingerprint_without_store_accessors_is_a_noop():
    c = _make_coordinator()
    c._store = object()  # older schema in flight: no fingerprint accessors
    c._reconcile_config_fingerprint()  # must not raise, must not reseed
    assert c._bias_state.cells == {}


def test_measured_total_stat_id_resolves_via_entity_registry(monkeypatch):
    from homeassistant.helpers import entity_registry as er

    class _Registry:
        def async_get_entity_id(self, domain, platform, unique_id):
            assert unique_id == "e1_measured_dc_power_total"
            return "sensor.balcony_measured_dc_total"

    monkeypatch.setattr(er, "async_get", lambda hass: _Registry())
    c = _make_coordinator()
    assert c._measured_total_stat_id() == "sensor.balcony_measured_dc_total"


def test_quantile_curves_convert_band_watts_to_slot_wh():
    c = _make_coordinator()
    start = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    result = ForecastResult(
        slot_starts=(start,),
        total_watts=(250.0,),
        plane_results=(),
        hourly_wh={},
        p10_watts=(100.0,),
        p50_watts=(200.0,),
        p90_watts=(300.0,),
    )
    curves = c._quantile_curves(result)
    key = start.isoformat()
    # Instantaneous watts -> per-slot Wh (w * 0.25), one entry per band.
    assert curves["p10"] == {key: 25.0}
    assert curves["p50"] == {key: 50.0}
    assert curves["p90"] == {key: 75.0}


async def test_async_read_daily_actuals_returns_only_the_daily_half():
    c = _make_coordinator()

    async def _read(day):
        return {"M1": 450.0}, {"M1": {_h(10): 450.0}}

    c._async_read_actuals = _read
    assert await c._async_read_daily_actuals(_DAY) == {"M1": 450.0}


async def test_async_read_actuals_delegates_to_actuals_module(monkeypatch):
    from custom_components.balcony_solar_forecast import _actuals

    seen = {}

    async def _read(coord, day):
        seen["day"] = day
        return {"M1": 1.0}, {}

    monkeypatch.setattr(_actuals, "async_read_actuals", _read)
    c = _make_coordinator()
    daily, hourly = await c._async_read_actuals(_DAY)
    assert (daily, hourly) == ({"M1": 1.0}, {})
    assert seen["day"] == _DAY


async def test_async_read_ac_actuals_without_meter_is_empty():
    c = _make_coordinator()  # the learning-test site has no AC meter
    assert await c._async_read_ac_actuals(_DAY) == {}


async def test_slow_only_hourly_runs_the_engine_without_day_ahead():
    """The nightly attribution pass returns the shademap-only curve sliced to
    the snapshot's local day (slow-only == raw when the layer is inactive is
    handled by the caller; here the layer IS active)."""
    c = _make_coordinator()
    c._shademap_state = ShademapState(
        channels={"M1": {"1:2:0": ShademapBin(tau=0.5, n=10)}}
    )
    c._cached_weather = lambda: _weather_series()
    out = await c._slow_only_hourly(_ISO)
    assert out  # hours of the local day came back
    assert all(k.startswith(_ISO) for k in out)
    assert all(v >= 0.0 for v in out.values())
    # Daylight hours carry real energy (the fixture is bright at mid-day).
    assert max(out.values()) > 0.0


async def test_slow_only_hourly_inactive_layer_or_no_weather_is_empty():
    c = _make_coordinator()
    c._cached_weather = lambda: _weather_series()
    assert await c._slow_only_hourly(_ISO) == {}  # no shademap channels

    c._shademap_state = ShademapState(
        channels={"M1": {"1:2:0": ShademapBin(tau=0.5, n=10)}}
    )
    c._cached_weather = lambda: None
    assert await c._slow_only_hourly(_ISO) == {}
