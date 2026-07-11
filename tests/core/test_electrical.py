"""Unit tests for core/electrical.py: Ross DC model + AC group clamp.

Plain pytest, no Home Assistant imports (SPEC §4). Physics constants are
pulled from const.py so the tests track any calibrated change of the model.
"""

from __future__ import annotations

import math

import pytest
from balcony_solar_forecast.const import (
    DEFAULT_INVERTER_EFFICIENCY,
    INVERTER_EFFICIENCY_MAX,
    INVERTER_EFFICIENCY_MIN,
    ROSS_COEFF,
    TEMP_COEFF_PER_K,
    TEMP_REF_C,
)
from balcony_solar_forecast.core.electrical import (
    clamp_groups,
    clamp_groups_ac,
    dc_power,
)
from balcony_solar_forecast.core.types import InverterGroup


def _expected_dc(poa: float, wp: float, temp: float, eff: float) -> float:
    """Reference Ross model recomputed independently of the implementation."""
    t_cell = temp + ROSS_COEFF * poa
    factor = 1.0 + TEMP_COEFF_PER_K * (t_cell - TEMP_REF_C)
    return wp * (poa / 1000.0) * factor * eff


# --------------------------------------------------------------------------
# dc_power
# --------------------------------------------------------------------------


def test_dc_power_zero_irradiance_is_zero():
    assert dc_power(0.0, 430.0, 20.0, 0.96) == 0.0


def test_dc_power_negative_irradiance_is_zero():
    # A dusk transposition rounding artefact must never produce power.
    assert dc_power(-5.0, 430.0, 20.0, 0.96) == 0.0


def test_dc_power_matches_reference_model_at_stc_like_point():
    got = dc_power(1000.0, 400.0, 25.0, 0.96)
    exp = _expected_dc(1000.0, 400.0, 25.0, 0.96)
    assert got == pytest.approx(exp)
    # Sanity: warm cell (~59 C) derates ~11 % before the 0.96 efficiency.
    assert 330.0 < got < 345.0


def test_dc_power_hot_cell_derates_below_cold_cell():
    hot = dc_power(800.0, 430.0, 35.0, 0.96)
    cold = dc_power(800.0, 430.0, 5.0, 0.96)
    assert hot < cold


def test_dc_power_cold_cell_can_exceed_nominal_ratio():
    # Below 25 C reference the silicon gains a little — kept, not capped.
    # POA 1000, ambient 0 C -> Tcell 34.2 C, still a mild loss; use a case
    # where the cell stays under 25 C by using low POA and cold air.
    poa, temp, wp, eff = 200.0, -10.0, 430.0, 1.0
    t_cell = temp + ROSS_COEFF * poa  # -10 + 6.84 = -3.16 C
    assert t_cell < TEMP_REF_C
    got = dc_power(poa, wp, temp, eff)
    # factor > 1 because the cell is colder than 25 C.
    linear = wp * (poa / 1000.0) * eff
    assert got > linear


def test_dc_power_scales_linearly_with_irradiance_at_fixed_cell_temp():
    # Hold ambient so both share the same Ross offset shape; ratio ~ POA ratio
    # is only approximate because Tcell tracks POA, so just assert monotonic +
    # positive scaling.
    low = dc_power(250.0, 430.0, 20.0, 0.96)
    high = dc_power(750.0, 430.0, 20.0, 0.96)
    assert 0.0 < low < high


def test_dc_power_efficiency_is_multiplicative():
    a = dc_power(600.0, 430.0, 20.0, 0.96)
    b = dc_power(600.0, 430.0, 20.0, 0.48)
    assert b == pytest.approx(a * 0.5)


def test_dc_power_absurd_heat_never_negative():
    # A physically nonsensical POA that would drive the derate below zero must
    # clamp to zero, not go negative.
    huge = 1_000_000.0
    assert dc_power(huge, 430.0, 60.0, 0.96) == 0.0


# --- per-plane Ross coefficient (audit #29) -------------------------------


def test_dc_power_ross_coeff_none_matches_default():
    # Omitting the coefficient (or passing None) uses the module-wide default.
    assert dc_power(800.0, 430.0, 30.0, 0.96) == dc_power(
        800.0, 430.0, 30.0, 0.96, ross_coeff=ROSS_COEFF
    )


