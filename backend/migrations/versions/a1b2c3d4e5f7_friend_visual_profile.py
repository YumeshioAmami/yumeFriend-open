"""friends.visual_mode / live2d_model_id / portrait_pack_id（v7.43 / D441）

Revision ID: a1b2c3d4e5f7
Revises: d5e6f7a8b9c0
Create Date: 2026-11-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "friends",
        sa.Column("visual_mode", sa.String(), nullable=False, server_default="live2d"),
    )
    op.add_column("friends", sa.Column("live2d_model_id", sa.String(), nullable=True))
    op.add_column("friends", sa.Column("portrait_pack_id", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.drop_column("portrait_pack_id")
        batch_op.drop_column("live2d_model_id")
        batch_op.drop_column("visual_mode")
