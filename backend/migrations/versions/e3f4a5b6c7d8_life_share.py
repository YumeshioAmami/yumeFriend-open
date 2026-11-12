"""friend_life.pending_share / last_share_at（v7.12 / D343）

生活线活动产生真实观察后，先挂成 pending_share；主动消息 tick 在合适时机
用 1~3 个短气泡分享，分享后清空并记录 last_share_at。

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-10-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "friend_life",
        sa.Column("pending_share", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column("friend_life", sa.Column("last_share_at", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("friend_life", schema=None) as batch_op:
        batch_op.drop_column("last_share_at")
        batch_op.drop_column("pending_share")
