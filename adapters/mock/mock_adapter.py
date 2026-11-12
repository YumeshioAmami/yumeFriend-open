#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P0 用的**假游戏适配器**。

它不是游戏，只实现 `docs/game_adapter.md` 的契约 v1，用来验证：
start → snapshot → command → event → stop 整条链路。

用法（由主代码 / 测试启动）：
    python adapters/mock/mock_adapter.py --port 12345 --token <token> [--heartbeat 5]

**故意不带任何游戏依赖**：P0 不碰真 MC、不碰 LLM。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

PROTOCOL_VERSION = 1

ACTIONS: dict[str, dict[str, Any]] = {
    "goto": {
        "params": {"x": "number", "y": "number", "z": "number"},
        "description": "走到坐标",
    },
    "follow": {"params": {"player": "string"}, "description": "跟着某个玩家"},
    "mode": {
        "params": {"mode": "string", "player": "string"},
        "description": "常态模式：follow / chat / free",
    },
    "mine": {"params": {"block": "string", "count": "number"}, "description": "挖方块"},
    "say": {"params": {"text": "string"}, "description": "在游戏里说一句"},
    "command": {
        "params": {"commands": "array"},
        "description": "内部动作：执行服务器命令（测试作弊审计用）",
    },
    "place": {
        "params": {"block": "string", "x": "number", "y": "number", "z": "number"},
        "description": "放一个方块",
    },
    "inspect_blocks": {
        "params": {"positions": "array"},
        "description": "批量读方块（蓝图校验用）",
    },
    "knowledge_query": {
        "params": {"query": "string", "name": "string"},
        "description": "内部动作：查配方/工具等级事实",
    },
    "configure_reflex": {
        "params": {"config": "object"},
        "description": "内部动作：更新反射配置",
    },
    "plugin_list": {"params": {}, "description": "列出示例插件"},
    "plugin_call": {
        "params": {"plugin": "string", "action": "string", "args": "object"},
        "description": "调用示例插件",
    },
}

CAPABILITIES = {
    "game": "mock",
    "display_name": "Mock Game",
    "protocol_version": PROTOCOL_VERSION,
    "adapter_version": "0.0.1-mock",
    "supported_versions": ["0.1"],
    "actions": ACTIONS,
    "limits": {"max_tasks": 4, "tick_hz": 20},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MockState:
    def __init__(self, token: str, heartbeat: float) -> None:
        self.token = token
        self.heartbeat = heartbeat
        self.session_id: str | None = None
        self.state = "idle"
        self.mode = "free"
        self.config: dict[str, Any] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self.buffered: deque[dict[str, Any]] = deque(maxlen=200)
        self.counter = 0
        self.blocks: dict[tuple[int, int, int], str] = {}
        self.heartbeat_task: asyncio.Task[None] | None = None
        self.started_at = time.monotonic()

    async def emit(self, payload: dict[str, Any]) -> None:
        payload.setdefault("event_id", f"{payload.get('type', 'event')}-{uuid.uuid4().hex[:10]}")
        payload.setdefault("ts", _now())
        if not self.queues:
            self.buffered.append(payload)
            return
        for queue in list(self.queues):
            queue.put_nowait(payload)

    async def finish_task(self, task_id: str, action: str, args: dict[str, Any]) -> None:
        await asyncio.sleep(0.05)
        task = self.tasks.get(task_id)
        if task is None or task.get("status") == "cancelled":
            return
        task["status"] = "done"
        # 先"说话"，再报任务完成 —— 更符合语义（她说了一句 → 这件事办完了）
        if action == "say":
            await self.emit(
                {"type": "chat", "from": "mock-friend", "text": str(args.get("text") or "")}
            )
        result: dict[str, Any] = {"action": action}
        if action == "command":
            result["commands"] = list(args.get("commands") or [])
        if action == "place":
            try:
                key = (int(args.get("x")), int(args.get("y")), int(args.get("z")))
                self.blocks[key] = str(args.get("block") or "air")
                result["placed"] = 1
            except (TypeError, ValueError):
                result["error"] = "坐标不合法"
        if action == "inspect_blocks":
            result["blocks"] = [
                {
                    "x": item.get("x"),
                    "y": item.get("y"),
                    "z": item.get("z"),
                    "name": self.blocks.get(
                        (
                            int(item.get("x") or 0),
                            int(item.get("y") or 0),
                            int(item.get("z") or 0),
                        ),
                        "air",
                    ),
                }
                for item in (args.get("positions") or [])
                if isinstance(item, dict)
            ]
        if action == "knowledge_query":
            query = str(args.get("query") or "")
            name = str(args.get("name") or "stone")
            result.update(
                {
                    "ok": True,
                    "query": query,
                    "name": name,
                    "source": "mock",
                    "recipes": [{"output": {"item": name, "count": 4}, "ingredients": []}]
                    if query == "recipe_for"
                    else [],
                    "minimum_tool": "wooden_pickaxe" if query == "tool_tier_for" else None,
                }
            )
        if action == "plugin_list":
            result["plugins"] = [
                {
                    "id": "inventory_bridge",
                    "name": "背包交互桥（示例）",
                    "available": True,
                    "actions": [
                        {"name": "inventory_summary", "effect": "read"},
                        {"name": "drop_item", "effect": "write"},
                        {"name": "use_held_item", "effect": "interact"},
                    ],
                }
            ]
        if action == "plugin_call":
            result.update(
                {
                    "ok": True,
                    "plugin": args.get("plugin"),
                    "action": args.get("action"),
                    "result": {"mock": True},
                }
            )
        await self.emit(
            {
                "type": "task_done",
                "task_id": task_id,
                "ok": True,
                "result": result,
            }
        )

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat)
            await self.emit({"type": "heartbeat"})


