"""persistent speech style profile / feedback events（D347）

Revision ID: b2c3d4e5f6a7
Revises: a9c1e2f3b4d5
Create Date: 2026-10-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a9c1e2f3b4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "friend_style_profiles",
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("profile_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "friend_style_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(), nullable=False, server_default="reflect"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("changes_json", sa.Text(), nullable=True),
        sa.Column("user_text", sa.Text(), nullable=True),
        sa.Column("assistant_excerpt", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("after_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("before_profile_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("after_profile_json", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index(
        "idx_friend_style_events_friend",
        "friend_style_events",
        ["friend_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_friend_style_events_friend", table_name="friend_style_events")
    op.drop_table("friend_style_events")
    op.drop_table("friend_style_profiles")
