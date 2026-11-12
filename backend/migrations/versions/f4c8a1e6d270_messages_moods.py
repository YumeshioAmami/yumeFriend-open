"""messages.moods：她每段给的情绪（v2.2 / D113）

Revision ID: f4c8a1e6d270
Revises: e2b7c14d9f83
Create Date: 2026-09-20

纯加列（可空，JSON 数组）：`["正常","生气","害羞"]`，按段的顺序 —— 沉浸模式按段换立绘。

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4c8a1e6d270"
down_revision: Union[str, Sequence[str], None] = "e2b7c14d9f83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("messages", sa.Column("moods", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_column("moods")
