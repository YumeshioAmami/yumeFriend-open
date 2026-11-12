"""memories 表：记忆（tag × layer 双维度，D9）。"""

from sqlalchemy import Float, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)
    tag: Mapped[str] = mapped_column(String, nullable=False)  # normal/rp/rp_play_style/notebook
    layer: Mapped[str] = mapped_column(String, nullable=False)  # pinned/permanent/long_term/short_term
    context: Mapped[str | None] = mapped_column(String, nullable=True)  # 角色名
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)

    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)  # 仅 short_term
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    refreshed_at: Mapped[str] = mapped_column(String, nullable=False)  # 去重命中时刷新（D28）
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("idx_memories_friend_layer", "friend_id", "layer"),
        Index("idx_memories_expires", "expires_at", sqlite_where=text("expires_at IS NOT NULL")),
        Index(
            "idx_memories_playstyle",
            "friend_id",
            "context",
            unique=True,
            sqlite_where=text("tag = 'rp_play_style' AND deleted_at IS NULL"),
        ),
    )
