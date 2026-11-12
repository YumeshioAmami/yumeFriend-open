"""蓝图库与自由创作记录（v5.1-P5 / D289）。"""

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class GameBlueprint(Base):
    __tablename__ = "game_blueprints"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source: Mapped[str] = mapped_column(
        String, nullable=False, default="learned", server_default="learned"
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_game_blueprints_friend", "friend_id", "name"),
    )


class GameCreativeRecord(Base):
    __tablename__ = "game_creative_records"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )
    brief: Mapped[str] = mapped_column(Text, nullable=False, default="")
    design: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    anchor: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    size: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    before: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    after: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    diff: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(
        String, nullable=False, default="designing", server_default="designing"
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_game_creative_friend", "friend_id", "created_at"),
    )
