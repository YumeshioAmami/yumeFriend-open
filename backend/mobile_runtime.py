"""Chaquopy 入口：启动本地 FastAPI / uvicorn。"""

from __future__ import annotations

import os
import threading
from pathlib import Path


def start_server(
    data_dir: str,
    web_dir: str,
    assets_dir: str = "",
    port: int = 8001,
    device_name: str = "",
    device_kind: str = "",
) -> str:
    """阻塞式启动本地服务；Kotlin 在后台线程调用。

    `data_dir` = App 私有文件目录（SQLite / 图片 / 语音都在这里）
    `web_dir`  = APK assets 解出的前端静态目录
    `assets_dir` = 只读打包资产目录（后续 int8 模型等）
    """
    os.environ["YF_USER_DATA"] = str(Path(data_dir))
    os.environ["YF_MOBILE_WEB_DIR"] = str(Path(web_dir))
    if assets_dir:
        os.environ["YF_MOBILE_ASSETS_DIR"] = str(Path(assets_dir))
    if device_name:
        os.environ["YF_DEVICE_NAME"] = device_name
    if device_kind:
        os.environ["YF_DEVICE_KIND"] = device_kind
    os.environ.setdefault("YF_MOBILE", "1")

    import uvicorn  # noqa: PLC0415

    from backend.mobile_app import create_mobile_app  # noqa: PLC0415

    app = create_mobile_app()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=int(port),
        log_level="info",
        access_log=False,
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    # Chaquopy 调用线程里安装信号处理器没有意义，直接跳过。
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    server.run()
    return "stopped"


def start_server_bg(
    data_dir: str,
    web_dir: str,
    assets_dir: str = "",
    port: int = 8001,
    device_name: str = "",
    device_kind: str = "",
) -> str:
    """非阻塞版本，给不需要等待的调用方。"""
    thread = threading.Thread(
        target=start_server,
        args=(data_dir, web_dir, assets_dir, port, device_name, device_kind),
        daemon=True,
        name="yf-mobile-server",
    )
    thread.start()
    return "started"
