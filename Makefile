# Balcony Solar Forecast — developer environment (thin wrapper around uv).
#
#   make install     create ./.venv from uv.lock and install the dev tooling
#                    (Home Assistant, pytest, pytest-cov,
#                    pytest-homeassistant-custom-component, ruff, mypy) —
#                    uv.lock is the single source of truth, also used by CI
#   make test        run the full test suite (HA layer included, PHACC plugin
#                    disabled — see CONTRIBUTING.md §4)
#   make test-core   run only the pure-core tests (no Home Assistant)
#   make lint        ruff check
#   make format      ruff check --fix   (lint autofix — `ruff format`, the
#                    formatter, is deliberately NOT used: the code is
#                    intentionally hand-formatted, see CONTRIBUTING.md §1)
#   make clean       remove the venv
#
# uv is the only bootstrap: it installs Python 3.14 itself (`.python-version`)
# and works identically on Linux, macOS, WSL and Windows. On a machine WITHOUT
# uv, run scripts/setup-env.sh (Linux/macOS/WSL) or scripts/setup-env.ps1
# (Windows) — they install uv first, then delegate to the same `uv sync`.

UV ?= uv

# The only OS-specific target left: removing the venv (cmd vs POSIX shell).
ifeq ($(OS),Windows_NT)
    RM = rmdir /s /q .venv
else
    RM = rm -rf .venv
endif

.PHONY: install test test-core lint format clean

install:
	$(UV) sync --group dev

test:
	$(UV) run pytest tests -p no:homeassistant

test-core:
	$(UV) run pytest tests/core -p no:homeassistant

lint:
	$(UV) run ruff check .

format:
	$(UV) run ruff check --fix .

clean:
	$(RM)
