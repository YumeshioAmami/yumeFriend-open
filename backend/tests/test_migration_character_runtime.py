"""V6.0-1：friend_states 迁移可升级、列齐全。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

import backend.config as config


ROOT = Path(__file__).resolve().parents[2]


def _alembic_cfg() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    return cfg


def test_friend_states_migration(tmp_path, monkeypatch):
    db_file = tmp_path / "character-runtime-migration.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    command.upgrade(_alembic_cfg(), "head")

    con = sqlite3.connect(db_file)
    try:
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "friend_states" in tables
        columns = {row[1] for row in con.execute("PRAGMA table_info(friend_states)")}
        assert {
            "friend_id",
            "affect",
            "energy",
            "concerns",
            "intent",
            "inner_note",
            "relationship_note",
            "open_loops",
            "active_thread",
            "updated_at",
        } <= columns
        friend_columns = {row[1] for row in con.execute("PRAGMA table_info(friends)")}
        assert "voice_examples" in friend_columns
        message_columns = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
        assert "segments" in message_columns
        assert "friend_episodes" in tables
        friend_columns = {row[1] for row in con.execute("PRAGMA table_info(friends)")}
        assert "last_reflection_at" in friend_columns
    finally:
        con.close()
