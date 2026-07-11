"""Tests for the shade-profile UI platform layer (select / date / sensor).

Owner: platform (shade-profile diagram, SPEC §15). The three entities that
drive the diagram — the module ``select``, the date ``date`` and the
``ShadeProfileSensor`` — had zero platform coverage (audit #19). These exercise
the entity-layer glue WITHOUT standing up a full HA instance: entities are
built via ``__new__`` (the ``_bare`` helper, mirroring ``tests/test_platforms``)
against a hand-rolled fake coordinator, and the restore path is driven through
``async_added_to_hass`` with the CoordinatorEntity super-plumbing monkeypatched
to a no-op. A final integration-light block round-trips the selection setters +
``build_shade_profile`` through a REAL coordinator (the learning-test's
``_make_coordinator``) with the two-plane fake site.

Import is via ``custom_components.balcony_solar_forecast`` (the real HA-importing
package), so HA must be installed; the whole module is skipped otherwise.
"""

from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("voluptuous")

from homeassistant.core import State  # noqa: E402
from homeassistant.helpers.update_coordinator import (  # noqa: E402
    CoordinatorEntity,
)

from custom_components.balcony_solar_forecast import date as date_mod  # noqa: E402
from custom_components.balcony_solar_forecast import select as select_mod  # noqa: E402
from custom_components.balcony_solar_forecast import sensor as sensor_mod  # noqa: E402
from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    ATTR_SP_AXIS_AZ_MAX,
    ATTR_SP_AXIS_AZ_MIN,
    ATTR_SP_AZIMUTH,
    ATTR_SP_HORIZON_AZIMUTH,
    ATTR_SP_SAMPLE_N,
    ATTR_SP_SHADE_HORIZON,
    ATTR_SP_STATIC_HORIZON,
    ATTR_SP_SUN_ELEVATION,
    ATTR_SP_TIME,
    ATTR_SP_TRANSMITTANCE,
    ATTR_SP_TRANSMITTANCE_INDIVIDUAL,
)

# The real-coordinator integration block reuses the learning-test builder.
from tests.test_coordinator_learning import _make_coordinator  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes + the __new__ entity builder (mirrors tests/test_platforms._bare).
# ---------------------------------------------------------------------------


class _FakeEntry:
    entry_id = "abc123"


def _bare(cls, coordinator, **attrs):
    """Instantiate an entity bypassing HA's CoordinatorEntity.__init__."""
    obj = cls.__new__(cls)
    obj.coordinator = coordinator
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


class _ShadeCoordinator:
    """Minimal stand-in exposing exactly the shade-profile surface the three
    entities read/write. ``builder`` selects the ``build_shade_profile`` shape:
    ``"return"`` returns ``profile``, ``"raise"`` raises, ``"missing"`` omits
    the attribute entirely (the getattr fallback path)."""

    def __init__(
        self,
        *,
        plane_names=(),
        module="",
        sp_date=None,
        profile=None,
        builder="return",
    ) -> None:
        self.entry = _FakeEntry()
        self.last_update_success = True
        self._plane_names = list(plane_names)
        self.shade_profile_module = module
        self.shade_profile_date = sp_date
        self._profile = profile
        self.module_set: list[str] = []
        self.date_set: list[date] = []
        if builder == "return":
            self.build_shade_profile = lambda: self._profile
        elif builder == "raise":
            def _boom():
                raise RuntimeError("diagram build blew up")

            self.build_shade_profile = _boom
        # builder == "missing": leave the attribute unset (getattr -> None).

    def shade_profile_plane_names(self) -> list[str]:
        return list(self._plane_names)

    def set_shade_profile_module(self, module: str) -> None:
        self.module_set.append(module)
        self.shade_profile_module = module

    def set_shade_profile_date(self, day: date) -> None:
        self.date_set.append(day)
        self.shade_profile_date = day


# ---------------------------------------------------------------------------
# Select: options / current_option / select+write / restore decision
# ---------------------------------------------------------------------------


