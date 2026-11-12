"""friends.avatar_path：朋友头像（v2.0，T2.0-3）

Revision ID: c2b8e5a71f03
Revises: b1a7f3c2d4e5
Create Date: 2026-09-19

纯加列（可空），**不需要数据迁移**：没有头像就是 NULL，界面回退成首字母色块。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2b8e5a71f03"
down_revision: Union[str, Sequence[str], None] = "b1a7f3c2d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("friends", sa.Column("avatar_path", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.drop_column("avatar_path")
