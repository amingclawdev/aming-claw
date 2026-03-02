$ErrorActionPreference = "Stop"

function Info($msg) {
    Write-Host "[agent] $msg"
}

function Stop-CodexTeamProcesses {
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -like "python*") -and (
            $_.CommandLine -like "*agent\\coordinator.py*" -or
            $_.CommandLine -like "*agent\\executor.py*"
        )
    }
    foreach ($p in $procs) {
        Info "Stopping PID=$($p.ProcessId) ($($p.Name))"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }

    $wrappers = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -like "powershell*") -and (
            $_.CommandLine -like "*start-coordinator.ps1*" -or
            $_.CommandLine -like "*start-executor.ps1*"
        )
    }
    foreach ($p in $wrappers) {
        Info "Stopping wrapper PID=$($p.ProcessId) ($($p.Name))"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

Set-Location (Join-Path $PSScriptRoot "..")

Stop-CodexTeamProcesses

Info "Starting coordinator in new PowerShell window..."
Start-Process powershell -ArgumentList "-NoExit", "-File", ".\scripts\start-coordinator.ps1" | Out-Null

Info "Starting executor in new PowerShell window..."
Start-Process powershell -ArgumentList "-NoExit", "-File", ".\scripts\start-executor.ps1" | Out-Null

Info "Done."
Info "Checks:"
Write-Host "  Get-CimInstance Win32_Process | ? { `$_.CommandLine -like '*agent\\*' } | select ProcessId,Name,CommandLine"