def test_select_options_mirror_plane_names():
    coord = _ShadeCoordinator(plane_names=["M1", "M2", "M3"])
    sel = _bare(select_mod.ShadeProfileModuleSelect, coord)
    assert sel.options == ["M1", "M2", "M3"]
    # Always available (config entity), independent of forecast state.
    assert sel.available is True


def test_select_current_option_passes_through_and_none_on_empty():
    sel = _bare(
        select_mod.ShadeProfileModuleSelect,
        _ShadeCoordinator(plane_names=["M1", "M2"], module="M2"),
    )
    assert sel.current_option == "M2"
    # Empty string collapses to None (no selection yet).
    empty = _bare(
        select_mod.ShadeProfileModuleSelect,
        _ShadeCoordinator(plane_names=["M1", "M2"], module=""),
    )
    assert empty.current_option is None


async def test_select_async_select_option_pushes_and_writes():
    coord = _ShadeCoordinator(plane_names=["M1", "M2"])
    written: list[bool] = []
    sel = _bare(
        select_mod.ShadeProfileModuleSelect,
        coord,
        async_write_ha_state=lambda: written.append(True),
    )
    await sel.async_select_option("M2")
    assert coord.module_set == ["M2"]
    assert written == [True]


async def test_select_restore_valid_option_is_pushed(monkeypatch):
    async def _noop_super(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop_super)
    coord = _ShadeCoordinator(plane_names=["M1", "M2"])
    sel = _bare(select_mod.ShadeProfileModuleSelect, coord)

    async def _last():
        return State("select.x", "M2")

    sel.async_get_last_state = _last
    await sel.async_added_to_hass()
    assert coord.module_set == ["M2"]


async def test_select_restore_unknown_option_is_ignored(monkeypatch):
    async def _noop_super(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop_super)
    coord = _ShadeCoordinator(plane_names=["M1", "M2"])
    sel = _bare(select_mod.ShadeProfileModuleSelect, coord)

    async def _last():
        # A plane renamed/removed since: not in options -> not pushed.
        return State("select.x", "M9")

    sel.async_get_last_state = _last
    await sel.async_added_to_hass()
    assert coord.module_set == []


async def test_select_restore_no_prior_state_is_ignored(monkeypatch):
    async def _noop_super(self):
        return None

    monkeypatch.setattr(CoordinatorEntity, "async_added_to_hass", _noop_super)
    coord = _ShadeCoordinator(plane_names=["M1", "M2"])
    sel = _bare(select_mod.ShadeProfileModuleSelect, coord)

    async def _last():
        return None

    sel.async_get_last_state = _last
    await sel.async_added_to_hass()
    assert coord.module_set == []


# ---------------------------------------------------------------------------
# Date: native_value / set+write / no restore (defaults to coordinator today)
# ---------------------------------------------------------------------------


def test_date_native_value_reads_coordinator():
    day = date(2026, 6, 21)
    d = _bare(date_mod.ShadeProfileDate, _ShadeCoordinator(sp_date=day))
    assert d.native_value == day
    assert d.available is True


async def test_date_async_set_value_pushes_and_writes():
    coord = _ShadeCoordinator()
    written: list[bool] = []
    d = _bare(
        date_mod.ShadeProfileDate,
        coord,
        async_write_ha_state=lambda: written.append(True),
    )
    day = date(2026, 3, 14)
    await d.async_set_value(day)
    assert coord.date_set == [day]
    assert written == [True]


def test_date_has_no_restore_plumbing():
    """No RestoreEntity restore of the value: a fresh entity's native_value is
    exactly whatever the coordinator's default property yields (today), with no
    async_added_to_hass restore leg of its own (behaviour + type contract)."""
    # The entity is not a RestoreEntity (contrast the select), so a restart
    # re-opens on the coordinator default rather than a persisted pick.
    from homeassistant.helpers.restore_state import RestoreEntity

    assert not issubclass(date_mod.ShadeProfileDate, RestoreEntity)
    # And native_value is a pure pass-through of the coordinator property.
    default_day = date(2026, 7, 10)
    d = _bare(date_mod.ShadeProfileDate, _ShadeCoordinator(sp_date=default_day))
    assert d.native_value == default_day


