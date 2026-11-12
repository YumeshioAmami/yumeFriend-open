"""minecraft-data 知识查询缓存（v5.1-P6 / D288）。

适配器负责读打包的 minecraft-data；这里只做短期缓存，避免同一轮反复问。
"""

from __future__ import annotations

import time
from typing import Any, Mapping

TTL_SECONDS = 300.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _key(query: str, name: str) -> str:
    return f"{query}:{name.strip().lower()}"


def get(query: str, name: str) -> dict[str, Any] | None:
    key = _key(query, name)
    hit = _cache.get(key)
    if hit is None:
        return None
    stored_at, value = hit
    if time.monotonic() - stored_at > TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return dict(value)


def remember(query: str, name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    _cache[_key(query, name)] = (time.monotonic(), result)
    if len(_cache) > 300:
        oldest = sorted(_cache.items(), key=lambda item: item[1][0])[:50]
        for key, _ in oldest:
            _cache.pop(key, None)
    return result


def clear() -> None:
    _cache.clear()
