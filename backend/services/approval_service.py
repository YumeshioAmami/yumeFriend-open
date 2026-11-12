"""工具审批（v2.1 / T2.1-4 / D99）：写操作要用户点头。

**这个模块最容易写成死锁**，所以口径写死在这里：

1. 先把 `approval_request` **推给前端**（在事件循环里 await）
2. 再 `await asyncio.to_thread(event.wait, timeout)` —— **等在线程里**，事件循环自由，
   用户点「允许」的那个 HTTP 请求才进得来
3. 用户点了（`resolve`）→ 置位 Event → 回到这里继续
4. 超时（默认 180 秒）= **拒绝**（宁可少做，不可乱做）

取消/没人在线也没关系：超时即拒绝。
"""

from __future__ import annotations

import asyncio
import threading
import uuid

from loguru import logger

from backend.api.ws import manager

APPROVAL_TIMEOUT_SECONDS = 180.0

_pending: dict[str, dict] = {}
_lock = threading.Lock()


def pending_count() -> int:
    with _lock:
        return len(_pending)


def resolve(request_id: str, *, allow: bool) -> bool:
    """用户在界面上点了「允许 / 拒绝」。返回是否找到了这条待办。"""
    with _lock:
        record = _pending.get(request_id)
        if record is not None:
            record["allow"] = bool(allow)
            record["event"].set()
    if record is None:
        logger.warning("审批 {} 不存在（可能超时了）", request_id[:8])
        return False
    logger.info("审批 {}：{}", request_id[:8], "允许" if allow else "拒绝")
    return True


def cancel_all() -> None:
    """进程退出时把没答完的都当拒绝（别让工具线程一直挂着）。"""
    with _lock:
        records = list(_pending.values())
    for record in records:
        record["allow"] = False
        record["event"].set()


async def ask(
    *,
    friend_id: str,
    friend_name: str,
    tool: str,
    summary: str,
    detail: str = "",
    timeout: float = APPROVAL_TIMEOUT_SECONDS,
) -> bool:
    """推一条审批事件，然后等用户点头。**超时 = 拒绝。**"""
    request_id = uuid.uuid4().hex
    event = threading.Event()
    with _lock:
        _pending[request_id] = {"event": event, "allow": False}
    try:
        await manager.broadcast(
            {
                "type": "approval_request",
                "id": request_id,
                "friend_id": friend_id,
                "friend_name": friend_name,
                "tool": tool,
                "summary": summary,
                "detail": detail,
                "expires_in": int(timeout),
            }
        )
        answered = await asyncio.to_thread(event.wait, timeout)
    finally:
        with _lock:
            record = _pending.pop(request_id, None)
    if not answered:
        logger.info("审批 {} 超时（当拒绝）", request_id[:8])
        return False
    return bool(record and record["allow"])
