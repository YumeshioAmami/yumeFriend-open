"""messages.sources：搜网来源（v2.0，T2.0-7 / D92）

Revision ID: d5e7a1c39f28
Revises: c4d9e13a6b27
Create Date: 2026-09-19

纯加列（可空），不需要数据迁移。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e7a1c39f28"
down_revision: Union[str, Sequence[str], None] = "c4d9e13a6b27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("messages", sa.Column("sources", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_column("sources")
