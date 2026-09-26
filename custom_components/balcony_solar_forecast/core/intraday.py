"""Intraday reference and censored-label policy, independent of HA and I/O."""

from collections.abc import Mapping, Sequence

from .electrical import _clamp_eta
from .types import ForecastResult, InverterGroup

# Near the inverter ceiling the meter reports only a lower bound on available
# production. Allow 5% measurement/rounding headroom before treating it as censored.
CLIP_HEADROOM_FRACTION = 0.95


def reference_power(result: ForecastResult, index: int, theta: float) -> dict[str, float]:
    """Slow-only × frozen theta, before the final clip or transient correction."""
    return {
        plane.name: series[index] * theta
        for plane in result.plane_results
        if len(series := (plane.slow_watts or plane.raw_watts or plane.watts)) > index
    }


def censored_sample(reference: Mapping[str, float], groups: Sequence[InverterGroup],
                    measured_total: float, names: set[str], *,
                    measured: Mapping[str, float] | None = None) -> bool:
    """Whether clipping prevents an identifiable measured/reference ratio.

    Live channels allow a group-level verdict. Historical site totals do not
    identify which inverter clipped: only a single fully metered group can use
    its total directly; mixed/partial potentially clipped sites are omitted.
    An unsaturated deficit retains the PRE-clip reference so applying the
    resulting scalar actually reproduces the measurement.
    """
    for group in groups:
        members = set(group.plane_names)
        if not members.intersection(names):
            continue
        eta = _clamp_eta(group.inverter_efficiency)
        ceiling = group.ac_limit_w / eta
        modeled = sum(reference.get(name, 0.0) for name in members)
        if measured is not None and members.issubset(measured):
            if sum(measured[name] for name in members) >= ceiling * CLIP_HEADROOM_FRACTION:
                return True
        elif measured is None and members == names:
            if measured_total >= ceiling * CLIP_HEADROOM_FRACTION:
                return True
        elif modeled >= ceiling:
            return True
    return False
