param(
    [Parameter(Mandatory = $true)][string]$OperatorChatId,
    [Parameter(Mandatory = $true)][string]$OperatorUserId,
    [string]$RequestId = "",
    [int]$CallerPid = 0,
    [int]$HealthWaitSeconds = 120,
    [switch]$NoHealthWait = $true,
    [switch]$HardRestart = $true,
    [switch]$BypassMutex = $true
)

$ErrorActionPreference = "Stop"

if (-not $RequestId) {
    $RequestId = "ops-" + [guid]::NewGuid().ToString("N").Substring(0, 12)
}

function Info($msg) {
    Write-Host "[ops-restart] $msg"
}

function Stop-CallerCoordinator {
    if ($CallerPid -le 0) { return }
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $CallerPid" -ErrorAction SilentlyContinue
        if ($null -eq $proc) { return }
        $cmd = [string]$proc.CommandLine
        if ($cmd -like "*agent\\coordinator.py*" -or $cmd -like "*agent/coordinator.py*") {
            Info "Stopping caller coordinator PID=$CallerPid ..."
            Stop-Process -Id $CallerPid -Force -ErrorAction SilentlyContinue
            taskkill /F /T /PID $CallerPid | Out-Null
        }
    }
    catch {
        Info "Stop caller coordinator failed: $($_.Exception.Message)"
    }
}

function Audit-Path {
    $root = if ($env:SHARED_VOLUME_PATH) { $env:SHARED_VOLUME_PATH } else { Join-Path (Get-Location).Path "shared-volume" }
    $dir = Join-Path $root "codex-tasks\\audit"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd"
    return Join-Path $dir ("ops-restart-" + $stamp + ".jsonl")
}

function Write-Audit($obj) {
    $line = $obj | ConvertTo-Json -Compress -Depth 8
    Add-Content -Path (Audit-Path) -Value $line -Encoding UTF8
}

function Stop-CodexExecutorIfRunning {
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -like "python*") -and ($_.CommandLine -like "*agent\\executor.py*")
    }
    foreach ($p in $procs) {
        Info "Stopping codex executor PID=$($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Start-CodexExecutor {
    Info "Starting codex executor..."
    Start-Process powershell -ArgumentList "-NoExit", "-File", ".\scripts\start-executor.ps1", "-Takeover" | Out-Null
}

Set-Location (Join-Path $PSScriptRoot "..")

$start = Get-Date
$rollbackAttempted = $false

Write-Audit @{
    ts = (Get-Date).ToString("o")
    event = "ops_restart_start"
    request_id = $RequestId
    operator_chat_id = $OperatorChatId
    operator_user_id = $OperatorUserId
}

try {
    Stop-CallerCoordinator

    # Fixed script only (no arbitrary command execution).
    & .\scripts\restart-all.ps1 -SkipChecks -HealthWaitSeconds $HealthWaitSeconds -NoHealthWait:$NoHealthWait -HardRestart:$HardRestart -BypassMutex:$BypassMutex | Out-Host

    $elapsed = [int]((Get-Date) - $start).TotalMilliseconds
    Write-Audit @{
        ts = (Get-Date).ToString("o")
        event = "ops_restart_done"
        request_id = $RequestId
        ok = $true
        elapsed_ms = $elapsed
        rollback_attempted = $rollbackAttempted
    }
    Info "done, elapsed=${elapsed}ms"
    exit 0
}
catch {
    $errText = $_.Exception.Message
    Info "restart failed: $errText"
    $rollbackAttempted = $true

    try {
        Info "running rollback/startup recovery..."
        Start-Process powershell -ArgumentList "-NoExit", "-File", ".\scripts\start-gateway.ps1" | Out-Null
        Start-Process powershell -ArgumentList "-NoExit", "-File", ".\scripts\start-coordinator.ps1", "-Takeover" | Out-Null
        Start-CodexExecutor
    }
    catch {
        Info "rollback failed: $($_.Exception.Message)"
    }

    $elapsed = [int]((Get-Date) - $start).TotalMilliseconds
    Write-Audit @{
        ts = (Get-Date).ToString("o")
        event = "ops_restart_done"
        request_id = $RequestId
        ok = $false
        elapsed_ms = $elapsed
        rollback_attempted = $rollbackAttempted
        error = $errText
    }
    throw
}
