"""messages.audio_segments：分段语音清单（v2.2 / D105）

Revision ID: d9f3b62e8c15
Revises: c8e1d47a2f60
Create Date: 2026-09-20

纯加列（可空，JSON 文本）：`[{"index":0,"path":"audio/<mid>/0.wav","duration_ms":1200,"text":"…"}]`
—— 沉浸模式靠它"一段一段说"；老消息 / 主动消息没有这一列的内容 → 前端退回单文件。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9f3b62e8c15"
down_revision: Union[str, Sequence[str], None] = "c8e1d47a2f60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("messages", sa.Column("audio_segments", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_column("audio_segments")
