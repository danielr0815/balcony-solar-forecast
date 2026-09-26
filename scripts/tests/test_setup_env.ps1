# Standalone Windows smoke: no network, Python 3.14 or uv installation required.
$ErrorActionPreference = "Stop"
$wrapper = Join-Path $PSScriptRoot "../setup-env.ps1"
$pwsh = (Get-Command pwsh).Source
$originalPath = $env:PATH
$temp = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
New-Item -ItemType Directory $temp | Out-Null
try {
    $env:BSF_BOOTSTRAP_LOG = Join-Path $temp "calls.txt"
    # Launcher exists but the exact project interpreter deliberately does not.
    Set-Content (Join-Path $temp "py.cmd") '@echo %* >> "%BSF_BOOTSTRAP_LOG%"
@if "%1"=="-3.14" exit /b 91
@exit /b 0'
    $env:PATH = $temp
    & $pwsh -NoProfile -File $wrapper
    if ($LASTEXITCODE -ne 0) { throw "Existing-Python bootstrap failed" }
    if ((Get-Content $env:BSF_BOOTSTRAP_LOG).Trim() -ne "-3 scripts/setup_env.py") {
        throw "Bootstrap must use the installed Python instead of requiring 3.14"
    }
    Remove-Item $env:BSF_BOOTSTRAP_LOG
    # A failed installer must retain its exit code, not print a false success.
    Set-Content (Join-Path $temp "py.cmd") '@exit /b 37'
    & $pwsh -NoProfile -File $wrapper
    if ($LASTEXITCODE -ne 37) { throw "Bootstrap lost the installer exit code" }
    Remove-Item (Join-Path $temp "py.cmd")
    Set-Content (Join-Path $temp "python.cmd") '@echo %* >> "%BSF_BOOTSTRAP_LOG%"
@exit /b 0'
    & $pwsh -NoProfile -File $wrapper
    if ($LASTEXITCODE -ne 0) { throw "Python without py launcher failed" }
    if ((Get-Content $env:BSF_BOOTSTRAP_LOG).Trim() -ne "scripts/setup_env.py") {
        throw "Plain Python fallback did not invoke the bootstrap"
    }
    Remove-Item $env:BSF_BOOTSTRAP_LOG
    Remove-Item (Join-Path $temp "python.cmd")
    Set-Content (Join-Path $temp "uv.cmd") '@echo uv %* >> "%BSF_BOOTSTRAP_LOG%"
@exit /b 0'
    & $pwsh -NoProfile -File $wrapper
    if ($LASTEXITCODE -ne 0) { throw "Existing-uv bootstrap failed" }
    if ((Get-Content $env:BSF_BOOTSTRAP_LOG).Trim() -ne "uv sync --locked --group dev") {
        throw "Existing uv should not require a Python launcher"
    }
    Write-Output "Windows bootstrap smoke passed (py, python, uv and installer failure)."
} finally {
    $env:PATH = $originalPath
    Remove-Item Env:BSF_BOOTSTRAP_LOG -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $temp
}
