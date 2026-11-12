"""游戏技能书（v5.1-P4 / D288）。

· `game_skills`：内置技能（friend_id = NULL）与每个朋友学到的技能；
· `game_skill_failures`：失败复盘笔记，只用于下一次改进，不混进技能检索。
"""

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class GameSkill(Base):
    __tablename__ = "game_skills"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    params: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    preconditions: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    steps: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    postconditions: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    versions: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    mods: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    stats: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    source: Mapped[str] = mapped_column(
        String, nullable=False, default="learned", server_default="learned"
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_game_skills_friend_name", "friend_id", "name"),
        Index("idx_game_skills_source", "source"),
    )


class GameSkillFailure(Base):
    __tablename__ = "game_skill_failures"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[str | None] = mapped_column(String, nullable=True)
    skill_name: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_game_skill_failures_friend", "friend_id", "created_at"),
    )
