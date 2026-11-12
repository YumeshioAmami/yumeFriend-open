"""messages.image_note：给每张图存一句"内容索引"（P4 / D176）

Revision ID: f1a3c8d72b46
Revises: e9c2a47b15d3
Create Date: 2026-09-23

**纯加列、可空** —— 老数据全是 NULL（= 还没看清过这张图），不需要数据迁移。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a3c8d72b46"
down_revision: Union[str, Sequence[str], None] = "e9c2a47b15d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("messages", sa.Column("image_note", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_column("image_note")
