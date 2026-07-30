#!/usr/bin/env bash
# Bootstrap the balcony-solar-forecast dev environment on Linux / macOS / WSL.
#
# Installs uv if it is missing, then `uv sync --group dev` (creates ./.venv
# from uv.lock with the dev tooling: Home Assistant, pytest, pytest-cov,
# pytest-homeassistant-custom-component, ruff, mypy). Thin wrapper around
# scripts/setup_env.py — with uv already installed, plain `uv sync --group dev`
# (or `make install`) does the same thing.
#
# Usage:  ./scripts/setup-env.sh
# Override the interpreter with:  PYTHON=python3.14 ./scripts/setup-env.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
exec "$PYTHON" scripts/setup_env.py
