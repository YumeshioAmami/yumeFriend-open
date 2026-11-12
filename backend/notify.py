"""进程内的「有事发生了」通知钩子（后端 → 桌面外壳）。

**为什么需要它**：桌面外壳（pywebview）想知道"朋友主动冒泡了 / 提醒到点了"，
但 `services/` 不该反向 import `desktop/`（那是倒着依赖）。
所以这里放一张极薄的登记表：

    桌面外壳启动 → register(handler)
    消息落库之后 → emit(event)

没有 handler 的时候（例如开发时只跑 `python -m backend.main`）= **什么都不发生**，
不会报错、也不会影响业务流程。这一点很重要：通知永远不该拖垮主流程。
"""

from __future__ import annotations

from typing import Any, Callable

from loguru import logger

Handler = Callable[[dict[str, Any]], None]

_handlers: list[Handler] = []


def register(handler: Handler) -> None:
    if handler not in _handlers:
        _handlers.append(handler)


def unregister(handler: Handler) -> None:
    if handler in _handlers:
        _handlers.remove(handler)


def clear() -> None:
    """测试用：把钩子清干净。"""
    _handlers.clear()


def emit(event: dict[str, Any]) -> None:
    """通知所有 handler。**任何一个炸了都只记一笔日志，绝不往外抛**。"""
    for handler in list(_handlers):
        try:
            handler(event)
        except Exception:  # noqa: BLE001 —— 通知失败不能影响消息本身
            logger.opt(exception=True).warning("通知钩子执行失败（已忽略）")
