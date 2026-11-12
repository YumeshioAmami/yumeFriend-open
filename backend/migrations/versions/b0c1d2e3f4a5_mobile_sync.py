"""mobile sync metadata / tombstones / conflicts / blobs + autonomy owner

Revision ID: b0c1d2e3f4a5
Revises: a4b5c6d7e8f9
Create Date: 2026-10-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "settings",
        sa.Column("autonomy_owner_device_id", sa.String(), nullable=True),
    )
    op.create_table(
        "sync_meta",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "sync_devices",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("last_seen_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("idx_sync_devices_seen", "sync_devices", ["last_seen_at"])
    op.create_table(
        "sync_tombstones",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("entity", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("deleted_at", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
    )
    op.create_index(
        "idx_sync_tombstones_entity", "sync_tombstones", ["entity", "entity_id"]
    )
    op.create_index("idx_sync_tombstones_time", "sync_tombstones", ["deleted_at"])
    op.create_table(
        "sync_conflicts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("entity", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("local_updated_at", sa.String(), nullable=True),
        sa.Column("remote_updated_at", sa.String(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("idx_sync_conflicts_time", "sync_conflicts", ["created_at"])
    op.create_table(
        "sync_blobs",
        sa.Column("sha256", sa.String(), primary_key=True),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "mime", sa.String(), nullable=False, server_default="application/octet-stream"
        ),
        sa.Column("path", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sync_blobs")
    op.drop_index("idx_sync_conflicts_time", table_name="sync_conflicts")
    op.drop_table("sync_conflicts")
    op.drop_index("idx_sync_tombstones_time", table_name="sync_tombstones")
    op.drop_index("idx_sync_tombstones_entity", table_name="sync_tombstones")
    op.drop_table("sync_tombstones")
    op.drop_index("idx_sync_devices_seen", table_name="sync_devices")
    op.drop_table("sync_devices")
    op.drop_table("sync_meta")
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("autonomy_owner_device_id")
