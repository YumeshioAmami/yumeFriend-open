"""settings 去掉看屏的随机区间（v2.2 / D103）

Revision ID: c8e1d47a2f60
Revises: b2d8f14c7a63
Create Date: 2026-09-20

看屏不再有独立定时器了（D103）：它变成"主动消息的第一步"，频率跟主动消息共用一个数字，
所以 `screen_watch_min_minutes` / `screen_watch_max_minutes` 没用了 —— 直接删列。
`screen_watch_enabled` 保留（含义变成"她找你说话之前先看一眼屏幕"）。
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8e1d47a2f60"
down_revision: Union[str, Sequence[str], None] = "b2d8f14c7a63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("screen_watch_max_minutes")
        batch_op.drop_column("screen_watch_min_minutes")


def downgrade() -> None:
    """Downgrade schema."""
    import sqlalchemy as sa

    op.add_column(
        "settings",
        sa.Column("screen_watch_min_minutes", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "settings",
        sa.Column("screen_watch_max_minutes", sa.Integer(), nullable=False, server_default="20"),
    )
