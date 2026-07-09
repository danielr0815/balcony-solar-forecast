#!/usr/bin/env python3
"""Cross-platform dev-environment bootstrap for balcony-solar-forecast.

Creates a local ``.venv`` and installs the ``[dependency-groups] dev`` tooling
from ``pyproject.toml`` — Home Assistant, pytest,
pytest-homeassistant-custom-component and ruff — the SAME setup as
battery-manager-ha. Home Assistant is unpinned; the matching HA version is
pinned transitively by ``pytest-homeassistant-custom-component``. The
integration itself has NO runtime dependencies (``requirements: []`` in the
manifest); these packages only run the tests + linter.

Pure standard-library, so it runs on a fresh machine (Linux / macOS / WSL /
Windows) before anything is installed. It is the single implementation behind
``make install`` and ``scripts/setup-env.{sh,ps1}``.

Usage::

    python scripts/setup_env.py [install|test|test-core|lint|format|clean]

``install`` (the default) creates the venv and installs the dev group. The
other subcommands run the corresponding tool from inside the venv so they work
identically on every OS (``make test`` / ``make lint`` delegate here).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"


def _venv_python() -> Path:
    """Path to the venv's Python interpreter (OS-specific layout)."""
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def _run(cmd: list[str]) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.check_call(cmd, cwd=str(ROOT))


def _ensure_venv() -> None:
    if _venv_python().exists():
        return
    print(f"Creating virtual environment in {VENV} ...", flush=True)
    venv.EnvBuilder(with_pip=True).create(str(VENV))


def install() -> None:
    """Create the venv (if needed) and install the dev dependency group."""
    _ensure_venv()
    py = str(_venv_python())
    _run([py, "-m", "pip", "install", "--upgrade", "pip"])
    # PEP 735 dependency group (pip >= 25.1). Fall back to explicit package
    # names on an older pip so a new machine still works after the pip upgrade
    # above (which normally lands pip >= 25.1) — belt and braces.
    try:
        _run([py, "-m", "pip", "install", "--group", "dev"])
    except subprocess.CalledProcessError:
        print("`pip install --group dev` failed; installing packages directly.")
        _run(
            [
                py, "-m", "pip", "install",
                "homeassistant",
                "pytest",
                "pytest-homeassistant-custom-component",
                "ruff",
            ]
        )
    activate = (
        r".venv\Scripts\activate" if os.name == "nt"
        else "source .venv/bin/activate"
    )
    print("\n[OK] Dev environment ready.", flush=True)
    print(f"     Activate:   {activate}")
    print("     Test:       make test        (or python scripts/setup_env.py test)")
    print("     Lint:       make lint        (or python scripts/setup_env.py lint)")


def _venv_run(args: list[str]) -> None:
    py = _venv_python()
    if not py.exists():
        sys.exit(
            "No .venv found. Run `make install` "
            "(or `python scripts/setup_env.py install`) first."
        )
    _run([str(py), *args])


def test() -> None:
    """Run the test suite.

    On POSIX (Linux/macOS/WSL) the full suite runs, including the Home Assistant
    layer. On Windows the HA test helpers cannot load (``homeassistant.runner``
    imports the POSIX-only ``fcntl``), so — exactly like battery-manager-ha — we
    run the pure-core suite natively and leave the HA layer to Linux/WSL/CI.
    """
    if os.name == "nt":
        print(
            "Windows detected: the Home Assistant test helpers "
            "(pytest-homeassistant-custom-component) cannot load here\n"
            "(homeassistant.runner imports the POSIX-only 'fcntl'). Running the "
            "pure-core suite;\nrun the full suite on Linux / WSL / CI.\n"
        )
        _venv_run(["-m", "pytest", "tests/core", "-p", "no:homeassistant"])
        return
    _venv_run(["-m", "pytest"])


def test_core() -> None:
    """Run only the pure-core tests (no Home Assistant import)."""
    _venv_run(["-m", "pytest", "tests/core", "-p", "no:homeassistant"])


def lint() -> None:
    _venv_run(["-m", "ruff", "check", "."])


def fmt() -> None:
    _venv_run(["-m", "ruff", "check", "--fix", "."])


def clean() -> None:
    """Remove the virtual environment."""
    if VENV.exists():
        print(f"Removing {VENV} ...", flush=True)
        shutil.rmtree(VENV, ignore_errors=True)


_COMMANDS = {
    "install": install,
    "test": test,
    "test-core": test_core,
    "lint": lint,
    "format": fmt,
    "clean": clean,
}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "install"
    handler = _COMMANDS.get(cmd)
    if handler is None:
        sys.exit(
            f"Unknown command {cmd!r}. "
            f"Choose from: {', '.join(_COMMANDS)}"
        )
    handler()


if __name__ == "__main__":
    main()
