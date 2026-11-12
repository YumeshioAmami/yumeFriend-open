"""memory_events 表：动过记忆的留痕（D230）

Revision ID: d1f4a7c25b90
Revises: c4e7a1b9d2f5
Create Date: 2026-10-03

**纯加表**，不动老数据（老库升级后 memory_events 是空的）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f4a7c25b90"
down_revision: Union[str, Sequence[str], None] = "c4e7a1b9d2f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "memory_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("friend_id", sa.String(), nullable=False),
        sa.Column("memory_id", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["friend_id"], ["friends.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_memory_events_friend", "memory_events", ["friend_id", "created_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_memory_events_friend", table_name="memory_events")
    op.drop_table("memory_events")
