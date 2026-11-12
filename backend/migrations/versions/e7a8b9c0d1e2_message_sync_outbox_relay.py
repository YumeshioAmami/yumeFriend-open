"""消息同步待推送队列 + 服务器转发水位（D382）

Revision ID: e7a8b9c0d1e2
Revises: c9d0e1f2a3b4
Create Date: 2026-10-31
"""

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_message_outbox",
        sa.Column(
            "message_id",
            sa.String(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("changed_at", sa.String(), nullable=False),
    )
    op.create_table(
        "sync_message_relay",
        sa.Column(
            "message_id",
            sa.String(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("relayed_at", sa.String(), nullable=False),
    )

    # 升级前本机可能已经有“服务器没收到”的旧消息；先全部当作待推送一次。
    # 服务器会按 id upsert，重复推送是幂等的。
    op.execute(
        sa.text(
            "INSERT INTO sync_message_outbox(message_id, changed_at) "
            "SELECT id, COALESCE(updated_at, created_at) FROM messages"
        )
    )

    # 服务器升级后把所有现有消息标成一次待转发，让其他设备拥有一次完整对齐机会。
    now = datetime.now(timezone.utc).isoformat()
    op.execute(
        sa.text(
            "INSERT INTO sync_message_relay(message_id, relayed_at) "
            "SELECT id, :now FROM messages"
        ).bindparams(now=now)
    )


def downgrade() -> None:
    op.drop_table("sync_message_relay")
    op.drop_table("sync_message_outbox")
