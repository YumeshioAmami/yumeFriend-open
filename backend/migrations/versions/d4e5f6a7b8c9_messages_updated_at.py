"""messages.updated_at + repair recent streamed replies（D361）

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-27
"""

from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("updated_at", sa.String(), nullable=True))
    op.execute("UPDATE messages SET updated_at = created_at WHERE updated_at IS NULL")
    # 旧同步把手机端的 streaming 空占位发到了服务器；这里把最近已完成回复重新标脏，
    # 下一次同步会用真正正文覆盖空占位。
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    op.execute(
        sa.text(
            "UPDATE messages SET updated_at = :now "
            "WHERE role = 'assistant' "
            "AND TRIM(COALESCE(content, '')) != '' "
            "AND created_at >= :cutoff"
        ).bindparams(now=now, cutoff=cutoff)
    )


def downgrade() -> None:
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_column("updated_at")
