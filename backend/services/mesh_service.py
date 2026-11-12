"""组网（mesh）软件的**检测 + 配置**（v3.0 / B12；2026-09-27 通用化）。

对用户一律叫「**组网软件**」，不点名某一家。
**当前实现 = Tailscale**（`tailscale serve` 那条路）；以后换/加别家
（不同组网软件），改的是这个文件，不动调用方。

用户口径（2026-09-21）：**联网软件不静默安装** —— 我们只做三件不动系统的事：

1. **检测**：装没装 / 登录没登录 / tailnet 域名是什么
2. **配置**：帮他跑 `tailscale serve --bg http://127.0.0.1:<我们的端口>`
   （这条只是让"tailnet 里的 https 地址"转发到本机后端；后端仍然只绑 127.0.0.1）
3. **出二维码**：`yumefriend://open?url=<地址>` —— 手机相机一扫，App 里地址就填好了

**永远不主动装、不登录、不改系统设置**：装/登录必须用户在官网上自己做（要管理员 + 他的账号）。
连不上、没登录这种，一律回**人话**（哪一步没做、去哪儿点），绝不当异常抛。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from loguru import logger

from backend import config

DOWNLOAD_URL = "https://tailscale.com/download/windows"
SERVE_PORT = 443
_CANDIDATES = (
    Path(r"C:\Program Files\Tailscale\tailscale.exe"),
    Path(r"C:\Program Files (x86)\Tailscale\tailscale.exe"),
    # 2026-09-21：用户就是装在 D:\Tools\Tailscale 的 —— 默认位置之外也得认
    Path(r"D:\Tools\Tailscale\tailscale.exe"),
    Path(r"D:\Others\Tailscale\tailscale.exe"),
)
# 装在"别的盘 / 别的目录"时的兜底搜索（只搜常见形状，不做全盘扫描）
_GLOBS = (
    r"D:\Tools\*\tailscale.exe",
    r"D:\*\Tailscale\tailscale.exe",
    r"E:\Tools\*\tailscale.exe",
    r"E:\*\Tailscale\tailscale.exe",
    str(Path.home() / "AppData" / "Local" / "Programs" / "Tailscale" / "tailscale.exe"),
)
_cached: Path | None = None


def exe() -> Path | None:
    """tailscale.exe 在哪：先常用位置 → PATH → 几个常见"装在别处"的形状。**找不到就 None**。"""
    global _cached
    if _cached is not None and _cached.is_file():
        return _cached
    for path in _CANDIDATES:
        if path.is_file():
            _cached = path
            return path
    on_path = shutil.which("tailscale")
    if on_path:
        _cached = Path(on_path)
        return _cached
    import glob  # noqa: PLC0415 —— 只有找不到时才付出这点代价

    for pattern in _GLOBS:
        for hit in sorted(glob.glob(pattern)):
            candidate = Path(hit)
            if candidate.is_file():
                logger.info("在非默认位置找到组网软件：{}", candidate)
                _cached = candidate
                return candidate
    return None


def installed() -> bool:
    return exe() is not None


def _lan_urls(addrs: list, port: int) -> list[str]:
    """从"本机地址表"里挑出**局域网**那几张，拼成 `http://<ip>:<port>/m/`（D150）。

    为什么用 Tailscale 的 `Self.Addrs`：它天然列出本机**所有网卡**的地址 ——
    不用引 psutil、也不用作系统调用。再用"私有网段"一筛：
    Tailscale 自己（100.64/10）、公网出口 IP、别的 VPN 的虚拟网段（如 Radmin 的 26.x）
    **全都自动排除**，剩下的就是能给对方直连的那张私有网卡。

    ⚠️ 用户实测（2026-09-22）：同 Wi-Fi 走这条**比中继快一个数量级**，而且完全不依赖 Tailscale。
    """
    urls: list[str] = []
    for raw in addrs or []:
        text = str(raw).strip()
        if not text:
            continue
        # 组网地址 / 私有 IPv4 / 纯 IP
        host = text[1 : text.index("]")] if text.startswith("[") else text.rsplit(":", 1)[0]
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local:
            continue
        url = f"http://{ip}:{port}/m/"
        if url not in urls:
            urls.append(url)
    return urls


def _local_lan_urls(port: int) -> list[str]:
    """Tailscale 起不来时的兜底：从主机名解析到的 IPv4 里挑局域网地址。"""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return _lan_urls([str(info[4][0]) for info in infos], port)


def _private_ipv4_hosts() -> list[str]:
    """本机的私有 IPv4（含组网软件那块网卡的私有地址）。"""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    hosts: list[str] = []
    for info in infos:
        host = str(info[4][0])
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local:
            continue
        if host not in hosts:
            hosts.append(host)
    return hosts


def forward_port() -> int | None:
    """**手机走的那条路的端口**：保活工具把 `<组网网卡 IP>:<这个口>` 转发到本机后端。

    （2026-10-01 实测：组网软件更换私有 IP 后，手机书签就会连不上。
     而那页原来给的是 `:8000` —— 后端只绑 127.0.0.1，那个地址**根本不通**。）
    """
    local = Path(os.environ.get("LOCALAPPDATA", "") or ".")
    path = local / "yumeFriend-portal" / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))  # ⚠️ 保活那边写出来的带 BOM
    except Exception:  # noqa: BLE001 —— 没装 / 没配就读不到，用默认口
        return None
    try:
        port = int(data.get("ipv6_http_port") or 0)
    except (TypeError, ValueError):
        port = 0
    return port or 8899


def _health_ok(url: str) -> bool:
    """探一下这条地址是不是我们的后端（带 1 秒超时；**绕开系统代理**）。"""
    import httpx  # noqa: PLC0415 —— 只有真探活时才用

    try:
        with httpx.Client(trust_env=False, timeout=1.0) as client:
            resp = client.get(url.rstrip("/").removesuffix("/m") + "/api/health")
        return resp.status_code == 200 and "yumeFriend" in resp.text
    except Exception:  # noqa: BLE001 —— 探不通就是不通
        return False


def phone_urls(app_port: int | None = None) -> list[dict]:
    """**手机该填的地址**（组网网卡 IP + 转发口），带探活结果，能通的排前面。

    为什么不让手机去连 `:8001`：后端**只绑 127.0.0.1**（安全，D283）——
    唯一的过路人是保活工具开的那个转发口（见 `forward_port`）。
    """
    port = forward_port() or 8899
    urls = [f"http://{host}:{port}/m/" for host in _private_ipv4_hosts()]
    if not urls:  # 连一个私有 IPv4 都读不到 → 退回老路子
        urls = _local_lan_urls(app_port or config.PORT)
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(urls))) as pool:
        flags = list(pool.map(_health_ok, urls))
    result = [{"url": url, "ok": bool(ok)} for url, ok in zip(urls, flags)]
    result.sort(key=lambda item: (not item["ok"],))  # 通的在前（稳定排序）
    return result


def _with_phones(base: dict) -> dict:
    """给状态补上「手机该填哪个地址」—— `status()` 的每条 return 路都要过这里。"""
    try:
        phones = phone_urls(base.get("port"))
    except Exception:  # noqa: BLE001 —— 这层出问题不该让整页打不开
        phones = []
    base["phone_urls"] = phones
    ok = next((item["url"] for item in phones if item["ok"]), None)
    if ok:
        # 有能用的就直接当家：比"组网软件没登录"那种提示有用得多
        base["hint"] = f"手机就用这个地址：{ok}（App 里填 / 扫下面那张码；地址变了在 App 里点「换一个地址」）"
    return base


def _run(args: list[str], *, timeout: float = 20.0) -> tuple[int, str]:
    """跑一次 tailscale 命令。**永不抛**：拿不到就返回 (1, 说明)。"""
    binary = exe()
    if binary is None:
        return 1, "没装组网软件"
    try:
        proc = subprocess.run(  # noqa: S603 —— 路径是我们自己写死的，参数也不来自用户
            [str(binary), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return 1, "组网软件的命令超时（它可能正在启动）"
    except OSError as exc:
        return 1, f"跑不了组网软件的命令：{exc}"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()


def status(port: int | None = None) -> dict:
    """当前状态（给「手机端」那页看的）。**任何一步失败都给能照做的人话。**

    `port`：本机后端实际在用的端口（D283 固定 8001）；不传就用配置里的。
    """
    our_port = int(port or config.PORT)
    base: dict = {
        "installed": installed(),
        "download_url": DOWNLOAD_URL,
        "running": False,
        "logged_in": False,
        "dns_name": None,
        "ip": None,
        "node_id": None,
        "serve_configured": False,
        "serve_url": None,
        "enable_url": None,
        "port": our_port,
        # D150：同一个 Wi-Fi 时的"快路"（不碰 Tailscale）
        "lan_urls": _local_lan_urls(our_port),
        # ⚠️ 后端目前**只绑 127.0.0.1**（架构约定）：要真走局域网，得再开一个监听口子，
        # 而那等于"同一个 Wi-Fi 的人也能连"——**等用户拍板（口令方案）再打开**。
        "lan_enabled": False,
        "warnings": [],
        "hint": "",
    }
    if not base["installed"]:
        base["hint"] = (
            "这台电脑还没装组网软件。装一下（官网下载 → 双击 → 登录你的账号），"
            "装完回来点「刷新」就行。"
        )
        return _with_phones(base)

    code, raw = _run(["status", "--json"])
    if code != 0:
        base["hint"] = f"组网软件没跑起来或没登录（{raw[:120]}）—— 打开托盘里那个图标登录一下。"
        return _with_phones(base)

    try:
        data = json.loads(raw)
    except ValueError:
        base["hint"] = "读不懂组网软件的状态输出 —— 试试在托盘里点一下「Connect」。"
        return _with_phones(base)

    state = str(data.get("BackendState") or "")
    self_node = data.get("Self") or {}
    dns = str(self_node.get("DNSName") or "").rstrip(".")
    ips = self_node.get("TailscaleIPs") or []
    # D150：从 Tailscale 报的"本机全部地址"里挑局域网那张 —— 比主机名兜底准得多
    self_addrs = [str(item) for item in (self_node.get("Addrs") or [])]
    lan = _lan_urls(self_addrs, our_port)
    if lan:
        base["lan_urls"] = lan
    # 别的 VPN 的虚拟网卡会混进地址表里当"可用地址"，拖慢打洞（Radmin VPN 固定用 26.x）
    if any(item.split(":")[0].startswith("26.") for item in self_addrs):
        base["warnings"].append(
            "检测到另一个 VPN 的网卡（26.x，常见于 Radmin VPN 这类工具）—— "
            "用 yumeFriend 的时候建议先退出它：它会让组网软件多试一堆没用的地址、拖慢直连。"
        )
    # 手机那条路是**直连**还是**走中继**（用户实测"图还是很慢"：一查是 relay "nue"）
    peers = []
    for peer in (data.get("Peer") or {}).values():
        if not isinstance(peer, dict):
            continue
        cur = str(peer.get("CurAddr") or "")
        relay = str(peer.get("Relay") or "")
        name = (
            str(peer.get("DNSName") or peer.get("HostName") or "").rstrip(".").split(".")[0]
        )
        online = bool(peer.get("Online"))
        # 在线的话**实测一发**（status 里的 CurAddr 在"IPv6 直连"时会骗人）
        probed, detail = (None, "")
        if online:
            probed, detail = _probe_path(name or str(peer.get("DNSName") or ""))
        # D150：`Peer.Addrs` 只当参考 —— **实测不可靠**：2026-09-23 手机直连成功（13ms）时
        # 这个字段依然是 null。所以"到底直没直连"一律以 `tailscale ping` 的实测为准（上面那行）。
        peer_endpoints = [str(item) for item in (peer.get("Addrs") or [])]
        peers.append(
            {
                "name": name,
                "os": str(peer.get("OS") or ""),
                "online": online,
                "relay": relay,
                "detail": detail,
                "endpoints": len(peer_endpoints),
                # 实测优先；ping 不通就退回"CurAddr 有值 = 直连"
                "direct": probed if probed is not None else (bool(cur) or not relay),
            }
        )
    base["peers"] = peers
    bad = [item for item in peers if item["online"] and not item["direct"]]
    if bad:
        first = bad[0]
        base["path_hint"] = (
            f"{first['name']} 和这台电脑**没直连**（走中继 {first['relay'] or '?'}）—— 图/语音都会慢。"
            "最可能是**手机端组网软件没在正常工作**（被国产 ROM 的省电杀了 / 版本旧）。"
            "手机上按顺序试：① 把组网软件升级到最新版；② 退出登录 → 重新登录；"
            "③ 系统设置 → 应用 → 组网软件：开「自启动 / 允许后台运行 / 省电不限制」；"
            "④ 打开它停在前台几秒，再回来点「刷新」。"
            "（实测：2026-09-23 这样弄完，同一网络下直连恢复到 **13ms**，中继是 380ms 起。）"
            "另外两条辅助：电脑上若开着**全局 VPN / 代理（TUN 模式）**会顺带把 Tailscale 带走 → 设成规则模式；"
            "家里的路由器开 **UPnP / NAT-PMP** 也能帮它打洞。"
        )
    base["running"] = state == "Running"
    base["logged_in"] = bool(dns) and state == "Running"
    base["dns_name"] = dns or None
    base["ip"] = ips[0] if ips else None
    base["node_id"] = str(self_node.get("ID") or "") or None
    if base["node_id"]:
        # 这台的"去开 Serve"链接（2026-09-21 实测：新 tailnet 默认没开 Serve，
        # 用户点一次这个链接里那个 Enable 就行；node 参数就是 Self.ID）
        base["enable_url"] = f"https://login.tailscale.com/f/serve?node={base['node_id']}"

    if not base["logged_in"]:
        base["hint"] = f"组网软件状态是「{state or '未知'}」—— 在托盘图标里登录 / Connect 一下。"
        return _with_phones(base)

    base["serve_configured"] = _serve_is_configured()
    base["serve_url"] = f"https://{dns}/m/" if dns else None
    base["hint"] = (
        "都配好了：手机装好 App（或填地址）就能连。"
        if base["serve_configured"]
        else "还差最后一步：点下面「一键配好」，把地址转发到本机后端。"
    )
    return _with_phones(base)


def _serve_is_configured() -> bool:
    """`tailscale serve status` 里有没有指向我们端口的转发。"""
    code, raw = _run(["serve", "status"])
    if code != 0:
        return False
    # 真输出长这样：
    #   https://my-pc.tail1234.ts.net (tailnet only)
    #   |-- / proxy http://127.0.0.1:8001
    # 所以"有没有转发到我们本机端口"才是准的判据（别只看有没有 443）
    return f"127.0.0.1:{config.PORT}" in raw


def _probe_path(target: str) -> tuple[bool | None, str]:
    """实测一台设备走的是**直连**还是**中继**：`(是否直连, 说明)`。

    为什么不用 status 里的 CurAddr：实测发现**有直连（IPv6）时 CurAddr 还是空的**
    （2026-09-22：`tailscale ping` 明明显示 `via [240c:...]:50636 in 81ms`，
    但 status 里 `CurAddr` 空、`Relay` 有值）—— 所以这里直接打一发 ping 看它怎么回。
    """
    if not target:
        return None, ""
    # ⚠️ 注意：**没建立直连时 tailscale ping 会返回非 0**，但输出照样有用
    #（"pong … via DERP(nue) in 366ms / direct connection not established"）→ 别因为 code!=0 就丢掉
    _code, raw = _run(["ping", "--c", "1", "--timeout", "3s", target], timeout=12.0)
    if not raw:
        return None, ""
    if "via DERP(" in raw:
        relay = raw.split("via DERP(", 1)[1].split(")", 1)[0]
        return False, f"中继 {relay}"
    if "via " in raw:
        endpoint = raw.split("via ", 1)[1].split(" ", 1)[0]
        rtt = ""
        if " in " in raw:
            rtt = raw.split(" in ", 1)[1].strip().split(" ", 1)[0]
        return True, f"直连 {endpoint}{f'（{rtt}）' if rtt else ''}"
    return None, ""


def configure_serve(port: int | None = None) -> dict:
    """跑 `tailscale serve --bg http://127.0.0.1:<port>`（幂等，重复跑没事）。返回人话结果。"""
    target_port = int(port or config.PORT)
    if not installed():
        return {"ok": False, "hint": "还没装组网软件 —— 先装好、登录，再回来点这个。"}
    code, raw = _run(["serve", "--bg", f"http://127.0.0.1:{target_port}"], timeout=90.0)
    if code == 0:
        logger.info("tailscale serve 配好了：127.0.0.1:{}", target_port)
        return {"ok": True, "hint": "配好了 —— 地址就是下面那个 https 链接。"}
    lowered = raw.lower()
    if "serve is not enabled" in lowered or "funnel is not enabled" in lowered:
        # 真机实测（2026-09-21）：tailnet 默认没开 Serve，要先在后台点一次启用
        import re  # noqa: PLC0415

        match = re.search(r"https://login\.tailscale\.com/\S+", raw)
        link = match.group(0) if match else "https://login.tailscale.com/admin/machines"
        hint = f"组网软件后台还没打开「Serve」—— 打开这个链接点一下启用（一次性）：{link}"
    elif "not logged in" in lowered or "logged out" in lowered:
        hint = "组网软件还没登录 —— 先登录（托盘图标里 Sign in）。"
    elif "https" in lowered and ("enable" in lowered or "cert" in lowered):
        hint = (
            "组网软件那边的 HTTPS 证书还没开：登录它的后台 → DNS → 打开「HTTPS Certificates」，"
            "然后回来再点一次。"
        )
    else:
        if "timeout" in lowered or "超时" in raw:
            hint = "配的时候超时了（第一次申请 https 证书有时慢）—— 过一会儿再点一次试试。"
        elif not raw:
            # 实测：tailnet 没开 Serve 时，那条提示走的是**控制台**，重定向抓不到
            # → 表现为"命令一直没返回"。所以这里直接把"去开 Serve"的链接给出来。
            state = status()
            link = state.get("enable_url") or "https://login.tailscale.com/admin/machines"
            hint = (
                "没配上（命令一直没返回）—— 最常见的原因是**你的组网软件后台还没打开 Serve**："
                f"打开这个链接点一下启用，再回来点一次：{link}"
            )
        else:
            hint = f"没配上：{raw[:200] or '原因不明'}"
    logger.warning("tailscale serve 没配成功：{}", raw[:200])
    return {"ok": False, "hint": hint}


def qr_png(payload: str) -> bytes:
    """把一段文字（我们的深链）画成二维码 PNG。缺 qrcode 库时抛 RuntimeError（上层转人话）。"""
    try:
        import io  # noqa: PLC0415

        import qrcode  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover —— 正常装依赖不会有
        raise RuntimeError("没装 qrcode 库（pip install \"qrcode[pil]\"）") from exc
    image = qrcode.make(payload)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def deep_link(url: str) -> str:
    """手机 App 认的深链：扫一下就自动把地址填好（见 android MainActivity.onNewIntent）。"""
    from urllib.parse import quote  # noqa: PLC0415

    return f"yumefriend://open?url={quote(url, safe='')}"


def apk_path() -> Path | None:
    """找手机 App 的安装包（有的话就能从手机直接下载，不用数据线/微信传）。"""
    roots = [config.ROOT_DIR / "dist"]
    if config.is_frozen():  # 绿色版：apk 放在 exe 旁边（或旁边的 dist\）
        exe_dir = Path(config.USER_DATA_DIR).parent
        roots = [exe_dir, exe_dir / "dist", *roots]
    for root in roots:
        if not root.is_dir():
            continue
        # 不能按文件名字典序：7.9.0 会排在 7.18.0 后面，手机就会下到旧包。
        hits = sorted(
            root.glob("*android.apk"),
            key=lambda path: _apk_version_key(path.name),
        )
        if hits:
            return hits[-1]
    return None


def _apk_version_key(name: str) -> tuple[int, ...]:
    matched = re.search(r"(\d+(?:\.\d+)+)", name)
    if not matched:
        return (0,)
    return tuple(int(part) for part in matched.group(1).split("."))
