"""friend_episodes + friends.last_reflection_at（V6.0-3 / D297）

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-10-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "friends",
        sa.Column("last_reflection_at", sa.String(), nullable=True),
    )
    op.create_table(
        "friend_episodes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("meaning", sa.Text(), nullable=True),
        sa.Column("tone", sa.String(), nullable=True),
        sa.Column("unresolved", sa.Text(), nullable=True),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("evidence", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("source_message_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("occurred_at", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=True),
        sa.Column("deleted_at", sa.String(), nullable=True),
    )
    op.create_index(
        "idx_friend_episodes_friend_time",
        "friend_episodes",
        ["friend_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_friend_episodes_friend_time", table_name="friend_episodes")
    op.drop_table("friend_episodes")
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.drop_column("last_reflection_at")
