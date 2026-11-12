"""friends.llm_base：这个朋友用哪个模型端点（v3.5 / D151）

Revision ID: e9c2a47b15d3
Revises: d4b7c91e5f28
Create Date: 2026-09-23

**纯加列、可空、无默认值** —— 老数据全是 NULL（= 跟随全局「聊天」用途），不需要数据迁移。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9c2a47b15d3"
down_revision: Union[str, Sequence[str], None] = "d4b7c91e5f28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("friends", sa.Column("llm_base", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.drop_column("llm_base")
