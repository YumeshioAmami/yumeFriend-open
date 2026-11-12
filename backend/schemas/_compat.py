"""Pydantic v1 / v2 兼容小工具。

桌面端用 pydantic v2；Android 的 Chaquopy 端为了避开 pydantic-core 的原生轮子，
固定使用 pydantic v1。业务代码只通过这两个函数取字段集合 / dump。
"""

from __future__ import annotations

from typing import Any


def fields_set(model: Any) -> set[str]:
    value = getattr(model, "model_fields_set", None)
    if value is None:
        value = getattr(model, "__fields_set__", set())
    return set(value)


def model_dump(model: Any, **kwargs: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    return model.dict(**kwargs)
