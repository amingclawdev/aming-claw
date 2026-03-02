<#
.SYNOPSIS
    返回项目内嵌 Python 的路径，若不存在则回退到系统 python。

.NOTES
    被 run-*.ps1 脚本调用，不直接执行。
    用法: $PYTHON = & "$PSScriptRoot\_get_python.ps1"
#>

$bundled = Join-Path $PSScriptRoot "..\runtime\python\python.exe"
if (Test-Path $bundled) {
    return (Resolve-Path $bundled).Path
}

# 回退：检查系统 python
$sys = Get-Command "python" -ErrorAction SilentlyContinue
if ($sys) {
    return $sys.Source
}

throw "未找到可用的 Python！请先运行 setup.ps1 初始化运行时环境。"