def test_dc_power_ross_coeff_matches_reference_model():
    poa, wp, temp, eff, k = 800.0, 430.0, 30.0, 0.96, 0.05
    t_cell = temp + k * poa
    factor = 1.0 + TEMP_COEFF_PER_K * (t_cell - TEMP_REF_C)
    exp = wp * (poa / 1000.0) * factor * eff
    assert dc_power(poa, wp, temp, eff, ross_coeff=k) == pytest.approx(exp)


def test_dc_power_higher_ross_coeff_derates_more_when_warm():
    # Warm regime (Tcell well above 25 C): a facade-parallel mount (high Ross
    # coeff -> hotter cell) loses more than a free-standing one (low coeff).
    facade = dc_power(800.0, 430.0, 30.0, 0.96, ross_coeff=0.056)
    free_standing = dc_power(800.0, 430.0, 30.0, 0.96, ross_coeff=0.02)
    assert facade < free_standing
    # Both bracket the default coefficient.
    default = dc_power(800.0, 430.0, 30.0, 0.96)
    assert facade < default < free_standing


# --------------------------------------------------------------------------
# clamp_groups
# --------------------------------------------------------------------------


def _group(name: str, planes: tuple[str, ...], limit: float) -> InverterGroup:
    return InverterGroup(name=name, plane_names=planes, ac_limit_w=limit)


def test_clamp_below_limit_passes_through():
    watts = {"M1": 300.0, "M2": 250.0}
    groups = [_group("WR1", ("M1", "M2"), 800.0)]
    out = clamp_groups(watts, groups)
    assert out == {"M1": 300.0, "M2": 250.0}


def test_clamp_two_430w_modules_cannot_exceed_800():
    # Two ports each near 430 W would sum to 860 > 800 AC limit.
    watts = {"M7": 430.0, "M8": 430.0}
    groups = [_group("WR4", ("M7", "M8"), 800.0)]
    out = clamp_groups(watts, groups)
    assert out["M7"] + out["M8"] == pytest.approx(800.0)
    # Proportional split of an equal pair -> equal halves.
    assert out["M7"] == pytest.approx(400.0)
    assert out["M8"] == pytest.approx(400.0)


def test_clamp_distributes_proportionally_for_unequal_pair():
    watts = {"A": 600.0, "B": 300.0}  # sum 900 > 800
    groups = [_group("G", ("A", "B"), 800.0)]
    out = clamp_groups(watts, groups)
    total = out["A"] + out["B"]
    assert total == pytest.approx(800.0)
    # Shares preserved: A had 2/3, B had 1/3.
    assert out["A"] == pytest.approx(800.0 * 2 / 3)
    assert out["B"] == pytest.approx(800.0 * 1 / 3)


def test_clamp_planes_outside_any_group_pass_through_unchanged():
    watts = {"M1": 500.0, "loose": 999.0}
    groups = [_group("WR1", ("M1",), 400.0)]
    out = clamp_groups(watts, groups)
    assert out["M1"] == pytest.approx(400.0)
    assert out["loose"] == 999.0


def test_clamp_ignores_missing_member_names():
    # A group naming a plane not present in the watts map must not crash and
    # must clamp on the members that ARE present.
    watts = {"M1": 500.0}
    groups = [_group("WR1", ("M1", "M2_absent"), 400.0)]
    out = clamp_groups(watts, groups)
    assert out["M1"] == pytest.approx(400.0)
    assert "M2_absent" not in out


def test_clamp_empty_group_is_noop():
    watts = {"M1": 500.0}
    groups = [_group("empty", (), 100.0)]
    out = clamp_groups(watts, groups)
    assert out == {"M1": 500.0}


def test_clamp_zero_total_group_is_noop():
    watts = {"M1": 0.0, "M2": 0.0}
    groups = [_group("WR1", ("M1", "M2"), 800.0)]
    out = clamp_groups(watts, groups)
    assert out == {"M1": 0.0, "M2": 0.0}


def test_clamp_does_not_mutate_input_mapping():
    watts = {"M7": 430.0, "M8": 430.0}
    snapshot = dict(watts)
    groups = [_group("WR4", ("M7", "M8"), 800.0)]
    clamp_groups(watts, groups)
    assert watts == snapshot


