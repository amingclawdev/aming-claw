#Requires -Version 5.1
Set-Location (Join-Path $PSScriptRoot "..")

# 加载 .env
$envPath = Join-Path (Get-Location) ".env"
Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }
    $pair = $_ -split '=', 2
    if ($pair.Length -eq 2) {
        [System.Environment]::SetEnvironmentVariable($pair[0].Trim(), $pair[1].Trim(), "Process")
    }
}

# 默认值
$root = (Get-Location).Path
if (-not $env:SHARED_VOLUME_PATH)     { $env:SHARED_VOLUME_PATH     = Join-Path $root "shared-volume" }
if (-not $env:EXECUTOR_BASE_URL)      { $env:EXECUTOR_BASE_URL      = "http://127.0.0.1:8090" }
if (-not $env:CODEX_WORKSPACE)        { $env:CODEX_WORKSPACE        = $root }
if (-not $env:WORKSPACE_PATH)         { $env:WORKSPACE_PATH         = $root }
if (-not $env:CODEX_DANGEROUS)        { $env:CODEX_DANGEROUS        = "1" }
if (-not $env:CODEX_TIMEOUT_SEC)      { $env:CODEX_TIMEOUT_SEC      = "900" }
if (-not $env:TASK_TIMEOUT_SEC)       { $env:TASK_TIMEOUT_SEC       = "1800" }
if (-not $env:EXECUTOR_HEARTBEAT_SEC) { $env:EXECUTOR_HEARTBEAT_SEC = "30" }

New-Item -ItemType Directory -Force -Path $env:SHARED_VOLUME_PATH | Out-Null

$scripts = Join-Path $root "scripts"

# gateway
Start-Process powershell -ArgumentList "-NoExit","-NoProfile","-File","$scripts\start-gateway.ps1" -WindowStyle Normal
Write-Host "[launch] gateway started"
Start-Sleep -Seconds 2

# manager
Start-Process powershell -ArgumentList "-NoExit","-NoProfile","-File","$scripts\start-manager.ps1","-Takeover" -WindowStyle Normal
Write-Host "[launch] manager started"
Start-Sleep -Seconds 1

# coordinator
Start-Process powershell -ArgumentList "-NoExit","-NoProfile","-File","$scripts\start-coordinator.ps1","-Takeover" -WindowStyle Normal
Write-Host "[launch] coordinator started"
Start-Sleep -Milliseconds 600

# executor
Start-Process powershell -ArgumentList "-NoExit","-NoProfile","-File","$scripts\start-executor.ps1","-Takeover" -WindowStyle Normal
Write-Host "[launch] executor started"

Write-Host ""
Write-Host "All 4 services launched in separate windows."
