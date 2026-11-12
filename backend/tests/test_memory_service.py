"""T3.2 验收：记忆服务 + 权限 + 去重（D28 / D29 / D230 / 宪法附录 B）。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.models import Friend
from backend.services import memory_service
from backend.services.actor import Actor
from backend.services.errors import NotFoundError, PermissionError_


@pytest.fixture()
def friend_id(tmp_path, monkeypatch):
    db_file = tmp_path / "mem.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as s:
        s.add(Friend(id="f1", name="阿月", created_at=now, updated_at=now))
        s.add(Friend(id="f2", name="老王", created_at=now, updated_at=now))
    return "f1"


def _ids(session, fid="f1"):
    return sorted(m.id for m in memory_service.list_alive(session, fid))


# ---------- 新增 ----------


def test_save_creates_new_memory(friend_id):
    with db.session_scope() as s:
        memory, merged = memory_service.save(
            s, Actor.AGENT, friend_id, content="用户生日 5 月 20 日", layer="pinned"
        )
        assert merged is False
        assert memory.layer == "pinned"
        assert memory.tag == "normal"
        assert memory.refreshed_at == memory.created_at


def test_short_term_gets_expiry(friend_id):
    with db.session_scope() as s:
        memory, _ = memory_service.save(
            s, Actor.AGENT, friend_id, content="今天用户心情不错", layer="short_term"
        )
        assert memory.expires_at is not None


def test_purge_expired_hard_deletes(friend_id):
    with db.session_scope() as s:
        memory, _ = memory_service.save(
            s, Actor.AGENT, friend_id, content="过期的", layer="short_term"
        )
        memory.expires_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with db.session_scope() as s:
        assert memory_service.purge_expired(s) == 1
        assert _ids(s) == []


# ---------- 去重（D28 / D29）----------


def test_near_duplicate_merges_and_refreshes(friend_id):
    """说第二遍同样的事 → 不新增，刷新 refreshed_at、重要度只升不降、内容取最新。"""
    with db.session_scope() as s:
        first, merged = memory_service.save(
            s, Actor.AGENT, friend_id, content="用户的生日是5月20号", importance=0.5
        )
        assert merged is False
        first.refreshed_at = "2026-01-01T00:00:00+00:00"  # 装作很久没提过

    with db.session_scope() as s:
        again, merged = memory_service.save(
            s, Actor.AGENT, friend_id, content="用户生日是5月20日", importance=0.9
        )
        assert merged is True
        assert again.id == first.id
        assert again.content == "用户生日是5月20日"
        assert again.importance == 0.9
        assert again.refreshed_at > "2026-01-01T00:00:00+00:00"
        assert len(_ids(s)) == 1, "去重命中时不许新增"


def test_importance_never_drops_on_merge(friend_id):
    with db.session_scope() as s:
        memory_service.save(s, Actor.AGENT, friend_id, content="用户不吃香菜", importance=0.9)
        memory, merged = memory_service.save(
            s, Actor.AGENT, friend_id, content="用户不吃香菜", importance=0.2
        )
        assert merged is True
        assert memory.importance == 0.9


def test_dedup_does_not_cross_layers_or_friends(friend_id):
    content = "用户生日是5月20号"
    with db.session_scope() as s:
        memory_service.save(s, Actor.AGENT, friend_id, content=content, layer="pinned")
        _, merged_same_layer = memory_service.save(
            s, Actor.AGENT, friend_id, content=content, layer="long_term"
        )
        assert merged_same_layer is False, "跨层不许合并（D29 第 1 条）"

        _, merged_other_friend = memory_service.save(
            s, Actor.AGENT, "f2", content=content, layer="pinned"
        )
        assert merged_other_friend is False, "朋友之间必须隔离"
        assert len(_ids(s, "f1")) == 2
        assert len(_ids(s, "f2")) == 1


def test_different_content_does_not_merge(friend_id):
    with db.session_scope() as s:
        memory_service.save(s, Actor.AGENT, friend_id, content="用户是后端开发")
        _, merged = memory_service.save(s, Actor.AGENT, friend_id, content="用户最近在学 Rust")
        assert merged is False
        assert len(_ids(s)) == 2


def test_similarity_is_conservative():
    """阈值 0.85 是实测定标出来的（D48）：下面两组必须被它分开。"""
    should_merge = [
        ("用户的生日是5月20号", "用户生日是5月20日"),
        ("用户的猫叫团子", "用户养的猫叫团子"),
        ("用户在学Rust", "用户在学习Rust"),
        ("用户不吃香菜", "用户不吃香菜。"),
    ]
    should_not_merge = [
        ("用户喜欢喝咖啡", "用户喜欢喝茶"),  # 最接近的反例（0.769）
        ("用户最近在学Rust", "用户最近在学 Python"),
        ("用户是后端开发", "用户最近在学Rust"),
        ("用户明天要面试", "用户下个月要搬家"),
    ]
    for a, b in should_merge:
        assert memory_service.similarity(a, b) >= memory_service.DEDUP_THRESHOLD, (a, b)
    for a, b in should_not_merge:
        assert memory_service.similarity(a, b) < memory_service.DEDUP_THRESHOLD, (a, b)
    assert memory_service.similarity("", "什么都行") == 0.0


# ---------- 权限（宪法附录 B）----------


def test_agent_can_modify_and_delete_any_layer(friend_id):
    """D230：朋友对**所有 layer** 都能改 / 删（原来只限 pinned）。"""
    with db.session_scope() as s:
        pinned, _ = memory_service.save(
            s, Actor.AGENT, friend_id, content="钉住的", layer="pinned"
        )
        long_term, _ = memory_service.save(
            s, Actor.AGENT, friend_id, content="长期的", layer="long_term"
        )
        pinned_id, long_term_id = pinned.id, long_term.id

    with db.session_scope() as s:
        assert memory_service.update(
            s, Actor.AGENT, long_term_id, content="改得动"
        ).content == "改得动"
        memory_service.delete(s, Actor.AGENT, long_term_id)
        assert long_term_id not in _ids(s)
        # pinned 照旧能改
        assert memory_service.update(
            s, Actor.AGENT, pinned_id, content="也能改"
        ).content == "也能改"


def test_agent_can_change_layer_and_expiry_follows(friend_id):
    """D230：改层（升 / 降级）；`expires_at` 必须跟着层走（否则会被 short_term 过期清理误杀）。"""
    with db.session_scope() as s:
        memory, _ = memory_service.save(
            s, Actor.AGENT, friend_id, content="其实只是这阵子的事", layer="pinned"
        )
        memory_id = memory.id

    with db.session_scope() as s:
        down = memory_service.update(s, Actor.AGENT, memory_id, layer="long_term")
        assert down.layer == "long_term" and down.expires_at is None
    with db.session_scope() as s:
        short = memory_service.update(s, Actor.AGENT, memory_id, layer="short_term")
        assert short.layer == "short_term" and short.expires_at  # 去短期 → 补寿命
    with db.session_scope() as s:
        up = memory_service.update(s, Actor.AGENT, memory_id, layer="pinned")
        assert up.layer == "pinned" and up.expires_at is None  # 离开短期 → 清寿命
    with db.session_scope() as s:
        with pytest.raises(ValueError):
            memory_service.update(s, Actor.AGENT, memory_id, layer="不存在的层")


def test_agent_cannot_touch_play_style_via_generic_crud(friend_id):
    """D230：`rp_play_style`（演法）走专用工具，通用 CRUD 不许动。"""
    with db.session_scope() as s:
        play_style, _ = memory_service.save_play_style(
            s, Actor.AGENT, friend_id, role="女友", text="软软的"
        )
        play_style_id = play_style.id
    with db.session_scope() as s:
        with pytest.raises(PermissionError_):
            memory_service.update(s, Actor.AGENT, play_style_id, content="换个演法")
        with pytest.raises(PermissionError_):
            memory_service.delete(s, Actor.AGENT, play_style_id)


def test_memory_events_are_recorded(friend_id):
    """D230 留痕：增 / 改 / 删各留一条，改层能看出旧值 → 新值。"""
    with db.session_scope() as s:
        memory, _ = memory_service.save(
            s, Actor.AGENT, friend_id, content="原始", layer="pinned"
        )
        memory_id = memory.id
    with db.session_scope() as s:
        memory_service.update(s, Actor.AGENT, memory_id, content="改过", layer="long_term")
        memory_service.delete(s, Actor.AGENT, memory_id)
    with db.session_scope() as s:
        events = memory_service.list_events(s, friend_id)
        actions = sorted(event.action for event in events)
        assert actions == ["create", "delete", "update"]
        changed = next(event for event in events if event.action == "update")
        assert changed.actor == "agent"
        assert json.loads(changed.before)["layer"] == "pinned"
        assert json.loads(changed.after)["layer"] == "long_term"
        assert json.loads(changed.after)["content"] == "改过"


def test_user_can_modify_and_delete_any_layer(friend_id):
    with db.session_scope() as s:
        memory, _ = memory_service.save(
            s, Actor.AGENT, friend_id, content="长期的", layer="long_term"
        )
        memory_id = memory.id
    with db.session_scope() as s:
        assert memory_service.update(
            s, Actor.USER, memory_id, content="用户改的", importance=0.7
        ).content == "用户改的"
        memory_service.delete(s, Actor.USER, memory_id)
        assert _ids(s) == []
        with pytest.raises(NotFoundError):
            memory_service.delete(s, Actor.USER, memory_id)  # 软删后再删 = 找不到


def test_update_rejects_empty_content(friend_id):
    with db.session_scope() as s:
        memory, _ = memory_service.save(s, Actor.AGENT, friend_id, content="有内容")
        with pytest.raises(ValueError):
            memory_service.update(s, Actor.USER, memory.id, content="   ")


def test_save_rejects_bad_enum_and_empty_content(friend_id):
    with db.session_scope() as s:
        with pytest.raises(ValueError):
            memory_service.save(s, Actor.AGENT, friend_id, content="x", layer="超级重要")
        with pytest.raises(ValueError):
            memory_service.save(s, Actor.AGENT, friend_id, content="   ")


# ---------- 回忆（D27）与浏览（api.md §3.1）----------


def test_recall_matches_keywords_and_stays_isolated(friend_id):
    with db.session_scope() as s:
        memory_service.save(s, Actor.AGENT, friend_id, content="用户的猫叫团子", layer="long_term")
        memory_service.save(s, Actor.AGENT, friend_id, content="用户不吃香菜", layer="long_term")
        memory_service.save(s, Actor.AGENT, "f2", content="团子其实是我的猫", layer="long_term")

    with db.session_scope() as s:
        hits = memory_service.recall(s, friend_id, "团子")
        assert [m.content for m in hits][:1] == ["用户的猫叫团子"]
        assert all(m.friend_id == friend_id for m in hits), "别的朋友的记忆不能混进来"


def test_search_filters_and_total(friend_id):
    with db.session_scope() as s:
        memory_service.save(s, Actor.AGENT, friend_id, content="用户的猫叫团子", layer="pinned")
        memory_service.save(s, Actor.AGENT, friend_id, content="用户不吃香菜", layer="long_term")
        memory_service.save(
            s, Actor.AGENT, friend_id, content="想吃火锅", layer="long_term", tag="rp"
        )

        all_items, total = memory_service.search(s, friend_id)
        assert total == 3 and len(all_items) == 3

        only_long, total_long = memory_service.search(s, friend_id, layer="long_term")
        assert total_long == 2
        assert all(m.layer == "long_term" for m in only_long)

        rp_only, _ = memory_service.search(s, friend_id, tag="rp")
        assert [m.content for m in rp_only] == ["想吃火锅"]

        found, total_found = memory_service.search(s, friend_id, text="香菜")
        assert total_found == 1 and found[0].content == "用户不吃香菜"
