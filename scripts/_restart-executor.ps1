Set-Location (Join-Path $PSScriptRoot "..")

# 杀掉现有 executor 进程（包括子进程）
$procs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(\.exe)?$' -and (
        [string]$_.CommandLine -like "*agent\executor.py*" -or
        [string]$_.CommandLine -like "*agent/executor.py*"
    )
}
foreach ($p in $procs) {
    Write-Host "Stopping executor PID=$($p.ProcessId)"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    taskkill /F /T /PID $p.ProcessId 2>&1 | Out-Null
}

# 释放端口 39101 的持有者
$listeners = Get-NetTCPConnection -LocalPort 39101 -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    foreach ($l in $listeners) {
        Write-Host "Releasing port 39101 holder PID=$($l.OwningProcess)"
        Stop-Process -Id $l.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

Start-Sleep -Seconds 1

# 启动新的 executor
$scripts = Join-Path (Get-Location) "scripts"
Start-Process powershell -ArgumentList "-NoExit","-NoProfile","-File","$scripts\start-executor.ps1","-Takeover" -WindowStyle Normal
Write-Host "Executor restarted."
