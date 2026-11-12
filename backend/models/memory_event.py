"""memory_events 表：动过记忆的留痕（D230）。

append-only：朋友 / 系统 / 用户每次**改**（内容 / importance / layer）或**删**一条记忆都写一行；
新建也写（create）。用来回答"朋友最近动过我什么记忆"。

两个刻意设计：
  · `friend_id` 有外键 + 级联 —— 物理删朋友时一起清，不留 orphan（宪法§9/§13）。
  · `memory_id` **只存字符串、不设外键** —— 记忆 30 天后被物理清理时，
    "她删过什么"这条证据还得在（外键的话会被级联删掉，留痕就没了）。
"""

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class MemoryEvent(Base):
    __tablename__ = "memory_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )
    memory_id: Mapped[str | None] = mapped_column(String, nullable=True)  # 无外键，见文件头
    actor: Mapped[str] = mapped_column(String, nullable=False)  # agent / system / user
    action: Mapped[str] = mapped_column(String, nullable=False)  # create / update / delete
    before: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON（改了哪些字段 → 旧值）
    after: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON（→ 新值）
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_memory_events_friend", "friend_id", "created_at"),
    )
