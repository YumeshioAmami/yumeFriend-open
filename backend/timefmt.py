"""时间小工具（P1 / D176）。

为什么单独一个模块：占位符（`agent/context.py`）和"翻相册"（`agent/tools.py`）都要把
**UTC ISO 时间戳**转成**用户本地**的 `MM-DD HH:MM` —— 放这儿两边都 import 得到，不打架。

为什么一定要转本地：原来占位符是拿 UTC 字符串切出来的月日（`created_at[5:10]`）——
半夜发的图（北京 00:30 = UTC 前一天 16:30）会显示成**前一天**，用户说"昨天那张"就对不上。
"""

from __future__ import annotations

from datetime import datetime, timezone


def local_stamp(created_at: str | None) -> str:
    """UTC ISO8601 → 本地 `MM-DD HH:MM`。解析不了就退回旧的"切月日"写法。"""
    if not created_at:
        return "?"
    try:
        at = datetime.fromisoformat(created_at)
    except ValueError:
        return created_at[5:10] if len(created_at) >= 10 else "?"
    if at.tzinfo is None:  # 老数据可能是不带时区的
        at = at.replace(tzinfo=timezone.utc)
    return f"{at.astimezone():%m-%d %H:%M}"


def local_stamp_full(created_at: str | None, reference: datetime | None = None) -> str:
    """UTC ISO8601 → 本地时间戳，**历史消息每条都用它**（v3.31 / D216）。

    跟 `local_stamp` 的区别：**跨年时带上年份**（`YYYY-MM-DD HH:MM`），当年只写 `MM-DD HH:MM`。
    为什么：朋友要算"这话是多久以前说的" —— 同一个 `09-28` 在跨年时会有歧义。

    `reference` = 本轮对话的"现在"（本地 naive），用来判是不是同一年；不给就用 `datetime.now()`。
    """
    if not created_at:
        return "?"
    try:
        at = datetime.fromisoformat(created_at)
    except ValueError:
        return local_stamp(created_at)
    if at.tzinfo is None:  # 老数据可能是不带时区的 → 按 UTC 处理（跟 local_stamp 一致）
        at = at.replace(tzinfo=timezone.utc)
    local = at.astimezone()
    ref = reference or datetime.now()
    if local.year != ref.year:
        return f"{local:%Y-%m-%d %H:%M}"
    return f"{local:%m-%d %H:%M}"
