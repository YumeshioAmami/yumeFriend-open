"""小世界：friend_spaces + space_events（D374）

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "friend_spaces",
        sa.Column("friend_id", sa.String(), nullable=False),
        sa.Column("layout", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("mode", sa.String(), nullable=False, server_default="free"),
        sa.Column("current_action", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["friend_id"], ["friends.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("friend_id"),
    )
    op.create_table(
        "space_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("friend_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("notable", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["friend_id"], ["friends.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_space_events_friend_id", "space_events", ["friend_id"], unique=False)
    op.create_index("ix_space_events_kind", "space_events", ["kind"], unique=False)
    op.create_index("ix_space_events_created_at", "space_events", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_space_events_created_at", table_name="space_events")
    op.drop_index("ix_space_events_kind", table_name="space_events")
    op.drop_index("ix_space_events_friend_id", table_name="space_events")
    op.drop_table("space_events")
    op.drop_table("friend_spaces")
