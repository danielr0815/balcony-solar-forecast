#!/usr/bin/env bash
# Bootstrap the balcony-solar-forecast dev environment on Linux / macOS / WSL.
#
# Thin wrapper around scripts/setup_env.py — identical to `make install`. Creates
# ./.venv and installs the dev tooling (Home Assistant, pytest,
# pytest-homeassistant-custom-component, ruff) from pyproject.toml.
#
# Usage:  ./scripts/setup-env.sh [install|test|test-core|lint|format|clean]
# Override the interpreter with:  PYTHON=python3.13 ./scripts/setup-env.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
exec "$PYTHON" scripts/setup_env.py "${1:-install}"