def create_app(token: str, heartbeat: float) -> FastAPI:
    app = FastAPI(title="yumeFriend mock game adapter")
    state = MockState(token, heartbeat)

    def require(request: Request) -> None:
        if request.headers.get("authorization") != f"Bearer {token}":
            raise HTTPException(
                status_code=401,
                detail={"code": "UNAUTHORIZED", "message": "适配器 token 不对"},
            )

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        require(request)
        return {
            "ok": True,
            "adapter_version": CAPABILITIES["adapter_version"],
            "protocol_version": PROTOCOL_VERSION,
            "uptime": round(time.monotonic() - state.started_at, 2),
        }

    @app.get("/capabilities")
    async def capabilities(request: Request) -> dict[str, Any]:
        require(request)
        return CAPABILITIES

    @app.post("/session/start")
    async def start_session(request: Request) -> dict[str, Any]:
        require(request)
        body = await request.json()
        if state.state == "running":
            raise HTTPException(
                status_code=409,
                detail={"code": "SESSION_EXISTS", "message": "已经有一局了"},
            )
        state.session_id = f"mock-{uuid.uuid4().hex[:8]}"
        state.state = "running"
        state.mode = "free"
        state.config = body.get("config") or {}
        state.blocks.clear()
        if state.heartbeat > 0 and state.heartbeat_task is None:
            state.heartbeat_task = asyncio.create_task(state.heartbeat_loop())
        await state.emit({"type": "status", "state": "running"})
        return {"session_id": state.session_id, "state": state.state}

    @app.post("/session/stop")
    async def stop_session(request: Request) -> dict[str, Any]:
        require(request)
        state.state = "ended"
        state.mode = "free"
        if state.heartbeat_task is not None:
            state.heartbeat_task.cancel()
            state.heartbeat_task = None
        return {"ok": True}

    @app.post("/command")
    async def command(request: Request) -> dict[str, Any]:
        require(request)
        body = await request.json()
        action = str(body.get("action") or "")
        args = body.get("args") or {}
        if state.state != "running":
            raise HTTPException(
                status_code=409,
                detail={"code": "NO_SESSION", "message": "还没开局"},
            )
        if action not in ACTIONS:
            raise HTTPException(
                status_code=400,
                detail={"code": "UNKNOWN_ACTION", "message": f"没有这个动作：{action}"},
            )
        if action == "mode":
            # D276：模式不是任务，不建 task_id、不发 task_done。
            state.mode = str(args.get("mode") or "free")
            return {"accepted": True, "mode": state.mode}
        if action == "configure_reflex":
            return {"accepted": True, "config": args.get("config") or {}}
        if action == "plugin_list":
            # 仍走任务流，方便主代码验证等待 task_done。
            pass
        state.counter += 1
        task_id = f"task-{state.counter}"
        state.tasks[task_id] = {"status": "running", "action": action}
        asyncio.create_task(state.finish_task(task_id, action, args))
        return {"accepted": True, "task_id": task_id}

    @app.post("/cancel")
    async def cancel(request: Request) -> dict[str, Any]:
        require(request)
        body = await request.json()
        task = state.tasks.get(str(body.get("task_id") or ""))
        if task is not None:
            task["status"] = "cancelled"
        return {"ok": True}

    @app.get("/snapshot")
    async def snapshot(request: Request) -> dict[str, Any]:
        require(request)
        return {
            "state": state.state,
            "mode": state.mode,
            "player": {"name": "mock-friend", "x": 0, "y": 64, "z": 0, "health": 20},
            "inventory": [
                {"item": "stone", "count": 3},
                {"item": "stone_pickaxe", "count": 1},
            ],
            "nearby": [],
            "tasks": [
                {"task_id": key, **value} for key, value in state.tasks.items()
            ],
            "config": state.config,
        }

    @app.websocket("/events")
    async def events(websocket: WebSocket) -> None:
        if websocket.query_params.get("token") != token:
            await websocket.close(code=4401)
            return
        await websocket.accept()

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        while state.buffered:
            queue.put_nowait(state.buffered.popleft())
        state.queues.add(queue)

        async def sender() -> None:
            while True:
                payload = await queue.get()
                await websocket.send_text(json.dumps(payload, ensure_ascii=False))

        sender_task = asyncio.create_task(sender())
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            sender_task.cancel()
            state.queues.discard(queue)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="yumeFriend mock game adapter")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--heartbeat", type=float, default=5.0)
    args = parser.parse_args()
    uvicorn.run(
        create_app(args.token, args.heartbeat),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
