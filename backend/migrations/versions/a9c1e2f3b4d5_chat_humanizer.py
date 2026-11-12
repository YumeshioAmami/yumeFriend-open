"""chat pacing / humanizer flags + friend expressions（v7.13 / D344）

Revision ID: a9c1e2f3b4d5
Revises: e3f4a5b6c7d8
Create Date: 2026-10-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a9c1e2f3b4d5"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "friends",
        sa.Column("chat_pacing_enabled", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "friends",
        sa.Column("humanizer_enabled", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "friend_expressions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("style", sa.Text(), nullable=False),
        sa.Column("situation", sa.Text(), nullable=False, server_default=""),
        sa.Column("score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("source", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index(
        "idx_friend_expressions_friend",
        "friend_expressions",
        ["friend_id", "updated_at"],
    )
    op.execute(
        "UPDATE friends SET chat_pacing_enabled = 1, humanizer_enabled = 1 "
        "WHERE name = '汐音'"
    )


def downgrade() -> None:
    op.drop_index("idx_friend_expressions_friend", table_name="friend_expressions")
    op.drop_table("friend_expressions")
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.drop_column("humanizer_enabled")
        batch_op.drop_column("chat_pacing_enabled")
