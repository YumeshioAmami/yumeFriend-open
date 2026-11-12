"""生活线（v7.12 / D341，v7.17 / D360）：她自己的真实活动与连续生活项目。

设计边界：
  · 只有电脑端会写这些表；手机端只读；
  · 活动必须有真实工具结果，不能是模型编的“我在看书”；
  · 生活项目只记录真实读到/看到的内容和她说过的想法，不凭空生成经历；
  · 删除朋友时通过 FK CASCADE 一起物理删除，不留 orphan。
"""

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class LifeProject(Base):
    """一条会延续几天的生活线索：话题 / 读到的内容 / 她的想法 / 未完成问题。"""

    __tablename__ = "life_projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )
    topic: Mapped[str] = mapped_column(String, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active"
    )  # active / resting / completed
    source: Mapped[str] = mapped_column(String, nullable=False, default="")
    step_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reading_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_depth: Mapped[str] = mapped_column(
        String, nullable=False, default=""
    )  # full_text / snippet / metadata / none
    last_thought: Mapped[str] = mapped_column(Text, nullable=False, default="")
    open_question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    last_activity_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_shared_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_life_projects_friend_time", "friend_id", "last_activity_at"),
        Index("idx_life_projects_friend_status", "friend_id", "status"),
    )


class LifeActivity(Base):
    """一次真实活动：开始 / 结束 / 观察 / 工具证据。"""

    __tablename__ = "life_activities"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("life_projects.id", ondelete="SET NULL"),
        nullable=True,
    )
    device_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    kind: Mapped[str] = mapped_column(
        String, nullable=False
    )  # bilibili_ranking / web_read / project_followup
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="running"
    )  # running / completed / failed / cancelled
    started_at: Mapped[str] = mapped_column(String, nullable=False)
    ends_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    observations: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    tool_calls: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_life_activities_friend_time", "friend_id", "started_at"),
    )


class FriendLife(Base):
    """每个朋友一行：当前活动指针 + 聊天暂停窗口。"""

    __tablename__ = "friend_life"

    friend_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("friends.id", ondelete="CASCADE"),
        primary_key=True,
    )
    current_activity_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("life_activities.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_activity_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_observation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # v7.12（D343）：活动新鲜观察，等主动消息 tick 决定要不要分享；分享后清空。
    pending_share: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_share_at: Mapped[str | None] = mapped_column(String, nullable=True)
    paused_by_chat: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    paused_until: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
