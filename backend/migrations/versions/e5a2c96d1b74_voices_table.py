"""voices 表 + friends.voice_id（v2.0，T2.0-4）

Revision ID: e5a2c96d1b74
Revises: d3f91b47c8ae
Create Date: 2026-09-19

两张关系：一个音色可以给多个朋友用；**删音色时 friends.voice_id 置空**（不连坐删朋友）。
（该行为依赖 SQLite 的 `PRAGMA foreign_keys = ON`，业务引擎在建连接时就开了。）
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5a2c96d1b74"
down_revision: Union[str, Sequence[str], None] = "d3f91b47c8ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "voices",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("gpt_weights_path", sa.Text(), nullable=False),
        sa.Column("sovits_weights_path", sa.Text(), nullable=False),
        sa.Column("ref_audio_path", sa.Text(), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("speed", sa.Float(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # SQLite 加带外键的列要走 batch 模式（重建表）
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.add_column(sa.Column("voice_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_friends_voice_id", "voices", ["voice_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("friends", schema=None) as batch_op:
        batch_op.drop_constraint("fk_friends_voice_id", type_="foreignkey")
        batch_op.drop_column("voice_id")
    op.drop_table("voices")
