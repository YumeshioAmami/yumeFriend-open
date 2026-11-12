"""日程 service 验收（v4.0-3 / D243、D244）：增删改查 + 状态机 + 留痕 + 扫描推进。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.models import Friend, Schedule, ScheduleEvent, Thought
from backend.services import schedule_service as ss
from backend.services.actor import Actor


@pytest.fixture()
def friend_id(tmp_path, monkeypatch):
    db_file = tmp_path / "schedules.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as s:
        s.add(Friend(id="f1", name="汐音", created_at=now, updated_at=now))
    return "f1"


def _events(s, schedule_id):
    return [
        e.action
        for e in s.query(ScheduleEvent)
        .filter(ScheduleEvent.schedule_id == schedule_id)
        .order_by(ScheduleEvent.created_at)
        .all()
    ]


# ---------- 增 ----------


def test_create_sets_fields_and_writes_event(friend_id):
    with db.session_scope() as s:
        sc = ss.create(
            s, Actor.AGENT, friend_id,
            title="提醒他买武忠祥", date="2026-09-24", time="15:30",
            nature="remind", owner="friend",
        )
        assert sc.state == "pending"
        assert sc.date == "2026-09-24" and sc.time == "15:30"
        assert sc.owner == "friend" and sc.source == "chat"
        assert _events(s, sc.id) == ["create"]
        # 留痕里记了 actor=agent（她动过这件事的证据）
        ev = s.query(ScheduleEvent).filter_by(schedule_id=sc.id).one()
        assert ev.actor == "agent" and ev.after is not None


def test_create_rejects_empty_title_and_bad_date(friend_id):
    with db.session_scope() as s:
        with pytest.raises(ValueError):
            ss.create(s, Actor.USER, friend_id, title="  ", date="2026-09-24")
        with pytest.raises(ValueError):
            ss.create(s, Actor.USER, friend_id, title="x", date="不是日期")
        with pytest.raises(ValueError):
            ss.create(s, Actor.USER, friend_id, title="x", date="2026-09-24", nature="乱写")


def test_time_parsing_is_forgiving(friend_id):
    with db.session_scope() as s:
        assert ss.create(s, Actor.USER, friend_id, title="a", date="2026-09-24", time="9:5").time == "09:05"
        assert ss.create(s, Actor.USER, friend_id, title="b", date="2026-09-24", time="15:00:00").time == "15:00"
        assert ss.create(s, Actor.USER, friend_id, title="c", date="2026-09-24", time="").time is None


# ---------- 改 / 改期 / 结算 ----------


def test_reschedule_resets_to_pending_and_drops_derived_note(friend_id):
    """改期 = active → pending，派生纸条作废（D244）。"""
    with db.session_scope() as s:
        sc = ss.create(s, Actor.USER, friend_id, title="交作业", date="2026-09-24", time="15:00")
        ss.activate(s, sc, said=True)
        s.add(
            Thought(
                id="t1", friend_id=friend_id, content="问他作业交了没",
                kind="casual", state="pending", source="schedule",
                schedule_id=sc.id, created_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        s.flush()
        ss.update(s, Actor.USER, sc.id, date="2026-09-25", time="10:00")
        assert sc.state == "pending" and sc.first_said_at is None
        thought = s.get(Thought, "t1")
        assert thought.state == "expired"
        assert "reschedule" in _events(s, sc.id)


def test_settle_records_reason_and_event(friend_id):
    with db.session_scope() as s:
        sc = ss.create(s, Actor.USER, friend_id, title="买书", date="2026-09-24")
        ss.settle(s, Actor.USER, sc.id, reason="done")
        assert sc.state == "settled" and sc.settled_reason == "done"
        assert sc.settled_at is not None
        assert "settle" in _events(s, sc.id)


def test_settle_rejects_bad_reason(friend_id):
    with db.session_scope() as s:
        sc = ss.create(s, Actor.USER, friend_id, title="买书", date="2026-09-24")
        with pytest.raises(ValueError):
            ss.settle(s, Actor.USER, sc.id, reason="也许吧")


def test_remove_soft_deletes_and_expires_notes(friend_id):
    with db.session_scope() as s:
        sc = ss.create(s, Actor.USER, friend_id, title="买书", date="2026-09-24")
        ss.remove(s, Actor.USER, sc.id)
        assert sc.deleted_at is not None
        assert ss.list_for(s, friend_id=friend_id) == []
        assert "delete" in _events(s, sc.id)


# ---------- 扫描推进 ----------


def test_scan_due_handles_time_and_advance(friend_id):
    now = datetime(2026, 9, 24, 15, 0).astimezone()
    with db.session_scope() as s:
        ss.create(s, Actor.USER, friend_id, title="只到日", date="2026-09-24")
        ss.create(s, Actor.USER, friend_id, title="晚点", date="2026-09-24", time="20:00")
        ss.create(s, Actor.USER, friend_id, title="提前量", date="2026-09-24", time="16:00", advance_minutes=120)
        ss.create(s, Actor.USER, friend_id, title="明天", date="2026-09-25")
        due = {x.title for x in ss.scan_due(s, now=now)}
    assert due == {"只到日", "提前量"}


def test_activate_and_mark_said(friend_id):
    with db.session_scope() as s:
        sc = ss.create(s, Actor.USER, friend_id, title="买书", date="2026-09-24")
        ss.activate(s, sc, said=True)
        assert sc.state == "active" and sc.first_said_at is not None
        assert "activate" in _events(s, sc.id)
        # 幂等：再 activate 不改 first_said_at
        first = sc.first_said_at
        ss.activate(s, sc)
        assert sc.first_said_at == first


def test_archive_expired_buffer_and_hard_limit(friend_id):
    now = datetime(2026, 10, 3, 12, 0).astimezone()
    now_utc = now.astimezone(timezone.utc)
    with db.session_scope() as s:
        # 硬上限：9/24 只到日 → 10/1 +7 天已过
        hard = ss.create(s, Actor.USER, friend_id, title="太久了", date="2026-09-24")
        ss.activate(s, hard, said=False)
        # 缓冲：active 且 4 天前说过（日期在硬上限内）
        buffered = ss.create(s, Actor.USER, friend_id, title="该放下了", date="2026-10-02")
        ss.activate(s, buffered, said=True)
        buffered.first_said_at = (now_utc - timedelta(days=4)).isoformat()
        s.flush()
        # 还活着的
        keep = ss.create(s, Actor.USER, friend_id, title="还早", date="2026-10-05")
        assert ss.archive_expired(s, now=now) == 2
        assert hard.state == "archived" and buffered.state == "archived"
        assert keep.state == "pending"


def test_purge_deleted_removes_old_soft_deletes(friend_id):
    with db.session_scope() as s:
        fresh = ss.create(s, Actor.USER, friend_id, title="刚删", date="2026-09-24")
        old = ss.create(s, Actor.USER, friend_id, title="早删", date="2026-09-24")
        ss.remove(s, Actor.USER, fresh.id)
        ss.remove(s, Actor.USER, old.id)
        old.deleted_at = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        s.flush()
        assert ss.purge_deleted(s) == 1
        assert s.get(Schedule, old.id) is None
        assert s.get(Schedule, fresh.id) is not None


# ---------- 列表 ----------


def test_list_for_filters_by_owner_and_archived(friend_id):
    with db.session_scope() as s:
        mine = ss.create(s, Actor.USER, friend_id, title="我的", date="2026-09-24", owner="user")
        hers = ss.create(s, Actor.AGENT, friend_id, title="她的", date="2026-09-24", owner="friend")
        ss.activate(s, mine)
        ss.activate(s, hers)
        ss.archive_expired(s, now=datetime(2026, 10, 3, 12, 0).astimezone())
        assert ss.list_for(s, friend_id=friend_id) == []
        assert len(ss.list_for(s, friend_id=friend_id, include_archived=True)) == 2
        assert [x.title for x in ss.list_for(s, friend_id=friend_id, owner="friend", include_archived=True)] == ["她的"]


def test_upcoming_for_prompt_lists_pending_and_active(friend_id):
    """v4.1（D246）：给模型看的日程清单（pending + active，带 id）。"""
    with db.session_scope() as s:
        pending = ss.create(
            s, Actor.USER, friend_id, title="提醒他喝水", date="2026-10-09", time="11:22"
        )
        hers = ss.create(s, Actor.AGENT, friend_id, title="买书", date="2026-10-12", owner="friend")
        ss.activate(s, hers)
        note = ss.upcoming_for_prompt(s, friend_id)
    assert note is not None
    assert f"id={pending.id}" in note and "提醒他喝水" in note
    assert "买书" in note and "只到日" in note
    assert "我自己的事" in note  # owner=friend


def test_upcoming_for_prompt_none_when_empty(friend_id):
    with db.session_scope() as s:
        assert ss.upcoming_for_prompt(s, friend_id) is None
