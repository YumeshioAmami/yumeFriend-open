#Requires -Version 5.1
<#
.SYNOPSIS
    启动 yumeFriend 桌面版（后端 + 窗口 + 托盘，一条命令）。

.DESCRIPTION
    做的事：检查 .venv 和前端构建产物 → 启动 backend.desktop.app。
    启动后：窗口看得见、关窗不会退出（D16），要退出点右下角托盘的「退出」。

    常见情况：
      · 提示"已经有一个在跑了" → 从托盘图标打开，或先跑 .\scripts\stop_backend.ps1
      · 提示"前端还没构建"     → cd frontend; npm run build

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_desktop.ps1
#>
[CmdletBinding()]
param(
    [switch]$Headless   # 只跑后端（不开窗口/托盘），调试用
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$sourceUserData = Join-Path $root "user_data"
New-Item -ItemType Directory -Path $sourceUserData -Force | Out-Null
# D364：测试统一用源码版，数据固定落在仓库根 user_data；
# 不允许误用打包版旁边的数据目录。
$env:YF_USER_DATA = $sourceUserData

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "✗ 找不到虚拟环境：$python" -ForegroundColor Red
    Write-Host "  先按 AGENTS.md §6 建 .venv 并装 backend/requirements.txt"
    exit 1
}

$dist = Join-Path $root "frontend\dist\index.html"
if (-not (Test-Path $dist)) {
    Write-Host "✗ 前端还没构建（窗口会打不开）：$dist" -ForegroundColor Red
    Write-Host "  先跑：cd frontend; npm install; npm run build"
    exit 1
}

Write-Host "启动 yumeFriend 桌面版…" -ForegroundColor Cyan
Write-Host "  · 关窗口不会退出（会缩到右下角托盘）"
Write-Host "  · 要真的退出：右键托盘图标 → 退出" -ForegroundColor DarkGray
Write-Host "  · 数据目录：$sourceUserData" -ForegroundColor DarkGray

$args = @("-m", "backend.desktop.app")
if ($Headless) { $args += "--headless" }

Push-Location $root
try {
    & $python @args
} finally {
    Pop-Location
}
