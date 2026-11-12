"""功能事件订阅（v7.20 / D373）。

这里只放"某功能发事件、另一个功能接住"的胶水。
它让 `api/messages.py` 不用再直接 import `life_service` ——
聊天只负责发 `chat.started` / `chat.finished`，生活线自己决定要不要暂停。
"""

from __future__ import annotations

from loguru import logger

from backend.features import events


def _on_chat_started(event: dict) -> None:
    friend_id = str(event.get("friend_id") or "")
    if not friend_id:
        return
    from backend.services import life_service  # noqa: PLC0415 —— 避免启动期循环

    life_service.chat_started(friend_id)


def _on_chat_finished(event: dict) -> None:
    friend_id = str(event.get("friend_id") or "")
    if not friend_id:
        return
    from backend.services import life_service  # noqa: PLC0415

    life_service.chat_finished(friend_id)


def _on_life_activity_finished(event: dict) -> None:
    """预留的协调点：生活线活动结束 → 其他功能可以在这里接。

    目前只记日志，证明事件真的走通了；以后小世界 / 记忆想接就 subscribe，
    不用改生活线。
    """
    logger.debug(
        "生活线活动结束：friend={} activity={} ok={}",
        str(event.get("friend_id") or "")[:8],
        str(event.get("activity_id") or "")[:8],
        event.get("ok"),
    )


def install() -> None:
    """幂等安装（重复调用不会重复订阅）。"""
    events.subscribe("chat.started", _on_chat_started)
    events.subscribe("chat.finished", _on_chat_finished)
    events.subscribe("life.activity.finished", _on_life_activity_finished)
