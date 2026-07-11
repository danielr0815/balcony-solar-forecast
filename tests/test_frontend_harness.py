"""Runtime harness for the bundled power-history card (Node, environment-gated).

``tests/harness/power_card_harness.mjs`` loads the REAL shipped card JS under
minimal ``customElements``/``HTMLElement`` stubs and drives the actual fetch
paths against a stubbed ``hass.callWS`` — the runtime companion to the static
greps in ``test_frontend_resource.py``, which cannot catch async/state-machine
breakage (the v0.15 property-shadowing bug was exactly such a runtime-only
failure). The harness asserts three scenarios:

  1. the WEEK view issues ONE daily-statistics query plus CONCURRENT
     ``get_issued_forecast`` lookups for the non-today days, builds per-day
     totals with an honest GAP for a day without an archived snapshot, uses
     the LIVE ``wh_period`` sum for today, and serves a repeat fetch of the
     same window from the per-window cache;
  2. DAY navigation clears the previous line synchronously and a date with
     ``available: false`` lands in the "missing" (NOT "error") state, with
     ``oldest_available`` captured for the archive-since note;
  3. a THROWING service call lands in "error" with the message remembered.

Environment-gated exactly like the suite's other optional-dependency skips:
runs only when ``node`` is on PATH (GitHub runners ship it) and SKIPS
otherwise, so the pure-Python paths stay runnable everywhere.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_HARNESS = Path(__file__).parent / "harness" / "power_card_harness.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_power_card_runtime_harness():
    """The Node harness exits 0 (all scenarios asserted inside the script)."""
    proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [shutil.which("node"), str(_HARNESS)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"power-card harness failed (exit {proc.returncode})\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
