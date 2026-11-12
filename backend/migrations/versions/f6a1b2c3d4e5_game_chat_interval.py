"""settings.game_chat_interval_seconds：游戏里主动说话的间隔（v5.0 / D255）

Revision ID: f6a1b2c3d4e5
Revises: e4c7b1a9d2f6
Create Date: 2026-10-18

纯加列，默认 90 秒；0 = 关。老数据自动用默认值。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = "e4c7b1a9d2f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "settings",
        sa.Column(
            "game_chat_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default="90",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("game_chat_interval_seconds")
