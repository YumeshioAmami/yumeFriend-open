"""settings.voice_keep_days：消息语音保留多少天（v3.0 / D141）

Revision ID: c7e1a4b9d203
Revises: f4c8a1e6d270
Create Date: 2026-09-21

纯加列，带 server_default：默认 **30 天**（0 = 永久留着）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e1a4b9d203"
down_revision: Union[str, Sequence[str], None] = "f4c8a1e6d270"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "settings",
        sa.Column("voice_keep_days", sa.Integer(), nullable=False, server_default="30"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("voice_keep_days")
