"""游戏陪伴表（v5.0 / D250、D251）。

· `game_bindings`：每个朋友在每个游戏里的身份/偏好（她叫什么、默认连哪）；
· `game_sessions`：一局（含滚动日志 / 最终摘要 / 摘要状态）；
· `game_tasks`：她下达的每条指令（留档，不靠内存）；
· `game_events`：append-only 事实流 —— **摘要的唯一依据**（D236：不许编）。
"""

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class GameBinding(Base):
    __tablename__ = "game_bindings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )
    game_kind: Mapped[str] = mapped_column(String, nullable=False)
    account_name: Mapped[str | None] = mapped_column(String, nullable=True)
    settings: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        UniqueConstraint("friend_id", "game_kind", name="uq_game_bindings_friend_kind"),
    )


class GameSession(Base):
    __tablename__ = "game_sessions"

    # 主键直接用适配器返回的 session_id（mc-xxxx）——一局一个，天然去重
    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )
    game_kind: Mapped[str] = mapped_column(String, nullable=False)
    binding_id: Mapped[str | None] = mapped_column(String, nullable=True)

    status: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    journal: Mapped[str | None] = mapped_column(Text, nullable=True)  # 滚动摘要
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_memory_id: Mapped[str | None] = mapped_column(String, nullable=True)
    summary_state: Mapped[str] = mapped_column(
        String, nullable=False, default="pending"
    )  # pending / done / failed
    error: Mapped[str | None] = mapped_column(String, nullable=True)

    started_at: Mapped[str] = mapped_column(String, nullable=False)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_game_sessions_friend_started", "friend_id", "started_at"),
        Index("idx_game_sessions_summary", "summary_state"),
    )


class GameTask(Base):
    __tablename__ = "game_tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("game_sessions.id", ondelete="CASCADE"), nullable=False
    )
    adapter_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    args: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    result: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("idx_game_tasks_session", "session_id", "created_at"),
        UniqueConstraint("session_id", "adapter_task_id", name="uq_game_tasks_adapter_id"),
    )


class GameEvent(Base):
    __tablename__ = "game_events"

    # 用适配器的 event_id 当主键 → 至少一次投递也不会重复
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("game_sessions.id", ondelete="CASCADE"), nullable=False
    )
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_game_events_session", "session_id", "created_at"),
        Index("idx_game_events_friend", "friend_id", "created_at"),
    )
