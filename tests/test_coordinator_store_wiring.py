"""Regression tests: the learner persist helpers must reach disk (CRITICAL-1).

The three v2-learner persist helpers (``_persist_bias_state`` /
``_persist_shademap_state`` / ``_persist_drift_state``) used to hand ``dict``
payloads into the real store setters, which call ``.to_dict()`` on their
argument — an ``AttributeError`` swallowed at DEBUG. Every disk copy of
bias/shademap/drift therefore stayed frozen at the last import/migration write
while live training was silently lost on each restart.

These tests drive the coordinator glue through the REAL ForecastStore (backed by
the HA-Store ``FakeStore`` from tests/test_store_v2.py) so a future regression
back to dicts fails loudly here.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

pytest.importorskip("homeassistant")

from custom_components.balcony_solar_forecast.const import (  # noqa: E402
    STORE_KEY_BIAS_STATE,
    STORE_KEY_DRIFT_STATE,
    STORE_KEY_SHADEMAP_STATE,
)
from custom_components.balcony_solar_forecast.core.types import (  # noqa: E402
    BiasCell,
    BiasState,
    DriftState,
    ShademapBin,
    ShademapState,
)
from custom_components.balcony_solar_forecast.store import ForecastStore  # noqa: E402

# Reuse the coordinator scaffold and the HA-Store backend fake from the two
# existing test modules (same import-reuse pattern as test_training_idempotence_rollback.py).
from tests.test_coordinator_learning import _make_coordinator  # noqa: E402
from tests.test_store_v2 import FakeStore  # noqa: E402


def _real_store() -> tuple[ForecastStore, FakeStore]:
    """A real ForecastStore over an in-memory HA-Store fake (records saves)."""
    backend = FakeStore(None)
    return ForecastStore(None, "e1", store=backend), backend  # type: ignore[arg-type]


def test_learner_persist_helpers_write_through_real_store(caplog):
    fs, backend = _real_store()
    c = _make_coordinator(store=fs)
    # Non-neutral in-memory state (any distinguishable values).
    c._bias_state = BiasState(cells={"clear|midday": BiasCell(theta=1.23, n=4)})
    c._shademap_state = ShademapState(
        channels={"M1": {"1:2:0": ShademapBin(tau=0.42, n=9)}}
    )
    c._drift_state = replace(
        DriftState(),
        fast_disabled=True,
        fast_loss_streak=3,
        fast_option_seen=True,
    )
    with caplog.at_level(logging.WARNING):
        c._persist_bias_state()
        c._persist_shademap_state()
        c._persist_drift_state()
    assert not [r for r in caplog.records if "Store setter" in r.getMessage()]
    # Read-back through the real store equals the trained in-memory state.
    assert fs.get_bias_state().to_dict() == c._bias_state.to_dict()
    assert fs.get_shademap_state().to_dict() == c._shademap_state.to_dict()
    assert fs.get_drift_state().fast_disabled is True
    assert fs.get_drift_state().fast_loss_streak == 3
    # And a delayed disk write was actually scheduled with the trained sections.
    snap = backend.pending_snapshot()
    assert snap[STORE_KEY_BIAS_STATE] == c._bias_state.to_dict()
    assert snap[STORE_KEY_SHADEMAP_STATE] == c._shademap_state.to_dict()
    assert snap[STORE_KEY_DRIFT_STATE] == c._drift_state.to_dict()


async def test_trained_state_survives_simulated_restart():
    fs1, backend1 = _real_store()
    c = _make_coordinator(store=fs1)
    c._drift_state = replace(DriftState(), fast_disabled=True)  # auto-disabled
    c._bias_state = BiasState(cells={"clear|midday": BiasCell(theta=1.23, n=4)})
    c._persist_drift_state()
    c._persist_bias_state()
    on_disk = backend1.pending_snapshot()  # what HA would flush
    # "restart": fresh store loads the flushed blob, fresh coordinator loads it.
    fs2 = ForecastStore(None, "e1", store=FakeStore(on_disk))  # type: ignore[arg-type]
    await fs2.async_load()
    c2 = _make_coordinator(store=fs2)
    c2._learner_states_loaded = False
    c2._load_learner_states()
    assert c2._drift_state.fast_disabled is True  # was: reverts to False
    assert c2._bias_state.cells["clear|midday"].theta == pytest.approx(1.23)


def test_call_store_setter_failure_logs_warning(caplog):
    c = _make_coordinator()

    class _Raising:
        def set_bias_state(self, state):
            raise RuntimeError("boom")

    c._store = _Raising()
    with caplog.at_level(logging.WARNING):
        c._call_store_setter("set_bias_state", c._bias_state)
    assert any(
        r.levelno >= logging.WARNING and "set_bias_state" in r.getMessage()
        for r in caplog.records
    )
