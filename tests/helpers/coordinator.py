"""Coordinator learning scenarios with in-memory HA and Store doubles.

The narrow builder intentionally bypasses HA's update machinery for unit tests.
Real constructor/lifecycle coverage belongs to test_setup_path and integration/.
Keep persistence semantics aligned with ForecastStore; do not add production
fallbacks merely to accommodate a missing fake method.
"""

from __future__ import annotations

import copy
from datetime import datetime

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import State  # noqa: E402

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    CORRECTION_SOURCE_NONE,
    INTRADAY_NEUTRAL,
    LEARNER_SNAPSHOT_RING,
)
from custom_components.balcony_solar_forecast.coordinator import (  # noqa: E402
    BalconySolarCoordinator,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasState,
    DriftState,
    InverterCalState,
    LearnerConfig,
    PlaneConfig,
    ShademapState,
    SiteConfig,
)

DOMAIN = "balcony_solar_forecast"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeStates:
    def __init__(self) -> None:
        self._d: dict[str, State] = {}

    def set(self, entity_id: str, value, last_updated: datetime | None = None) -> None:
        self._d[entity_id] = State(entity_id, str(value), last_updated=last_updated)

    def get(self, entity_id: str) -> State | None:
        return self._d.get(entity_id)


class _FakeConfig:
    time_zone = "UTC"


class _FakeHass:
    def __init__(self) -> None:
        self.states = _FakeStates()
        self.config = _FakeConfig()

    async def async_add_executor_job(self, func, *args):
        """Run inline: the engine pass is pure CPU with no loop interaction,
        and the executor indirection must not change test-visible results."""
        return func(*args)


class _FakeStore:
    """In-memory stand-in for the (owner: store) v2 getters/setters."""

    def __init__(self) -> None:
        self.bias = BiasState().to_dict()
        self.shademap = ShademapState().to_dict()
        self.drift = DriftState().to_dict()
        self.snapshots: list[dict] = []
        self.issued: dict[str, dict] = {}
        self.actuals: dict[str, dict] = {}
        self.hourly_actuals: dict[str, dict[str, dict[str, float]]] = {}

    # v2 learner state
    def get_bias_state(self) -> BiasState:
        return BiasState.from_dict(self.bias)

    def set_bias_state(self, state) -> None:
        self.bias = state.to_dict()

    def get_shademap_state(self) -> ShademapState:
        return ShademapState.from_dict(self.shademap)

    def set_shademap_state(self, state) -> None:
        self.shademap = state.to_dict()

    def get_quantile_state(self):
        from custom_components.balcony_solar_forecast.core.types import QuantileState

        return QuantileState.from_dict(getattr(self, "quantile", {}))

    def set_quantile_state(self, state) -> None:
        self.quantile = state.to_dict()

    def get_scoreboard_state(self):
        from custom_components.balcony_solar_forecast.core.types import ScoreboardState

        return ScoreboardState.from_dict(getattr(self, "scoreboard", {}))

    def set_scoreboard_state(self, state) -> None:
        self.scoreboard = state.to_dict()

    def get_curve_audit(self) -> dict:
        return copy.deepcopy(getattr(self, "curve_audit", {}))

    def set_curve_audit(self, state: dict) -> None:
        self.curve_audit = copy.deepcopy(state)

    # learning-health bookkeeping (SPEC §10): plain validated dict, no dataclass
    def get_learning_health(self) -> dict:
        return dict(getattr(self, "learning_health", {}))

    def set_learning_health(self, health: dict) -> None:
        self.learning_health = dict(health)

    def get_drift_state(self) -> DriftState:
        return DriftState.from_dict(self.drift)

    def set_drift_state(self, state) -> None:
        self.drift = state.to_dict()

    # inverter DC->AC efficiency calibration (AC-side Phase 3)
    def get_inverter_cal_state(self) -> InverterCalState:
        return InverterCalState.from_dict(getattr(self, "inverter_cal", {}))

    def set_inverter_cal_state(self, state) -> None:
        self.inverter_cal = state.to_dict()

    # config fingerprint the day-ahead bias was learned against (A4)
    def get_config_fingerprint(self):
        return getattr(self, "config_fingerprint", None)

    def set_config_fingerprint(self, fp) -> None:
        self.config_fingerprint = fp

    # rollback ring (real ForecastStore API)
    def get_snapshots(self):
        from custom_components.balcony_solar_forecast.core.types import LearnerSnapshot

        return [LearnerSnapshot.from_dict(e) for e in self.snapshots]

    def push_snapshot(self, snapshot) -> None:
        self.snapshots.append(snapshot.to_dict())
        if len(self.snapshots) > LEARNER_SNAPSHOT_RING:
            del self.snapshots[: len(self.snapshots) - LEARNER_SNAPSHOT_RING]

    # v1 rings
    def get_issued(self, iso):
        return self.issued.get(iso)

    def record_issued(self, iso, snap):
        self.issued[iso] = snap

    def get_actuals(self, iso):
        return self.actuals.get(iso)

    def has_actuals(self, iso):
        return iso in self.actuals

    def record_actuals(self, iso, per_module):
        self.actuals[iso] = dict(per_module)

    def actuals_dates(self):
        return sorted(self.actuals)

    def get_last_payload(self):
        return None

    def get_hourly_actuals(self, iso):
        return self.hourly_actuals.get(iso)

    def record_hourly_actuals(self, iso, per_channel):
        self.hourly_actuals[iso] = {c: dict(h) for c, h in per_channel.items()}

    def is_inverter_day_trained(self, iso):
        return iso in getattr(self, "inverter_trained_days", set())

    def mark_inverter_day_trained(self, iso):
        if not hasattr(self, "inverter_trained_days"):
            self.inverter_trained_days = set()
        self.inverter_trained_days.add(iso)

    # trained-day idempotence markers (real ForecastStore API)
    def is_day_trained(self, iso):
        return iso in getattr(self, "trained_days", set())

    def mark_day_trained(self, iso):
        if not hasattr(self, "trained_days"):
            self.trained_days = set()
        self.trained_days.add(iso)


