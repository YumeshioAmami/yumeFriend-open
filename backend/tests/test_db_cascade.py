"""T1.3 核心验收：PRAGMA foreign_keys=ON 真的生效 → 删朋友级联清干净。

这是最容易"假通过"的地方：SQLite 默认不开外键，如果没开，
删朋友后 messages/memories/reminders 会变成孤儿数据（违反宪法第九条）。
"""

import uuid
from datetime import datetime, timezone

import backend.db as db
from backend.models import Friend, Memory, MemoryEvent, Message, Schedule, ScheduleEvent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh_engine(tmp_path, monkeypatch):
    """把引擎指到临时库，并重建（引擎是缓存的，必须重置）。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    engine = db.get_engine()
    db.Base.metadata.create_all(engine)
    return engine


def test_foreign_keys_pragma_is_on(tmp_path, monkeypatch):
    engine = _fresh_engine(tmp_path, monkeypatch)
    with engine.connect() as conn:
        enabled = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert enabled == 1, "PRAGMA foreign_keys 没有打开 —— CASCADE 会失效"


def test_deleting_friend_cascades_to_children(tmp_path, monkeypatch):
    _fresh_engine(tmp_path, monkeypatch)
    now = _now()
    fid = str(uuid.uuid4())

    with db.session_scope() as s:
        s.add(Friend(id=fid, name="小敏", personality="温柔", created_at=now, updated_at=now))
        s.add(
            Message(
                id=str(uuid.uuid4()),
                friend_id=fid,
                role="user",
                content="你好",
                created_at=now,
            )
        )
        s.add(
            Memory(
                id=str(uuid.uuid4()),
                friend_id=fid,
                content="用户生日 5 月 20 日",
                tag="notebook",
                layer="pinned",
                created_at=now,
                refreshed_at=now,
            )
        )
        s.add(
            Schedule(
                id=str(uuid.uuid4()),
                friend_id=fid,
                title="晚上 8 点吃药",
                nature="remind",
                date="2026-09-24",
                state="pending",
                owner="user",
                source="chat",
                created_at=now,
            )
        )
        s.add(
            ScheduleEvent(
                id=str(uuid.uuid4()),
                friend_id=fid,
                schedule_id="gone",
                actor="agent",
                action="create",
                created_at=now,
            )
        )
        s.add(
            MemoryEvent(
                id=str(uuid.uuid4()),
                friend_id=fid,
                memory_id="gone",
                actor="agent",
                action="delete",
                created_at=now,
            )
        )

    with db.session_scope() as s:
        assert s.query(Message).count() == 1
        assert s.query(Memory).count() == 1
        assert s.query(Schedule).count() == 1
        assert s.query(ScheduleEvent).count() == 1
        assert s.query(MemoryEvent).count() == 1
        s.query(Friend).filter(Friend.id == fid).delete()

    with db.session_scope() as s:
        assert s.query(Message).count() == 0, "messages 有孤儿数据"
        assert s.query(Memory).count() == 0, "memories 有孤儿数据"
        assert s.query(Schedule).count() == 0, "schedules 有孤儿数据"
        assert s.query(ScheduleEvent).count() == 0, "schedule_events 有孤儿数据"
        assert s.query(MemoryEvent).count() == 0, "memory_events 有孤儿数据"


def test_playstyle_unique_index(tmp_path, monkeypatch):
    """同一 friend + 同一 role 只允许一条 rp_play_style（data_model §4）。"""
    _fresh_engine(tmp_path, monkeypatch)
    now = _now()
    fid = str(uuid.uuid4())
    with db.session_scope() as s:
        s.add(Friend(id=fid, name="小敏", personality="温柔", created_at=now, updated_at=now))

    def add(role: str) -> None:
        with db.session_scope() as s:
            s.add(
                Memory(
                    id=str(uuid.uuid4()),
                    friend_id=fid,
                    content=f"play_style for {role}",
                    tag="rp_play_style",
                    layer="long_term",
                    context=role,
                    created_at=now,
                    refreshed_at=now,
                )
            )

    add("女朋友")
    try:
        add("女朋友")
    except Exception:
        pass  # 唯一索引拦住 = 符合预期
    else:
        raise AssertionError("同一 role 的第二条 play_style 没有被唯一索引拦住")

    with db.session_scope() as s:
        assert s.query(Memory).filter(Memory.context == "女朋友").count() == 1
    add("学姐")  # 不同 role 可以各存一条
    with db.session_scope() as s:
        assert s.query(Memory).count() == 2
