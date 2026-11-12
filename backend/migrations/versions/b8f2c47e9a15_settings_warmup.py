"""settings 定时预热字段（v2.0，D87）

Revision ID: b8f2c47e9a15
Revises: f6c1a2b3d4e5
Create Date: 2026-09-19

纯加列，带 server_default（老库升级后 = 开启 + 5 分钟，跟新库一致），不需要数据迁移。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8f2c47e9a15"
down_revision: Union[str, Sequence[str], None] = "f6c1a2b3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "settings",
        sa.Column("warmup_enabled", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "settings",
        sa.Column("warmup_interval_minutes", sa.Integer(), nullable=False, server_default="5"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("warmup_interval_minutes")
        batch_op.drop_column("warmup_enabled")