# ---------------------------------------------------------------------------
# ShadeProfileSensor: recompute / native_value / attrs / robustness
# ---------------------------------------------------------------------------


def _profile(**over):
    """A canned build_shade_profile result (parallel arrays + summary)."""
    base = {
        "module": "M1",
        "date": "2026-06-21",
        "sample_count": 40,
        "shaded_fraction": 0.25,
        "has_learned_data": False,
        ATTR_SP_TIME: ["2026-06-21T05:00:00"],
        ATTR_SP_AZIMUTH: [70.0],
        ATTR_SP_SUN_ELEVATION: [5.0],
        ATTR_SP_TRANSMITTANCE: [1.0],
        ATTR_SP_HORIZON_AZIMUTH: [90.0],
        ATTR_SP_STATIC_HORIZON: [0.0],
        ATTR_SP_SHADE_HORIZON: [0.0],
    }
    base.update(over)
    return base


def test_sensor_recompute_populates_data_from_builder():
    profile = _profile()
    coord = _ShadeCoordinator(profile=profile, builder="return")
    s = _bare(sensor_mod.ShadeProfileSensor, coord, _data={})
    s._recompute()
    assert s._data == profile


def test_sensor_native_value_is_percent_rounded():
    coord = _ShadeCoordinator(
        profile=_profile(sample_count=40, shaded_fraction=0.2537), builder="return"
    )
    s = _bare(sensor_mod.ShadeProfileSensor, coord, _data={})
    s._recompute()
    assert s.native_value == pytest.approx(25.4)


def test_sensor_native_value_none_when_no_samples():
    s = _bare(
        sensor_mod.ShadeProfileSensor,
        _ShadeCoordinator(),
        _data={"sample_count": 0, "shaded_fraction": 0.5},
    )
    assert s.native_value is None
    # Missing sample_count key altogether -> also None.
    s2 = _bare(
        sensor_mod.ShadeProfileSensor,
        _ShadeCoordinator(),
        _data={"shaded_fraction": 0.5},
    )
    assert s2.native_value is None
    # Samples present but no fraction -> None (guarded division).
    s3 = _bare(
        sensor_mod.ShadeProfileSensor,
        _ShadeCoordinator(),
        _data={"sample_count": 40, "shaded_fraction": None},
    )
    assert s3.native_value is None


def test_sensor_extra_state_attributes_pass_the_dict_through():
    profile = _profile()
    s = _bare(sensor_mod.ShadeProfileSensor, _ShadeCoordinator(), _data=profile)
    attrs = s.extra_state_attributes
    assert attrs == profile
    # A copy, not the same object (dict() the internal store).
    assert attrs is not s._data


def test_sensor_without_builder_yields_empty_and_none():
    coord = _ShadeCoordinator(builder="missing")
    assert not hasattr(coord, "build_shade_profile")
    s = _bare(sensor_mod.ShadeProfileSensor, coord, _data={"stale": 1})
    s._recompute()
    assert s._data == {}
    assert s.native_value is None
    assert s.extra_state_attributes == {}


def test_sensor_builder_exception_is_swallowed_to_empty():
    coord = _ShadeCoordinator(builder="raise")
    s = _bare(sensor_mod.ShadeProfileSensor, coord, _data={"stale": 1})
    s._recompute()
    assert s._data == {}
    assert s.native_value is None


def test_sensor_available_even_when_update_failed():
    coord = _ShadeCoordinator()
    coord.last_update_success = False
    s = _bare(sensor_mod.ShadeProfileSensor, coord, _data={})
    # Diagnostic: pure geometry, always available.
    assert s.available is True


