#!/usr/bin/env python3
"""Cross-platform dev-environment bootstrap for balcony-solar-forecast.

Installs `uv` if it is missing, then runs ``uv sync --group dev`` — which
creates ``./.venv`` from ``uv.lock`` (the single source of truth, also used
by CI) with the dev tooling: Home Assistant, pytest, pytest-cov,
pytest-homeassistant-custom-component, ruff and mypy. The integration itself
has NO runtime dependencies (``requirements: []`` in the manifest); these
packages only run the tests + linter/typer.

Pure standard-library, so it runs on a fresh machine (Linux / macOS / WSL /
Windows) before anything is installed. It is the single implementation behind
``scripts/setup-env.{sh,ps1}``; with `uv` already on PATH, plain
``uv sync --group dev`` (or ``make install``) does the same thing directly.

Usage::

    python scripts/setup_env.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str]) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.check_call(cmd, cwd=str(ROOT))


def _find_uv() -> str | None:
    """Path to a `uv` executable — on PATH or in the per-user script dir."""
    uv = shutil.which("uv")
    if uv:
        return uv
    # `pip install --user uv` lands here; it is just not on PATH yet.
    candidates = (
        [Path.home() / ".local" / "bin" / "uv"]
        if os.name != "nt"
        else list(
            Path.home().glob(
                "AppData/Roaming/Python/Python*/Scripts/uv.exe"
            )
        )
    )
    for cand in candidates:
        if cand.exists():
            return str(cand)
    return None


def install_uv() -> str:
    """Install uv via the user-site pip, return the executable path."""
    print("uv not found — installing it with `pip install --user uv` ...",
          flush=True)
    _run([sys.executable, "-m", "pip", "install", "--user", "uv"])
    uv = _find_uv()
    if uv is None:
        sys.exit(
            "uv was installed but its executable is not findable. Add the "
            "pip user-script directory to PATH (see the warning above), then "
            "run `uv sync --group dev`."
        )
    return uv


def main() -> None:
    uv = _find_uv() or install_uv()
    _run([uv, "sync", "--group", "dev"])
    print("\n[OK] Dev environment ready.", flush=True)
    print("     Test:  make test   (or: uv run pytest tests -p no:homeassistant)")
    print("     Lint:  make lint   (or: uv run ruff check .)")


if __name__ == "__main__":
    main()
