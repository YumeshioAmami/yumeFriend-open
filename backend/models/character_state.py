"""角色运行时状态（V6.0-1 / D296）。

一个朋友一行，保存“她此刻是谁、她此刻想怎样”。
这不是用户写的人格；人格仍然是 friends.personality，系统不改它。
"""

from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class FriendState(Base):
    __tablename__ = "friend_states"

    friend_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("friends.id", ondelete="CASCADE"),
        primary_key=True,
    )
    affect: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    energy: Mapped[float] = mapped_column(Float, nullable=False, default=0.55)
    concerns: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    intent: Mapped[str] = mapped_column(String, nullable=False, default="observe")
    inner_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    relationship_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    open_loops: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    active_thread: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
