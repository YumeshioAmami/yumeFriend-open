"""说话方式档案的请求体（D347）。"""

from typing import Literal

from pydantic import BaseModel


class StyleChangeIn(BaseModel):
    action: Literal["add", "update", "remove"]
    section: Literal["length", "tone", "diction", "structure", "situations", "keep"]
    lesson: str | None = None
    target_id: str | None = None
    why: str | None = None


class StyleChangesIn(BaseModel):
    changes: list[StyleChangeIn]
    summary: str | None = None


class StyleRollbackIn(BaseModel):
    event_id: str
