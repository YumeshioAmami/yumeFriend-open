"""messages.mood：这一轮她用什么表情（v2.2 / T2.2-3 / D102）

Revision ID: b2d8f14c7a63
Revises: a1c4e7b93d52
Create Date: 2026-09-20

纯加列（可空），不需要数据迁移。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2d8f14c7a63"
down_revision: Union[str, Sequence[str], None] = "a1c4e7b93d52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("messages", sa.Column("mood", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_column("mood")
