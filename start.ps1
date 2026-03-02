#Requires -Version 5.1
<#
.SYNOPSIS
    aming-claw 一键启动脚本

.DESCRIPTION
    启动全部服务：
      - executor-gateway  (截图/执行网关，端口 8090)
      - codex coordinator (Telegram Bot 消息处理)
      - codex executor    (任务调度与执行)

.USAGE
    右键 -> 用 PowerShell 运行，或:
        .\start.ps1             # 正常启动（已在运行则跳过）
        .\start.ps1 -Restart    # 强制重启所有服务
#>

param(
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ── 工具函数 ──────────────────────────────────────────────────────────────────
function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "===> $msg" -ForegroundColor Cyan
}
function Write-OK([string]$msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Write-Fail([string]$msg) { Write-Host "  [X]  $msg" -ForegroundColor Red }

# ── 1. 检查环境是否已 Setup ───────────────────────────────────────────────────
Write-Step "检查运行时环境..."

$PY_EXE = Join-Path $PSScriptRoot "runtime\python\python.exe"
$marker = Join-Path $PSScriptRoot "runtime\setup-done.txt"

if (-not (Test-Path $PY_EXE) -or -not (Test-Path $marker)) {
    Write-Warn "尚未完成初始化，正在自动运行 setup.ps1..."
    & "$PSScriptRoot\setup.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "setup.ps1 失败，请检查网络后重试"
        Read-Host "按 Enter 退出"
        exit 1
    }
}

$pyVer = (& $PY_EXE --version 2>&1) -replace "Python ", ""
Write-OK "Python $pyVer -> $PY_EXE"

# ── 2. 检查 .env ─────────────────────────────────────────────────────────────
Write-Step "加载 .env..."

$envPath = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envPath)) {
    Write-Fail ".env 不存在！请先运行 setup.ps1，然后填写 .env"
    Read-Host "按 Enter 退出"
    exit 1
}

Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }
    $pair = $_ -split '=', 2
    if ($pair.Length -eq 2) {
        [System.Environment]::SetEnvironmentVariable($pair[0].Trim(), $pair[1].Trim(), "Process")
    }
}

if (-not $env:TELEGRAM_BOT_TOKEN_CODEX -and -not $env:TELEGRAM_BOT_TOKEN) {
    Write-Fail "TELEGRAM_BOT_TOKEN_CODEX 未配置！请编辑 .env 后重试"
    Read-Host "按 Enter 退出"
    exit 1
}
if (-not $env:EXECUTOR_API_TOKEN) {
    Write-Fail "EXECUTOR_API_TOKEN 未配置！请编辑 .env 后重试"
    Read-Host "按 Enter 退出"
    exit 1
}
Write-OK ".env 已加载"

# ── 3. 检查 Codex CLI ────────────────────────────────────────────────────────
Write-Step "检查 Codex CLI..."

$codexBin = if ($env:CODEX_BIN) { $env:CODEX_BIN } else { "codex.cmd" }
$codexFound = Get-Command $codexBin -ErrorAction SilentlyContinue
if (-not $codexFound) {
    Write-Warn "未找到 Codex CLI ($codexBin)，executor 可能无法执行任务"
    Write-Host "  请安装 Codex CLI: npm install -g @openai/codex" -ForegroundColor Yellow
    Write-Host "  然后运行: codex login" -ForegroundColor Yellow
} else {
    Write-OK "Codex CLI: $($codexFound.Source)"
}

# ── 4. 设置默认环境变量 ───────────────────────────────────────────────────────
if (-not $env:SHARED_VOLUME_PATH) {
    $env:SHARED_VOLUME_PATH = Join-Path $PSScriptRoot "shared-volume"
}
if (-not $env:CODEX_WORKSPACE) {
    $env:CODEX_WORKSPACE = $PSScriptRoot
}
if (-not $env:CODEX_SEARCH_WORKSPACE) {
    $env:CODEX_SEARCH_WORKSPACE = Join-Path $env:CODEX_WORKSPACE "search-workspace"
}
if (-not $env:WORKSPACE_PATH) {
    $env:WORKSPACE_PATH = $PSScriptRoot
}
if (-not $env:EXECUTOR_BASE_URL) {
    $env:EXECUTOR_BASE_URL = "http://127.0.0.1:8090"
}

New-Item -ItemType Directory -Force -Path $env:SHARED_VOLUME_PATH | Out-Null
New-Item -ItemType Directory -Force -Path $env:CODEX_SEARCH_WORKSPACE | Out-Null

# ── 5. 停止旧进程（-Restart 模式）─────────────────────────────────────────────
if ($Restart) {
    Write-Step "停止旧进程（Restart 模式）..."
    & "$PSScriptRoot\scripts\restart-all.ps1" -SkipChecks -NoHealthWait -BypassMutex -HardRestart
    Write-OK "所有服务已重启"
    Read-Host "按 Enter 退出（服务在后台窗口中运行）"
    exit 0
}

# ── 6. 检查是否已在运行 ───────────────────────────────────────────────────────
Write-Step "检查现有进程..."

