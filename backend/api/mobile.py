"""「手机端」那页的后端（v3.0 / B12 / T3.0-B12；2026-09-27 通用化）。

四件事：

```
GET  /api/mobile/status      装没装 / 登录没 / 域名 / serve 配没配 / APK 有没有
POST /api/mobile/serve       一键配「组网软件」的转发（当前实现 = `tailscale serve`）
GET  /api/mobile/qr.png      二维码（内容是 yumefriend://open?url=<地址>）
GET  /api/mobile/apk         把手机 App 的安装包发给手机（省得用数据线/微信传）
```

**注意**：不下载、不安装、不登录组网软件（用户口径）—— 只检测 + 配置转发。
给用户看的文案一律说「**组网软件**」，不点名某一家（实现细节在 `mesh_service`）。
"""

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

from backend.services import mesh_service
from backend.services.errors import ServiceError

router = APIRouter(prefix="/api/mobile", tags=["mobile"])


def _runtime_port() -> int | None:
    """桌面版实际在用的端口（D283 固定 8001）；读不到就 None。"""
    try:
        from backend.desktop.app import RUNTIME_PORT_FILE  # noqa: PLC0415 —— 懒导入

        if RUNTIME_PORT_FILE.is_file():
            text = RUNTIME_PORT_FILE.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
    except Exception:  # noqa: BLE001 —— 读不到就用默认端口
        pass
    return None


def _status_payload() -> dict:
    """状态 + APK 情况 + 实际端口（`status` / `serve` 两个接口共用）。"""
    state = mesh_service.status(port=_runtime_port())
    apk = mesh_service.apk_path()
    state["apk_ready"] = apk is not None
    state["apk_name"] = apk.name if apk else None
    return state


@router.get("/status")
def read_status() -> dict:
    """手机端连接状态（设置页那一屏全靠它）。"""
    return _status_payload()


@router.post("/serve")
def configure_serve() -> dict:
    """一键把地址转发到本机后端（幂等）。"""
    result = mesh_service.configure_serve()
    return {**result, "status": _status_payload()}


@router.get("/qr.png")
def qr_png(kind: str = "auto") -> Response:
    """二维码：手机相机一扫，App 里地址就自动填好（内容 = 我们的深链）。

    - `kind=auto`（**默认**）= **手机该填的那条**（组网网卡 IP + 电脑上的转发口，见
      `mesh_service.phone_urls`）。2026-10-01 起这是主路径：组网软件给的地址
      才是手机真正能连上的；`lan_urls`（:8001）那条后端只绑 127.0.0.1，**不通**。
    - `kind=lan` = 同一个 Wi-Fi 那条（有就用，没有退回 auto）。
    - `kind=tailnet` = 老路子（Tailscale serve 的 https 地址）。
    """
    state = mesh_service.status(port=_runtime_port())
    phones = [item["url"] for item in (state.get("phone_urls") or []) if item.get("ok")] or [
        item["url"] for item in (state.get("phone_urls") or [])
    ]
    if kind == "lan":
        url = (phones[-1] if phones else None) or (state.get("lan_urls") or [None])[0]
        missing = "还没有局域网地址 —— 确认电脑连着 Wi-Fi / 网线，再点「刷新」。"
    elif kind == "tailnet":
        url = state.get("serve_url") or (phones[0] if phones else None)
        missing = "还拿不到地址 —— 先把组网软件装好、登录，再点上面的「一键配好」。"
    else:
        url = (phones[0] if phones else None) or (state.get("lan_urls") or [None])[0] or state.get("serve_url")
        missing = "还没探到能用的地址 —— 确认电脑上的网络转发工具开着（手机走的就是它开的转发口），再点「刷新」。"
    if not url:
        raise ServiceError("MOBILE_NOT_READY", missing, 400)
    try:
        png = mesh_service.qr_png(mesh_service.deep_link(url))
    except RuntimeError as exc:
        raise ServiceError("QR_UNAVAILABLE", str(exc), 500) from exc
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/apk")
def apk() -> FileResponse:
    """手机 App 安装包（放在 exe 旁边或仓库的 dist\\ 里就能下）。"""
    path = mesh_service.apk_path()
    if path is None:
        raise ServiceError(
            "APK_NOT_FOUND",
            "这台电脑上没有手机 App 的安装包。用 `scripts\\build_apk.ps1` 打一个，"
            "或者把 yumeFriend-*-android.apk 放到 yumeFriend.exe 旁边。",
            404,
        )
    return FileResponse(
        path,
        media_type="application/vnd.android.package-archive",
        filename=path.name,
    )
