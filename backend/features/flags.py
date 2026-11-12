"""功能开关（v7.20 / D373）。

开关存在 `settings.features` 这一个 JSON 列里，形如：

    {"life": false, "game": true}

没写的功能 = 用它自己的 `default_enabled`。核心功能始终为开。

进程内有一份缓存：HTTP 中间件每个请求都要判断功能是否开着，
不能每次都查库。写开关时同步更新缓存；启动时 `refresh()` 一次。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from backend.features.catalog import BY_ID, CORE_FEATURE_ID, FEATURES

_cache: set[str] | None = None


def default_flags() -> dict[str, bool]:
    return {feature.id: (True if feature.core else feature.default_enabled) for feature in FEATURES}


def parse_flags(raw: str | None) -> dict[str, bool]:
    """把数据库里的 JSON 解析成"每个功能都有值"的完整字典。"""
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    result = default_flags()
    for feature in FEATURES:
        if feature.core:
            result[feature.id] = True
            continue
        value = data.get(feature.id)
        if isinstance(value, bool):
            result[feature.id] = value
    return result


def enabled_ids_from_flags(flags: dict[str, bool]) -> set[str]:
    return {feature_id for feature_id, enabled in flags.items() if enabled}


def enabled_ids_from_row(row: Any) -> set[str]:
    return enabled_ids_from_flags(parse_flags(getattr(row, "features", None)))


def refresh(session: Session) -> set[str]:
    """从 DB 重新读一遍开关，并写进缓存。"""
    global _cache
    from backend.services import settings_service  # noqa: PLC0415 —— 避免循环

    ids = enabled_ids_from_row(settings_service.get(session))
    _cache = ids
    return ids


def set_cache(ids: set[str]) -> None:
    global _cache
    _cache = set(ids)


def invalidate() -> None:
    global _cache
    _cache = None


def snapshot() -> set[str] | None:
    """当前缓存。`None` = 还没初始化（调用方应 fail-open）。"""
    return _cache


def enabled(feature_id: str) -> bool:
    """某个功能现在是否开着。缓存没初始化时一律放行（fail-open）。"""
    if feature_id == CORE_FEATURE_ID:
        return True
    cached = _cache
    if cached is None:
        return True
    return feature_id in cached


def tool_enabled(tool_name: str) -> bool:
    from backend.features.catalog import feature_for_tool  # noqa: PLC0415

    owner = feature_for_tool(tool_name)
    return True if owner is None else enabled(owner)


def prompt_block_enabled(block: str) -> bool:
    from backend.features.catalog import feature_for_prompt_block  # noqa: PLC0415

    owner = feature_for_prompt_block(block)
    return True if owner is None else enabled(owner)


def job_enabled(job_id: str) -> bool:
    from backend.features.catalog import feature_for_job  # noqa: PLC0415

    owner = feature_for_job(job_id)
    return True if owner is None else enabled(owner)


def public_features(enabled_ids: set[str]) -> list[dict]:
    """给前端的功能清单。"""
    return [
        {
            "id": feature.id,
            "name": feature.name,
            "description": feature.description,
            "category": feature.category,
            "core": feature.core,
            "default_enabled": feature.default_enabled,
            "enabled": True if feature.core else feature.id in enabled_ids,
            "ui_entries": list(feature.ui_entries),
        }
        for feature in FEATURES
    ]


def known_feature(feature_id: str) -> bool:
    return feature_id in BY_ID
