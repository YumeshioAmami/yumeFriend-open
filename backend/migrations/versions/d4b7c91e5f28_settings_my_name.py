"""settings.my_name：沉浸模式里"我"叫什么（v3.0 / B9 / D142）

Revision ID: d4b7c91e5f28
Revises: c7e1a4b9d203
Create Date: 2026-09-21

纯加列，带 server_default：默认"我"。
以前这个名字存在浏览器 localStorage → 电脑和手机各叫各的；挪进 DB 后两端一致。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4b7c91e5f28"
down_revision: Union[str, Sequence[str], None] = "c7e1a4b9d203"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "settings",
        sa.Column("my_name", sa.String(), nullable=False, server_default="我"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("my_name")
