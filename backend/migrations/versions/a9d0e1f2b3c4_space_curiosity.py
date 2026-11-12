"""小世界 Phase B v2：好奇 / 提问冲动状态

Revision ID: a9d0e1f2b3c4
Revises: f8b9c0d1e2f3
Create Date: 2026-11-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9d0e1f2b3c4"
down_revision: Union[str, Sequence[str], None] = "f8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "friend_space_runtime",
        sa.Column("discovery_score", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "friend_space_runtime",
        sa.Column("wonder_said", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "friend_space_runtime",
        sa.Column("last_discovery_at", sa.String(), nullable=True),
    )
    op.add_column(
        "space_object_knowledge",
        sa.Column("novelty", sa.Float(), nullable=False, server_default="1"),
    )
    op.add_column(
        "space_object_knowledge",
        sa.Column("salience", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "space_object_knowledge",
        sa.Column("ask_pressure", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "space_object_knowledge",
        sa.Column("ask_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "space_object_knowledge",
        sa.Column("ignored_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "space_object_knowledge",
        sa.Column("last_seen_at", sa.String(), nullable=True),
    )
    op.add_column(
        "space_object_knowledge",
        sa.Column("last_observed_at", sa.String(), nullable=True),
    )
    op.add_column(
        "space_object_knowledge",
        sa.Column("last_asked_at", sa.String(), nullable=True),
    )
    op.add_column(
        "space_object_knowledge",
        sa.Column("last_answered_at", sa.String(), nullable=True),
    )


def downgrade() -> None:
    for column in (
        "last_answered_at",
        "last_asked_at",
        "last_observed_at",
        "last_seen_at",
        "ignored_count",
        "ask_count",
        "ask_pressure",
        "salience",
        "novelty",
    ):
        op.drop_column("space_object_knowledge", column)
    for column in ("last_discovery_at", "wonder_said", "discovery_score"):
        op.drop_column("friend_space_runtime", column)
