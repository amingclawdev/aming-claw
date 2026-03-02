#Requires -Version 5.1
<#
.SYNOPSIS
    aming-claw 初始化脚本：检查并安装 Git，然后调用 setup.ps1

.DESCRIPTION
    - 检测系统是否已安装 Git
    - 若未安装，使用 winget 自动安装 Git for Windows
    - 初始化 Git 仓库（若尚未初始化）
    - 自动调用 setup.ps1 完成 Python 环境配置

.USAGE
    右键 -> 用 PowerShell 运行，或在 PowerShell 终端执行:
        .\init.ps1
#>

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

# ── 步骤 1: 检查 Git ─────────────────────────────────────────────────────────
Write-Step "检查 Git 是否已安装..."

$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if ($gitCmd) {
    $gitVer = (git --version 2>&1)
    Write-OK "Git 已安装: $gitVer"
} else {
    Write-Warn "未检测到 Git，尝试自动安装..."

    # 检查 winget 是否可用
    $wingetCmd = Get-Command winget -ErrorAction SilentlyContinue
    if ($wingetCmd) {
        Write-Host "  使用 winget 安装 Git for Windows..."
        try {
            winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) {
                throw "winget 安装 Git 返回错误码 $LASTEXITCODE"
            }

            # 刷新 PATH：winget 安装后 PATH 可能未立即生效
            $machPath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
            $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
            $env:Path = "$machPath;$userPath"

            $gitCmd = Get-Command git -ErrorAction SilentlyContinue
            if ($gitCmd) {
                $gitVer = (git --version 2>&1)
                Write-OK "Git 安装成功: $gitVer"
            } else {
                Write-Warn "Git 已通过 winget 安装，但需要重启终端才能使用"
                Write-Host "  请关闭此终端，打开新终端后重新运行 .\init.ps1" -ForegroundColor Yellow
                Read-Host "按 Enter 退出"
                exit 1
            }
        } catch {
            Write-Fail "winget 安装 Git 失败: $_"
            Write-Host ""
            Write-Host "  请手动安装 Git:" -ForegroundColor Yellow
            Write-Host "    方式 1: winget install Git.Git" -ForegroundColor Yellow
            Write-Host "    方式 2: 从 https://git-scm.com/download/win 下载安装" -ForegroundColor Yellow
            Write-Host ""
            Read-Host "安装 Git 后按 Enter 重试，或 Ctrl+C 退出"
            # 重试检测
            $gitCmd = Get-Command git -ErrorAction SilentlyContinue
            if (-not $gitCmd) {
                Write-Fail "仍未检测到 Git，请安装后重新运行 .\init.ps1"
                exit 1
            }
            $gitVer = (git --version 2>&1)
            Write-OK "Git 已检测到: $gitVer"
        }
    } else {
        Write-Fail "未检测到 winget 包管理器"
        Write-Host ""
        Write-Host "  请手动安装 Git:" -ForegroundColor Yellow
        Write-Host "    从 https://git-scm.com/download/win 下载安装" -ForegroundColor Yellow
        Write-Host ""
        Read-Host "安装 Git 后按 Enter 重试，或 Ctrl+C 退出"
        $gitCmd = Get-Command git -ErrorAction SilentlyContinue
        if (-not $gitCmd) {
            Write-Fail "仍未检测到 Git，请安装后重新运行 .\init.ps1"
            exit 1
        }
        $gitVer = (git --version 2>&1)
        Write-OK "Git 已检测到: $gitVer"
    }
}

# ── 步骤 2: 初始化 Git 仓库 ──────────────────────────────────────────────────
Write-Step "检查 Git 仓库状态..."

if (Test-Path (Join-Path $PSScriptRoot ".git")) {
    $branch = (git -C $PSScriptRoot rev-parse --abbrev-ref HEAD 2>&1)
    Write-OK "Git 仓库已初始化 (分支: $branch)"
} else {
    Write-Host "  初始化 Git 仓库..."
    git -C $PSScriptRoot init
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "git init 失败"
        exit 1
    }

    # 设置默认分支名
    git -C $PSScriptRoot branch -M main 2>$null

    # 如果有 .gitignore，做一次初始提交
    if (Test-Path (Join-Path $PSScriptRoot ".gitignore")) {
        git -C $PSScriptRoot add .gitignore
        git -C $PSScriptRoot commit -m "Initial commit: add .gitignore" --allow-empty 2>$null
    }
    Write-OK "Git 仓库初始化完成"
}

# ── 步骤 3: 检查 .gitignore 关键条目 ─────────────────────────────────────────
Write-Step "验证 .gitignore 配置..."

$gitignorePath = Join-Path $PSScriptRoot ".gitignore"
$requiredEntries = @(
    ".env",
    "runtime/",
    "__pycache__/",
    "shared-volume/"
)

if (Test-Path $gitignorePath) {
    $gitignoreContent = Get-Content $gitignorePath -Raw
    $missing = @()
    foreach ($entry in $requiredEntries) {
        # 简单检查条目是否存在（忽略注释行）
        $pattern = [regex]::Escape($entry)
        if ($gitignoreContent -notmatch "(?m)^\s*$pattern") {
            $missing += $entry
        }
    }

    if ($missing.Count -gt 0) {
        Write-Warn "以下条目建议添加到 .gitignore:"
        foreach ($m in $missing) {
            Write-Host "      $m" -ForegroundColor Yellow
        }
        # 自动追加缺失条目
        $appendBlock = "`n# aming-claw auto-added`n"
        foreach ($m in $missing) {
            $appendBlock += "$m`n"
        }
        Add-Content -Path $gitignorePath -Value $appendBlock
        Write-OK "已自动追加缺失条目到 .gitignore"
    } else {
        Write-OK ".gitignore 配置完整"
    }
} else {
    Write-Warn ".gitignore 不存在，正在创建..."
    $defaultIgnore = @"
# Environment
.env
.env.*
!.env.example

# Python
__pycache__/
*.pyc

# Runtime (downloaded by setup.ps1)
runtime/

# Task storage
shared-volume/

# Logs
logs/
*.log
"@
    Set-Content -Path $gitignorePath -Value $defaultIgnore
    Write-OK ".gitignore 已创建"
}

# ── 步骤 4: 运行 setup.ps1 ───────────────────────────────────────────────────
Write-Step "调用 setup.ps1 进行环境配置..."

$setupScript = Join-Path $PSScriptRoot "setup.ps1"
if (Test-Path $setupScript) {
    & $setupScript
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "setup.ps1 执行失败，请检查输出信息"
        exit 1
    }
} else {
    Write-Warn "未找到 setup.ps1，跳过环境配置"
}

# ── 完成 ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  初始化完成！" -ForegroundColor Green
Write-Host ""
Write-Host "  已完成:" -ForegroundColor White
Write-Host "    - Git 检查/安装" -ForegroundColor White
Write-Host "    - Git 仓库验证" -ForegroundColor White
Write-Host "    - .gitignore 验证" -ForegroundColor White
Write-Host "    - Python 环境配置 (setup.ps1)" -ForegroundColor White
Write-Host ""
Write-Host "  下一步:" -ForegroundColor White
Write-Host "    1. 编辑 .env，填写 TELEGRAM_BOT_TOKEN_CODEX 等必填项" -ForegroundColor White
Write-Host "    2. 确保 Codex CLI 已安装并登录 (codex login)" -ForegroundColor White
Write-Host "    3. 运行 .\start.ps1 启动所有服务" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor Green
