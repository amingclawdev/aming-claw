param(
    [switch]$SkipChecks,
    [int]$HealthWaitSeconds = 120,
    [switch]$NoHealthWait
)

$ErrorActionPreference = "Stop"

function Info($msg) {
    Write-Host "[reload] $msg"
}

function Test-ExecutorHealth {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:8090/health" -TimeoutSec 2
        return $resp.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Stop-ExecutorIfRunning {
    $listeners = Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue
    if ($null -eq $listeners) {
        return
    }

    $procIds = $listeners | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $procIds) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
        if ($null -eq $proc) {
            continue
        }

        $name = ($proc.Name | ForEach-Object { $_.ToLowerInvariant() })
        $cmd = ($proc.CommandLine | ForEach-Object { $_.ToLowerInvariant() })
        $looksLikeExecutor = ($name -like "python*") -and (
            $cmd -like "*uvicorn*" -or $cmd -like "*app.main:app*" -or $cmd -like "*executor-gateway*"
        )

        if ($looksLikeExecutor) {
            Info "Stopping existing executor process PID=$procId ..."
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        } else {
            throw "Port 8090 is occupied by non-executor process PID=$procId ($($proc.Name))."
        }
    }

    $wrappers = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -like "powershell*") -and ($_.CommandLine -like "*start-gateway.ps1*")
    }
    foreach ($p in $wrappers) {
        Info "Stopping wrapper PID=$($p.ProcessId) ($($p.Name))"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

Set-Location (Join-Path $PSScriptRoot "..")

if (-not $SkipChecks) {
    Info "Running quick syntax checks..."
    python -m compileall gateway agent | Out-Host
    Info "Checks passed."
}

Info "Restarting host executor..."
Stop-ExecutorIfRunning
Start-Process powershell -ArgumentList "-NoExit", "-File", ".\scripts\start-gateway.ps1" | Out-Null

$skipWait = $NoHealthWait -or $HealthWaitSeconds -le 0
if ($skipWait) {
    Info "Skip health wait enabled. Host executor started asynchronously."
    return
}

$ready = $false
for ($i = 0; $i -lt $HealthWaitSeconds; $i++) {
    Start-Sleep -Seconds 1
    if (Test-ExecutorHealth) {
        $ready = $true
        break
    }
}

if (-not $ready) {
    throw "Host executor did not become healthy within $HealthWaitSeconds seconds."
}
Info "Host executor started and healthy."
