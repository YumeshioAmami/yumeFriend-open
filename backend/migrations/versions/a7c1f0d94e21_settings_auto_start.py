"""settings.auto_start：开机自启做成设置项（v3.15 / 2026-09-26）

Revision ID: a7c1f0d94e21
Revises: f1a3c8d72b46
Create Date: 2026-09-26

纯加列，带 server_default：默认 0（**关**）。不替用户塞自启。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c1f0d94e21"
down_revision: Union[str, Sequence[str], None] = "f1a3c8d72b46"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "settings",
        sa.Column("auto_start", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("auto_start")
