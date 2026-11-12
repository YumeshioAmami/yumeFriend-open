"""friends.avatar_description：「她的样子」（v2.0，D84）

Revision ID: d3f91b47c8ae
Revises: c2b8e5a71f03
Create Date: 2026-09-19

纯加列（可空），不需要数据迁移：没描述就是 NULL，提示词里那段整段不出现。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3f91b47c8ae"
down_revision: Union[str, Sequence[str], None] = "c2b8e5a71f03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("friends", sa.Column("avatar_description", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("friends", schema=None) as op_batch:
        op_batch.drop_column("avatar_description")
