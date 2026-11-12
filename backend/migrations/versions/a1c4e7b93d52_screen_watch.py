"""settings 定时看屏字段（v2.1 / T2.1-5 / D100）

Revision ID: a1c4e7b93d52
Revises: f7a9c2d64b31
Create Date: 2026-09-20

纯加列，带 server_default：默认**关**（要用户自己开）+ 间隔 10~20 分钟。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c4e7b93d52"
down_revision: Union[str, Sequence[str], None] = "f7a9c2d64b31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "settings",
        sa.Column("screen_watch_enabled", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "settings",
        sa.Column("screen_watch_min_minutes", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "settings",
        sa.Column("screen_watch_max_minutes", sa.Integer(), nullable=False, server_default="20"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("screen_watch_max_minutes")
        batch_op.drop_column("screen_watch_min_minutes")
        batch_op.drop_column("screen_watch_enabled")
