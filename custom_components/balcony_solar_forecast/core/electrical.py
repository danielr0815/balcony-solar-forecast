"""DC power model + per-inverter-group AC clamp (stdlib math only).

Owner: engine. Pure, HA-free. Implements SPEC §4 step 7:
  - Ross cell temperature: Tcell = Tamb + ROSS_COEFF * POA.
  - Power derate TEMP_COEFF_PER_K per K vs TEMP_REF_C.
  - Per-inverter-group AC clamp: min(sum of member ports, ac_limit_w).
  - Per-inverter-group DC->AC transform (clamp_groups_ac): served DC clipped at
    ac_limit_w / eta_inv and delivered AC = min(eta_inv * sum(DC), ac_limit_w).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..const import (
    DEFAULT_INVERTER_EFFICIENCY,
    INVERTER_EFFICIENCY_MAX,
    INVERTER_EFFICIENCY_MIN,
    ROSS_COEFF,
    TEMP_COEFF_PER_K,
    TEMP_REF_C,
)
from .types import InverterGroup

__all__ = ["dc_power", "clamp_groups", "clamp_groups_ac"]


def dc_power(
    poa_w_m2: float,
    wp: float,
    temp_amb: float,
    efficiency: float,
    ross_coeff: float | None = None,
) -> float:
    """DC power (W) for one plane in one slot.

    Ross cell temperature Tcell = temp_amb + k * poa_w_m2, where k is
    ``ross_coeff`` when given else the default ROSS_COEFF; output is derated by
    TEMP_COEFF_PER_K per K above TEMP_REF_C. ``wp`` is scaled by POA / 1000
    (STC reference irradiance) and by ``efficiency``.

    Args:
        poa_w_m2: plane-of-array irradiance for the plane, W/m^2.
        wp: module STC peak power, W.
        temp_amb: ambient air temperature, deg C.
        efficiency: system/DC efficiency (0..1).
        ross_coeff: per-plane Ross cell-temperature coefficient (mounting-
            dependent); None uses the module-wide default ROSS_COEFF.

    Returns:
        DC power in watts (>= 0).
    """
    # No irradiance -> no power (also guards against tiny negative POA that a
    # transposition rounding artefact could produce at dusk).
    if poa_w_m2 <= 0.0:
        return 0.0

    # Ross NOCT-style cell temperature: linear in POA (SPEC §4). The mounting
    # geometry sets the coefficient; fall back to the default when unset.
    k_ross = ross_coeff if ross_coeff is not None else ROSS_COEFF
    t_cell = temp_amb + k_ross * poa_w_m2

    # Temperature derate relative to the 25 C STC reference. Above 25 C this
    # loses power (coeff is negative); below 25 C it is a small gain, which is
    # physically real for silicon and kept (no artificial ceiling at Wp here —
    # the AC clamp is the hardware limit that bounds delivered power).
    temp_factor = 1.0 + TEMP_COEFF_PER_K * (t_cell - TEMP_REF_C)
    if temp_factor < 0.0:
        # Absurdly hot cell: never yield negative power.
        temp_factor = 0.0

    # STC scaling: Wp is defined at 1000 W/m^2. Linear irradiance response.
    power = wp * (poa_w_m2 / 1000.0) * temp_factor * efficiency

    if power < 0.0:
        return 0.0
    return power


def clamp_groups(
    plane_watts: Mapping[str, float],
    groups: Sequence[InverterGroup],
) -> dict[str, float]:
    """Apply each inverter group's AC clamp to its member planes.

    For every group the summed member power is limited to ``ac_limit_w``;
    when clamping bites, the reduction is distributed proportionally back
    onto the member planes so per-plane outputs stay consistent with the
    clamped total.

    Args:
        plane_watts: {plane_name: unclamped DC power W}.
        groups: inverter groups defining the clamps.

    Returns:
        {plane_name: clamped power W}. Planes not in any group pass through
        unchanged.
    """
    # Start from a mutable copy so pass-through planes are preserved verbatim.
    out: dict[str, float] = dict(plane_watts)

    for group in groups:
        members = group.plane_names
        if not members:
            continue

        total = 0.0
        present: list[str] = []
        for name in members:
            if name in out:
                total += out[name]
                present.append(name)

        if not present:
            continue

        limit = group.ac_limit_w
        if total <= limit or total <= 0.0:
            # Within the AC limit (or no power) -> nothing to redistribute.
            continue

        # Clamp bites: scale every member down by the same factor so the sum
        # equals the limit and per-plane shares stay proportional.
        scale = limit / total
        for name in present:
            out[name] = out[name] * scale

    return out


def _clamp_eta(eta: float) -> float:
    """Clamp a group's DC->AC efficiency into [MIN, MAX]; garbage -> default.

    Defensive: a directly-constructed InverterGroup can carry any value (only
    ``from_dict`` clamps on load), so the physical transform floors/caps it here
    too. NaN / non-numeric degrade to ``DEFAULT_INVERTER_EFFICIENCY`` rather than
    poisoning the whole slot.
    """
    try:
        e = float(eta)
    except (TypeError, ValueError):
        return DEFAULT_INVERTER_EFFICIENCY
    if e != e:  # NaN guard
        return DEFAULT_INVERTER_EFFICIENCY
    if e < INVERTER_EFFICIENCY_MIN:
        return INVERTER_EFFICIENCY_MIN
    if e > INVERTER_EFFICIENCY_MAX:
        return INVERTER_EFFICIENCY_MAX
    return e


def clamp_groups_ac(
    plane_watts: Mapping[str, float],
    groups: Sequence[InverterGroup],
    *,
    ungrouped_eta: float = DEFAULT_INVERTER_EFFICIENCY,
) -> tuple[dict[str, float], dict[str, float]]:
    """Per-inverter-group DC->AC transform: served DC + delivered AC per plane.

    The deterministic physical transform layered on top of the DC model. Per
    group, with eta = ``inverter_efficiency`` clamped to [INVERTER_EFFICIENCY_MIN,
    INVERTER_EFFICIENCY_MAX]:

      * ``U`` = summed unclamped member DC;
      * ``AC_group = min(eta * U, ac_limit_w)`` — the micro-inverter's AC clamp;
      * ``D = ac_limit_w / eta`` — the DC clip point, HIGHER than ``ac_limit_w``:
        this is where the ports really clip, because the micro-inverter clips its
        AC output and back-drives the MPP off its maximum, so the DC that can be
        served rises to ``ac_limit_w / eta`` before the ceiling bites;
      * ``dc_clamped_group = min(U, D)``, and by construction
        ``AC_group == eta * dc_clamped_group`` in both regimes.

    BOTH results are redistributed proportionally back onto the members (the same
    proportional-scale idiom as :func:`clamp_groups`): each member keeps its share
    ``U_name / U`` of the clamped group DC and of the group AC. A group whose
    members sum to zero (night / dark slot) passes through as 0.0 for both.

    Planes in NO inverter group have no configured inverter or AC limit, so there
    is no group clip to apply. DECISION (documented): their AC is still modeled as
    ``DC * DEFAULT_INVERTER_EFFICIENCY`` — physically a plane always feeds *some*
    micro-inverter even when the operator did not group it, so a flat default
    conversion is more faithful than pretending AC == DC. Their DC passes through
    unchanged. (The reference site groups every plane, so this is an edge case.)

    NOTE: this is ADDITIONAL to :func:`clamp_groups`, which is left intact — the
    DC learning / scoreboard path depends on its exact ``min(sum, ac_limit)``
    clip at ``ac_limit_w`` (not at the corrected ``ac_limit_w / eta``).

    Args:
        plane_watts: {plane_name: unclamped DC power W}.
        groups: inverter groups defining the AC clamp + efficiency.

    Returns:
        ``(dc_clamped_by_plane, ac_by_plane)``: the served DC per plane (clipped
        at ``ac_limit_w / eta`` for grouped planes, unchanged for ungrouped) and
        the delivered AC per plane.
    """
    # DC baseline: pass every plane's DC through; grouped members are overwritten
    # below with their clip-point-corrected DC. AC baseline: ungrouped planes get
    # the flat ``ungrouped_eta`` conversion (the datasheet default, or the trusted
    # site-level LEARNED eta_inv when the caller injects it — the engine passes
    # the SAME eta it weights the pre-clamp AC by, so the served and pre-clamp AC
    # of an ungrouped plane stay mutually consistent); grouped members are
    # overwritten with their per-group eta * clamped DC.
    eta_ungrouped = _clamp_eta(ungrouped_eta)
    dc_out: dict[str, float] = dict(plane_watts)
    ac_out: dict[str, float] = {
        name: watts * eta_ungrouped
        for name, watts in plane_watts.items()
    }

    for group in groups:
        members = group.plane_names
        if not members:
            continue

        total = 0.0
        present: list[str] = []
        for name in members:
            if name in dc_out:
                total += dc_out[name]
                present.append(name)

        if not present:
            continue

        eta = _clamp_eta(group.inverter_efficiency)

        if total <= 0.0:
            # No power in this group's ports -> passthrough zero (both curves).
            for name in present:
                dc_out[name] = 0.0
                ac_out[name] = 0.0
            continue

        # Corrected DC clip point sits at ac_limit / eta (above the AC limit),
        # where the inverter's AC clamp back-drives the MPP off its maximum.
        clip_point = group.ac_limit_w / eta
        dc_group = total if total <= clip_point else clip_point
        scale_dc = dc_group / total
        scale_ac = eta * scale_dc  # AC = eta * clamped DC, split proportionally
        for name in present:
            base = dc_out[name]
            dc_out[name] = base * scale_dc
            ac_out[name] = base * scale_ac

    return dc_out, ac_out
