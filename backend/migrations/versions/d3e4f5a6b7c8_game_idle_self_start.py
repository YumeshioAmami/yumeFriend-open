"""settings.game_idle_self_start_seconds / cooldown（v5.0 / D268）

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-18

纯加列：空闲自启动阈值 + 不做后的冷却；0 = 关。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "settings",
        sa.Column(
            "game_idle_self_start_seconds",
            sa.Integer(),
            nullable=False,
            server_default="180",
        ),
    )
    op.add_column(
        "settings",
        sa.Column(
            "game_idle_self_start_cooldown_seconds",
            sa.Integer(),
            nullable=False,
            server_default="300",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("game_idle_self_start_cooldown_seconds")
        batch_op.drop_column("game_idle_self_start_seconds")
