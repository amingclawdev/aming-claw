param(
    [switch]$SkipChecks,
    [int]$HealthWaitSeconds = 300,
    [switch]$NoHealthWait = $true,
    [switch]$BypassMutex,
    [switch]$HardRestart = $true
)

$ErrorActionPreference = "Stop"
$mutex = $null

if (-not $BypassMutex) {
    try {
        $created = $false
        $mutex = New-Object System.Threading.Mutex($false, "Global\aming_claw_restart_all", [ref]$created)
        if (-not $mutex.WaitOne(0)) {
            throw "restart-all is already running. Please wait for the current run to finish. Or use -BypassMutex."
        }
    }
    catch {
        throw
    }
} else {
    Write-Host "[restart-all] BypassMutex enabled."
}

function Info($msg) {
    Write-Host "[restart-all] $msg"
}

if ($HardRestart) {
    Info "Mode: hard-restart (kill old processes then start new ones)."
} else {
    Info "Mode: graceful-restart."
}

function Get-CodexTeamProcesses {
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $cmd = [string]$_.CommandLine
        if (-not $cmd) { $cmd = "" }
        $cmd = $cmd.ToLowerInvariant()
        $name = [string]$_.Name
        if (-not $name) { $name = "" }
        $name = $name.ToLowerInvariant()
        (
            ($name -like "python*") -and (
                $cmd -like "*agent\\coordinator.py*" -or
                $cmd -like "*agent/coordinator.py*" -or
                $cmd -like "*agent\\executor.py*" -or
                $cmd -like "*agent/executor.py*"
            )
        ) -or (
            ($name -like "powershell*" -or $name -eq "cmd.exe") -and (
                $cmd -like "*start-coordinator.ps1*" -or
                $cmd -like "*start-executor.ps1*"
            )
        )
    }
}

