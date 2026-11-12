"""知识库：documents + document_chunks（v2.1 / T2.1-3 / D98）

Revision ID: e6b3f82a71c9
Revises: d5e7a1c39f28
Create Date: 2026-09-20

两张新表；`documents.friend_id` 走 CASCADE（删朋友 → 资料和块一起没）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6b3f82a71c9"
down_revision: Union[str, Sequence[str], None] = "d5e7a1c39f28"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "documents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("friend_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="ready"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["friend_id"], ["friends.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_documents_friend", "documents", ["friend_id"])

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("document_id", sa.String(), nullable=False),
        sa.Column("friend_id", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_chunks_friend", "document_chunks", ["friend_id"])
    op.create_index("idx_chunks_doc", "document_chunks", ["document_id", "seq"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_chunks_doc", table_name="document_chunks")
    op.drop_index("idx_chunks_friend", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("idx_documents_friend", table_name="documents")
    op.drop_table("documents")
