"""schedules + schedule_events 表 + thoughts.schedule_id + 搬 reminders（v4.0 / D243、D244）

Revision ID: b4e9c1a7d283
Revises: e7b2c9d41a60
Create Date: 2026-10-09

**这一步只"加"不"删"**：
  · 建 `schedules`（母表）/ `schedule_events`（留痕）；
  · `thoughts` 加 `schedule_id`；
  · 把现有 `reminders` **搬进** `schedules`（一次性、可重跑：已搬过的 id 不重复插）；
  · **暂不 drop `reminders`** —— 旧代码还指着它，等日程功能整体切换（v4.0-4/5）后再删，
    届时另起一个迁移。这样每一步都能跑测试、都能回滚。

`trigger_time`（UTC ISO8601）→ `date` + `time`（**本机本地值**，D244）：
日历日不能被 UTC 挪一天，所以这里 `astimezone()` 到本机时区再取日期 / 时刻。
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e9c1a7d283"
down_revision: Union[str, Sequence[str], None] = "e7b2c9d41a60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _to_local(trigger_time: str) -> tuple[str, str | None]:
    """UTC ISO8601 → (本地 YYYY-MM-DD, 本地 HH:MM)。解析不了就当"只到日"用原字符串前 10 位。"""
    try:
        moment = datetime.fromisoformat(str(trigger_time).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        text = str(trigger_time or "")
        return (text[:10] or "1970-01-01", None)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone()
    return (local.strftime("%Y-%m-%d"), local.strftime("%H:%M"))


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "schedules",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("friend_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("nature", sa.String(), nullable=False, server_default="remind"),
        sa.Column("date", sa.String(), nullable=False),
        sa.Column("time", sa.String(), nullable=True),
        sa.Column("advance_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("owner", sa.String(), nullable=False, server_default="user"),
        sa.Column("source", sa.String(), nullable=False, server_default="chat"),
        sa.Column("first_said_at", sa.String(), nullable=True),
        sa.Column("settled_at", sa.String(), nullable=True),
        sa.Column("settled_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=True),
        sa.Column("deleted_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["friend_id"], ["friends.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_schedules_due",
        "schedules",
        ["date", "time"],
        sqlite_where=sa.text("state = 'pending' AND deleted_at IS NULL"),
    )
    op.create_index("idx_schedules_friend", "schedules", ["friend_id", "state", "date"])

    op.create_table(
        "schedule_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("friend_id", sa.String(), nullable=False),
        sa.Column("schedule_id", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["friend_id"], ["friends.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_schedule_events_friend", "schedule_events", ["friend_id", "created_at"])

    op.add_column("thoughts", sa.Column("schedule_id", sa.String(), nullable=True))

    _move_reminders()


def _move_reminders() -> None:
    """把 reminders 一次性搬进 schedules（幂等：id 已存在就跳过）。"""
    bind = op.get_bind()
    tables = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    }
    if "reminders" not in tables:
        return  # 全新库（没建过 reminders）→ 没东西可搬

    rows = bind.execute(
        sa.text(
            "SELECT id, friend_id, content, trigger_time, is_completed, completed_at,"
            " created_at, deleted_at FROM reminders"
        )
    ).fetchall()
    for row in rows:
        (
            rid,
            friend_id,
            content,
            trigger_time,
            is_completed,
            completed_at,
            created_at,
            deleted_at,
        ) = row
        # 朋友可能已经被物理删了 → 外键会挂，直接跳过（CASCADE 本该已清，双保险）
        exists = bind.execute(
            sa.text("SELECT 1 FROM friends WHERE id = :fid"), {"fid": friend_id}
        ).fetchone()
        if exists is None:
            continue
        if bind.execute(
            sa.text("SELECT 1 FROM schedules WHERE id = :sid"), {"sid": rid}
        ).fetchone():
            continue
        day, clock = _to_local(trigger_time)
        if is_completed:
            state, settled_at, settled_reason = "settled", completed_at, "done"
        else:
            state, settled_at, settled_reason = "pending", None, None
        bind.execute(
            sa.text(
                "INSERT INTO schedules (id, friend_id, title, nature, date, time,"
                " advance_minutes, state, owner, source, first_said_at, settled_at,"
                " settled_reason, created_at, updated_at, deleted_at)"
                " VALUES (:id, :friend_id, :title, 'remind', :date, :time, 0, :state,"
                " 'user', 'migration', NULL, :settled_at, :settled_reason, :created_at,"
                " NULL, :deleted_at)"
            ),
            {
                "id": rid,
                "friend_id": friend_id,
                "title": content,
                "date": day,
                "time": clock,
                "state": state,
                "settled_at": settled_at,
                "settled_reason": settled_reason,
                "created_at": created_at,
                "deleted_at": deleted_at,
            },
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("thoughts", schema=None) as batch_op:
        batch_op.drop_column("schedule_id")
    op.drop_index("idx_schedule_events_friend", table_name="schedule_events")
    op.drop_table("schedule_events")
    op.drop_index("idx_schedules_friend", table_name="schedules")
    op.drop_index("idx_schedules_due", table_name="schedules")
    op.drop_table("schedules")
