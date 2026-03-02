#Requires -Version 5.1
<#
.SYNOPSIS
    aming-claw 一键环境配置脚本（无需预装 Python）

.DESCRIPTION
    - 下载内嵌版 Python 3.12 到 runtime\python\
    - 安装所有依赖包（requests, fastapi, uvicorn, pyyaml 等）
    - 若 .env 不存在，从 .env.example 复制并提示配置

.USAGE
    右键 -> 用 PowerShell 运行，或在 PowerShell 终端执行:
        .\setup.ps1
#>

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ── 配置 ───────────────────────────────────────────────────────────────────────
$PY_VER       = "3.12.8"
$PY_URL       = "https://www.python.org/ftp/python/$PY_VER/python-$PY_VER-embed-amd64.zip"
$GET_PIP_URL  = "https://bootstrap.pypa.io/get-pip.py"
$RUNTIME_DIR  = Join-Path $PSScriptRoot "runtime"
$PY_DIR       = Join-Path $RUNTIME_DIR "python"
$PY_EXE       = Join-Path $PY_DIR "python.exe"
$PIP_EXE      = Join-Path $PY_DIR "Scripts\pip.exe"
# ──────────────────────────────────────────────────────────────────────────────

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "===> $msg" -ForegroundColor Cyan
}

function Write-OK([string]$msg) {
    Write-Host "  [OK] $msg" -ForegroundColor Green
}

function Write-Warn([string]$msg) {
    Write-Host "  [!]  $msg" -ForegroundColor Yellow
}

function Write-Fail([string]$msg) {
    Write-Host "  [X]  $msg" -ForegroundColor Red
}

# ── 步骤 1: 下载 + 解压 Python ──────────────────────────────────────────────
Write-Step "检查内嵌 Python ($PY_VER)..."

if (Test-Path $PY_EXE) {
    $ver = (& $PY_EXE --version 2>&1) -replace "Python ", ""
    Write-OK "已存在 Python $ver -> $PY_EXE"
} else {
    New-Item -ItemType Directory -Force -Path $RUNTIME_DIR | Out-Null
    New-Item -ItemType Directory -Force -Path $PY_DIR | Out-Null

    $zipPath = Join-Path $RUNTIME_DIR "python-embed.zip"
    Write-Host "  下载 Python $PY_VER 内嵌包 (约 10 MB)..."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $PY_URL -OutFile $zipPath -UseBasicParsing
    } catch {
        Write-Fail "下载失败: $_"
        Write-Host "  请手动下载 $PY_URL 后解压到 runtime\python\" -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  解压中..."
    Expand-Archive -Path $zipPath -DestinationPath $PY_DIR -Force
    Remove-Item $zipPath -Force
    Write-OK "Python 解压完成"
}

# ── 步骤 2: 启用 site-packages（嵌入版默认禁用 pip）────────────────────────
Write-Step "启用 site-packages..."

$pthFile = Get-ChildItem $PY_DIR -Filter "python*._pth" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($pthFile) {
    $content = Get-Content $pthFile.FullName -Raw
    if ($content -match '#\s*import site') {
        $content = $content -replace '#\s*import site', 'import site'
        Set-Content -Path $pthFile.FullName -Value $content -NoNewline
        Write-OK "已在 $($pthFile.Name) 中启用 import site"
    } else {
        Write-OK "site 已启用 ($($pthFile.Name))"
    }
} else {
    Write-Warn "未找到 ._pth 文件，跳过（可能已配置）"
}

# ── 步骤 3: 安装 pip ──────────────────────────────────────────────────────────
Write-Step "检查 pip..."

$pipOk = $false
$ErrorActionPreference = "SilentlyContinue"
& $PY_EXE -m pip --version 2>&1 | Out-Null
$pipOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"

if (-not $pipOk) {
    Write-Host "  下载 get-pip.py..."
    $getPipPath = Join-Path $RUNTIME_DIR "get-pip.py"
    try {
        Invoke-WebRequest -Uri $GET_PIP_URL -OutFile $getPipPath -UseBasicParsing
    } catch {
        Write-Fail "下载 get-pip.py 失败: $_"
        exit 1
    }
    Write-Host "  安装 pip..."
    & $PY_EXE $getPipPath --no-warn-script-location
    Remove-Item $getPipPath -Force -ErrorAction SilentlyContinue
    Write-OK "pip 安装完成"
} else {
    $pipVer = (& $PY_EXE -m pip --version 2>&1)
    Write-OK "pip 已就绪: $pipVer"
}