function Wait-CodexTeamStopped {
    param(
        [int]$TimeoutSec = 20
    )
    for ($i = 0; $i -lt $TimeoutSec; $i++) {
        $left = @(Get-CodexTeamProcesses)
        if ($left.Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-CodexTeamProcesses {
    if ($HardRestart) {
        Info "Hard restart enabled: force-kill existing agent processes first..."
        $targets = @(Get-CodexTeamProcesses)
        foreach ($p in $targets) {
            Info "Force killing PID=$($p.ProcessId) ($($p.Name))"
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            taskkill /F /T /PID $p.ProcessId | Out-Null
        }
        # Kill lock-port owners as an extra fallback when commandline matching misses.
        $coordPort = 39102
        $execPort = 39101
        $coordL = Get-NetTCPConnection -LocalPort $coordPort -State Listen -ErrorAction SilentlyContinue
        $execL = Get-NetTCPConnection -LocalPort $execPort -State Listen -ErrorAction SilentlyContinue
        $lockPids = @()
        if ($coordL) { $lockPids += ($coordL | Select-Object -ExpandProperty OwningProcess -Unique) }
        if ($execL) { $lockPids += ($execL | Select-Object -ExpandProperty OwningProcess -Unique) }
        $lockPids = $lockPids | Select-Object -Unique
        foreach ($lp in $lockPids) {
            if ($lp -gt 0) {
                Info "Force killing lock-port owner PID=$lp"
                Stop-Process -Id $lp -Force -ErrorAction SilentlyContinue
                taskkill /F /T /PID $lp | Out-Null
            }
        }
        if (-not (Wait-CodexTeamStopped -TimeoutSec 15)) {
            $still = @(Get-CodexTeamProcesses | Select-Object -ExpandProperty ProcessId)
            throw "Hard restart failed, remaining PIDs: $($still -join ', ')"
        }
        return
    }

    $targets = @(Get-CodexTeamProcesses)
    foreach ($p in $targets) {
        Info "Stopping PID=$($p.ProcessId) ($($p.Name))"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }

    if (Wait-CodexTeamStopped -TimeoutSec 12) {
        return
    }

    # Fallback: kill process trees for stubborn wrapper/processes.
    $left = @(Get-CodexTeamProcesses)
    foreach ($p in $left) {
        Info "Force killing process tree PID=$($p.ProcessId) ($($p.Name))"
        taskkill /F /T /PID $p.ProcessId | Out-Null
    }

    if (-not (Wait-CodexTeamStopped -TimeoutSec 8)) {
        $still = @(Get-CodexTeamProcesses | Select-Object -ExpandProperty ProcessId)
        throw "Failed to stop existing agent processes. Remaining PIDs: $($still -join ', ')"
    }
}

function Get-PythonByScript([string]$scriptName) {
    $rx = [regex]::Escape($scriptName)
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $name = [string]$_.Name
        $cmd = [string]$_.CommandLine
        $name -match '^python(\.exe)?$' -and $cmd -match $rx
    }
}

function Enforce-SingleBotWorkers {
    Start-Sleep -Seconds 2

    $coordinators = @(Get-PythonByScript "coordinator.py" | Sort-Object ProcessId -Descending)
    if ($coordinators.Count -gt 1) {
        $keepPid = $coordinators[0].ProcessId
        Info "Multiple coordinators detected, keeping PID=$keepPid and stopping others..."
        for ($i = 1; $i -lt $coordinators.Count; $i++) {
            $targetPid = $coordinators[$i].ProcessId
            Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
            taskkill /F /T /PID $targetPid | Out-Null
        }
    }

    $executors = @(Get-PythonByScript "executor.py" | Sort-Object ProcessId -Descending)
    if ($executors.Count -gt 1) {
        $keepPid = $executors[0].ProcessId
        Info "Multiple executors detected, keeping PID=$keepPid and stopping others..."
        for ($i = 1; $i -lt $executors.Count; $i++) {
            $targetPid = $executors[$i].ProcessId
            Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
            taskkill /F /T /PID $targetPid | Out-Null
        }
    }
}

Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".\.env")) {
    throw ".env not found. Create it from .env.example first."
}

if (-not $SkipChecks) {
    Info "Running quick syntax checks..."
    $PYTHON = & (Join-Path $PSScriptRoot "_get_python.ps1")
    & $PYTHON -m compileall agent gateway | Out-Host
    Info "Checks passed."
}

Info "Reloading host executor..."
try {
    & .\scripts\reload-after-executor-change.ps1 -SkipChecks -HealthWaitSeconds $HealthWaitSeconds -NoHealthWait:$NoHealthWait | Out-Host
}
catch {
    Info "Host executor restart warning: $($_.Exception.Message)"
    Info "Continuing to start agent (screenshot may be unavailable until executor is healthy)."
}

Info "Restarting agent processes..."
Stop-CodexTeamProcesses

Info "Starting codex coordinator in new window..."
Start-Process powershell -ArgumentList "-NoExit", "-File", ".\scripts\start-coordinator.ps1", "-Takeover" | Out-Null

Info "Starting codex executor in new window..."
Start-Process powershell -ArgumentList "-NoExit", "-File", ".\scripts\start-executor.ps1", "-Takeover" | Out-Null

Enforce-SingleBotWorkers

Info "Done."
Info "Useful checks:"
Write-Host "  curl http://localhost:8090/health"
Write-Host "  Get-CimInstance Win32_Process | ? { `$_.Name -match '^python(\\.exe)?$' -and (`$_.CommandLine -like '*agent\\coordinator.py*' -or `$_.CommandLine -like '*agent\\executor.py*' -or `$_.CommandLine -like '*agent/coordinator.py*' -or `$_.CommandLine -like '*agent/executor.py*') } | select ProcessId,CreationDate,CommandLine"

if ($mutex -ne $null) {
    $mutex.ReleaseMutex() | Out-Null
    $mutex.Dispose()
}
