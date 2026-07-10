"""DC power model + per-inverter-group AC clamp (stdlib math only).

Owner: engine. Pure, HA-free. Implements SPEC §4 step 7:
  - Ross cell temperature: Tcell = Tamb + ROSS_COEFF * POA.
  - Power derate TEMP_COEFF_PER_K per K vs TEMP_REF_C.
  - Per-inverter-group AC clamp: min(sum of member ports, ac_limit_w).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..const import ROSS_COEFF, TEMP_COEFF_PER_K, TEMP_REF_C
from .types import InverterGroup

__all__ = ["dc_power", "clamp_groups"]


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
