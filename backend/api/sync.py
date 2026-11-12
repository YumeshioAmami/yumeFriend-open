"""同步控制接口（D302 / D303）。

这些接口只在本机 127.0.0.1 上给前端用；真正的局域网服务由 sync_lan 临时启动。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body

from backend.db import session_scope
from backend.services import discovery_service, server_sync, sync_lan, sync_service

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.get("/status")
def status() -> dict:
    with session_scope() as session:
        device = {
            "device_id": sync_service.device_id(session),
            "device_name": sync_service.device_name(session),
            "device_kind": sync_service.device_kind(session),
        }
    return {"ok": True, "device": device, "session": sync_lan.status()}


@router.post("/start")
def start(body: dict = Body(default_factory=dict)) -> dict:
    name = str(body.get("name") or "").strip()
    kind = str(body.get("kind") or "unknown").strip()
    if not name:
        with session_scope() as session:
            name = sync_service.device_name(session)
    return sync_lan.start(name, kind)


@router.post("/stop")
def stop() -> dict:
    return sync_lan.stop()


@router.get("/peers")
def peers() -> dict:
    found = discovery_service.discover(timeout=1.2)
    with session_scope() as session:
        local_id = sync_service.device_id(session)
    return {
        "ok": True,
        "peers": [item for item in found if item.get("device_id") != local_id],
    }


@router.post("/run")
def run(body: dict = Body(...)) -> dict:
    peer = body.get("peer")
    code = str(body.get("code") or "").strip()
    direction = str(body.get("direction") or "both").strip().lower()
    if not isinstance(peer, dict):
        raise ValueError("先选择一个同步设备")
    if len(code) != 6:
        raise ValueError("同步码是 6 位数字")
    return sync_lan.sync_with_peer(peer, code, direction)


@router.get("/server")
def server_status() -> dict:
    with session_scope() as session:
        return {"ok": True, **server_sync.status(session)}


@router.patch("/server")
async def server_config(body: dict = Body(...)) -> dict:
    mode = str(body.get("mode") or "local")
    base_url = body.get("base_url")
    token = body.get("token")

    def _save() -> dict:
        with session_scope() as session:
            return server_sync.configure(
                session,
                mode=mode,
                base_url=base_url if isinstance(base_url, str) else None,
                token=token if isinstance(token, str) else None,
            )

    result = await asyncio.to_thread(_save)
    server_sync.restart_loop()
    return {"ok": True, **result}


@router.post("/server/test")
def server_test(body: dict = Body(default_factory=dict)) -> dict:
    base_url = str(body.get("base_url") or "")
    token = str(body.get("token") or "")
    if not token:
        with session_scope() as session:
            token = server_sync.configured_token(session)
    if not base_url:
        with session_scope() as session:
            base_url = server_sync.status(session).get("base_url", "")
    try:
        return {"ok": True, **(server_sync.test_connection(base_url, token))}
    except Exception as exc:  # noqa: BLE001 —— 测试连通要如实回错误
        return {"ok": False, "error": str(exc)}


@router.post("/server/now")
def server_now() -> dict:
    return server_sync.exchange_once()


@router.post("/server/pause")
def server_pause() -> dict:
    return {"ok": True, **server_sync.set_paused(True)}


@router.post("/server/resume")
def server_resume() -> dict:
    return {"ok": True, **server_sync.set_paused(False)}


@router.post("/server/push-chat")
async def server_push_chat() -> dict:
    return await asyncio.to_thread(server_sync.push_chat)


@router.post("/server/pull-chat")
async def server_pull_chat() -> dict:
    return await asyncio.to_thread(server_sync.pull_chat)


@router.post("/server/push-memories")
async def server_push_memories() -> dict:
    return await asyncio.to_thread(server_sync.push_memories)


@router.post("/server/pull-memories")
async def server_pull_memories() -> dict:
    return await asyncio.to_thread(server_sync.pull_memories)
