"""手机端本地服务启动路径：全新数据库必须先建表，再做轻量迁移。"""

from __future__ import annotations

import sqlite3

import backend.config as config
import backend.db as db
from backend import mobile_app


def test_mobile_fresh_database_starts_without_missing_table(tmp_path, monkeypatch):
    data_dir = tmp_path / "mobile-data"
    db_file = data_dir / "yumefriend.db"
    monkeypatch.setattr(config, "USER_DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)

    # 全新安装：第一次和第二次启动都必须成功（迁移可重复执行）。
    mobile_app._create_tables()
    mobile_app._create_tables()

    with sqlite3.connect(db_file) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type='table'"
            ).fetchall()
        }
        message_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
    assert "messages" in tables
    assert "sync_message_outbox" in tables
    assert "updated_at" in message_cols
