"""messages.image_path / image_no（v2.0，T2.0-6 / D82）

Revision ID: c4d9e13a6b27
Revises: b8f2c47e9a15
Create Date: 2026-09-19

纯加列（可空），不需要数据迁移：老消息没有图就是 NULL。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d9e13a6b27"
down_revision: Union[str, Sequence[str], None] = "b8f2c47e9a15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("messages", sa.Column("image_path", sa.String(), nullable=True))
    op.add_column("messages", sa.Column("image_no", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_column("image_no")
        batch_op.drop_column("image_path")
