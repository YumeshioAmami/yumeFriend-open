"""内存环形缓冲：日志窗口的实时数据源（api.md §6 / D31）。

**双写**：这个缓冲（最近 2000 条，给窗口实时读）+ 日志文件（轮转，给导出和崩溃追溯）。

隐私（D31 第 5 条）：
  · **工具调用的参数会进日志**（调试必需，是刻意开的例外）
  · **但聊天正文不进日志** —— 这不是这里管的，是调用方不许把正文塞进 log 消息
  · **API key 永不记** —— 除了调用方自律，这里再加一道正则兜底
"""

from __future__ import annotations

import itertools
import re
from collections import deque
from typing import Any

RING_SIZE = 2000

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_buffer: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)
_counter = itertools.count(1)

# 兜底：万一哪条消息里带了 key，落库前打码
_SECRET = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})")


def _redact(text: str) -> str:
    return _SECRET.sub("sk-***", text)


def add(*, level: str, message: str, where: str, time_text: str) -> dict[str, Any]:
    entry = {
        "seq": next(_counter),
        "time": time_text,
        "level": level,
        "where": where,
        "message": _redact(message),
    }
    _buffer.append(entry)
    return entry


def snapshot(
    *, since: int = 0, level: str | None = None, limit: int = 500
) -> tuple[list[dict[str, Any]], int]:
    """增量读取：只要 seq > since 的；可再按级别过滤。返回 `(items, next_seq)`。"""
    wanted = level.upper() if level else None
    items = [
        entry
        for entry in _buffer
        if entry["seq"] > since and (wanted is None or entry["level"] == wanted)
    ]
    if len(items) > limit:
        items = items[-limit:]
    next_seq = max((entry["seq"] for entry in items), default=since)
    return items, next_seq


def clear() -> None:
    _buffer.clear()


def size() -> int:
    return len(_buffer)


def recent_errors(limit: int = 1) -> list[dict[str, Any]]:
    return [e for e in reversed(_buffer) if e["level"] in ("ERROR", "CRITICAL")][
        :limit
    ]


def tail(limit: int = 20) -> list[dict[str, Any]]:
    return list(_buffer)[-limit:]


def sink(message: Any) -> None:  # loguru 的 sink 签名：收一个 Message
    record = message.record
    add(
        level=record["level"].name,
        message=record["message"],
        where=f"{record['name']}:{record['function']}",
        time_text=record["time"].astimezone().isoformat(timespec="seconds"),
    )
