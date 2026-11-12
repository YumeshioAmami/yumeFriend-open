"""小世界 Phase B v2.1：micro 深思节拍

Revision ID: b3c4d5e6f7a8
Revises: a9d0e1f2b3c4
Create Date: 2026-11-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "a9d0e1f2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "friend_space_runtime",
        sa.Column("last_deliberation_at", sa.String(), nullable=True),
    )
    op.add_column(
        "friend_space_runtime",
        sa.Column("next_deliberation_at", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("friend_space_runtime", "next_deliberation_at")
    op.drop_column("friend_space_runtime", "last_deliberation_at")
