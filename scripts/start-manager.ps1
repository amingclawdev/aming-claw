param(
    [switch]$Takeover,
    # SM and MCP are independent. Takeover restarts manager/executor only by default;
    # pass -StopMcp for an explicit full host cleanup.
    [switch]$StopMcp,
    [string]$Project = "aming-claw",
    # Windows cold-start (sidecar + aiohttp + executor Python init + per-project chain history backfill)
    # takes 21-25s base + up to 30s for incremental backfill scans; 90s gives safe margin
    [int]$HealthWaitSeconds = 90
)

$ErrorActionPreference = "Stop"
$mutex = $null

try {
    $created = $false
    $mutex = New-Object System.Threading.Mutex($false, "Global\aming_claw_manager", [ref]$created)
    if (-not $mutex.WaitOne(0)) {
        Write-Host "Manager mutex already held; another manager launcher is active. Exit."
        return
    }
}
catch {
    throw
}

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
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $name = [string]$_.Name
        $cmd  = [string]$_.CommandLine
        $name -match '^python.*(\.exe)?$' -and (
            $cmd -like "*agent.executor_worker*" -or
            $cmd -like "*agent\executor_worker.py*" -or
            $cmd -like "*agent/executor_worker.py*"
        )
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

function Wait-ManagedWorker {
    param([int]$WaitSeconds)
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline) {
        $manager = @(Get-ManagerPythonProcesses | Select-Object -First 1)
        $worker = @(Get-ExecutorWorkerProcesses | Select-Object -First 1)
        if ($manager.Count -gt 0 -and $worker.Count -gt 0) {
            return @{
                manager_pid = $manager[0].ProcessId
                worker_pid = $worker[0].ProcessId
            }
        }
        Start-Sleep -Milliseconds 750
    }
    throw "Managed executor worker did not appear within $WaitSeconds seconds."
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
    Write-Host "Manager already running (PID=$pids). Skip starting duplicate instance."
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

if (-not $env:GOVERNANCE_URL) {
    $env:GOVERNANCE_URL = "http://localhost:40000"
}

if (-not $env:CODEX_WORKSPACE) {
    $env:CODEX_WORKSPACE = (Get-Location).Path
}

Write-Host "Starting aming-claw host manager..."
Write-Host "  project:   $Project"
Write-Host "  governance:$($env:GOVERNANCE_URL)"
Write-Host "  workspace: $($env:CODEX_WORKSPACE)"
try {
    $proc = Start-Process -FilePath $PYTHON `
        -ArgumentList @(
            ".\agent\service_manager.py",
            "--project", $Project,
            "--governance-url", $env:GOVERNANCE_URL,
            "--workspace", $env:CODEX_WORKSPACE
        ) `
        -WorkingDirectory (Get-Location).Path `
        -WindowStyle Hidden `
        -PassThru
    $health = Wait-ManagedWorker -WaitSeconds $HealthWaitSeconds
    Write-Host "Manager healthy."
    Write-Host "  manager:   $($health.manager_pid)"
    Write-Host "  worker:    $($health.worker_pid)"
    Write-Host "  launcher:  $($proc.Id)"
}
finally {
    if ($mutex -ne $null) {
        $mutex.ReleaseMutex() | Out-Null
        $mutex.Dispose()
    }
}
