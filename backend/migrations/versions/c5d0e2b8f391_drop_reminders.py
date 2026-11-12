"""drop reminders（v4.0 / D243）

Revision ID: c5d0e2b8f391
Revises: b4e9c1a7d283
Create Date: 2026-10-09

日程功能整体切换完成（扫描 / 工具 / API 全走 `schedules`）→ 老 `reminders` 表没人读了。
数据早在 `b4e9c1a7d283` 搬进 `schedules`，这里**只删表**（删除要彻底：不留孤儿 / 死表）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d0e2b8f391"
down_revision: Union[str, Sequence[str], None] = "b4e9c1a7d283"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_reminders_due")
    op.execute("DROP TABLE IF EXISTS reminders")


def downgrade() -> None:
    """Downgrade schema：把 v1 的 reminders 表结构补回来（数据不回填，已搬进 schedules）。"""
    op.create_table(
        "reminders",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("friend_id", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("trigger_time", sa.String(), nullable=False),
        sa.Column("is_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("deleted_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["friend_id"], ["friends.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_reminders_due",
        "reminders",
        ["trigger_time"],
        sqlite_where=sa.text("is_completed = 0 AND deleted_at IS NULL"),
    )
