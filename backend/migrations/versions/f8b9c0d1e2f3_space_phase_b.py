"""小世界 Phase B：持续运行态 + 物件知识

Revision ID: f8b9c0d1e2f3
Revises: e7a8b9c0d1e2
Create Date: 2026-10-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "e7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "friend_space_runtime",
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("attention", sa.String(), nullable=False, server_default="busy"),
        sa.Column("intent", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("interrupt_stack", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("opening_stage", sa.String(), nullable=False, server_default="waiting"),
        sa.Column("opening_target", sa.String(), nullable=True),
        sa.Column("opening_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_perception_at", sa.String(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "space_object_knowledge",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("user_explanation", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.UniqueConstraint("friend_id", "object_key", name="uq_space_knowledge"),
    )
    op.create_index(
        "ix_space_object_knowledge_friend_id",
        "space_object_knowledge",
        ["friend_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_space_object_knowledge_friend_id", table_name="space_object_knowledge")
    op.drop_table("space_object_knowledge")
    op.drop_table("friend_space_runtime")