def _site() -> SiteConfig:
    return SiteConfig(
        latitude=48.5,
        longitude=12.2,
        planes=(
            PlaneConfig(name="M1", azimuth_deg=115.0, tilt_deg=70.0, wp=370.0,
                        actual_entity="sensor.m1"),
            PlaneConfig(name="M2", azimuth_deg=205.0, tilt_deg=70.0, wp=430.0,
                        actual_entity="sensor.m2"),
        ),
        groups=(),
    )


class _Entry:
    def __init__(self, data=None, options=None):
        self.entry_id = "e1"
        self.data = data or {}
        self.options = options or {}


def _make_coordinator(store: _FakeStore | None = None) -> BalconySolarCoordinator:
    """Build a bare coordinator with only the attributes the glue methods use."""
    c = BalconySolarCoordinator.__new__(BalconySolarCoordinator)
    from custom_components.balcony_solar_forecast._operations import LearnerOperations

    c._operations = LearnerOperations()
    c._bootstrap_lock = c._operations.lock
    c.hass = _FakeHass()
    c._store = store or _FakeStore()
    from custom_components.balcony_solar_forecast._weather_cache import WeatherCache

    c._weather = WeatherCache()
    c._weather.restore(c._store.get_last_payload())
    c._site = _site()
    c.entry = _Entry()
    c._learner_config = LearnerConfig()
    c._bias_state = BiasState()
    c._shademap_state = ShademapState()
    c._drift_state = DriftState()
    c._learner_states_loaded = True
    c._intraday_scalar = INTRADAY_NEUTRAL
    from collections import deque

    c._intraday_samples = deque()
    c._correction_source = CORRECTION_SOURCE_NONE
    c._last_result = None
    # Shade-profile diagram selection + memo (normally set in __init__).
    c._shade_profile_module = None
    c._shade_profile_date = None
    c._shade_profile_cache = None
    # v0.4 scoreboard attributes (_build_data now assembles the scoreboard
    # summary): neutral empty ring, defaults.
    from custom_components.balcony_solar_forecast.const import (
        DEFAULT_SCOREBOARD_WINDOW_DAYS,
    )
    from custom_components.balcony_solar_forecast.core.types import ScoreboardState

    c._scoreboard_enabled = True
    c._scoreboard_window_days = DEFAULT_SCOREBOARD_WINDOW_DAYS
    c._scoreboard_state = ScoreboardState()
    # v0.4 quantile lane: enabled by default, empty ring (cold start -> neutral).
    from custom_components.balcony_solar_forecast.core.types import QuantileState

    c._quantiles_enabled = True
    c._quantile_state = QuantileState()
    # AC-side Phase 3 inverter calibration: neutral (untrusted) by default.
    c._inverter_cal_state = InverterCalState()
    return c
