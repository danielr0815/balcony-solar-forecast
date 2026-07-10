# Balcony Solar Forecast — developer environment.
#
#   make install     create ./.venv and install the dev tooling (Home Assistant,
#                     pytest, pytest-homeassistant-custom-component, ruff) from
#                     the [dependency-groups] dev in pyproject.toml — identical
#                     to battery-manager-ha. HA is unpinned; the matching version
#                     is pinned by pytest-homeassistant-custom-component.
#   make test        run the full test suite (HA layer included)
#   make test-core   run only the pure-core tests (no Home Assistant)
#   make lint        ruff check
#   make format      ruff check --fix
#   make clean       remove the venv
#
# Every target delegates to scripts/setup_env.py (pure stdlib) so it behaves
# identically on Linux, macOS, WSL and Windows. On a machine WITHOUT make, run
# scripts/setup-env.sh (Linux/macOS/WSL) or scripts/setup-env.ps1 (Windows) —
# they call the same script.

# Bootstrap interpreter (only used to create the venv). Windows -> the py
# launcher pinned to 3.13; POSIX -> python3. Override with `make PY=... install`.
ifeq ($(OS),Windows_NT)
    PY ?= py -3.13
else
    PY ?= python3
endif

.PHONY: install test test-core lint format clean

install:
	$(PY) scripts/setup_env.py install

test:
	$(PY) scripts/setup_env.py test

test-core:
	$(PY) scripts/setup_env.py test-core

lint:
	$(PY) scripts/setup_env.py lint

format:
	$(PY) scripts/setup_env.py format

clean:
	$(PY) scripts/setup_env.py clean
