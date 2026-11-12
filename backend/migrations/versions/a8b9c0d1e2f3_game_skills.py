"""game skills + failure notes（v5.1-P4 / D288）

Revision ID: a8b9c0d1e2f3
Revises: f4a5b6c7d8e9
Create Date: 2026-10-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "game_skills",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("preconditions", sa.Text(), nullable=False),
        sa.Column("steps", sa.Text(), nullable=False),
        sa.Column("postconditions", sa.Text(), nullable=False),
        sa.Column("versions", sa.Text(), nullable=False),
        sa.Column("mods", sa.Text(), nullable=False),
        sa.Column("stats", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(), nullable=False, server_default="learned"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index(
        "idx_game_skills_friend_name",
        "game_skills",
        ["friend_id", "name"],
    )
    op.create_index("idx_game_skills_source", "game_skills", ["source"])

    op.create_table(
        "game_skill_failures",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan_id", sa.String(), nullable=True),
        sa.Column("skill_name", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index(
        "idx_game_skill_failures_friend",
        "game_skill_failures",
        ["friend_id", "created_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "idx_game_skill_failures_friend", table_name="game_skill_failures"
    )
    op.drop_table("game_skill_failures")
    op.drop_index("idx_game_skills_source", table_name="game_skills")
    op.drop_index("idx_game_skills_friend_name", table_name="game_skills")
    op.drop_table("game_skills")
