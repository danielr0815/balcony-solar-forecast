"""Execute the shipped JavaScript under Node at its browser/HA boundaries.

The original power harness protects historical data-path regressions; the DOM
harness also instantiates real constructors and dispatches real control events.
Browser timezone varies independently from the HA site's timezone.
"""

from __future__ import annotations

import os
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("browser_zone", ["UTC", "America/New_York"])
def test_cards_runtime_dom_regressions(browser_zone):
    from balcony_solar_forecast import const

    harness = Path(__file__).parent / "harness" / "cards_regression_harness.mjs"
    proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [shutil.which("node"), str(harness)],
        env={**os.environ, "TZ": browser_zone,
             "BSF_SHADE_THRESHOLD": str(const.SHADE_PROFILE_TAU_THRESHOLD)},
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