function Get-ServiceProcesses([string]$scriptName) {
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $cmd = [string]$_.CommandLine
        $cmd -like "*$scriptName*"
    }
}

$coordRunning = @(Get-ServiceProcesses "coordinator.py").Count -gt 0
$execRunning  = @(Get-ServiceProcesses "executor.py").Count -gt 0
$mgrRunning   = @(Get-ServiceProcesses "manager.py").Count -gt 0
$gwRunning    = @(Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue).Count -gt 0

$allRunning = $coordRunning -and $execRunning -and $mgrRunning -and $gwRunning

if ($allRunning) {
    Write-OK "所有服务已在运行"
    Write-Host ""
    Write-Host "  coordinator: 运行中" -ForegroundColor Green
    Write-Host "  executor   : 运行中" -ForegroundColor Green
    Write-Host "  manager    : 运行中" -ForegroundColor Green
    Write-Host "  gateway    : 运行中 (http://localhost:8090)" -ForegroundColor Green
    Write-Host ""
    Write-Host "  提示: 使用 .\start.ps1 -Restart 可强制重启所有服务" -ForegroundColor White
    Read-Host "按 Enter 退出"
    exit 0
}

# ── 7. 启动各服务 ─────────────────────────────────────────────────────────────
Write-Step "启动服务..."

# 环境变量传入子窗口
$envBlock = @"
`$env:TELEGRAM_BOT_TOKEN_CODEX='$env:TELEGRAM_BOT_TOKEN_CODEX'
`$env:TELEGRAM_BOT_TOKEN='$env:TELEGRAM_BOT_TOKEN'
`$env:EXECUTOR_API_TOKEN='$env:EXECUTOR_API_TOKEN'
`$env:EXECUTOR_BASE_URL='$env:EXECUTOR_BASE_URL'
`$env:SHARED_VOLUME_PATH='$env:SHARED_VOLUME_PATH'
`$env:CODEX_WORKSPACE='$env:CODEX_WORKSPACE'
`$env:CODEX_SEARCH_WORKSPACE='$env:CODEX_SEARCH_WORKSPACE'
`$env:WORKSPACE_PATH='$env:WORKSPACE_PATH'
`$env:CODEX_BIN='$codexBin'
`$env:CODEX_DANGEROUS='$(if ($env:CODEX_DANGEROUS -ne '') { $env:CODEX_DANGEROUS } else { '1' })'
`$env:CODEX_TIMEOUT_SEC='$(if ($env:CODEX_TIMEOUT_SEC -ne '') { $env:CODEX_TIMEOUT_SEC } else { '900' })'
`$env:TASK_TIMEOUT_SEC='$(if ($env:TASK_TIMEOUT_SEC -ne '') { $env:TASK_TIMEOUT_SEC } else { '1800' })'
`$env:EXECUTOR_HEARTBEAT_SEC='$(if ($env:EXECUTOR_HEARTBEAT_SEC -ne '') { $env:EXECUTOR_HEARTBEAT_SEC } else { '30' })'
"@

function Start-ServiceWindow([string]$title, [string]$script, [string]$extra = "") {
    $cmd = "$envBlock`n$extra`n& '$script'"
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-NoProfile",
        "-Command", $cmd
    ) -WindowStyle Normal | Out-Null
    Write-OK "$title 已启动"
    Start-Sleep -Milliseconds 600
}

# executor-gateway
if (-not $gwRunning) {
    Start-ServiceWindow "executor-gateway" "$PSScriptRoot\scripts\start-gateway.ps1"
    Start-Sleep -Seconds 2
}

# manager (outer service supervisor — start before coordinator/executor)
if (-not $mgrRunning) {
    Start-ServiceWindow "manager" "$PSScriptRoot\scripts\start-manager.ps1" "-Takeover"
    Start-Sleep -Seconds 1
}

# coordinator
if (-not $coordRunning) {
    Start-ServiceWindow "coordinator" "$PSScriptRoot\scripts\start-coordinator.ps1" "-Takeover"
}

# executor
if (-not $execRunning) {
    Start-ServiceWindow "executor" "$PSScriptRoot\scripts\start-executor.ps1" "-Takeover"
}

# ── 8. 完成 ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  aming-claw 已启动！" -ForegroundColor Green
Write-Host ""
Write-Host "  服务状态:" -ForegroundColor White
Write-Host "    executor-gateway : http://localhost:8090" -ForegroundColor White
Write-Host "    manager          : 管理服务（/mgr_restart /mgr_reinit）" -ForegroundColor White
Write-Host "    coordinator      : 监听 Telegram 消息" -ForegroundColor White
Write-Host "    executor         : 等待执行任务" -ForegroundColor White
Write-Host ""
Write-Host "  快捷命令:" -ForegroundColor White
Write-Host "    .\start.ps1 -Restart   重启所有服务" -ForegroundColor White
Write-Host "    .\setup.ps1            重新安装依赖" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Read-Host "按 Enter 退出（服务在后台窗口中持续运行）"
