"""friends.portrait_transform：立绘的缩放 / 位移（v2.2 / D110）

Revision ID: e2b7c14d9f83
Revises: d9f3b62e8c15
Create Date: 2026-09-20

纯加列（可空，JSON 文本）：不同立绘的高矮 / 构图差很多，用户要在沉浸模式里
自己拖到自己满意的地方（`{"scale":1.0,"x":0,"y":0}`）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b7c14d9f83"
down_revision: Union[str, Sequence[str], None] = "d9f3b62e8c15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("friends", sa.Column("portrait_transform", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.drop_column("portrait_transform")
