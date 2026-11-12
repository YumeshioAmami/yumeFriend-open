"""v7.12 / D343：活动驱动的主动分享。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Friend, FriendLife, LifeProject, Message
from backend.services import life_service, proactive_service, sync_service


@pytest.fixture()
def share_friend(tmp_path, monkeypatch):
    db_file = tmp_path / "life-share.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as session:
        sync_service.configure_device(session, name="测试电脑", kind="desktop")
        session.add(
            Friend(
                id="f-share",
                name="汐音",
                presence=1,
                life_enabled=1,
                created_at=now,
                updated_at=now,
            )
        )
        life = life_service.get_or_create_life(session, "f-share")
        life.pending_share = "刷 B 站看到：一个测试视频"
        session.flush()
    return "f-share"


class ScriptedProvider:
    def __init__(self, text: str):
        self.text = text
        self.config = ProviderConfig(
            name="fake",
            base_url="http://x",
            api_key="k",
            model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, messages, tools=None, **_kwargs):  # noqa: ANN001, ANN201
        yield {"type": "content", "delta": self.text}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}


def test_life_share_delivers_short_bubbles_and_clears_pending(share_friend):
    provider = ScriptedProvider("[20:10] 我刚刷到一个视频。<<<MSG>>>你肯定想看。")
    with db.session_scope() as session:
        out = asyncio.run(proactive_service.run_life_share_tick(session, provider))
    assert out["sent"] == 1
    assert out["bubbles"] == 2
    with db.session_scope() as session:
        msg = session.query(Message).filter_by(friend_id=share_friend).one()
        assert msg.is_proactive == 1
        assert "[20:10]" not in msg.content
        assert len(json.loads(msg.segments)) == 2
        life = session.get(FriendLife, share_friend)
        assert life.pending_share == ""
        assert life.last_share_at


def test_life_share_respects_silence(share_friend):
    provider = ScriptedProvider("[沉默]")
    with db.session_scope() as session:
        out = asyncio.run(proactive_service.run_life_share_tick(session, provider))
    assert out["sent"] == 0
    assert out["reason"] == "silent"
    with db.session_scope() as session:
        assert session.query(Message).filter_by(friend_id=share_friend).count() == 0
        assert session.get(FriendLife, share_friend).pending_share == ""


def test_life_share_saves_her_thought_into_project(share_friend):
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as session:
        life = session.get(FriendLife, share_friend)
        life.pending_share = "接着上次的收音机话题，又看到一篇新文章"
        session.add(
            LifeProject(
                id="p-share",
                friend_id=share_friend,
                topic="老式收音机修复",
                status="active",
                source="rss:少数派",
                step_count=2,
                reading_excerpt="修好之后，喇叭里先传出一阵电流声。",
                content_depth="full_text",
                last_thought="上次我说这声音很有意思。",
                open_question="它后来还能用吗？",
                evidence="[]",
                last_activity_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    provider = ScriptedProvider(
        "我后来又看到一段。<<<MSG>>>修好以后居然还能收到台。<<<MSG>>>你说这算不算很神奇？"
    )
    with db.session_scope() as session:
        out = asyncio.run(proactive_service.run_life_share_tick(session, provider))
    assert out["sent"] == 1
    with db.session_scope() as session:
        project = session.get(LifeProject, "p-share")
        assert "我后来又看到一段" in project.last_thought
        assert project.open_question.endswith("？")
        assert project.last_shared_at
