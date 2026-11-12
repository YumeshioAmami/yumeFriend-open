"""迁移测试：`settings.purposes`（v2.0，T2.0-2）。

**不可逆操作必须有测试**（AGENTS.md §7 / §7 分工）——数据库迁移算一个。
这个测试验的核心只有一句：**升级不能把用户已经填好的配置弄丢**。

做法：在 v1 的老结构下写好数据 → 升到 head → 断言
① 搬进 `purposes.chat`（包含 api_key）② 老字段原样留着（当备份）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

import backend.config as config

ROOT = Path(__file__).resolve().parents[2]
INITIAL_REV = "e01827d64317"


def _alembic_cfg() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    return cfg


def _insert_old_settings(db: Path, bases: list[dict], active: str | None) -> None:
    """按 v1 的老结构插一行 settings（那时还没有 purposes 列）。"""
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO settings (id, active_message_frequency, deep_thinking, show_tool_calls,"
        " llm_bases, llm_active, memory_token_budget, updated_at)"
        " VALUES (1, 60, 0, 0, ?, ?, NULL, '2026-01-01T00:00:00+00:00')",
        (json.dumps(bases, ensure_ascii=False), active),
    )
    con.commit()
    con.close()


@pytest.fixture()
def migrated_db(tmp_path, monkeypatch):
    """把 alembic 指到临时库上（env.py 读的是 backend.config.DB_PATH）。"""
    db = tmp_path / "migration.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    cfg = _alembic_cfg()
    command.upgrade(cfg, INITIAL_REV)  # 先退到 v1 的老结构
    return db, cfg


def test_upgrade_moves_active_provider_into_purposes_and_keeps_key(migrated_db):
    db, cfg = migrated_db
    _insert_old_settings(
        db,
        [
            {
                "name": "nanogpt",
                "base_url": "https://nano-gpt.com/api/v1",
                "model": "qwen/qwen3.8-27b-uncensored",
                "api_key": "sk-not-a-real-key",
                "proxy": "auto",
            },
            {"name": "local", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen", "api_key": ""},
        ],
        "nanogpt",
    )

    command.upgrade(cfg, "head")

    con = sqlite3.connect(db)
    purposes_raw, llm_bases_raw, llm_active = con.execute(
        "SELECT purposes, llm_bases, llm_active FROM settings WHERE id = 1"
    ).fetchone()
    con.close()

    purposes = json.loads(purposes_raw)
    assert purposes["chat"]["base_url"] == "https://nano-gpt.com/api/v1"
    assert purposes["chat"]["model"] == "qwen/qwen3.8-27b-uncensored"
    assert purposes["chat"]["api_key"] == "sk-not-a-real-key"  # ← 最要紧：key 没丢
    assert purposes["chat"]["proxy"] == "auto"
    assert purposes["vision"]["follow_chat"] is True

    # 老字段一个都没动（当备份留着）
    old_bases = json.loads(llm_bases_raw)
    assert [b["name"] for b in old_bases] == ["nanogpt", "local"]
    assert old_bases[0]["api_key"] == "sk-not-a-real-key"
    assert llm_active == "nanogpt"


def test_upgrade_without_settings_row_keeps_purposes_empty(migrated_db):
    """全新的库（还没有 settings 行）：升级不能报错，purposes 留空。"""
    db, cfg = migrated_db
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "settings" in tables  # 表在，只是没行


def test_upgrade_with_empty_llm_bases_leaves_purposes_empty(migrated_db):
    """老配置本来就是空的：purposes 也留空，不瞎造一个 chat 出来。"""
    db, cfg = migrated_db
    _insert_old_settings(db, [], None)

    command.upgrade(cfg, "head")

    con = sqlite3.connect(db)
    (purposes_raw,) = con.execute("SELECT purposes FROM settings WHERE id = 1").fetchone()
    con.close()
    assert json.loads(purposes_raw) == {}
