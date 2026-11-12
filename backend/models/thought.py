"""thoughts 表：小纸条 / 念头（D192）。

跟 `memories` 的分工（这是设计核心）：
  · `memories` = **长期事实**（"他叫梦汐""他在做 yumeFriend"）—— 一直在；
  · `thoughts` = **"我此刻想跟他说的一件具体的事"** —— 有始有终：
      待说（还没说）→ 待回（说了、他还没回应）→ 销账 / 到期作废。

用户 2026-09-27 拍板：纸条要有明确的"进 / 说 / 销账"规则，否则要么积一堆没人用、要么躺着占位。
"""

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class Thought(Base):
    __tablename__ = "thoughts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 什么时候拿出来说：casual=随缘 / due=到点（v1 就这两档；"等某个事件"等触发器落地再加）
    kind: Mapped[str] = mapped_column(String, nullable=False, default="casual")
    due_at: Mapped[str | None] = mapped_column(String, nullable=True)  # kind=due 用
    # v4.0（D243）：从哪条日程派生（可空，**不设外键**，同 memory_events.memory_id）
    schedule_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # pending=待说 / awaiting=待回（说了他没回）/ done=销账 / expired=作废
    state: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    source: Mapped[str] = mapped_column(String, nullable=False, default="model")  # model | auto

    said_at: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index(
            "idx_thoughts_alive",
            "friend_id",
            "state",
            sqlite_where=text("state IN ('pending','awaiting')"),
        ),
    )
