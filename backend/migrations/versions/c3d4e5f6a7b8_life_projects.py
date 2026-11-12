"""continuous life projects / article reading（D360）

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "life_projects",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "friend_id",
            sa.String(),
            sa.ForeignKey("friends.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("topic", sa.String(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("source", sa.String(), nullable=False, server_default=""),
        sa.Column("step_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reading_excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_depth", sa.String(), nullable=False, server_default=""),
        sa.Column("last_thought", sa.Text(), nullable=False, server_default=""),
        sa.Column("open_question", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("last_activity_at", sa.String(), nullable=True),
        sa.Column("last_shared_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_index(
        "idx_life_projects_friend_time",
        "life_projects",
        ["friend_id", "last_activity_at"],
    )
    op.create_index(
        "idx_life_projects_friend_status",
        "life_projects",
        ["friend_id", "status"],
    )
    with op.batch_alter_table("life_activities", schema=None) as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_life_activities_project_id",
            "life_projects",
            ["project_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "idx_life_activities_project", ["project_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("life_activities", schema=None) as batch_op:
        batch_op.drop_index("idx_life_activities_project")
        batch_op.drop_constraint(
            "fk_life_activities_project_id", type_="foreignkey"
        )
        batch_op.drop_column("project_id")
    op.drop_index("idx_life_projects_friend_status", table_name="life_projects")
    op.drop_index("idx_life_projects_friend_time", table_name="life_projects")
    op.drop_table("life_projects")