# ── 步骤 4: 安装项目依赖 ──────────────────────────────────────────────────────
Write-Step "安装项目依赖..."

$reqFiles = @(
    "agent\requirements.txt",
    "executor-gateway\requirements.txt"
)
foreach ($req in $reqFiles) {
    $reqPath = Join-Path $PSScriptRoot $req
    if (Test-Path $reqPath) {
        Write-Host "  安装 $req ..."
        & $PY_EXE -m pip install -r $reqPath `
            --no-warn-script-location `
            --quiet
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "安装 $req 失败，请检查网络后重试"
            exit 1
        }
        Write-OK "$req 安装完成"
    } else {
        Write-Warn "未找到 $req，跳过"
    }
}

# ── 步骤 5: 验证依赖 ──────────────────────────────────────────────────────────
Write-Step "验证依赖..."

$checks = @(
    @{ module = "requests";   pkg = "agent" },
    @{ module = "fastapi";    pkg = "executor-gateway" },
    @{ module = "uvicorn";    pkg = "executor-gateway" },
    @{ module = "yaml";       pkg = "executor-gateway (pyyaml)" }
)
$allOk = $true
foreach ($c in $checks) {
    $ok = $false
    try {
        & $PY_EXE -c "import $($c.module)" 2>&1 | Out-Null
        $ok = ($LASTEXITCODE -eq 0)
    } catch { $ok = $false }
    if ($ok) {
        Write-OK "$($c.module) OK"
    } else {
        Write-Fail "$($c.module) 缺失（来自 $($c.pkg)）"
        $allOk = $false
    }
}
if (-not $allOk) {
    Write-Host ""
    Write-Warn "部分依赖验证失败，请重新运行 setup.ps1 或手动检查网络"
    exit 1
}

# ── 步骤 6: 检查 .env ─────────────────────────────────────────────────────────
Write-Step "检查配置文件 .env..."

$envPath    = Join-Path $PSScriptRoot ".env"
$envExample = Join-Path $PSScriptRoot ".env.example"

if (-not (Test-Path $envPath)) {
    if (Test-Path $envExample) {
        Copy-Item $envExample $envPath
        Write-Warn ".env 不存在，已从 .env.example 复制"
        Write-Host ""
        Write-Host "  *** 请打开 .env 文件，填写以下必填项后再运行 start.ps1 ***" -ForegroundColor Yellow
        Write-Host "      TELEGRAM_BOT_TOKEN_CODEX=<你的 Bot Token>" -ForegroundColor Yellow
        Write-Host "      EXECUTOR_API_TOKEN=<任意随机字符串，用于内部验证>" -ForegroundColor Yellow
        Write-Host ""
    } else {
        Write-Warn ".env 和 .env.example 均不存在，请手动创建 .env"
    }
} else {
    # 检查关键字段
    $envContent = Get-Content $envPath -Raw
    $missingKeys = @()
    if ($envContent -notmatch 'TELEGRAM_BOT_TOKEN[^=]*=\s*\S') { $missingKeys += "TELEGRAM_BOT_TOKEN_CODEX" }
    if ($envContent -notmatch 'EXECUTOR_API_TOKEN\s*=\s*\S') { $missingKeys += "EXECUTOR_API_TOKEN" }

    if ($missingKeys.Count -gt 0) {
        Write-Warn ".env 存在，但以下必填项尚未填写:"
        foreach ($k in $missingKeys) { Write-Host "      - $k" -ForegroundColor Yellow }
    } else {
        Write-OK ".env 已配置"
    }
}

# ── 步骤 7: 写入 runtime 标记 ─────────────────────────────────────────────────
$markerPath = Join-Path $RUNTIME_DIR "setup-done.txt"
"Setup completed at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`nPython: $PY_VER`nPython exe: $PY_EXE" | Set-Content $markerPath

# ── 完成 ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  环境配置完成！" -ForegroundColor Green
Write-Host "  Python : $PY_EXE" -ForegroundColor Green
Write-Host ""
Write-Host "  下一步:" -ForegroundColor White
Write-Host "    1. 编辑 .env，填写 TELEGRAM_BOT_TOKEN_CODEX 等必填项" -ForegroundColor White
Write-Host "    2. 确保 Codex CLI 已安装并登录（codex login）" -ForegroundColor White
Write-Host "    3. 运行 start.ps1 启动所有服务" -ForegroundColor White
Write-Host "============================================================" -ForegroundColor Green
