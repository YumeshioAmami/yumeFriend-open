"""功能间事件总线（v7.20 / D373）。

规则：**功能模块之间不许直接 import 对方的 service**；要通知就发事件。
本模块只做进程内、同步/异步混合的极薄一层：

- `emit(name, **payload)`：同步发，handler 抛错只记日志，绝不影响主流程；
- `emit_async(name, **payload)`：测试 / 需要等待时用；
- handler 可以是普通函数，也可以是 async 函数（同步发时会 schedule 到当前 loop）。

事件名统一用 `领域.动作`，例如 `chat.started` / `life.activity.finished`。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable

from loguru import logger

Handler = Callable[[dict[str, Any]], Any]

_handlers: dict[str, list[Handler]] = {}


def subscribe(event: str, handler: Handler) -> None:
    handlers = _handlers.setdefault(event, [])
    if handler not in handlers:
        handlers.append(handler)


def unsubscribe(event: str, handler: Handler) -> None:
    handlers = _handlers.get(event)
    if handlers and handler in handlers:
        handlers.remove(handler)
    if handlers is not None and not handlers:
        _handlers.pop(event, None)


def clear() -> None:
    """测试用：把订阅清干净。"""
    _handlers.clear()


def _payload(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"event": event, **payload}


def emit(event: str, **payload: Any) -> None:
    """发一个事件。**任何 handler 炸了都只记日志，绝不外抛。**"""
    body = _payload(event, payload)
    for handler in list(_handlers.get(event, ())):
        try:
            result = handler(body)
            if inspect.isawaitable(result):
                try:
                    asyncio.get_running_loop().create_task(result)
                except RuntimeError:
                    # 没有运行中的 loop（脚本 / 测试同步上下文）→ 关掉，别留下未 await 的警告
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
        except Exception:  # noqa: BLE001 —— 事件失败不能影响主流程
            logger.opt(exception=True).warning("事件 handler 执行失败（已忽略）：{}", event)


async def emit_async(event: str, **payload: Any) -> None:
    """发一个事件并等待所有 handler（测试用；生产主流程用 `emit`）。"""
    body = _payload(event, payload)
    for handler in list(_handlers.get(event, ())):
        try:
            result = handler(body)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).warning("事件 handler 执行失败（已忽略）：{}", event)
