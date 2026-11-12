"""朋友学到的说话方式档案（D347）。

这是**当前有效的说话规则**，不是普通事实记忆，也不是人格覆盖。
原始反馈留在 `friend_style_events`，注入 prompt 的只有压缩后的档案。
"""

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class FriendStyleProfile(Base):
    __tablename__ = "friend_style_profiles"

    friend_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("friends.id", ondelete="CASCADE"),
        primary_key=True,
    )
    profile_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class FriendStyleEvent(Base):
    __tablename__ = "friend_style_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("friends.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False, default="reflect")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    changes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    assistant_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    before_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    after_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    before_profile_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    after_profile_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_friend_style_events_friend", "friend_id", "created_at"),
    )
