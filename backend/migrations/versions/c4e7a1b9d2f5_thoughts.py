"""thoughts 表：小纸条 / 念头（D192）

Revision ID: c4e7a1b9d2f5
Revises: a7c1f0d94e21
Create Date: 2026-09-27

**纯加表**，不动老数据（老库升级后 thoughts 是空的）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e7a1b9d2f5"
down_revision: Union[str, Sequence[str], None] = "a7c1f0d94e21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "thoughts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("friend_id", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="casual"),
        sa.Column("due_at", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("source", sa.String(), nullable=False, server_default="model"),
        sa.Column("said_at", sa.String(), nullable=True),
        sa.Column("expires_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["friend_id"], ["friends.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_thoughts_alive",
        "thoughts",
        ["friend_id", "state"],
        sqlite_where=sa.text("state IN ('pending','awaiting')"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_thoughts_alive", table_name="thoughts")
    op.drop_table("thoughts")
