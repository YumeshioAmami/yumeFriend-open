"""B1 / D137：**软删清理** —— 30 天前的软删数据物理删掉（data_model §7.1/§7.3 / 宪法§9）。

盯三件事（顺序也是风险从大到小）：

1. **没满 30 天的一个都不许动**（"30 天内可恢复"是给用户的承诺）
2. 满了 30 天的朋友 → 连**文件**一起彻底没了：头像 / 立绘 / 背景 + 消息 / 记忆 / 日程（外键级联）
3. 没满 30 天的软删记忆同理；**活着的**数据一根毫毛都不动
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.models import Friend, Memory, Message, Schedule
from backend.scheduler import jobs


def _days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture()
def user_data(tmp_path, monkeypatch):
    """临时库 + 临时 user_data（清理会真的删文件，绝不能碰仓库里的 user_data）。"""
    db_file = tmp_path / "cleanup.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    monkeypatch.setattr(config, "PORTRAIT_DIR", tmp_path / "portraits")
    monkeypatch.setattr(config, "BACKGROUND_DIR", tmp_path / "backgrounds")
    monkeypatch.setattr(config, "DOCUMENT_DIR", tmp_path / "documents")
    monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "UPLOAD_PENDING_DIR", tmp_path / "uploads" / "pending")
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    return tmp_path


def _add_friend(session, friend_id: str, *, deleted_days_ago: float | None = None) -> None:
    now = _days_ago(0)
    session.add(
        Friend(
            id=friend_id,
            name=f"朋友{friend_id}",
            personality="话不多",
            created_at=now,
            updated_at=now,
            avatar_path=f"avatars/{friend_id}.png",
            deleted_at=None if deleted_days_ago is None else _days_ago(deleted_days_ago),
        )
    )
    session.add(
        Message(id=f"msg-{friend_id}", friend_id=friend_id, role="user", content="你好", created_at=now)
    )
    session.add(
        Memory(
            id=f"mem-{friend_id}",
            friend_id=friend_id,
            content="用户生日 5/20",
            tag="notebook",
            layer="long_term",
            importance=0.5,
            created_at=now,
            refreshed_at=now,
        )
    )
    session.add(
        Schedule(
            id=f"sch-{friend_id}",
            friend_id=friend_id,
            title="晚上 8 点吃药",
            nature="remind",
            date="2026-01-01",
            time="20:00",
            state="pending",
            owner="user",
            source="chat",
            created_at=now,
        )
    )


def _add_files(user_data, friend_id: str) -> dict:
    """给她摆上头像 / 立绘 / 背景三个文件，看 purge 有没有一起清。"""
    config.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    avatar = config.AVATAR_DIR / f"{friend_id}.png"
    avatar.write_bytes(b"png")
    portrait_dir = config.PORTRAIT_DIR / friend_id
    portrait_dir.mkdir(parents=True, exist_ok=True)
    (portrait_dir / "高兴.webp").write_bytes(b"webp")
    config.BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    background = config.BACKGROUND_DIR / f"{friend_id}.jpg"
    background.write_bytes(b"jpg")
    return {"avatar": avatar, "portrait_dir": portrait_dir, "background": background}


# ---------- 朋友 ----------


def test_old_soft_deleted_friend_is_purged_completely(user_data):
    with db.session_scope() as session:
        _add_friend(session, "old", deleted_days_ago=31)
        _add_friend(session, "recent", deleted_days_ago=29)
        _add_friend(session, "alive")
    old_files = _add_files(user_data, "old")
    recent_files = _add_files(user_data, "recent")

    friends, memories, schedules = jobs._purge_soft_deleted()

    assert (friends, memories, schedules) == (1, 0, 0)
    with db.session_scope() as session:
        alive_ids = {row.id for row in session.query(Friend).all()}
        assert alive_ids == {"recent", "alive"}, "只该删掉满 30 天那个"
        assert session.query(Message).filter(Message.friend_id == "old").count() == 0
        assert session.query(Memory).filter(Memory.friend_id == "old").count() == 0
        assert session.query(Schedule).filter(Schedule.friend_id == "old").count() == 0
        # 没满 30 天的那些连带数据一个都不许少（不然"恢复"就不是完整的了）
        assert session.query(Message).filter(Message.friend_id == "recent").count() == 1
        assert session.query(Memory).filter(Memory.friend_id == "recent").count() == 1

    assert not old_files["avatar"].exists()
    assert not old_files["portrait_dir"].exists()
    assert not old_files["background"].exists()
    assert recent_files["avatar"].exists(), "没满 30 天的文件不许动"
    assert recent_files["portrait_dir"].exists()


def test_purge_is_idempotent(user_data):
    with db.session_scope() as session:
        _add_friend(session, "old", deleted_days_ago=40)

    assert jobs._purge_soft_deleted() == (1, 0, 0)
    assert jobs._purge_soft_deleted() == (0, 0, 0), "重复跑什么都不该再删（幂等）"


async def test_daily_cleanup_job_does_it(user_data):
    """接线也要盯着：`_cleanup_job`（每天 4 点那条）真的会走到软删清理。"""
    with db.session_scope() as session:
        _add_friend(session, "old", deleted_days_ago=45)

    await jobs._cleanup_job()

    with db.session_scope() as session:
        assert session.query(Friend).filter(Friend.id == "old").count() == 0


# ---------- 记忆 ----------


def test_soft_deleted_memories_are_purged_after_30_days(user_data):
    with db.session_scope() as session:
        _add_friend(session, "f1")
        now = _days_ago(0)
        session.add(
            Memory(
                id="mem-old",
                friend_id="f1",
                content="删了很久的",
                tag="normal",
                layer="long_term",
                importance=0.5,
                created_at=now,
                refreshed_at=now,
                deleted_at=_days_ago(31),
            )
        )
        session.add(
            Memory(
                id="mem-recent",
                friend_id="f1",
                content="刚删的",
                tag="normal",
                layer="long_term",
                importance=0.5,
                created_at=now,
                refreshed_at=now,
                deleted_at=_days_ago(3),
            )
        )
        session.add(
            Memory(
                id="mem-alive",
                friend_id="f1",
                content="还活着的",
                tag="normal",
                layer="long_term",
                importance=0.5,
                created_at=now,
                refreshed_at=now,
            )
        )

    friends, memories, _schedules = jobs._purge_soft_deleted()

    assert (friends, memories) == (0, 1)
    with db.session_scope() as session:
        left = {row.id for row in session.query(Memory).all()}
    # mem-f1 是 `_add_friend` 顺手建的那条（活着的），也该留着
    assert left == {"mem-f1", "mem-recent", "mem-alive"}


# ---------- 日程（D138 / v4.0）----------


def test_deleted_and_archived_schedules_are_purged_after_30_days(user_data):
    """软删的 / 已归档的日程：满 30 天清掉，没满的留着（还会被界面读出来）。"""
    with db.session_scope() as session:
        _add_friend(session, "f1")
        now = _days_ago(0)

        def _sch(sid: str, **over) -> None:
            values = dict(
                id=sid,
                friend_id="f1",
                title=f"日程 {sid}",
                nature="remind",
                date="2026-01-01",
                state="pending",
                owner="user",
                source="chat",
                created_at=now,
            )
            values.update(over)
            session.add(Schedule(**values))

        _sch("sch-deleted-old", deleted_at=_days_ago(31))
        _sch("sch-deleted-recent", deleted_at=_days_ago(3))
        _sch("sch-archived-old", state="archived", updated_at=_days_ago(31))
        _sch("sch-archived-recent", state="archived", updated_at=_days_ago(2))
        _sch("sch-open")

    _friends, _memories, schedules = jobs._purge_soft_deleted()

    assert schedules == 2
    with db.session_scope() as session:
        left = {row.id for row in session.query(Schedule).all()}
    # sch-f1 是 `_add_friend` 顺手建的那条（活着）
    assert left == {
        "sch-f1",
        "sch-deleted-recent",
        "sch-archived-recent",
        "sch-open",
    }


def test_threshold_boundary(user_data):
    """卡在 30 天线上：29.9 天不动，30.1 天删（用 `now` 参数把时间钉死，别靠运气）。"""
    from backend.services import friend_service, memory_service

    with db.session_scope() as session:
        logger_now = _days_ago(0)
        _add_friend(session, "f1")
        session.add(
            Memory(
                id="mem-30-1",
                friend_id="f1",
                content="x",
                tag="normal",
                layer="long_term",
                importance=0.5,
                created_at=logger_now,
                refreshed_at=logger_now,
                deleted_at=_days_ago(30.1),
            )
        )
        session.add(
            Memory(
                id="mem-29-9",
                friend_id="f1",
                content="y",
                tag="normal",
                layer="long_term",
                importance=0.5,
                created_at=logger_now,
                refreshed_at=logger_now,
                deleted_at=_days_ago(29.9),
            )
        )
        session.add(
            Friend(
                id="f-old",
                name="旧的",
                created_at=logger_now,
                updated_at=logger_now,
                deleted_at=_days_ago(30.1),
            )
        )
        session.add(
            Friend(
                id="f-keep",
                name="留着的",
                created_at=logger_now,
                updated_at=logger_now,
                deleted_at=_days_ago(29.9),
            )
        )

    with db.session_scope() as session:
        assert [f.id for f in friend_service.list_purgeable(session)] == ["f-old"]
        assert memory_service.purge_deleted(session) == 1
