"""游戏会话服务（P0：进程 + 契约 + 状态机 + 任务/事件；**不接 LLM、不写库**）。

D250 的口径：
· 一次只有一个朋友、一局；
· 适配器独立进程，崩了只影响这一局；
· 主代码只认契约（`AdapterClient`），不认识任何具体游戏。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from loguru import logger

from backend.game.client import AdapterClient
from backend.game.contract import Capabilities, GameEvent
from backend.game.process import AdapterProcess
from backend.game import registry

EventHook = Callable[["GameSession", GameEvent], Awaitable[None] | None]

# 会话状态机（D250）：idle → starting → running → stopping → ended / crashed
STATES = ("starting", "running", "stopping", "ended", "crashed")


@dataclass
class GameSession:
    id: str
    friend_id: str
    game_kind: str
    state: str
    # D276：常态模式（follow / chat / free），不是任务。
    mode: str = "free"
    capabilities: Capabilities | None = None
    config: dict[str, Any] = field(default_factory=dict)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 适配器的 task_done 可能比 /command 的 HTTP 响应先到；先把完成结果暂存，
    # 等 command() 建好任务行后再补上状态（否则任务会永远停在 running）。
    completed_tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[GameEvent] = field(default_factory=list)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    last_event_at: float = field(default_factory=time.monotonic)
    adapter_pid: int | None = None

    _client: AdapterClient | None = None
    _process: AdapterProcess | None = None
    _events_task: asyncio.Task[None] | None = None
    _heartbeat_task: asyncio.Task[None] | None = None

    @property
    def adapter_alive(self) -> bool:
        return self._process is not None and self._process.pid is not None


class GameService:
    def __init__(
        self,
        *,
        heartbeat_timeout: float = 20.0,
        max_events: int = 500,
        on_event: EventHook | None = None,
    ) -> None:
        self._session: GameSession | None = None
        self._heartbeat_timeout = heartbeat_timeout
        self._max_events = max_events
        self._on_event = on_event

    @property
    def session(self) -> GameSession | None:
        return self._session

    async def start(
        self,
        *,
        friend_id: str,
        game_kind: str,
        command: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        config: Mapping[str, Any] | None = None,
        on_session_ready: Callable[[GameSession], Awaitable[None]] | None = None,
    ) -> GameSession:
        """起一局。`command` 不给就走注册表（`registry.command_for(game_kind)`）。"""
        current = self._session
        if current is not None and current.state in ("starting", "running", "stopping"):
            raise RuntimeError("同一时间只允许一局（D250）")

        if command is None:
            command = registry.command_for(game_kind)
        process = AdapterProcess(command, env=env)
        session = GameSession(
            id=str(uuid.uuid4()),
            friend_id=friend_id,
            game_kind=game_kind,
            state="starting",
            config=dict(config or {}),
        )
        try:
            client = await process.start()
            capabilities = await client.capabilities()
            if capabilities.game != game_kind:
                raise RuntimeError(
                    f"适配器自报的游戏是 {capabilities.game!r}，跟要开的 {game_kind!r} 不一致"
                )
            started = await client.start_session(session.config)
            session_id = str(started.get("session_id") or "").strip()
            if session_id:
                session.id = session_id
        except Exception:
            await process.stop()
            raise

        session.capabilities = capabilities
        session.adapter_pid = process.pid
        session._client = client
        session._process = process
        session.state = "running"
        session.last_event_at = time.monotonic()
        self._session = session

        # D280：先把会话行落库，再开始消费事件。
        # 否则适配器登录后的第一条 player_near 可能比 create_session 先到，
        # game_events 的外键会失败，事件流直接断掉（表现为"她进游戏了但软件不认"）。
        if on_session_ready is not None:
            try:
                await on_session_ready(session)
            except Exception:
                await process.stop()
                raise

        session._events_task = asyncio.create_task(
            self._consume_events(session), name=f"game-events-{session.id[:8]}"
        )
        session._heartbeat_task = asyncio.create_task(
            self._watch_heartbeat(session), name=f"game-heartbeat-{session.id[:8]}"
        )
        logger.info(
            "游戏会话已开始：{} / {}（session={} pid={}）",
            friend_id,
            game_kind,
            session.id[:8],
            session.adapter_pid,
        )
        return session

    async def command(
        self,
        action: str,
        args: Mapping[str, Any] | None = None,
        *,
        origin: str = "auto",
    ) -> dict[str, Any]:
        session, client = self._running()
        result = await client.command(action, args)
        task_id = str(result.get("task_id") or "").strip()
        if task_id:
            task = {
                "action": action,
                "args": dict(args or {}),
                "origin": origin,
                "status": "running",
            }
            early = session.completed_tasks.pop(task_id, None)
            if early is not None:
                task["status"] = "done" if early.get("ok", True) else "failed"
                task["result"] = early.get("result")
            session.tasks[task_id] = task
        return result

    async def cancel(self, task_id: str) -> dict[str, Any]:
        session, client = self._running()
        result = await client.cancel(task_id)
        task = session.tasks.get(task_id)
        if task is not None:
            task["status"] = "cancelled"
        return result

    async def snapshot(self) -> dict[str, Any]:
        _session, client = self._running()
        return await client.snapshot()

    async def stop(self) -> None:
        session = self._session
        if session is None or session.state == "ended":
            return
        session.state = "stopping"

        for task in (session._heartbeat_task, session._events_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        session._heartbeat_task = None
        session._events_task = None

        client = session._client
        if client is not None:
            try:
                await asyncio.wait_for(client.stop_session(), timeout=3.0)
            except Exception as exc:  # noqa: BLE001 —— 收尾失败不该挡住停进程
                logger.warning("适配器 /session/stop 没成功（继续收进程）：{}", exc)

        if session._process is not None:
            await session._process.stop()
            session._process = None
        session._client = None
        session.state = "ended"
        logger.info("游戏会话已结束：session={}", session.id[:8])

    def status(self) -> dict[str, Any] | None:
        session = self._session
        if session is None:
            return None
        return {
            "id": session.id,
            "friend_id": session.friend_id,
            "game_kind": session.game_kind,
            "state": session.state,
            "started_at": session.started_at,
            "mode": session.mode,
            "error": session.error,
            "adapter_pid": session.adapter_pid,
            "adapter_alive": session.adapter_alive,
            "tasks": {key: dict(value) for key, value in session.tasks.items()},
            "capabilities": (
                {
                    "game": session.capabilities.game,
                    "adapter_version": session.capabilities.adapter_version,
                    "supported_versions": list(session.capabilities.supported_versions),
                    "actions": list(session.capabilities.action_names()),
                }
                if session.capabilities is not None
                else None
            ),
        }

    # ---------- 内部 ----------

    def _running(self) -> tuple[GameSession, AdapterClient]:
        session = self._session
        if session is None or session.state != "running" or session._client is None:
            state = session.state if session is not None else "idle"
            raise RuntimeError(f"现在没有进行中的游戏会话（state={state}）")
        return session, session._client

    async def _consume_events(self, session: GameSession) -> None:
        client = session._client
        if client is None:
            return
        try:
            async for event in client.events():
                await self._handle_event(session, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 断流只影响这一局
            if session.state == "running":
                session.state = "crashed"
                session.error = f"事件流断了：{exc}"
                logger.warning("游戏事件流出错（会话标 crashed）：{}", exc)
        else:
            if session.state == "running":
                session.state = "crashed"
                session.error = "事件流被适配器关闭"
                logger.warning("游戏事件流被适配器关闭（会话标 crashed）")

    async def _handle_event(self, session: GameSession, event: GameEvent) -> None:
        session.last_event_at = time.monotonic()
        session.events.append(event)
        if len(session.events) > self._max_events:
            del session.events[: len(session.events) - self._max_events]

        if event.type == "task_done":
            task_id = str(event.payload.get("task_id") or "")
            task = session.tasks.get(task_id)
            if task is not None:
                ok = bool(event.payload.get("ok", True))
                task["status"] = "done" if ok else "failed"
                task["result"] = event.payload.get("result")
            elif task_id:
                # 先到一步的完成事件：command() 稍后补状态
                session.completed_tasks[task_id] = {
                    "ok": bool(event.payload.get("ok", True)),
                    "result": event.payload.get("result"),
                }
                if len(session.completed_tasks) > 200:
                    session.completed_tasks.pop(next(iter(session.completed_tasks)))
        elif event.type == "error":
            message = str(event.payload.get("message") or "")
            logger.warning("游戏适配器上报错误：{}", message)
            # 适配器进程还活着，但 Minecraft 连接已经断了：
            # 必须把会话标成 crashed，否则面板会一直显示 running，看起来像"她站着不动"。
            if "连接结束了" in message or "被服务器踢了" in message:
                if session.state == "running":
                    session.state = "crashed"
                    session.error = message
                    logger.warning("游戏连接已断开，会话标 crashed：{}", message)

        if self._on_event is not None:
            result = self._on_event(session, event)
            if inspect.isawaitable(result):
                await result

    async def _watch_heartbeat(self, session: GameSession) -> None:
        interval = max(0.05, min(self._heartbeat_timeout / 4.0, 1.0))
        try:
            while session.state == "running":
                await asyncio.sleep(interval)
                if session.state != "running":
                    return
                if time.monotonic() - session.last_event_at > self._heartbeat_timeout:
                    session.state = "crashed"
                    session.error = f"适配器 {self._heartbeat_timeout:g}s 没有心跳"
                    logger.warning("游戏适配器心跳超时，会话标 crashed")
                    if session._events_task is not None:
                        session._events_task.cancel()
                    return
        except asyncio.CancelledError:
            raise
