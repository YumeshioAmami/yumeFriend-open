"""V6.0-3：情节记忆 / 停歇反思 / prompt 注入。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import backend.config as config
import backend.db as db
from backend.agent import context as ctx
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Friend
from backend.services import character_state_service, episode_service, message_service, reflection_service


@pytest.fixture()
def friend_id(tmp_path, monkeypatch):
    db_file = tmp_path / "episodes.db"
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


class FakeProvider:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[list[dict]] = []
        self.config = ProviderConfig(
            name="fake",
            base_url="http://x",
            api_key="k",
            model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, messages, **_kwargs):
        self.calls.append(messages)
        yield {"type": "content", "delta": self.text}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}


class EmptyProvider(FakeProvider):
    async def chat_stream(self, messages, **_kwargs):
        self.calls.append(messages)
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}


class ErrorProvider(FakeProvider):
    async def chat_stream(self, messages, **_kwargs):
        self.calls.append(messages)
        yield {"type": "error", "error": "401 invalid api key"}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}


def test_episode_prompt_note_groups_episodes_and_understanding(friend_id):
    with db.session_scope() as s:
        episode_service.save(
            s,
            friend_id,
            kind="episode",
            summary="他说自己不是懒，是根本启动不了。",
            meaning="他怕被当成不努力的人。",
            tone="发沉",
            unresolved="还没聊到怎么重构日程。",
        )
        episode_service.save(
            s,
            friend_id,
            kind="correction",
            summary="我一开始把无法启动解释成坐不住，他纠正了我。",
            confidence=0.9,
        )
        episode_service.save(
            s,
            friend_id,
            kind="insight",
            summary="他灵感驱动，讨厌被日程表限制。",
            confidence=0.8,
        )
        note = episode_service.prompt_note(s, friend_id)
        assert "# 你们之间" in note
        assert "# 你目前对他的理解" in note
        assert "无法启动" in note
        assert "坐不住" in note
        assert "灵感驱动" in note
        assert "kind" not in note


async def test_reflection_saves_episode_insight_correction_and_state(friend_id):
    payload = {
        "episodes": [
            {
                "summary": "他解释自己不是坐不住，是启动不了；我一开始又给了学习方案。",
                "meaning": "他需要先被理解，不是先被安排。",
                "tone": "有点闷",
                "unresolved": "还没聊完怎么重构日程。",
                "importance": 0.8,
                "confidence": 0.9,
                "evidence": ["我的痛点是，我根本就学不起来"],
                "source_message_ids": [],
            }
        ],
        "insights": [
            {
                "summary": "他灵感驱动，讨厌被日程表限制。",
                "meaning": "给他自由环境比给他计划更有用。",
                "confidence": 0.8,
                "evidence": ["我这种灵感驱动的人最讨厌的就是定时任务"],
                "source_message_ids": [],
            }
        ],
        "corrections": [
            {
                "summary": "不能把“无法启动”解释成“坐不住”。",
                "meaning": "下次先问是哪条规矩卡住他。",
                "confidence": 0.9,
                "evidence": ["不是这样，是无法启动"],
                "source_message_ids": [],
            }
        ],
        "state_delta": {
            "appraisal": "我这次又急着解题了。",
            "affect_delta": {"valence": -0.1, "connection": 0.1},
            "energy": 0.45,
            "intent": "问清楚",
            "inner_note": "下次先别给方案。",
            "relationship_note": "想靠近一点，但别再当顾问。",
        },
    }
    provider = FakeProvider(json.dumps(payload, ensure_ascii=False))
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        for index, (role, content) in enumerate(
            [
                ("user", "我的痛点是，我根本就学不起来"),
                ("assistant", "那先换个说法。"),
                ("user", "不是这样，是无法启动"),
                ("assistant", "我找错方向了。"),
            ],
            start=1,
        ):
            message_service.create(
                s,
                friend_id,
                role=role,
                content=content,
                stream_status=message_service.COMPLETED,
            )
        stats = await reflection_service.run_reflection(s, friend, provider, now=datetime.now(timezone.utc))
        assert stats["episodes"] == 1
        assert stats["insights"] == 1
        assert stats["corrections"] == 1
        assert friend.last_reflection_at is not None
        assert character_state_service.state_to_dict(
            character_state_service.get_or_create(s, friend_id)
        )["intent"] == "问清楚"
        shown = episode_service.prompt_note(s, friend_id)
        assert "无法启动" in shown
        assert "先问是哪条规矩" in shown
    assert "刚才的对话" in provider.calls[0][1]["content"]


async def test_bad_reflection_does_not_advance_watermark(friend_id):
    provider = FakeProvider("这不是 JSON")
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        for _ in range(4):
            message_service.create(
                s,
                friend_id,
                role="user",
                content="一句话",
                stream_status=message_service.COMPLETED,
            )
        with pytest.raises(ValueError):
            await reflection_service.run_reflection(s, friend, provider)
        assert friend.last_reflection_at is None


async def test_empty_reflection_provider_is_reported_honestly(friend_id):
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        for _ in range(4):
            message_service.create(
                s,
                friend_id,
                role="user",
                content="一句话",
                stream_status=message_service.COMPLETED,
            )
        with pytest.raises(ValueError, match="没有返回内容"):
            await reflection_service.run_reflection(s, friend, EmptyProvider(""))
        assert friend.last_reflection_at is None


async def test_reflection_provider_error_is_reported_honestly(friend_id):
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        for _ in range(4):
            message_service.create(
                s,
                friend_id,
                role="user",
                content="一句话",
                stream_status=message_service.COMPLETED,
            )
        with pytest.raises(ValueError, match="401 invalid api key"):
            await reflection_service.run_reflection(s, friend, ErrorProvider(""))
        assert friend.last_reflection_at is None


async def test_reflection_tick_calls_llm_outside_db_session(monkeypatch):
    """D319：等待模型时不能持有 SQLite session。"""
    active = {"value": False}

    @contextmanager
    def fake_scope():
        active["value"] = True
        try:
            yield SimpleNamespace(
                get=lambda *_args, **_kwargs: SimpleNamespace(
                    id="f1", name="汐音", deleted_at=None
                )
            )
        finally:
            active["value"] = False

    async def fake_call(_provider, _prompt):
        assert active["value"] is False
        return {"episodes": [], "insights": [], "corrections": []}

    monkeypatch.setattr(db, "session_scope", fake_scope)
    monkeypatch.setattr(
        reflection_service,
        "_find_candidates",
        lambda *_args, **_kwargs: [SimpleNamespace(id="f1", name="汐音")],
    )
    monkeypatch.setattr(
        reflection_service,
        "_prepare_reflection",
        lambda *_args, **_kwargs: {
            "prompt": [],
            "scanned": 4,
            "occurred_at": "2026-10-22T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        reflection_service,
        "_call_reflection_provider",
        fake_call,
    )
    monkeypatch.setattr(
        reflection_service,
        "_apply_reflection",
        lambda *_args, **_kwargs: {
            "episodes": 0,
            "insights": 0,
            "corrections": 0,
        },
    )
    monkeypatch.setattr(
        reflection_service.settings_service,
        "llm_config",
        lambda _session: ("[]", None),
    )
    monkeypatch.setattr(
        "backend.llm.factory.create_provider",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )

    handled = await reflection_service.tick()
    assert handled == 1


def test_context_injects_episode_note(friend_id):
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        messages = ctx.build_messages(
            friend,
            user_input="在吗",
            now=datetime(2026, 10, 21, 22, 0),
            tool_lines=[],
            episode_note="# 你们之间\n- 他上次说无法启动。",
        )
        assert "# 你们之间" in messages[0]["content"]
        assert "他上次说无法启动" in messages[0]["content"]
