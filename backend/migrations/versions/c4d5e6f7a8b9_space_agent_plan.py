"""小世界 Phase D：目标 / 多步计划 / 动作结果

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-11-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("friend_space_runtime", sa.Column("goal", sa.String(), nullable=True))
    op.add_column(
        "friend_space_runtime",
        sa.Column("plan", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "friend_space_runtime",
        sa.Column("plan_step", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "friend_space_runtime",
        sa.Column("last_action_result", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("friend_space_runtime", "last_action_result")
    op.drop_column("friend_space_runtime", "plan_step")
    op.drop_column("friend_space_runtime", "plan")
    op.drop_column("friend_space_runtime", "goal")
