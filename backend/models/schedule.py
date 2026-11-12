"""schedules / schedule_events：日程系统（v4.0 / D243、D244）。

日程 = **她和你的约定**：零录入（一句话）· 她主动 · 不催不烦。

两张表的分工：
  · `schedules`       = **母表，唯一真相源**（reminder / 小纸条都只是它的派生，单向写回）；
  · `schedule_events` = 动过日程的**留痕**（append-only，同 D230 的 `memory_events`）。

两个刻意设计：
  · `date` / `time` 存**本地值**（`YYYY-MM-DD` / `HH:MM`）—— 本项目唯一不遵守"UTC ISO8601"
    的地方：日历日不能被 UTC 挪一天（D244）。其余时间戳仍是 UTC ISO8601。
  · `schedule_events.schedule_id` **只存字符串、不设外键** —— 日程被物理清理时，
    "她动过什么"这条证据还得在（同 `memory_events.memory_id`）。
"""

from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base

# 允许的取值（service 层强校验；这里只列出来，别散落各处）
NATURES = ("start", "due", "appointment", "remind")
STATES = ("pending", "active", "settled", "archived")
OWNERS = ("user", "friend")
SOURCES = ("chat", "ui", "migration")
SETTLED_REASONS = ("done", "dropped")


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)  # 这件事是什么（一句话）
    nature: Mapped[str] = mapped_column(String, nullable=False, default="remind")
    date: Mapped[str] = mapped_column(String, nullable=False)  # 本地 YYYY-MM-DD
    time: Mapped[str | None] = mapped_column(String, nullable=True)  # 本地 HH:MM；空 = 只到日
    advance_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 提前量

    state: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    owner: Mapped[str] = mapped_column(String, nullable=False, default="user")
    source: Mapped[str] = mapped_column(String, nullable=False, default="chat")

    first_said_at: Mapped[str | None] = mapped_column(String, nullable=True)
    settled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    settled_reason: Mapped[str | None] = mapped_column(String, nullable=True)  # done / dropped

    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index(
            "idx_schedules_due",
            "date",
            "time",
            sqlite_where=text("state = 'pending' AND deleted_at IS NULL"),
        ),
        Index("idx_schedules_friend", "friend_id", "state", "date"),
    )


class ScheduleEvent(Base):
    """日程改动留痕（append-only）：谁（actor）对哪条（schedule_id）做了什么（action）。"""

    __tablename__ = "schedule_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )
    schedule_id: Mapped[str | None] = mapped_column(String, nullable=True)  # 无外键，见文件头
    actor: Mapped[str] = mapped_column(String, nullable=False)  # agent / system / user
    action: Mapped[str] = mapped_column(String, nullable=False)
    before: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON：改了的字段 → 旧值
    after: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON：→ 新值
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_schedule_events_friend", "friend_id", "created_at"),)
