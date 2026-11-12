"""settings.file_write_approval：文件写操作逐笔点头（v2.1 / T2.1-4 / D99）

Revision ID: f7a9c2d64b31
Revises: e6b3f82a71c9
Create Date: 2026-09-20

纯加列，带 server_default=1（**默认要点头**），不需要数据迁移。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a9c2d64b31"
down_revision: Union[str, Sequence[str], None] = "e6b3f82a71c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "settings",
        sa.Column("file_write_approval", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("file_write_approval")
