# 启动 coordinator 和 executor（在独立窗口中）
Set-Location (Join-Path $PSScriptRoot "..")

$scripts = Join-Path (Get-Location) "scripts"

Start-Process powershell -ArgumentList "-NoExit","-NoProfile","-File","$scripts\start-coordinator.ps1","-Takeover" -WindowStyle Normal
Write-Host "[launch] coordinator started"
Start-Sleep -Milliseconds 800

Start-Process powershell -ArgumentList "-NoExit","-NoProfile","-File","$scripts\start-executor.ps1","-Takeover" -WindowStyle Normal
Write-Host "[launch] executor started"
