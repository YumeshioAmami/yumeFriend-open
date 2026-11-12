"""life line + server sync metadata + message origin device

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-10-25

纯加列 / 加表；第一期只把已有的「汐音」打开生活线（D341）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "friends",
        sa.Column("life_enabled", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "messages",
        sa.Column("origin_device_id", sa.String(), nullable=True),
    )
    op.create_table(
        "life_activities",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_id", sa.String(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("started_at", sa.String(), nullable=False),
        sa.Column("ends_at", sa.String(), nullable=True),
        sa.Column("finished_at", sa.String(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("observations", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("tool_calls", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index(
        "idx_life_activities_friend_time",
        "life_activities",
        ["friend_id", "started_at"],
    )
    op.create_table(
        "friend_life",
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "current_activity_id",
            sa.String(),
            sa.ForeignKey("life_activities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_activity_at", sa.String(), nullable=True),
        sa.Column("last_observation", sa.Text(), nullable=False, server_default=""),
        sa.Column("paused_by_chat", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused_until", sa.String(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    # 用户明确要求第一期只给汐音开生活线；其余朋友保持默认关。
    op.execute("UPDATE friends SET life_enabled = 1 WHERE name = '汐音'")


def downgrade() -> None:
    op.drop_table("friend_life")
    op.drop_index("idx_life_activities_friend_time", table_name="life_activities")
    op.drop_table("life_activities")
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_column("origin_device_id")
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.drop_column("life_enabled")