def test_sensor_unrecorded_attributes_are_exactly_the_curve_and_axis_attrs():
    # The nine per-selection curve arrays (incl. the pooled/individual τ pair and
    # the per-sample evidence count) PLUS the two year-stable axis bounds
    # (constant site geometry — recorder history is noise) are excluded.
    assert sensor_mod.ShadeProfileSensor._unrecorded_attributes == frozenset(
        {
            ATTR_SP_TIME,
            ATTR_SP_AZIMUTH,
            ATTR_SP_SUN_ELEVATION,
            ATTR_SP_TRANSMITTANCE,
            ATTR_SP_TRANSMITTANCE_INDIVIDUAL,
            ATTR_SP_SAMPLE_N,
            ATTR_SP_HORIZON_AZIMUTH,
            ATTR_SP_STATIC_HORIZON,
            ATTR_SP_SHADE_HORIZON,
            ATTR_SP_AXIS_AZ_MIN,
            ATTR_SP_AXIS_AZ_MAX,
        }
    )


# ---------------------------------------------------------------------------
# Integration-light: real coordinator + fake site (M1 115 / M2 205).
# ---------------------------------------------------------------------------


def _real_coordinator():
    """A real BalconySolarCoordinator with the fake two-plane site whose
    selection setters are made side-effect free (no listener plumbing)."""
    c = _make_coordinator()
    # set_shade_profile_* call async_update_listeners(); the __new__-built
    # coordinator has no listener registry, so neutralise it (unit-style).
    c.async_update_listeners = lambda: None
    return c


def test_real_default_module_is_front_plane_on_tie():
    # Azimuths 115 (M1) / 205 (M2): counts tie -> max keeps the FIRST -> M1.
    c = _real_coordinator()
    assert c.shade_profile_module == "M1"


def test_real_set_module_and_date_round_trip():
    c = _real_coordinator()
    c.set_shade_profile_module("M2")
    assert c.shade_profile_module == "M2"
    day = date(2026, 6, 21)
    c.set_shade_profile_date(day)
    assert c.shade_profile_date == day


def test_real_build_shade_profile_round_trips_selection():
    c = _real_coordinator()
    c.set_shade_profile_module("M2")
    day = date(2026, 6, 21)  # midsummer -> real daylight samples
    c.set_shade_profile_date(day)
    result = c.build_shade_profile()
    assert result["module"] == "M2"
    assert result["date"] == day.isoformat()
    assert result["sample_count"] > 0
    # Empty shademap on the fake site -> no learned shading blended in.
    assert result["has_learned_data"] is False


def test_real_build_shade_profile_default_module():
    c = _real_coordinator()
    day = date(2026, 6, 21)
    c.set_shade_profile_date(day)
    result = c.build_shade_profile()
    # No explicit module -> the front-plane default (M1).
    assert result["module"] == "M1"


def test_real_build_shade_profile_for_is_read_only_and_memo_safe():
    # The get_shade_profile service path (card comparison overlay): an EXPLICIT
    # module/date query must neither mutate the live selection nor evict the
    # single-slot primary memo.
    c = _real_coordinator()
    c.set_shade_profile_module("M1")
    primary_day = date(2026, 6, 21)
    c.set_shade_profile_date(primary_day)
    primary = c.build_shade_profile()  # populates the single-slot memo
    assert primary["module"] == "M1"

    # Ad-hoc query for a DIFFERENT module + date.
    other = c.build_shade_profile_for("M2", date(2026, 12, 21))
    assert other["module"] == "M2"
    assert other["date"] == "2026-12-21"
    assert other["sample_count"] > 0
    # sample_n rides along, parallel to the sun-path samples.
    assert len(other[ATTR_SP_SAMPLE_N]) == other["sample_count"]

    # Live selection untouched...
    assert c.shade_profile_module == "M1"
    assert c.shade_profile_date == primary_day
    # ...and the primary memo entry survives (same cached object returned).
    assert c.build_shade_profile() is primary


def test_real_build_shade_profile_for_unknown_module_is_empty():
    c = _real_coordinator()
    assert c.build_shade_profile_for("nope", date(2026, 6, 21)) == {}
