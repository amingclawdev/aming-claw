Set-Location (Join-Path $PSScriptRoot "..")

$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(\.exe)?$' -and (
        [string]$_.CommandLine -like "*agent\coordinator.py*" -or
        [string]$_.CommandLine -like "*agent/coordinator.py*"
    )
}
foreach ($p in $procs) {
    Write-Host "Stopping coordinator PID=$($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    taskkill /F /T /PID $p.ProcessId 2>&1 | Out-Null
}

$listeners = Get-NetTCPConnection -LocalPort 39102 -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    foreach ($l in $listeners) {
        Stop-Process -Id $l.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

Start-Sleep -Seconds 1
$scripts = Join-Path (Get-Location) "scripts"
Start-Process powershell -ArgumentList "-NoExit","-NoProfile","-File","$scripts\start-coordinator.ps1","-Takeover" -WindowStyle Normal
Write-Host "Coordinator restarted."
