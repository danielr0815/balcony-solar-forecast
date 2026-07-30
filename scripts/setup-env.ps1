<#
    Bootstrap the balcony-solar-forecast dev environment on Windows (PowerShell).

    Installs uv if it is missing, then `uv sync --group dev` (creates .\.venv
    from uv.lock with the dev tooling: Home Assistant, pytest, pytest-cov,
    pytest-homeassistant-custom-component, ruff, mypy). Thin wrapper around
    scripts/setup_env.py — with uv already installed, plain
    `uv sync --group dev` (or `make install`) does the same thing.

    Usage:  .\scripts\setup-env.ps1

    Note: the full test suite runs everywhere (`make test` ==
    `uv run pytest tests -p no:homeassistant`); `-p no:homeassistant` is what
    keeps the HA test helpers off Windows — the PHACC plugin imports the
    POSIX-only 'fcntl'.
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.14 scripts/setup_env.py
} else {
    & python scripts/setup_env.py
}
exit $LASTEXITCODE
