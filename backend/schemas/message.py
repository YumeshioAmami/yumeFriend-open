"""消息相关的请求体（api.md §2.1 与【v2】2）。"""

from pydantic import BaseModel, Field


class MessageCreate(BaseModel):
    # v2（T2.0-6）：可以只发图不说话 → 下限放到 0；"总得有点什么"交给校验器
    content: str = Field(default="", max_length=20000)
    # v2（T2.0-6）：先经 `/api/uploads/image` 传上来拿到的 id
    image_id: str | None = Field(default=None, max_length=64)
    # v2.1（T2.1-5 / D100）：true = 后端**截一张屏**，当成这条消息带的图
    screen: bool = False

    # 内容校验放在 API 层（pydantic v1 / v2 都能跑；见 backend/api/messages.py）
