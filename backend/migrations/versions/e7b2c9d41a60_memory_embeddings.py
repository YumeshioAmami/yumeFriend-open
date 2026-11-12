"""memory_embeddings 表：记忆向量（D231）

Revision ID: e7b2c9d41a60
Revises: d1f4a7c25b90
Create Date: 2026-10-03

**纯加表**，不动老数据（老库升级后是空的，由后台回填补齐）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b2c9d41a60"
down_revision: Union[str, Sequence[str], None] = "d1f4a7c25b90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "memory_embeddings",
        sa.Column("memory_id", sa.String(), nullable=False),
        sa.Column("friend_id", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["friend_id"], ["friends.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("memory_id"),
    )
    op.create_index("idx_memory_embeddings_friend", "memory_embeddings", ["friend_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_memory_embeddings_friend", table_name="memory_embeddings")
    op.drop_table("memory_embeddings")
