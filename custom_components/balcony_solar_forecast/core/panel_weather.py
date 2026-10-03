"""Bounded, observational PV disturbance evidence; never a cloud truth label.

References must be slow-only × theta, excluding the intraday correction. The
caller owns geometry, source units and physical clipping information.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from statistics import median

from .measurement_quality import dc_power


@dataclass(frozen=True, slots=True)
class PanelObservation:
    source: str
    group: str
    orientation: str
    reported_at: datetime
    watts: float
    reference_watts: float
    clipped: bool = False
    eligible: bool = True
    azimuth_deg: float | None = None
    tilt_deg: float | None = None


@dataclass(frozen=True, slots=True)
class PanelFrame:
    end: datetime
    available_at: datetime
    generation: str
    panels: tuple[PanelObservation, ...]
    forecast_class: str = "unknown"


@dataclass(frozen=True, slots=True)
class WeatherEvidence:
    state: str
    reason: str
    eligible_panels: int = 0
    electrical_groups: int = 0
    orientations: int = 0
    residual_ratio: float | None = None
    common_ramp: float | None = None
    forecast_class: str = "unknown"

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def _normal(panel: PanelObservation) -> tuple[float, float, float] | None:
    az, tilt = panel.azimuth_deg, panel.tilt_deg
    if (isinstance(az, bool) or isinstance(tilt, bool)
            or not isinstance(az, (int,float)) or not isinstance(tilt, (int,float))
            or not math.isfinite(az) or not math.isfinite(tilt)
            or not 0 <= az <= 360 or not 0 <= tilt <= 90):
        return None
    a, b = math.radians(az), math.radians(tilt)
    return math.sin(b)*math.cos(a), math.sin(b)*math.sin(a), math.cos(b)


def orientation_cohorts(panels: list[PanelObservation]) -> dict[str, str]:
    """Complete-link cohorts of normals less than 30 degrees apart.

    Fixed azimuth/tilt rounding creates false diversity at bin edges. Sorting
    sources gives deterministic membership; every pair within a cohort must be
    close, so a chain of close neighbours cannot bridge far orientations.
    """
    groups: list[list[tuple[float,float,float]]] = []
    labels = {}
    threshold = math.cos(math.radians(30))
    for panel in sorted(panels, key=lambda p:p.source):
        normal = _normal(panel)
        if normal is None:
            continue
        index = next((i for i, group in enumerate(groups)
                      if all(sum(a*b for a,b in zip(normal, peer, strict=True)) > threshold+1e-12
                             for peer in group)), len(groups))
        if index == len(groups):
            groups.append([])
        groups[index].append(normal)
        labels[panel.source] = f"orientation_{index}"
    return labels


class PanelWeatherMonitor:
    """Minute frames, at most 31; a restart/model change needs fresh history."""
    def __init__(self) -> None:
        self._frames: deque[tuple[datetime, dict[str, float]]] = deque(maxlen=31)
        self._generation: str | None = None
        self._cohort: tuple | None = None

    def observe(self, frame: PanelFrame, *, now: datetime) -> WeatherEvidence:
        def unknown(reason: str) -> WeatherEvidence:
            return WeatherEvidence("unknown", reason, forecast_class=frame.forecast_class)
        if any(t.tzinfo is None for t in (frame.end, frame.available_at, now)):
            return unknown("invalid_time")
        if frame.end > frame.available_at or frame.available_at > now:
            return unknown("not_yet_available")
        if (now - frame.end).total_seconds() > 90:
            return unknown("stale_frame")
        if self._generation != frame.generation:
            self._generation = frame.generation
            self._frames.clear()
            self._cohort = None
        if self._frames and frame.end <= self._frames[-1][0]:
            return unknown("repeated_frame")
        seen: set[str] = set()
        usable: list[PanelObservation] = []
        for panel in frame.panels:
            if panel.source in seen:
                continue  # the same physical source is not independent evidence
            seen.add(panel.source)
            if panel.reported_at.tzinfo is None:
                continue
            age = (frame.end - panel.reported_at).total_seconds()
            if (not panel.eligible or panel.clipped or not 0 <= age <= 90
                    or dc_power(panel.watts) is None
                    or dc_power(panel.reference_watts) is None
                    or panel.reference_watts < 20):
                continue
            usable.append(panel)
        if usable and (max(p.reported_at for p in usable)
                       - min(p.reported_at for p in usable)).total_seconds() > 60:
            return unknown("incoherent_frame")
        if any(p.azimuth_deg is not None or p.tilt_deg is not None for p in usable):
            labels = orientation_cohorts(usable)
            usable = [replace(p, orientation=labels[p.source]) for p in usable if p.source in labels]
        signature = tuple(sorted((p.source,p.group,p.orientation,p.azimuth_deg,p.tilt_deg) for p in usable))
        if signature != self._cohort:
            self._frames.clear()
            self._cohort = signature
        groups = {p.group for p in usable}
        orientations = {p.orientation for p in usable}
        if len(usable) < 3 or len(groups) < 2 or len(orientations) < 2:
            return unknown("insufficient_diversity")
        # Equal weight to orientation cohorts within each electrical group,
        # then equal group weight: four eastern ports cannot dominate a site.
        cohorts: dict[tuple[str, str], list[float]] = {}
        for panel in usable:
            cohorts.setdefault((panel.group, panel.orientation), []).append(
                panel.watts / panel.reference_watts)
        ratios = {group: median([median(values) for (g, _), values in cohorts.items() if g == group])
                  for group in groups}
        previous = next((values for end, values in reversed(self._frames)
                         if 240 <= (frame.end - end).total_seconds() <= 360
                         and set(values) == groups), None)
        self._frames.append((frame.end, ratios))
        common_ramp: float | None = None
        state, reason = "steady", "no_common_change"
        if previous is not None and all(previous[g] > .1 for g in groups):
            ramps = [ratios[g] / previous[g] - 1 for g in groups]
            if all(r < -.1 for r in ramps) or all(r > .1 for r in ramps):
                common_ramp = median(ramps)
                state, reason = "common_change", "cloud_or_common_curtailment"
        elif previous is None:
            state, reason = "warming_up", "five_minute_reference_missing"
        residual = median(list(ratios.values()))
        if state == "steady" and all(r < .8 for r in ratios.values()):
            state, reason = "common_damping", "cloud_or_reference_error_or_curtailment"
        return WeatherEvidence(state, reason, len(usable), len(groups), len(orientations),
                               residual, common_ramp, frame.forecast_class)


def without_target(frame: PanelFrame, source: str, *, whole_group: bool = True) -> PanelFrame:
    """Leave-target/group-out input for offline learning-gate experiments."""
    group = next((p.group for p in frame.panels if p.source == source), None)
    return replace(frame, panels=tuple(p for p in frame.panels if p.source != source
                                       and not (whole_group and group is not None and p.group == group)))
