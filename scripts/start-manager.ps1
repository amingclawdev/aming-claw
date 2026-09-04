param(
    [switch]$Takeover,
    # SM and MCP are independent. Takeover restarts manager/executor only by default;
    # pass -StopMcp for an explicit full host cleanup.
    [switch]$StopMcp,
    [string]$Project = "aming-claw",
    # Windows cold-start includes sidecar/aiohttp initialization; executor startup and
    # per-project backfill are optional/degraded evidence and do not gate manager health.
    [int]$HealthWaitSeconds = 90
)

$ErrorActionPreference = "Stop"
$mutex = $null
$mutexAcquired = $false

Set-Location (Join-Path $PSScriptRoot "..")

function Get-ManagerPythonProcesses {
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $name = [string]$_.Name
        $cmd  = [string]$_.CommandLine
        $name -match '^python.*(\.exe)?$' -and (
            $cmd -like "*agent\service_manager.py*" -or
            $cmd -like "*agent/service_manager.py*" -or
            $cmd -like "*-m agent.service_manager*"
        )
    }
}

function Get-ExecutorWorkerProcesses {
    param([string]$ProjectId = $Project)
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $name = [string]$_.Name
        $cmd  = [string]$_.CommandLine
        $name -match '^python.*(\.exe)?$' -and (
            $cmd -like "*agent.executor_worker*" -or
            $cmd -like "*agent\executor_worker.py*" -or
            $cmd -like "*agent/executor_worker.py*"
        ) -and $cmd -match "(?:--project|-Project)\s+$([regex]::Escape($ProjectId))(?:\s|$)"
    }
}

function Get-McpServerProcesses {
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $name = [string]$_.Name
        $cmd  = [string]$_.CommandLine
        $name -match '^python.*(\.exe)?$' -and (
            $cmd -like "*agent.mcp.server*" -or
            $cmd -like "*agent\mcp\server.py*" -or
            $cmd -like "*agent/mcp/server.py*"
        )
    }
}