def test_clamp_multiple_independent_groups():
    watts = {"M1": 500.0, "M2": 500.0, "M3": 100.0, "M4": 100.0}
    groups = [
        _group("WR1", ("M1", "M2"), 800.0),  # 1000 -> 800
        _group("WR2", ("M3", "M4"), 800.0),  # 200 -> untouched
    ]
    out = clamp_groups(watts, groups)
    assert out["M1"] + out["M2"] == pytest.approx(800.0)
    assert out["M3"] == 100.0
    assert out["M4"] == 100.0


def test_clamp_exactly_at_limit_not_scaled():
    watts = {"M1": 400.0, "M2": 400.0}  # sum exactly 800
    groups = [_group("WR1", ("M1", "M2"), 800.0)]
    out = clamp_groups(watts, groups)
    assert out["M1"] == 400.0
    assert out["M2"] == 400.0


def test_clamp_all_outputs_finite():
    watts = {"M1": 430.0, "M2": 430.0}
    groups = [_group("WR1", ("M1", "M2"), 800.0)]
    out = clamp_groups(watts, groups)
    assert all(math.isfinite(v) for v in out.values())


# --------------------------------------------------------------------------
# clamp_groups_ac (Phase 1 DC->AC transform)
# --------------------------------------------------------------------------


def _group_eff(
    name: str, planes: tuple[str, ...], limit: float, eff: float
) -> InverterGroup:
    return InverterGroup(
        name=name, plane_names=planes, ac_limit_w=limit, inverter_efficiency=eff
    )


def test_group_default_inverter_efficiency():
    # A group built without an explicit efficiency carries the class default.
    g = _group("WR1", ("M1",), 800.0)
    assert g.inverter_efficiency == DEFAULT_INVERTER_EFFICIENCY


def test_ac_no_clip_ac_is_eta_times_u_dc_unchanged():
    # eta * U below the AC limit -> no clip: DC passes through, AC = eta * U.
    eta = DEFAULT_INVERTER_EFFICIENCY
    watts = {"M1": 300.0, "M2": 250.0}  # U = 550, eta*U = 530.75 < 800
    groups = [_group_eff("WR1", ("M1", "M2"), 800.0, eta)]
    dc, ac = clamp_groups_ac(watts, groups)
    # DC unchanged (no clip).
    assert dc["M1"] == pytest.approx(300.0)
    assert dc["M2"] == pytest.approx(250.0)
    # AC = eta * DC per member; site AC = eta * U exactly.
    assert ac["M1"] == pytest.approx(300.0 * eta)
    assert ac["M2"] == pytest.approx(250.0 * eta)
    assert sum(ac.values()) == pytest.approx(550.0 * eta)


def test_ac_clip_ac_is_limit_dc_is_limit_over_eta():
    # eta * U above the AC limit -> AC clamps to the limit; DC clips at limit/eta.
    eta = DEFAULT_INVERTER_EFFICIENCY
    limit = 800.0
    watts = {"M7": 430.0, "M8": 430.0}  # U = 860, eta*U = 829.9 > 800
    groups = [_group_eff("WR4", ("M7", "M8"), limit, eta)]
    dc, ac = clamp_groups_ac(watts, groups)
    # AC pinned to the group limit.
    assert sum(ac.values()) == pytest.approx(limit)
    # Served DC pinned to the CORRECTED clip point limit/eta (above the AC limit).
    assert sum(dc.values()) == pytest.approx(limit / eta)
    assert limit / eta > limit  # the clip point really sits above the AC limit


