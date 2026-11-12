#Requires -Version 5.1
<#
.SYNOPSIS
    打包 yumeFriend 桌面版（PyInstaller，v3.0 / T3.0-A2）。

.DESCRIPTION
    做三件事：① 构建前端（npm run build）② 装 PyInstaller（没有才装）③ 跑 spec 出包。
    产物：dist\yumeFriend\yumeFriend.exe（目录模式 —— 发出去是整个文件夹）

    数据在 **exe 旁边的 user_data\**（绿色版），覆盖新版本 exe 不会丢数据。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_desktop.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_desktop.ps1 -SkipFrontend
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend,   # 前端已经构建过就跳过（省一分钟）
    [switch]$Clean,          # 先把 build/ 和 dist/ 清掉再打
    [switch]$NoZip           # 不生成发布 zip（只出文件夹）
)

$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$versionMatch = Select-String -LiteralPath (Join-Path $root "backend\config.py") -Pattern '^APP_VERSION\s*=\s*"([^"]+)"'
if (-not $versionMatch) {
    Write-Host "✗ 读不到 backend/config.py 里的 APP_VERSION" -ForegroundColor Red
    exit 1
}
$version = $versionMatch.Matches[0].Groups[1].Value

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "✗ 找不到虚拟环境：$python" -ForegroundColor Red
    exit 1
}

if (-not $SkipFrontend) {
    Write-Host "① 构建前端…" -ForegroundColor Cyan
    if (-not (Test-Path (Join-Path $root "frontend\node_modules"))) {
        Write-Host "  先装前端依赖（npm install）…"
        Push-Location (Join-Path $root "frontend")
        npm install
        Pop-Location
    }
    Push-Location (Join-Path $root "frontend")
    npm run build
    Pop-Location
} else {
    Write-Host "① 跳过前端构建（-SkipFrontend）" -ForegroundColor DarkGray
}

$dist = Join-Path $root "frontend\dist\index.html"
if (-not (Test-Path $dist)) {
    Write-Host "✗ 前端还没构建：$dist" -ForegroundColor Red
    exit 1
}

Write-Host "② 检查 PyInstaller…" -ForegroundColor Cyan
& $python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  没装，装一下…"
    & $python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { Write-Host "✗ 装 PyInstaller 失败" -ForegroundColor Red; exit 1 }
}

Write-Host "②.5 准备游戏适配器（Node + 裁剪 minecraft-data）…" -ForegroundColor Cyan
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "✗ 打包需要 Node（Q43）：先装 Node 或把它放进 PATH" -ForegroundColor Red
    exit 1
}
$mc = Join-Path $root "adapters\minecraft"
if (-not (Test-Path (Join-Path $mc "node_modules\mineflayer"))) {
    Write-Host "  先装适配器依赖（npm install，约 30 秒）…"
    Push-Location $mc
    npm install --no-audit --no-fund
    Pop-Location
    if ($LASTEXITCODE -ne 0) { Write-Host "✗ 适配器依赖安装失败" -ForegroundColor Red; exit 1 }
}
& $python -X utf8 (Join-Path $root "scripts\prune_minecraft_data.py") --apply
if ($LASTEXITCODE -ne 0) { Write-Host "✗ 裁剪 minecraft-data 失败" -ForegroundColor Red; exit 1 }

if ($Clean) {
    Write-Host "   清理 build\ 和 dist\ …" -ForegroundColor DarkGray
    foreach ($name in @("build", "dist")) {
        $path = Join-Path $root $name
        if (Test-Path $path) { Remove-Item -LiteralPath $path -Recurse -Force }
    }
}

