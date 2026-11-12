"""friends.temperature：每个朋友自己的"温度"（v4.3 / D249）

Revision ID: a3f8c2d41b07
Revises: c5d0e2b8f391
Create Date: 2026-10-13

**纯加列、可空、无默认值** —— 老数据全是 NULL（= 跟随默认：主对话 0.7 / 主动消息 0.8），
不需要数据迁移，行为跟以前一模一样。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f8c2d41b07"
down_revision: Union[str, Sequence[str], None] = "c5d0e2b8f391"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("friends", sa.Column("temperature", sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.drop_column("temperature")
