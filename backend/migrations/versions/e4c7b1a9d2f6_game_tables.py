"""game_* 四张表（v5.0 / D250、D251）

Revision ID: e4c7b1a9d2f6
Revises: a3f8c2d41b07
Create Date: 2026-10-18

纯新增表，不动老数据。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4c7b1a9d2f6"
down_revision: Union[str, Sequence[str], None] = "a3f8c2d41b07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "game_bindings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("game_kind", sa.String(), nullable=False),
        sa.Column("account_name", sa.String(), nullable=True),
        sa.Column("settings", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.UniqueConstraint("friend_id", "game_kind", name="uq_game_bindings_friend_kind"),
    )

    op.create_table(
        "game_sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("game_kind", sa.String(), nullable=False),
        sa.Column("binding_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("config", sa.Text(), nullable=True),
        sa.Column("journal", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_memory_id", sa.String(), nullable=True),
        sa.Column("summary_state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("started_at", sa.String(), nullable=False),
        sa.Column("ended_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index(
        "idx_game_sessions_friend_started", "game_sessions", ["friend_id", "started_at"]
    )
    op.create_index("idx_game_sessions_summary", "game_sessions", ["summary_state"])

    op.create_table(
        "game_tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(),
            sa.ForeignKey("game_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("adapter_task_id", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("args", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("finished_at", sa.String(), nullable=True),
        sa.UniqueConstraint(
            "session_id", "adapter_task_id", name="uq_game_tasks_adapter_id"
        ),
    )
    op.create_index("idx_game_tasks_session", "game_tasks", ["session_id", "created_at"])

    op.create_table(
        "game_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(),
            sa.ForeignKey("game_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("idx_game_events_session", "game_events", ["session_id", "created_at"])
    op.create_index("idx_game_events_friend", "game_events", ["friend_id", "created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_game_events_friend", table_name="game_events")
    op.drop_index("idx_game_events_session", table_name="game_events")
    op.drop_table("game_events")
    op.drop_index("idx_game_tasks_session", table_name="game_tasks")
    op.drop_table("game_tasks")
    op.drop_index("idx_game_sessions_summary", table_name="game_sessions")
    op.drop_index("idx_game_sessions_friend_started", table_name="game_sessions")
    op.drop_table("game_sessions")
    op.drop_table("game_bindings")
