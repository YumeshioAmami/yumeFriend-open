"""v7.12（D342）：轻量服务器模式的最小 FastAPI 应用。

只暴露 `/api/health` 和 `/api/server-sync/*`；不加载聊天、生活线、游戏、
语音或桌面外壳。服务器只做权威数据存储 + 增量交换。
"""

from __future__ import annotations

from fastapi import FastAPI

from backend.api import server_sync as server_sync_api
from backend.config import APP_NAME, APP_VERSION, ensure_dirs


def create_server_app() -> FastAPI:
    ensure_dirs()
    app = FastAPI(title=f"{APP_NAME}-server", version=APP_VERSION)

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "app": APP_NAME,
            "version": APP_VERSION,
            "platform": "server",
        }

    app.include_router(server_sync_api.router)
    return app


app = create_server_app()