def test_ac_proportional_split_sums_to_totals():
    # Unequal members under a clip: each keeps its U-share of both curves.
    eta = 0.95
    limit = 800.0
    watts = {"A": 600.0, "B": 300.0}  # U = 900, eta*U = 855 > 800
    groups = [_group_eff("G", ("A", "B"), limit, eta)]
    dc, ac = clamp_groups_ac(watts, groups)
    # Totals hit the clip targets.
    assert sum(ac.values()) == pytest.approx(limit)
    assert sum(dc.values()) == pytest.approx(limit / eta)
    # Shares preserved (A had 2/3, B had 1/3) on BOTH curves.
    assert ac["A"] == pytest.approx(limit * 2 / 3)
    assert ac["B"] == pytest.approx(limit * 1 / 3)
    assert dc["A"] == pytest.approx((limit / eta) * 2 / 3)
    assert dc["B"] == pytest.approx((limit / eta) * 1 / 3)
    # AC == eta * clamped DC, per member (the core invariant).
    assert ac["A"] == pytest.approx(eta * dc["A"])
    assert ac["B"] == pytest.approx(eta * dc["B"])


def test_ac_efficiency_clamped_above_max():
    # A group eta above the ceiling is clamped to INVERTER_EFFICIENCY_MAX.
    watts = {"M1": 500.0}  # below limit at any eta
    groups = [_group_eff("WR1", ("M1",), 800.0, 2.0)]
    _, ac = clamp_groups_ac(watts, groups)
    assert ac["M1"] == pytest.approx(500.0 * INVERTER_EFFICIENCY_MAX)


def test_ac_efficiency_clamped_below_min():
    # A group eta below the floor is clamped to INVERTER_EFFICIENCY_MIN.
    watts = {"M1": 500.0}
    groups = [_group_eff("WR1", ("M1",), 800.0, 0.1)]
    _, ac = clamp_groups_ac(watts, groups)
    assert ac["M1"] == pytest.approx(500.0 * INVERTER_EFFICIENCY_MIN)


def test_ac_ungrouped_plane_uses_default_efficiency():
    # A plane in NO group: DC unchanged, AC = DC * DEFAULT_INVERTER_EFFICIENCY.
    watts = {"M1": 500.0, "loose": 400.0}
    groups = [_group_eff("WR1", ("M1",), 800.0, 0.90)]
    dc, ac = clamp_groups_ac(watts, groups)
    assert dc["loose"] == pytest.approx(400.0)
    assert ac["loose"] == pytest.approx(400.0 * DEFAULT_INVERTER_EFFICIENCY)
    # The grouped plane still uses ITS group's eta (0.90), not the default.
    assert ac["M1"] == pytest.approx(500.0 * 0.90)


def test_ac_zero_total_group_passthrough_zero():
    # No power in the group's ports -> both curves zero for its members.
    watts = {"M1": 0.0, "M2": 0.0}
    groups = [_group_eff("WR1", ("M1", "M2"), 800.0, DEFAULT_INVERTER_EFFICIENCY)]
    dc, ac = clamp_groups_ac(watts, groups)
    assert dc["M1"] == 0.0 and dc["M2"] == 0.0
    assert ac["M1"] == 0.0 and ac["M2"] == 0.0


def test_ac_per_group_eta_override_differs_from_default():
    # Two below-limit groups, one at default eta and one overridden lower: the
    # overridden group delivers strictly less AC for the same DC.
    watts = {"H": 400.0, "L": 400.0}
    groups = [
        _group_eff("HI", ("H",), 800.0, DEFAULT_INVERTER_EFFICIENCY),
        _group_eff("LO", ("L",), 800.0, 0.90),
    ]
    _, ac = clamp_groups_ac(watts, groups)
    assert ac["H"] == pytest.approx(400.0 * DEFAULT_INVERTER_EFFICIENCY)
    assert ac["L"] == pytest.approx(400.0 * 0.90)
    assert ac["L"] < ac["H"]


def test_ac_does_not_mutate_input_mapping():
    watts = {"M7": 430.0, "M8": 430.0}
    snapshot = dict(watts)
    groups = [_group_eff("WR4", ("M7", "M8"), 800.0, DEFAULT_INVERTER_EFFICIENCY)]
    clamp_groups_ac(watts, groups)
    assert watts == snapshot


def test_ac_all_outputs_finite():
    watts = {"M1": 430.0, "M2": 430.0}
    groups = [_group_eff("WR1", ("M1", "M2"), 800.0, DEFAULT_INVERTER_EFFICIENCY)]
    dc, ac = clamp_groups_ac(watts, groups)
    assert all(math.isfinite(v) for v in dc.values())
    assert all(math.isfinite(v) for v in ac.values())
