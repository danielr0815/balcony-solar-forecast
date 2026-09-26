<# Bootstrap with an existing uv or Python; uv installs the project interpreter. #>
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (Get-Command uv -ErrorAction SilentlyContinue) {
    & uv sync --locked --group dev
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    # The bootstrap is stdlib-only and does not require the target Python 3.14.
    & py -3 scripts/setup_env.py
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python scripts/setup_env.py
} else {
    Write-Error "Install uv or Python 3.10+ before running setup-env.ps1."
    exit 1
}
exit $LASTEXITCODE
