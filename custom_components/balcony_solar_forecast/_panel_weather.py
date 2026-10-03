"""Minute-sampled panel evidence, isolated from forecast and learner mutations."""
from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change

from .core import bias, solpos
from .core.measurement_quality import dc_power
from .core.panel_weather import (
    PanelFrame,
    PanelObservation,
    PanelWeatherMonitor,
    WeatherEvidence,
)


class PanelWeatherObserver:
    """Poll current states; last_reported also covers unchanged transport reports.

    Polling avoids an unfiltered state_reported subscription and prevents
    publishing every panel packet to Recorder. Only a compact diagnostic is
    exported, while the core owns the bounded minute-frame history.
    """
    def __init__(self, coord: Any) -> None:
        self._coord = coord
        self._monitor = PanelWeatherMonitor()
        self._unsub: Any = None
        self._evidence = WeatherEvidence("unknown", "not_started")
        self._sampled_at: str | None = None

    @callback
    def start(self) -> None:
        if self._unsub is None:
            self._unsub = async_track_time_change(self._coord.hass, self.sample, second=0)

    @callback
    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._monitor = PanelWeatherMonitor()
        self._evidence = WeatherEvidence("unknown", "stopped")

    def summary(self) -> dict[str, object]:
        return {**self._evidence.to_dict(), "sampled_at": self._sampled_at,
                "mode": "observational", "reference": "slow_only_times_theta"}

    @callback
    def sample(self, now: datetime) -> None:
        coord = self._coord
        self._sampled_at = now.isoformat()
        result = getattr(coord, "_last_result", None)
        if not getattr(coord, "last_update_success", False) or result is None:
            self._evidence = WeatherEvidence("unknown", "forecast_unavailable")
            return
        starts = result.slot_starts
        if not starts or now < starts[0] or now >= starts[-1] + timedelta(minutes=15):
            self._evidence = WeatherEvidence("unknown", "reference_unavailable")
            return
        # Interpolate midpoint power, removing predictable solar geometry ramps.
        centers = tuple(t + timedelta(minutes=7, seconds=30) for t in starts)
        right = bisect_right(centers, now)
        left = max(0, right - 1)
        right = min(right, len(starts) - 1)
        before = coord._intraday_reference(result, starts[left])
        after = coord._intraday_reference(result, starts[right])
        span = (centers[right] - centers[left]).total_seconds()
        weight = max(0.0, min(1.0, (now - centers[left]).total_seconds() / span)) if span else 0.0
        reference = {name: watts + weight * (after.get(name, watts) - watts)
                     for name, watts in before.items()}
        sun_az, sun_el = solpos.sun_position(now, coord._site.latitude, coord._site.longitude)
        group_by_name = {name: group for group in coord._site.groups for name in group.plane_names}
        readings: dict[str, float] = {}
        states: dict[str, Any] = {}
        for plane in coord._site.planes:
            if not plane.actual_entity:
                continue
            state = coord.hass.states.get(plane.actual_entity)
            if state is None:
                continue
            watts = dc_power(state.state, state.attributes.get("unit_of_measurement", "W"))
            if watts is None or watts > 2 * plane.wp:
                continue
            states[plane.name], readings[plane.name] = state, watts
        panels = []
        for plane in coord._site.planes:
            if plane.name not in states:
                continue
            group = group_by_name.get(plane.name)
            clipped = False
            if group is not None:
                measured = sum(readings.get(name, 0.0) for name in group.plane_names)
                modeled = sum(reference.get(name, 0.0) for name in group.plane_names)
                if group.inverter_efficiency <= 0 or group.ac_limit_w <= 0:
                    clipped = True
                else:
                    limit = group.ac_limit_w / group.inverter_efficiency
                    clipped = max(measured, modeled) >= .95 * limit
            beta, elevation = math.radians(plane.tilt_deg), math.radians(sun_el)
            incoming = (math.sin(beta) * math.cos(elevation) * math.cos(math.radians(sun_az-plane.azimuth_deg))
                        + math.cos(beta) * math.sin(elevation))
            state = states[plane.name]
            panels.append(PanelObservation(
                plane.actual_entity or plane.name, group.name if group else plane.name,
                "configured", state.last_reported,
                readings[plane.name], reference.get(plane.name, 0.0), clipped,
                group is not None and sun_el >= 10 and incoming >= .15,
                azimuth_deg=plane.azimuth_deg, tilt_deg=plane.tilt_deg,
            ))
        weather_class = "unknown"
        captured = getattr(coord, "_result_reference_context", None)
        weather = (captured[2] if captured is not None and captured[0] is result else
                   coord._cached_weather() if captured is None else None)
        if weather is not None:
            slot = next((s for s in weather.slots if s.start <= now < s.start + timedelta(minutes=15)), None)
            if slot is not None:
                weather_class = bias.classify_cloud(cloud_low=slot.cloud_low, cloud_mid=slot.cloud_mid,
                                                    cloud_high=slot.cloud_high,
                                                    visibility_m=slot.visibility_m, month=now.month,
                                                    ghi=slot.ghi, elevation_deg=sun_el)
        self._evidence = self._monitor.observe(
            PanelFrame(now, now, str(id(result)), tuple(panels), weather_class), now=now)
