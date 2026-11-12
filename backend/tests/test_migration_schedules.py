"""迁移测试：reminders → schedules（v4.0-2 / D243、D244）。

**不可逆操作必须有测试**（AGENTS.md §7 / 分工表）。这一步把老"代办"搬进日程母表，
核心只验一句：**搬过去的一条不少、字段对得上**（尤其"UTC → 本地日期"不能挪一天）。

搬完后由收尾迁移（`c5d0e2b8f391`）**删掉 `reminders` 表** —— 所以这里断言：
数据一条不少地进了 `schedules`，而老表**已经不在**（不留死表）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

import backend.config as config

ROOT = Path(__file__).resolve().parents[2]
INITIAL_REV = "e01827d64317"
# 一个**故意跨日**的 UTC 时刻：北京 9/24 16:30 → UTC 是 9/24 08:30（同一天）
# 再配一个 UTC 日界附近的，确保换算真的用了本地时区（见 test 里的断言）
UTC_AFTERNOON = "2026-09-24T08:30:00+00:00"
UTC_EDGE = "2026-09-23T20:00:00+00:00"  # 北京 9/24 04:00 → UTC 还是 9/23


def _alembic_cfg() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    return cfg


@pytest.fixture()
def old_db(tmp_path, monkeypatch):
    """升到 v1 老结构，给测试往里塞 reminders。"""
    db = tmp_path / "schedule_migration.db"
    monkeypatch.setattr(config, "DB_PATH", db)
    cfg = _alembic_cfg()
    command.upgrade(cfg, INITIAL_REV)
    return db, cfg


def _local(iso: str) -> tuple[str, str]:
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M")


def _seed(db: Path, reminders: list[tuple]) -> None:
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute(
        "INSERT INTO friends (id, name, personality, custom_instructions, presence,"
        " current_mode, current_role, unread_count, last_catchup_at, created_at,"
        " updated_at, deleted_at) VALUES ('f1','阿月','p','c',1,'normal',NULL,0,?,?,?,NULL)",
        (UTC_AFTERNOON, UTC_AFTERNOON, UTC_AFTERNOON),
    )
    for row in reminders:
        con.execute(
            "INSERT INTO reminders (id, friend_id, content, trigger_time, is_completed,"
            " completed_at, created_at, deleted_at) VALUES (?,?,?,?,?,?,?,?)",
            row,
        )
    con.commit()
    con.close()


def _rows(db: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_reminders_are_moved_into_schedules_with_local_date(old_db):
    db, cfg = old_db
    _seed(
        db,
        [
            ("r-pending", "f1", "提醒他交作业", UTC_EDGE, 0, None, UTC_AFTERNOON, None),
            ("r-done", "f1", "睡觉", UTC_AFTERNOON, 1, UTC_AFTERNOON, UTC_AFTERNOON, None),
        ],
    )

    command.upgrade(cfg, "head")

    rows = _rows(
        db,
        "SELECT id, friend_id, title, nature, date, time, advance_minutes, state, owner,"
        " source, settled_at, settled_reason, created_at FROM schedules ORDER BY id",
    )
    by_id = {r[0]: r for r in rows}
    assert set(by_id) == {"r-pending", "r-done"}

    day_edge, clock_edge = _local(UTC_EDGE)
    day_done, clock_done = _local(UTC_AFTERNOON)

    p = by_id["r-pending"]
    assert p[1] == "f1"
    assert p[2] == "提醒他交作业"  # content → title，一个字没丢
    assert p[3:6] == ("remind", day_edge, clock_edge)
    assert p[6] == 0 and p[7] == "pending" and p[8] == "user" and p[9] == "migration"
    assert p[10] is None and p[11] is None

    d = by_id["r-done"]
    assert d[2] == "睡觉"
    assert d[3:6] == ("remind", day_done, clock_done)
    assert d[7] == "settled" and d[9] == "migration"
    assert d[10] == UTC_AFTERNOON and d[11] == "done"

    # 老表已被收尾迁移删掉（数据只在 schedules 里，不留死表）
    tables = {
        r[0]
        for r in _rows(db, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "reminders" not in tables


def test_upgrade_without_reminders_is_fine(old_db):
    """全新 / 没有 reminders 的库：升级不能报错，schedules 是空的。"""
    db, cfg = old_db
    command.upgrade(cfg, "head")
    assert _rows(db, "SELECT count(*) FROM schedules")[0][0] == 0


def test_thoughts_gains_schedule_id_column(old_db):
    db, cfg = old_db
    command.upgrade(cfg, "head")
    cols = {r[1] for r in _rows(db, "PRAGMA table_info(thoughts)")}
    assert "schedule_id" in cols


def test_new_tables_exist(old_db):
    db, cfg = old_db
    command.upgrade(cfg, "head")
    tables = {
        r[0]
        for r in _rows(db, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"schedules", "schedule_events"} <= tables