# ⚠️ PyInstaller 每次会把整个 dist\yumeFriend\ **删掉重建** ——
# 而"绿色版"的数据就放在那儿（user_data\）。不保护的话，重新打包 = 把用户聊天记录删了。
$distApp = Join-Path $root "dist\yumeFriend"
$distData = Join-Path $distApp "user_data"
$stash = Join-Path $root "dist\_user_data_stash"
$distRoot = [System.IO.Path]::GetFullPath((Join-Path $root "dist"))
$resolvedData = [System.IO.Path]::GetFullPath($distData)
# ⚠️ 程序正开着的时候，它的数据库被占着（而且 PyInstaller 会删整个 dist\yumeFriend）→ 直接别打
$running = Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($distRoot, [StringComparison]::OrdinalIgnoreCase)
}
if ($running) {
    Write-Host "✗ 现在有 yumeFriend 在跑（PID $(($running.ProcessId) -join ', ')），先退出它再打包：" -ForegroundColor Red
    Write-Host "   ① 点右下角托盘图标 → 退出"
    Write-Host "   ② 或者跑 .\scripts\stop_backend.ps1"
    exit 1
}
$lockedDb = Join-Path $distData "yumefriend.db"
if (Test-Path $lockedDb) {
    try {
        $handle = [System.IO.File]::Open($lockedDb, 'Open', 'ReadWrite', 'None')
        $handle.Close()
    } catch {
        Write-Host "✗ 数据文件被别的程序占着（$lockedDb）—— 把 yumeFriend 退干净再打包。" -ForegroundColor Red
        exit 1
    }
}
# ⚠️ 2026-09-26 事故：这里原来把"上一次没收干净的 stash"当垃圾 `Remove-Item -Recurse -Force` 删了，
#    而里面正躺着他的 user_data（打包中断留下的）→ 绕过回收站物理删除，SSD 的 TRIM 让
#    TestDisk/PhotoRec 也救不回来（教训：SSD 上 undelete 是幻想）。**现在绝不删，停下来交给人工。**
$resolvedStash = [System.IO.Path]::GetFullPath($stash)
if (Test-Path $stash) {
    if (-not $resolvedStash.StartsWith($distRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "✗ _user_data_stash 解析出来的路径不在 dist 里，拒绝处理：$resolvedStash" -ForegroundColor Red
        exit 1
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $rescue = Join-Path "D:\yumeFriend-backup" "stash-rescue-$stamp"
    New-Item -ItemType Directory -Path $rescue -Force | Out-Null
    Copy-Item -LiteralPath $stash -Destination (Join-Path $rescue "_user_data_stash") -Recurse -Force
    Write-Host "✗ dist 里还留着上一次的 _user_data_stash（说明上次打包中途断了）。" -ForegroundColor Red
    Write-Host "  我不会删它 —— 已整份复制到：$rescue" -ForegroundColor Yellow
    Write-Host "  请先人工把里面的 user_data 挪回 $distData（确认不要了再删），然后重新打包。" -ForegroundColor Yellow
    exit 1
}

# 打包前先给 D 盘留一份（一天一次，别每次打包都拷几百 MB）
if ((Test-Path $distData) -and $resolvedData.StartsWith($distRoot, [StringComparison]::OrdinalIgnoreCase)) {
    $day = Get-Date -Format 'yyyyMMdd'
    $preBackup = Join-Path "D:\yumeFriend-backup" "pre-pack-$day"
    if (-not (Test-Path $preBackup)) {
        New-Item -ItemType Directory -Path $preBackup -Force | Out-Null
        Copy-Item -LiteralPath $distData -Destination (Join-Path $preBackup "user_data") -Recurse -Force
        Write-Host "   （打包前先备份一份到 $preBackup）" -ForegroundColor DarkGray
    }
}
if ((Test-Path $distData) -and $resolvedData.StartsWith($distRoot, [StringComparison]::OrdinalIgnoreCase)) {
    Write-Host "③.0 发现 dist 里有 user_data → 先挪到 _user_data_stash，打完再挪回来（别删你的数据）" -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $stash -Force | Out-Null
    Move-Item -LiteralPath $distData -Destination (Join-Path $stash "user_data") -Force
}

Write-Host "③ 打包（大概要 1~3 分钟）…" -ForegroundColor Cyan
& $python -m PyInstaller --noconfirm --distpath (Join-Path $root "dist") --workpath (Join-Path $root "build") (Join-Path $root "yumeFriend.spec")
if ($LASTEXITCODE -ne 0) { Write-Host "✗ 打包失败" -ForegroundColor Red; exit 1 }

if (Test-Path (Join-Path $stash "user_data")) {
    Move-Item -LiteralPath (Join-Path $stash "user_data") -Destination $distData -Force
    Write-Host "   数据挪回来了：$distData" -ForegroundColor Green
    Remove-Item -LiteralPath $stash -Recurse -Force -ErrorAction SilentlyContinue
}

$exe = Join-Path $root "dist\yumeFriend\yumeFriend.exe"
if (-not (Test-Path $exe)) {
    Write-Host "✗ 没看到产物：$exe" -ForegroundColor Red
    exit 1
}

# 使用说明放进包里（双击 exe 的人先看到这个）
Copy-Item -LiteralPath (Join-Path $root "scripts\packaged_readme.txt") -Destination (Join-Path $root "dist\yumeFriend\使用说明.txt") -Force

# D456：自动更新器是独立 onefile exe，随桌面 zip 分发。
Write-Host "③.5 构建自动更新器…" -ForegroundColor Cyan
$updaterWork = Join-Path $root "build\updater"
& $python -m PyInstaller --noconfirm --onefile --windowed `
    --name "yumeFriend-updater" `
    --distpath (Join-Path $root "dist") `
    --workpath $updaterWork `
    --specpath $updaterWork `
    --icon (Join-Path $root "assets\yumeFriend.ico") `
    (Join-Path $root "scripts\updater_entry.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "✗ 更新器构建失败" -ForegroundColor Red
    exit 1
}
$updaterExe = Join-Path $root "dist\yumeFriend-updater.exe"
if (-not (Test-Path $updaterExe)) {
    Write-Host "✗ 没看到更新器产物：$updaterExe" -ForegroundColor Red
    exit 1
}
Copy-Item -LiteralPath $updaterExe -Destination (Join-Path $root "dist\yumeFriend\yumeFriend-updater.exe") -Force
Write-Host "   更新器已放入打包目录" -ForegroundColor DarkGray

$zip = Join-Path $root "dist\yumeFriend-$version.zip"
if (-not $NoZip) {
    Write-Host "④ 打发布 zip…" -ForegroundColor Cyan
    if (Test-Path $zip) { Remove-Item -LiteralPath $zip -Force }
    # ⚠️ Windows PowerShell 5.1 默认**不加载** System.IO.Compression.FileSystem，
    # 不先 Add-Type 直接 new 类型会报 "Unable to find type [System.IO.Compression.ZipFile]"
    # （2026-09-23 打包时踩到：exe 打好了，zip 这步整个失败）。
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    Add-Type -AssemblyName System.IO.Compression
    # ⚠️ 发布包里**绝不能带 user_data**（2026-09-23 发现）：
    # 那是"你自己的"聊天记录 / 图片 / 语音 / 密钥，不是"软件本体"——
    # 旧写法 Compress-Archive 整个文件夹，等于把你整份数据打包发给别人。
    # 做法：先整目录压一份，再把 user_data/* 的条目从包里删掉（比"拷到暂存目录再压"快得多）。
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        (Join-Path $root "dist\yumeFriend"),
        $zip,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false   # 不加一层 base 目录（解压出来就是 yumeFriend.exe 那一层）
    )
    $archive = [System.IO.Compression.ZipFile]::Open($zip, [System.IO.Compression.ZipArchiveMode]::Update)
    # ⚠️ .NET 写出来的条目名用**反斜杠**（`user_data\avatar\`），别用 `user_data/*` 去匹配（匹配不到）
    $userDataEntries = @($archive.Entries | Where-Object { $_.FullName -match '^user_data[\\/]' })
    foreach ($entry in $userDataEntries) { $entry.Delete() }
    $archive.Dispose()
    Write-Host ("   发布包：" + $zip)
} else {
    Write-Host "④ 跳过 zip（-NoZip）" -ForegroundColor DarkGray
}

# 算大小要**排掉 user_data/** —— 那是你聊天记录/图片/语音（几十上百 MB），不是软件本体的体积
$size = (
    Get-ChildItem (Join-Path $root "dist\yumeFriend") -Recurse -File |
    Where-Object { $_.FullName -notlike "*\user_data\*" } |
    Measure-Object -Property Length -Sum
).Sum / 1MB
Write-Host ""
Write-Host "✅ 打包完成" -ForegroundColor Green
Write-Host ("   产物：" + $exe)
Write-Host ("   大小：{0:N1} MB（整个文件夹一起发出去）" -f $size)
Write-Host "   数据：exe 旁边的 user_data\（覆盖新版本 exe 不会丢数据）"
Write-Host "   下一步：跑 .\scripts\check_packaged.ps1 验一遍（或直接双击）"
