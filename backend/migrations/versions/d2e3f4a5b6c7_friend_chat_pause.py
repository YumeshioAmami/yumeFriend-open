"""friends.chat_pause_until（v7.12 / D342）

手机端不拥有 FriendLife 行；聊天暂停窗口写在 friends.chat_pause_until，
通过普通 friends 同步到电脑，避免覆盖电脑的 current_activity_id。

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-10-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("friends", sa.Column("chat_pause_until", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.drop_column("chat_pause_until")
