"""D192 验收：小纸条 / 念头 —— 进 / 说 / 销账 三条规则都要能判断。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.agent import tools
from backend.models import Friend, Thought
from backend.services import thought_service

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def friend_id(tmp_path, monkeypatch):
    db_file = tmp_path / "thoughts.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    now = NOW.isoformat()
    with db.session_scope() as s:
        s.add(Friend(id="f1", name="阿月", created_at=now, updated_at=now))
    return "f1"


def _pending(scope, fid):
    with scope() as s:
        return [t for t in thought_service.alive(s, fid) if t.state == "pending"]


# ---------- 进：写 / 合并 / 上限 ----------


def test_add_creates_a_pending_note_with_expiry(friend_id):
    with db.session_scope() as s:
        thought, merged = thought_service.add(s, friend_id, content="问问他面试结果")
    assert merged is False
    assert thought.state == "pending"
    assert thought.expires_at is not None  # 随缘型 3 天寿命


def test_same_thing_merges_instead_of_adding(friend_id):
    """同一件事 → 改写已有那张（不然她会有 3 张"问他面试"）。"""
    with db.session_scope() as s:
        first, _ = thought_service.add(s, friend_id, content="问他面试结果怎么样")
        again, merged = thought_service.add(s, friend_id, content="问他面试结果怎么样？")
    assert merged is True
    assert again.id == first.id
    assert len(_pending(db.session_scope, friend_id)) == 1


def test_pending_notes_are_unlimited(friend_id):
    """v4.0（D243 改版第 3 条）：纸条不再设 3 条上限，可以无限攒（注入时再封顶）。"""
    with db.session_scope() as s:
        for content in ("问问他面试结果", "提醒他买书", "答应帮他查资料", "想问问他考试", "跟他说晚安"):
            thought_service.add(s, friend_id, content=content)
        pending = [t for t in thought_service.alive(s, friend_id) if t.state == "pending"]
    assert len(pending) == 5


def test_due_kind_needs_a_time(friend_id):
    with db.session_scope() as s:
        with pytest.raises(ValueError):
            thought_service.add(s, friend_id, content="提醒他交作业", kind="due")


# ---------- 说：挑哪张 ----------


def test_next_thought_waits_for_a_due_time(friend_id):
    with db.session_scope() as s:
        thought_service.add(
            s, friend_id, content="问他面试", kind="due",
            due_at=(NOW + timedelta(hours=3)).isoformat(),
        )
        assert thought_service.next_thought(s, friend_id, now=NOW) is None  # 还没到点
        later = thought_service.next_thought(s, friend_id, now=NOW + timedelta(hours=4))
    assert later is not None and later.content == "问他面试"


def test_next_thought_prefers_due_then_oldest_casual(friend_id):
    with db.session_scope() as s:
        thought_service.add(s, friend_id, content="随缘的 A")
        thought_service.add(s, friend_id, content="到点的 B", kind="due", due_at=NOW.isoformat())
    with db.session_scope() as s:
        picked = thought_service.next_thought(s, friend_id, now=NOW)
    assert picked is not None and picked.content == "到点的 B"


# ---------- 销账：说了 ≠ 销账 ----------


def test_mark_said_goes_to_awaiting_not_done(friend_id):
    """用户 2026-09-27 专门指出的那点：**说了但没回 → 不能销账**。"""
    with db.session_scope() as s:
        thought, _ = thought_service.add(s, friend_id, content="问他面试")
        thought_service.mark_said(s, thought, now=NOW)
        assert thought.state == "awaiting"
        assert thought.said_at is not None
    # 待回的：还活着，但**不参加"要说"的挑选**（next_thought 只挑 pending）
    with db.session_scope() as s:
        assert thought_service.next_thought(s, friend_id, now=NOW) is None


def test_notes_for_turn_caps_injection_and_keeps_notes_alive(friend_id):
    """v4.0（D243 改版）：他开口时**最多给她看 3 条**，而且**不销账**（提不提她自己定）。"""
    with db.session_scope() as s:
        said, _ = thought_service.add(s, friend_id, content="问他面试")
        thought_service.mark_said(s, said, now=NOW)  # 说了他没回 → 待回（排在前面）
        for content in ("提醒他买书", "答应帮他查资料", "想问问他考试"):
            thought_service.add(s, friend_id, content=content)
    with db.session_scope() as s:
        note = thought_service.notes_for_turn(s, friend_id, now=NOW)
        assert note is not None
        shown = [line for line in note.splitlines() if line.startswith("- ")]
        assert len(shown) == 3, "每轮注入最多 3 条"
        assert shown[0].endswith("问他面试")  # 待回优先
        assert "别像念待办" in note
        assert thought_service.alive(s, friend_id) != []  # **没被销账**


def test_purge_expires_old_notes(friend_id):
    """到期的自然放下：待说 3 天 / 待回 7 天。"""
    with db.session_scope() as s:
        # `now=NOW`：纸条寿命按用例里的"现在"算 —— 否则 add() 用真实时间写 expires_at，
        # 真实日期一过 NOW+3 天，这条用例就必挂（2026-10-03 踩过）
        thought_service.add(s, friend_id, content="很久以前想说的", now=NOW)
        said, _ = thought_service.add(s, friend_id, content="说了但没回的", now=NOW)
        thought_service.mark_said(s, said, now=NOW)
        # 4 天后：随缘型（3 天）该过期；待回（7 天）还挂着
        assert thought_service.purge_expired(s, now=NOW + timedelta(days=4)) == 1
        assert [t.state for t in thought_service.alive(s, friend_id)] == ["awaiting"]
        # 8 天后：待回也放下了
        assert thought_service.purge_expired(s, now=NOW + timedelta(days=8)) == 1
        assert thought_service.alive(s, friend_id) == []


# ---------- 工具：她自己写 ----------


def test_save_thought_tool_writes_a_note(friend_id):
    with db.session_scope() as s:
        record = tools.execute(
            s, friend_id, {"id": "t1", "name": "save_thought", "arguments": {"content": "答应帮他查一下"}}
        )
        rows = s.query(Thought).all()
    assert record["ok"] is True
    assert len(rows) == 1 and rows[0].content == "答应帮他查一下"


def test_save_thought_tool_refuses_empty_content(friend_id):
    with db.session_scope() as s:
        record = tools.execute(
            s, friend_id, {"id": "t1", "name": "save_thought", "arguments": {"content": "   "}}
        )
    assert record["ok"] is False


# ---------- 界面接口：跟记忆放一个面板 → 要能看、能划掉 ----------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.app import create_app

    db_file = tmp_path / "thoughts-api.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    app = create_app()
    with TestClient(app) as c:
        yield c


def _friend_via_api(client, name="阿月"):
    return client.post("/api/friends", json={"name": name}).json()["id"]


def test_thoughts_api_lists_and_drops(client):
    fid = _friend_via_api(client)
    with db.session_scope() as s:
        thought_service.add(s, fid, content="问他面试结果")
        said, _ = thought_service.add(s, fid, content="说说那件事")
        thought_service.mark_said(s, said, now=NOW)
    body = client.get(f"/api/friends/{fid}/thoughts").json()
    assert body["counts"] == {"pending": 1, "awaiting": 1, "alive": 2}
    assert len(body["thoughts"]) == 2
    # 划掉一张 → 从"还惦记着"里消失
    first = body["thoughts"][0]["id"]
    assert client.delete(f"/api/thoughts/{first}").status_code == 204
    after = client.get(f"/api/friends/{fid}/thoughts").json()
    assert after["counts"]["alive"] == 1


def test_thoughts_api_404_for_unknown_friend(client):
    assert client.get("/api/friends/nope/thoughts").status_code == 404
