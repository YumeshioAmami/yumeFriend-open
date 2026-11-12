# stop_backend.ps1 —— 停掉本机 yumeFriend 的桌面外壳 / 后端
#
# 背景一（为什么按端口杀）：
#   `Stop-Process -Id $handle.Id` 有时杀不掉真正监听 8000 的进程，
#   结果它一直抓着 SQLite 文件（删 user_data 会失败、数据看着"没清干净"）。
#
# 背景二（为什么还要按命令行扫一遍，2026-09-19 补）：
#   托盘「退出」曾经会留下**僵尸进程** —— 窗口和托盘图标都没了，
#   但 python 还在后台跑，一手攥着单实例互斥体、一手占着 8000 端口。
#   再启动就永远提示"已经打开了"，而用户根本找不到它去关。
#   只按端口杀兜不住"后端已死、外壳还活着"：那时没人监听 8000 了，
#   端口扫描扑空，可单实例锁还被死者占着。
#   所以这里补一发按命令行的兜底扫描。

$ErrorActionPreference = 'SilentlyContinue'
$killed = $false

function Get-YumeFriendLeftover {
    # 两类都算"我们自己"：
    #   ① 开发版：python 跑 backend.desktop.app / backend.main
    #   ② 打包版：进程名就叫 yumeFriend.exe（2026-09-21 补 —— 以前只认 python，
    #      打包版的残留它不认，反而报"被别的程序占着"，用户只能去任务管理器杀）
    Get-CimInstance Win32_Process | Where-Object {
        if ($_.Name -and $_.Name.ToLower() -eq 'yumefriend.exe') { return $true }
        if (-not $_.Name -or $_.Name.ToLower() -ne 'python.exe') { return $false }
        # ⚠️ 只认 **yumeFriend 的 python**：别的项目（yumeStudy）也用
        # `python -m backend.desktop.app`，只看命令行会误杀（2026-10-20 踩过）。
        $exe = [string]$_.ExecutablePath
        $cmd = [string]$_.CommandLine
        return (
            ($exe -and $exe -match 'yumeFriend') -or
            ($cmd -and $cmd -match 'yumeFriend')
        )
    }
}

# ── 0) 先按命令行扫（最安全：只杀**我们自己**的进程）
foreach ($z in (Get-YumeFriendLeftover)) {
    Write-Host "杀掉 PID $($z.ProcessId)（yumeFriend 残留进程）"
    Stop-Process -Id $z.ProcessId -Force
    $killed = $true
}

# ── 1) 再按端口杀 —— **只杀确认是我们自己的**（D283）：
#    桌面版固定 8001；8000 可能是别的项目（2026-09-19 踩过），不要再碰它。
$ports = @(8001)

foreach ($port in ($ports | Select-Object -Unique)) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen
    if (-not $conns) { continue }
    foreach ($c in $conns) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)"
        $mine = ($owner.Name -and $owner.Name.ToLower() -eq 'yumefriend.exe') -or (
            $owner.Name -and $owner.Name.ToLower() -eq 'python.exe' -and (
                ($owner.ExecutablePath -and $owner.ExecutablePath -match 'yumeFriend') -or
                ($owner.CommandLine -and $owner.CommandLine -match 'yumeFriend')
            )
        )
        if ($mine) {
            Write-Host "杀掉 PID $($c.OwningProcess)（监听 $port，确认是 yumeFriend）"
            Stop-Process -Id $c.OwningProcess -Force
            $killed = $true
        } else {
            Write-Host "⚠️ $port 被**别的程序**占着（PID $($c.OwningProcess)），不杀它"
            if ($owner.CommandLine) { Write-Host "    命令行：$($owner.CommandLine)" }
            Write-Host '    （yumeFriend 固定用 8001；先关掉那个程序再启动）'
        }
    }
}

if (-not $killed) {
    Write-Host 'yumeFriend 没在跑'
    exit 0
}

Start-Sleep -Seconds 2

$left = Get-YumeFriendLeftover
if ($left) {
    Write-Host "⚠️ 还有残留进程：$(($left.ProcessId) -join ', ')"
} else {
    Write-Host '✅ 没有残留进程，单实例锁已释放（可以再启动）'
}