function Stop-ManagerProcessTree {
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

function Stop-ManagerByLockPort {
    param([int]$Port = 39103)
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($null -eq $listeners) { return }
    $pids = $listeners | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($pidVal in $pids) {
        Write-Host "Takeover: stopping lock-port owner PID=$pidVal ..."
        Stop-ManagerProcessTree -TargetPid $pidVal
    }
}

function Get-ManagerHealth {
    param([string]$ManagerUrl)
    try {
        return Invoke-RestMethod `
            -Uri "$($ManagerUrl.TrimEnd('/'))/api/manager/health" `
            -Method Get `
            -TimeoutSec 2 `
            -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function Get-ManagerEvidence {
    $manager = @(Get-ManagerPythonProcesses | Select-Object -First 1)
    $worker = @(Get-ExecutorWorkerProcesses -ProjectId $Project | Select-Object -First 1)
    return [pscustomobject]@{
        manager_pid = $(if ($manager.Count -gt 0) { $manager[0].ProcessId } else { $null })
        worker_pid = $(if ($worker.Count -gt 0) { $worker[0].ProcessId } else { $null })
        executor_state = $(if ($worker.Count -gt 0) { "optional_present" } else { "waived_or_degraded" })
    }
}

function Write-HealthyManagerEvidence {
    param(
        [string]$Message,
        [object]$Evidence,
        [object]$LauncherPid = $null
    )
    Write-Host $Message
    $managerPid = if ($null -ne $Evidence.manager_pid) { $Evidence.manager_pid } else { "not_observed" }
    $workerPid = if ($null -ne $Evidence.worker_pid) { $Evidence.worker_pid } else { "not_observed (optional)" }
    Write-Host "  manager:       $managerPid"
    Write-Host "  executor_state: $($Evidence.executor_state)"
    Write-Host "  worker:        $workerPid"
    if ($null -ne $LauncherPid) {
        Write-Host "  launcher:      $LauncherPid"
    }
}

function Wait-ManagerHealth {
    param([int]$WaitSeconds)
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline) {
        $health = Get-ManagerHealth -ManagerUrl $MANAGER_URL
        if ($null -ne $health -and [bool]$health.ok) {
            return $health
        }
        Start-Sleep -Milliseconds 750
    }
    return $null
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
# 使用内嵌 Python（优先）或系统 Python
$PYTHON = & (Join-Path $PSScriptRoot "_get_python.ps1")
Write-Host "Using Python: $PYTHON"

$planeJson = & $PYTHON -c 'import json, sys; from agent.runtime_plane import resolve_runtime_plane; p = resolve_runtime_plane(sys.argv[1]); print(json.dumps({"governance_url": p.governance_url, "manager_url": p.manager_url, "name": p.name}))' $Project
if ($LASTEXITCODE -ne 0) {
    throw "Could not resolve the runtime plane for project $Project."
}
$plane = $planeJson | ConvertFrom-Json
if ($env:GOVERNANCE_URL -and $env:GOVERNANCE_URL -ne $plane.governance_url) {
    throw "Configured GOVERNANCE_URL crosses the $Project runtime plane."
}
if ($env:MANAGER_URL -and $env:MANAGER_URL -ne $plane.manager_url) {
    throw "Configured MANAGER_URL crosses the $Project runtime plane."
}
$env:GOVERNANCE_URL = $plane.governance_url
$env:MANAGER_URL = $plane.manager_url
$env:PROJECT_ID = $Project
$env:EXECUTOR_PROJECT_ID = $Project
$MANAGER_URL = $plane.manager_url
if ($plane.name -eq "dev") {
    $devBindingJson = & $PYTHON -c 'import json; from pathlib import Path; from agent.governance.db import verified_stable_database_binding; from agent.runtime_plane import resolve_ac_dev_storage_root; binding = verified_stable_database_binding(); stable = Path(str(binding["shared_volume_path"])); dev = resolve_ac_dev_storage_root(stable); print(json.dumps({"stable_shared_volume": str(stable), "dev_storage_root": str(dev), "shared_volume_path": str(dev / "runtime")}))'
    if ($LASTEXITCODE -ne 0) {
        throw "Could not resolve the canonical AC dev storage binding."
    }
    $devBinding = $devBindingJson | ConvertFrom-Json
    if ($env:AMING_CLAW_SHARED_VOLUME -and $env:AMING_CLAW_SHARED_VOLUME -ne $devBinding.stable_shared_volume) {
        throw "Configured AMING_CLAW_SHARED_VOLUME crosses the $Project runtime plane."
    }
    if ($env:AMING_CLAW_DEV_STORAGE_ROOT -and $env:AMING_CLAW_DEV_STORAGE_ROOT -ne $devBinding.dev_storage_root) {
        throw "Configured AMING_CLAW_DEV_STORAGE_ROOT crosses the $Project runtime plane."
    }
    if ($env:SHARED_VOLUME_PATH -and $env:SHARED_VOLUME_PATH -ne $devBinding.shared_volume_path) {
        throw "Configured SHARED_VOLUME_PATH crosses the $Project runtime plane."
    }
    $env:AMING_CLAW_SHARED_VOLUME = $devBinding.stable_shared_volume
    $env:AMING_CLAW_DEV_STORAGE_ROOT = $devBinding.dev_storage_root
    $env:SHARED_VOLUME_PATH = $devBinding.shared_volume_path
}

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

try {
    $created = $false
    $mutex = New-Object System.Threading.Mutex($false, "Global\aming_claw_manager", [ref]$created)
    if (-not $mutex.WaitOne(0)) {
        $mutex.Dispose()
        $mutex = $null
        Write-Host "Manager mutex already held; waiting for ServiceManager health."
        $health = Wait-ManagerHealth -WaitSeconds $HealthWaitSeconds
        if ($null -eq $health) {
            throw "ServiceManager health did not become healthy within $HealthWaitSeconds seconds while the manager mutex was held."
        }
        Write-HealthyManagerEvidence `
            -Message "Manager became healthy while another launcher held the mutex." `
            -Evidence (Get-ManagerEvidence)
        return
    }
    $mutexAcquired = $true

    $health = Get-ManagerHealth -ManagerUrl $MANAGER_URL
    if ($null -ne $health -and [bool]$health.ok) {
        Write-HealthyManagerEvidence `
            -Message "Manager already healthy." `
            -Evidence (Get-ManagerEvidence)
        return
    }

    $existing = @(Get-ManagerPythonProcesses)
    if ($Takeover) {
        $lockPort = 39103
        if ($env:MANAGER_SINGLETON_PORT -and ($env:MANAGER_SINGLETON_PORT -as [int])) {
            $lockPort = [int]$env:MANAGER_SINGLETON_PORT
        }
        Stop-ManagerByLockPort -Port $lockPort
        Start-Sleep -Milliseconds 500
    }
    if ($existing.Count -gt 0 -and -not $Takeover) {
        $pids = ($existing | Select-Object -ExpandProperty ProcessId) -join ", "
        Write-Host "Manager process already exists (PID=$pids); waiting for ServiceManager health."
        $health = Wait-ManagerHealth -WaitSeconds $HealthWaitSeconds
        if ($null -eq $health) {
            throw "ServiceManager health did not become healthy within $HealthWaitSeconds seconds for the existing manager process."
        }
        Write-HealthyManagerEvidence `
            -Message "Manager healthy." `
            -Evidence (Get-ManagerEvidence)
        return
    }
    if ($existing.Count -gt 0 -and $Takeover) {
        $pids = ($existing | Select-Object -ExpandProperty ProcessId)
        foreach ($id in $pids) {
            Write-Host "Takeover: stopping existing manager PID=$id ..."
            Stop-ManagerProcessTree -TargetPid $id
        }
    }

    if ($Takeover) {
        $workerPids = @(Get-ExecutorWorkerProcesses | Select-Object -ExpandProperty ProcessId -Unique)
        foreach ($id in $workerPids) {
            Write-Host "Takeover: stopping existing executor worker PID=$id ..."
            Stop-ManagerProcessTree -TargetPid $id
        }

        if ($StopMcp) {
            $mcpPids = @(Get-McpServerProcesses | Select-Object -ExpandProperty ProcessId -Unique)
            foreach ($id in $mcpPids) {
                Write-Host "Takeover: stopping existing MCP server PID=$id ..."
                Stop-ManagerProcessTree -TargetPid $id
            }
        } else {
            Write-Host "Takeover: leaving MCP server processes running. Pass -StopMcp for explicit MCP cleanup."
        }
    }

    if (-not $env:SHARED_VOLUME_PATH) {
        $env:SHARED_VOLUME_PATH = Join-Path (Get-Location).Path "shared-volume"
    }
    New-Item -ItemType Directory -Force -Path $env:SHARED_VOLUME_PATH | Out-Null

    if (-not $env:CODEX_WORKSPACE) {
        $env:CODEX_WORKSPACE = (Get-Location).Path
    }

    Write-Host "Starting aming-claw host manager..."
    Write-Host "  project:   $Project"
    Write-Host "  governance:$($env:GOVERNANCE_URL)"
    Write-Host "  manager:   $MANAGER_URL"
    Write-Host "  workspace: $($env:CODEX_WORKSPACE)"
    $proc = Start-Process -FilePath $PYTHON `
        -ArgumentList @(
            "-m", "agent.service_manager",
            "--project", $Project,
            "--governance-url", $env:GOVERNANCE_URL,
            "--workspace", $env:CODEX_WORKSPACE
        ) `
        -WorkingDirectory (Get-Location).Path `
        -WindowStyle Hidden `
        -PassThru
    $health = Wait-ManagerHealth -WaitSeconds $HealthWaitSeconds
    if ($null -eq $health) {
        throw "ServiceManager health did not become healthy within $HealthWaitSeconds seconds after launch."
    }
    Write-HealthyManagerEvidence `
        -Message "Manager healthy." `
        -Evidence (Get-ManagerEvidence) `
        -LauncherPid $proc.Id
}
finally {
    if ($mutex -ne $null) {
        if ($mutexAcquired) {
            $mutex.ReleaseMutex() | Out-Null
        }
        $mutex.Dispose()
    }
}
