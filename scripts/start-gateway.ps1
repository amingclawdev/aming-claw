$ErrorActionPreference = "Stop"

if (-not (Test-Path ".\.env")) {
    throw ".env not found. Create it from .env.example first."
}

Write-Host "Loading .env into current shell..."
Get-Content .\.env | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }
    $pair = $_ -split '=', 2
    if ($pair.Length -eq 2) {
        [System.Environment]::SetEnvironmentVariable($pair[0], $pair[1], "Process")
    }
}

if (-not $env:WORKSPACE_PATH -or $env:WORKSPACE_PATH -eq "/workspace") {
    $env:WORKSPACE_PATH = (Get-Location).Path
    Write-Host "WORKSPACE_PATH not suitable for host mode, using: $env:WORKSPACE_PATH"
}

if (-not $env:SHARED_VOLUME_PATH) {
    $env:SHARED_VOLUME_PATH = Join-Path (Get-Location).Path "shared-volume"
}
New-Item -ItemType Directory -Force -Path $env:SHARED_VOLUME_PATH | Out-Null
Write-Host "SHARED_VOLUME_PATH=$($env:SHARED_VOLUME_PATH)"

# 使用内嵌 Python（优先）或系统 Python
$PYTHON = & (Join-Path $PSScriptRoot "_get_python.ps1")
Write-Host "Using Python: $PYTHON"

Write-Host "Starting executor-gateway on host (Windows screenshot capable)..."
Push-Location .\executor-gateway
try {
    $depsReady = $false
    try {
        & $PYTHON -c "import fastapi,uvicorn,requests" 2>&1 | Out-Null
        $depsReady = ($LASTEXITCODE -eq 0)
    } catch { $depsReady = $false }
    if (-not $depsReady) {
        Write-Host "Installing executor dependencies..."
        & $PYTHON -m pip install -r requirements.txt --no-warn-script-location
    } else {
        Write-Host "Executor dependencies already satisfied."
    }
    & $PYTHON -m uvicorn app.main:app --host 0.0.0.0 --port 8090
}
finally {
    Pop-Location
}
