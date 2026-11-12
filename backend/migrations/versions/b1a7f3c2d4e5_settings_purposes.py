"""settings.purposes：按用途的模型配置（v2.0，D80）

Revision ID: b1a7f3c2d4e5
Revises: e01827d64317
Create Date: 2026-09-19

**为什么这一步带数据迁移**：
v1 把 provider 存在 `llm_bases`（JSON 数组）+ `llm_active`（名字）；
v2 改成"按用途存"（`purposes`）。**老字段一个都不删**（当备份），
但必须把 active 那条搬进 `purposes.chat`——否则用户升级完打开设置会发现"我配的东西没了"。
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1a7f3c2d4e5"
down_revision: Union[str, Sequence[str], None] = "e01827d64317"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _seed_purposes_from_llm_bases() -> None:
    """把 v1 的 active provider 搬进 purposes.chat（搬不动就留空，不报错）。"""
    conn = op.get_bind()
    row = conn.execute(sa.text("SELECT llm_bases, llm_active FROM settings WHERE id = 1")).fetchone()
    if row is None:
        return
    bases_raw, active = row[0], row[1]
    try:
        bases = json.loads(bases_raw or "[]")
    except (TypeError, ValueError):
        bases = []
    if not isinstance(bases, list) or not bases:
        return
    picked = next((b for b in bases if isinstance(b, dict) and b.get("name") == active), None)
    if picked is None:
        picked = next((b for b in bases if isinstance(b, dict)), None)
    if picked is None:
        return
    purposes = {
        "chat": {
            "mode": "api",
            "base_url": picked.get("base_url", ""),
            "api_key": picked.get("api_key", ""),
            "model": picked.get("model", ""),
            "proxy": picked.get("proxy", ""),
        },
        "vision": {"follow_chat": True},
    }
    conn.execute(
        sa.text("UPDATE settings SET purposes = :p WHERE id = 1"),
        {"p": json.dumps(purposes, ensure_ascii=False)},
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "settings",
        sa.Column("purposes", sa.Text(), nullable=False, server_default="{}"),
    )
    _seed_purposes_from_llm_bases()


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("settings", schema=None) as batch_op:
        batch_op.drop_column("purposes")
