<#
    Bootstrap the balcony-solar-forecast dev environment on Windows (PowerShell).

    Thin wrapper around scripts/setup_env.py — identical to `make install`.
    Creates .\.venv and installs the dev tooling (Home Assistant, pytest,
    pytest-homeassistant-custom-component, ruff) from pyproject.toml.

    Usage:  .\scripts\setup-env.ps1 [install|test|test-core|lint|format|clean]

    Note: the full test suite runs on Linux / WSL / CI; on Windows use
    `test-core` (the Home Assistant test helpers cannot load here — HA's runner
    imports the POSIX-only 'fcntl').
#>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$cmd = if ($args.Count -ge 1) { $args[0] } else { "install" }

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.13 scripts/setup_env.py $cmd
} else {
    & python scripts/setup_env.py $cmd
}
exit $LASTEXITCODE
