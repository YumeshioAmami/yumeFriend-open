"""v7.15 / D351：主动消息统一开口理由。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Friend, FriendLife, LifeProject, Message, Thought
from backend.services import (
    character_state_service,
    initiative_service,
    life_service,
    proactive_service,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


class FakeProvider:
    def __init__(self, says: str):
        self.says = says
        self.calls: list[list[dict]] = []
        self.config = ProviderConfig(
            name="fake",
            base_url="http://x",
            api_key="k",
            model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, messages, tools=None, **_kwargs):  # noqa: ANN001, ANN201
        self.calls.append(messages)
        yield {"type": "content", "delta": self.says}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}


@pytest.fixture()
def scope(tmp_path, monkeypatch):
    db_file = tmp_path / "initiative.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    return db.session_scope


def _friend(scope, *, life: bool = False, presence: int = 1) -> str:
    with scope() as session:
        session.add(
            Friend(
                id="f-init",
                name="汐音",
                presence=presence,
                life_enabled=1 if life else 0,
                created_at=NOW.isoformat(),
                updated_at=NOW.isoformat(),
            )
        )
    return "f-init"


def _always_hit():
    import random

    rng = random.Random()
    rng.random = lambda: 0.0  # type: ignore[method-assign]
    return rng


def test_life_candidate_wins_and_clears_pending(scope):
    fid = _friend(scope, life=True)
    with scope() as session:
        life = life_service.get_or_create_life(session, fid)
        life.pending_share = "刷 B 站看到：一个测试视频"
        from backend.services import thought_service

        thought_service.add(session, fid, content="问问他面试结果")

    provider = FakeProvider("刚刷到一个视频。<<<MSG>>>你肯定想看。")
    with scope() as session:
        out = asyncio.run(
            proactive_service.run_life_share_tick(session, provider, now=NOW)
        )
    assert out["sent"] == 1
    assert out["kind"] == "life"
    assert out["bubbles"] == 2
    with scope() as session:
        assert session.get(FriendLife, fid).pending_share == ""
        thought = session.query(Thought).filter_by(friend_id=fid).one()
        assert thought.state == "pending"
        msg = session.query(Message).filter_by(friend_id=fid, is_proactive=1).one()
        assert len(json.loads(msg.segments)) == 2


def test_life_prompt_carries_project_continuity(scope):
    fid = _friend(scope, life=True)
    with scope() as session:
        life = life_service.get_or_create_life(session, fid)
        life.pending_share = "接着上次的收音机话题，又看到一篇新文章"
        recent_at = datetime.now(timezone.utc).isoformat()
        session.add(
            LifeProject(
                id="p-init",
                friend_id=fid,
                topic="老式收音机修复",
                status="active",
                source="rss:少数派",
                step_count=2,
                reading_excerpt="修好之后，喇叭里先传出一阵电流声。",
                content_depth="full_text",
                last_thought="上次我说这声音很有意思。",
                open_question="它后来还能用吗？",
                evidence="[]",
                last_activity_at=recent_at,
                created_at=recent_at,
                updated_at=recent_at,
            )
        )
    provider = FakeProvider("我后来又看到一段。<<<MSG>>>它居然还能收到台。")
    with scope() as session:
        out = asyncio.run(
            proactive_service.run_life_share_tick(session, provider, now=NOW)
        )
    assert out["sent"] == 1
    prompt = "\n".join(str(item.get("content")) for item in provider.calls[0])
    assert "老式收音机修复" in prompt
    assert "上次我说这声音很有意思" in prompt
    assert "我后来又看到" in prompt


def test_life_enabled_friend_can_use_thought_when_no_life_share(scope):
    fid = _friend(scope, life=True)
    with scope() as session:
        from backend.services import thought_service

        thought, _ = thought_service.add(session, fid, content="问问他面试结果")

    provider = FakeProvider("对了，你面试怎么样？")
    with scope() as session:
        out = asyncio.run(
            proactive_service.run_life_share_tick(session, provider, now=NOW)
        )
    assert out["sent"] == 1
    assert out["kind"] == "thought"
    with scope() as session:
        stored = session.query(Thought).filter_by(friend_id=fid).one()
        assert stored.state == "awaiting"
        prompt = "\n".join(str(item.get("content")) for item in provider.calls[0])
        assert "问问他面试结果" in prompt


def test_open_loop_is_a_real_candidate_without_life_or_thought(scope):
    fid = _friend(scope)
    with scope() as session:
        character_state_service.apply_delta(
            session,
            fid,
            {"open_loops_add": ["上次那个科技新闻还没聊完"]},
        )
    provider = FakeProvider("上次那个新闻我后来又想到一点。")
    with scope() as session:
        out = asyncio.run(
            proactive_service.run_life_share_tick(session, provider, now=NOW)
        )
    assert out["sent"] == 1
    assert out["kind"] == "open_loop"


def test_30s_tick_does_not_send_a_reasonless_checkin(scope):
    _friend(scope)
    with scope() as session:
        out = asyncio.run(
            proactive_service.run_life_share_tick(
                session, FakeProvider("在吗"), now=NOW
            )
        )
    assert out == {"sent": 0, "reason": "no_candidate"}


def test_empty_life_observation_is_not_a_proactive_candidate(scope):
    fid = _friend(scope, life=True)
    with scope() as session:
        life = life_service.get_or_create_life(session, fid)
        life.pending_share = "搜了半天「B站热门」，结果只跳出两个首页链接，等于什么都没看到。"
    with scope() as session:
        out = asyncio.run(
            proactive_service.run_life_share_tick(
                session, FakeProvider("不该发出去"), now=NOW
            )
        )
    assert out == {"sent": 0, "reason": "no_candidate"}
    with scope() as session:
        assert session.query(Message).filter_by(friend_id=fid, is_proactive=1).count() == 0


def test_calendar_junk_is_not_a_proactive_candidate(scope):
    fid = _friend(scope, life=True)
    with scope() as session:
        life = life_service.get_or_create_life(session, fid)
        life.pending_share = "搜了「今天值得关注的科技新闻」，看到：日历网；今日黄历宜忌查询。"
    with scope() as session:
        out = asyncio.run(
            proactive_service.run_life_share_tick(
                session, FakeProvider("不该发出去"), now=NOW
            )
        )
    assert out == {"sent": 0, "reason": "no_candidate"}


def test_heartbeat_still_falls_back_to_checkin_and_respects_cooldown(scope):
    fid = _friend(scope)
    moment = datetime.now(timezone.utc)
    with scope() as session:
        first = asyncio.run(
            proactive_service.run_heartbeat_tick(
                session,
                FakeProvider("在忙吗？"),
                rng=_always_hit(),
                now=moment,
                screen_watch=False,
            )
        )
        assert first["sent"] == 1
        assert first["kind"] == "checkin"

    with scope() as session:
        out = asyncio.run(
            proactive_service.run_initiative_tick(
                session,
                FakeProvider("又来一句"),
                rng=_always_hit(),
                now=moment + timedelta(minutes=10),
                allow_checkin=True,
            )
        )
    assert out == {"sent": 0, "reason": "no_dice_hit"}


def test_long_proactive_message_is_split_without_losing_text(scope):
    fid = _friend(scope, life=True)
    with scope() as session:
        life = life_service.get_or_create_life(session, fid)
        life.pending_share = "刷到一条很长的观察，讲的是老式收音机修复的过程"
    text = (
        "刚看到一条很有意思的东西，讲的是以前的人怎么做饭。"
        "里面有个细节我看了两遍，觉得有点好笑。"
        "你要是感兴趣，我回头可以再讲给你听。"
    )
    with scope() as session:
        out = asyncio.run(
            proactive_service.run_life_share_tick(
                session, FakeProvider(text), now=NOW
            )
        )
        msg = session.query(Message).filter_by(friend_id=fid, is_proactive=1).one()
        segments = json.loads(msg.segments)
    assert out["sent"] == 1
    assert 1 < len(segments) <= 3
    assert "".join(segments) == text
