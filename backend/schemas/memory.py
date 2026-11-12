"""记忆相关的请求体（api.md §3）。"""

from typing import Literal

from pydantic import BaseModel, Field


class MemoryCreate(BaseModel):
    friend_id: str
    content: str = Field(min_length=1, max_length=2000)
    # 用户可指定任意 layer；不填默认 pinned（data_model §4：UI 默认 pinned）
    layer: Literal["pinned", "permanent", "long_term", "short_term"] = "pinned"
    tag: Literal["normal", "rp", "rp_play_style", "notebook"] = "notebook"
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    context: str | None = None


class MemoryUpdate(BaseModel):
    """可改内容 / 重要度 / **layer（升·降级，D230）** —— **`tag` 不可改**。"""

    content: str | None = Field(default=None, min_length=1, max_length=2000)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    layer: Literal["pinned", "permanent", "long_term", "short_term"] | None = None
