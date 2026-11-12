#Requires -Version 5.1
<#
.SYNOPSIS
    在桌面放一个双击就能启动 yumeFriend 的快捷方式。

.DESCRIPTION
    为什么需要：**双击 .ps1 默认是用记事本打开**（Windows 的安全设计：.ps1 的默认动作是"编辑"）。
    所以入口做成 `start_yumeFriend.bat`（双击 = 运行），再给它配图标和快捷方式。

    到 M7 打包成 exe 之后，这个快捷方式就换成指向 exe 了（双击即用，带自己的图标）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\create_desktop_shortcut.ps1
#>
[CmdletBinding()]
param(
    [string]$Name = "yumeFriend"
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root "start_yumeFriend.bat"
$icon = Join-Path $root "assets\yumeFriend.ico"

if (-not (Test-Path $target)) {
    throw "找不到启动器：$target"
}

$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop "$Name.lnk"

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $target
$lnk.WorkingDirectory = $root
$lnk.Description = "启动 yumeFriend（关窗不会退出，从右下角托盘退出）"
if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
$lnk.Save()

Write-Host "✓ 桌面快捷方式已创建：$lnkPath" -ForegroundColor Green
Write-Host "  双击它就能启动（黑窗口是日志，别关；退出走托盘）" -ForegroundColor DarkGray
