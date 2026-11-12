"""friends.voice_reply_enabled + messages 语音字段（v2.0，T2.0-5 / D85）

Revision ID: f6c1a2b3d4e5
Revises: e5a2c96d1b74
Create Date: 2026-09-19

纯加列，**不需要数据迁移**：
· `voice_reply_enabled` 默认 0（关）→ 老用户行为完全不变
· messages 的语音字段全可空 → 没有语音就是 NULL
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6c1a2b3d4e5"
down_revision: Union[str, Sequence[str], None] = "e5a2c96d1b74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "friends",
        sa.Column(
            "voice_reply_enabled", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column("messages", sa.Column("audio_status", sa.String(), nullable=True))
    op.add_column("messages", sa.Column("audio_path", sa.String(), nullable=True))
    op.add_column("messages", sa.Column("audio_duration_ms", sa.Integer(), nullable=True))
    op.add_column("messages", sa.Column("audio_error", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_column("audio_error")
        batch_op.drop_column("audio_duration_ms")
        batch_op.drop_column("audio_path")
        batch_op.drop_column("audio_status")
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.drop_column("voice_reply_enabled")
