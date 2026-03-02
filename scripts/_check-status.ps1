$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match '^python(\.exe)?$'
}

Write-Host "=== Python 进程 ==="
foreach ($p in $procs) {
    $cmd = [string]$p.CommandLine
    $label = if ($cmd -like "*coordinator*") { "coordinator" }
             elseif ($cmd -like "*executor*")  { "executor" }
             elseif ($cmd -like "*service_manager*" -or $cmd -like "*manager*") { "manager" }
             elseif ($cmd -like "*uvicorn*" -or $cmd -like "*gateway*") { "gateway" }
             else { "other" }
    Write-Host "  PID=$($p.ProcessId) [$label] $($cmd.Substring(0, [Math]::Min(120, $cmd.Length)))"
}

Write-Host ""
Write-Host "=== 端口 8090 ==="
$gw = Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue
if ($gw) { Write-Host "  LISTEN  PID=$($gw.OwningProcess)" } else { Write-Host "  未监听" }

Write-Host ""
Write-Host "=== 单例锁端口 ==="
foreach ($port in @(39101, 39102, 39103)) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    $label = switch($port) { 39101{"executor"} 39102{"coordinator"} 39103{"manager"} }
    if ($conn) { Write-Host "  :$port ($label) LISTEN  PID=$($conn.OwningProcess)" }
    else       { Write-Host "  :$port ($label) 未持有" }
}
