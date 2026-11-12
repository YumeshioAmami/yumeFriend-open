"""character runtime: friend_states（V6.0-1 / D296）

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-10-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "friend_states",
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("affect", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("energy", sa.Float(), nullable=False, server_default="0.55"),
        sa.Column("concerns", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("intent", sa.String(), nullable=False, server_default="observe"),
        sa.Column("inner_note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "relationship_note", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("open_loops", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("active_thread", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("friend_states")
