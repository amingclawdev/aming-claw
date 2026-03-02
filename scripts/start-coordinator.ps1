param(
    [switch]$Takeover
)

$ErrorActionPreference = "Stop"
$mutex = $null

try {
    $created = $false
    $mutex = New-Object System.Threading.Mutex($false, "Global\aming_claw_codex_coordinator", [ref]$created)
    if (-not $mutex.WaitOne(0)) {
        Write-Host "Coordinator mutex already held; another coordinator launcher is active. Exit."
        return
    }
}
catch {
    throw
}

Set-Location (Join-Path $PSScriptRoot "..")

function Get-CoordinatorPythonProcesses {
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $name = [string]$_.Name
        $cmd = [string]$_.CommandLine
        $name -match '^python(\.exe)?$' -and (
            $cmd -like "*agent\\coordinator.py*" -or
            $cmd -like "*agent/coordinator.py*"
        )
    }
}

function Stop-CoordinatorByLockPort {
    param(
        [int]$Port = 39102
    )
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($null -eq $listeners) { return }
    $pids = $listeners | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($pidVal in $pids) {
        Write-Host "Takeover: stopping lock-port owner PID=$pidVal ..."
        Stop-Process -Id $pidVal -Force -ErrorAction SilentlyContinue
        taskkill /F /T /PID $pidVal | Out-Null
    }
}

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

if (-not $env:TELEGRAM_BOT_TOKEN_CODEX -and -not $env:TELEGRAM_BOT_TOKEN) {
    throw "TELEGRAM_BOT_TOKEN_CODEX (preferred) or TELEGRAM_BOT_TOKEN is required."
}

# 自动探测 CLAUDE_BIN（若 .env 未设置）
if (-not $env:CLAUDE_BIN) {
    $claudeFound = Get-Command claude.cmd -ErrorAction SilentlyContinue
    if (-not $claudeFound) { $claudeFound = Get-Command claude -ErrorAction SilentlyContinue }
    if ($claudeFound) {
        $env:CLAUDE_BIN = $claudeFound.Source
        Write-Host "Auto-detected CLAUDE_BIN=$($env:CLAUDE_BIN)"
    }
}

# 使用内嵌 Python（优先）或系统 Python
$PYTHON = & (Join-Path $PSScriptRoot "_get_python.ps1")
Write-Host "Using Python: $PYTHON"

$depsReady = $false
try {
    & $PYTHON -c "import requests" 2>&1 | Out-Null
    $depsReady = ($LASTEXITCODE -eq 0)
} catch { $depsReady = $false }
if (-not $depsReady) {
    Write-Host "Installing agent dependencies..."
    & $PYTHON -m pip install -r .\agent\requirements.txt --no-warn-script-location
} else {
    Write-Host "agent dependencies already satisfied."
}

$existing = @(Get-CoordinatorPythonProcesses)
if ($Takeover) {
    $lockPort = 39102
    if ($env:COORDINATOR_SINGLETON_PORT -and ($env:COORDINATOR_SINGLETON_PORT -as [int])) {
        $lockPort = [int]$env:COORDINATOR_SINGLETON_PORT
    }
    Stop-CoordinatorByLockPort -Port $lockPort
    Start-Sleep -Milliseconds 500
}
if ($existing.Count -gt 0 -and -not $Takeover) {
    $pids = ($existing | Select-Object -ExpandProperty ProcessId) -join ", "
    Write-Host "Coordinator already running (PID=$pids). Skip starting duplicate instance."
    return
}
if ($existing.Count -gt 0 -and $Takeover) {
    $pids = ($existing | Select-Object -ExpandProperty ProcessId)
    foreach ($id in $pids) {
        Write-Host "Takeover: stopping existing coordinator PID=$id ..."
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        taskkill /F /T /PID $id | Out-Null
    }
}

if (-not $env:SHARED_VOLUME_PATH) {
    $env:SHARED_VOLUME_PATH = Join-Path (Get-Location).Path "shared-volume"
}
New-Item -ItemType Directory -Force -Path $env:SHARED_VOLUME_PATH | Out-Null

Write-Host "Starting agent coordinator..."
try {
    & $PYTHON .\agent\coordinator.py
}
finally {
    if ($mutex -ne $null) {
        $mutex.ReleaseMutex() | Out-Null
        $mutex.Dispose()
    }
}
