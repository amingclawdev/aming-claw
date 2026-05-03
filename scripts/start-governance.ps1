param(
    [switch]$Takeover,
    [int]$Port = 40000,
    [int]$HealthWaitSeconds = 30
)

$ErrorActionPreference = "Stop"
$mutex = $null

try {
    $created = $false
    $mutex = New-Object System.Threading.Mutex($false, "Global\aming_claw_governance_host", [ref]$created)
    if (-not $mutex.WaitOne(0)) {
        Write-Host "Governance mutex already held; another governance launcher is active. Exit."
        return
    }
}
catch {
    throw
}

Set-Location (Join-Path $PSScriptRoot "..")

function Get-GovernancePythonProcesses {
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $name = [string]$_.Name
        $cmd  = [string]$_.CommandLine
        $name -match '^python.*(\.exe)?$' -and (
            $cmd -like "*start_governance.py*" -or
            $cmd -like "*agent.governance.server*" -or
            $cmd -like "*agent\\governance\\server.py*" -or
            $cmd -like "*agent/governance/server.py*"
        )
    }
}

function Stop-GovernanceProcessTree {
    param([int]$TargetPid)
    try {
        Start-Process -FilePath "taskkill.exe" `
            -ArgumentList @("/F", "/T", "/PID", "$TargetPid") `
            -WindowStyle Hidden `
            -Wait `
            -PassThru `
            -ErrorAction SilentlyContinue | Out-Null
    }
    catch {
    }
    Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue
}

function Stop-GovernanceByPort {
    param([int]$ListenPort)
    $listeners = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue
    if ($null -eq $listeners) { return }
    $pids = $listeners | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($pidVal in $pids) {
        Write-Host "Takeover: stopping governance port owner PID=$pidVal ..."
        Stop-GovernanceProcessTree -TargetPid $pidVal
    }
}

function Wait-GovernanceHealthy {
    param(
        [int]$WaitSeconds,
        [int]$ListenPort
    )
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    $healthUrl = "http://localhost:$ListenPort/api/health"
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-RestMethod $healthUrl -TimeoutSec 3
            if ($resp.status -eq "ok") {
                return $resp
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds 750
    }
    throw "Governance did not become healthy on $healthUrl within $WaitSeconds seconds."
}

if (-not (Test-Path ".\\.env")) {
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

$PYTHON = & (Join-Path $PSScriptRoot "_get_python.ps1")
Write-Host "Using Python: $PYTHON"

$depsReady = $false
try {
    & $PYTHON -c "import requests, yaml, networkx, redis" 2>&1 | Out-Null
    $depsReady = ($LASTEXITCODE -eq 0)
} catch { $depsReady = $false }
if (-not $depsReady) {
    Write-Host "Installing governance dependencies..."
    & $PYTHON -m pip install -r .\agent\requirements.txt --no-warn-script-location
} else {
    Write-Host "governance dependencies already satisfied."
}

$existing = @(Get-GovernancePythonProcesses)
if ($Takeover) {
    Stop-GovernanceByPort -ListenPort $Port
    Start-Sleep -Milliseconds 500
}
if ($existing.Count -gt 0 -and -not $Takeover) {
    $pids = ($existing | Select-Object -ExpandProperty ProcessId) -join ", "
    Write-Host "Governance already running (PID=$pids). Skip starting duplicate instance."
    return
}
if ($existing.Count -gt 0 -and $Takeover) {
    foreach ($procId in ($existing | Select-Object -ExpandProperty ProcessId -Unique)) {
        Write-Host "Takeover: stopping existing governance PID=$procId ..."
        Stop-GovernanceProcessTree -TargetPid $procId
    }
}

$env:GOVERNANCE_PORT = "$Port"
if (-not $env:DBSERVICE_URL) {
    $env:DBSERVICE_URL = "http://localhost:40002"
}
if (-not $env:REDIS_URL) {
    $env:REDIS_URL = "redis://localhost:40079/0"
}
if (-not $env:MEMORY_BACKEND) {
    $env:MEMORY_BACKEND = "docker"
}
if (-not $env:SHARED_VOLUME_PATH) {
    $env:SHARED_VOLUME_PATH = Join-Path (Get-Location).Path "shared-volume"
}
if (-not $env:CODEX_WORKSPACE) {
    $env:CODEX_WORKSPACE = (Get-Location).Path
}
New-Item -ItemType Directory -Force -Path $env:SHARED_VOLUME_PATH | Out-Null
$logDir = Join-Path (Join-Path $env:SHARED_VOLUME_PATH "codex-tasks") "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stdoutLog = Join-Path $logDir "governance-$Port-$logStamp.out.log"
$stderrLog = Join-Path $logDir "governance-$Port-$logStamp.err.log"
$env:GOVERNANCE_STDOUT_LOG = $stdoutLog
$env:GOVERNANCE_STDERR_LOG = $stderrLog

Write-Host "Starting host governance..."
Write-Host "  port:      $($env:GOVERNANCE_PORT)"
Write-Host "  dbservice: $($env:DBSERVICE_URL)"
Write-Host "  redis:     $($env:REDIS_URL)"
Write-Host "  shared:    $($env:SHARED_VOLUME_PATH)"
Write-Host "  stdout:    $stdoutLog"
Write-Host "  stderr:    $stderrLog"

try {
    $proc = Start-Process -FilePath $PYTHON `
        -ArgumentList @(".\start_governance.py") `
        -WorkingDirectory (Get-Location).Path `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru
    $health = Wait-GovernanceHealthy -WaitSeconds $HealthWaitSeconds -ListenPort $Port
    Write-Host "Governance healthy."
    Write-Host "  pid:       $($proc.Id)"
    Write-Host "  health:    http://localhost:$Port/api/health"
    $health | ConvertTo-Json -Depth 6
}
finally {
    if ($mutex -ne $null) {
        $mutex.ReleaseMutex() | Out-Null
        $mutex.Dispose()
    }
}
