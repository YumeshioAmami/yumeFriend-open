"""V6.0-1：角色状态、评价状态块、prompt 注入。"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

import backend.config as config
import backend.db as db
from backend.agent import context as ctx
from backend.agent import loop
from backend.agent.state_block import StateBlockParser, strip_state_blocks
from backend.api import messages as messages_api
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Friend, FriendState, Message
from backend.services import character_state_service, message_service


@pytest.fixture()
def friend_id(tmp_path, monkeypatch):
    db_file = tmp_path / "character-state.db"
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


class ScriptedProvider:
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

    async def chat_stream(self, messages, tools=None, **_kwargs):  # noqa: ANN001, ANN201
        self.calls.append(messages)
        yield {"type": "content", "delta": self.text}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


def test_default_state_is_created(friend_id):
    with db.session_scope() as s:
        state = character_state_service.get_or_create(s, friend_id)
        assert state.intent == "observe"
        assert state.energy == pytest.approx(0.55)
        assert "valence" in state.affect


def test_apply_delta_updates_and_clamps(friend_id):
    with db.session_scope() as s:
        state = character_state_service.apply_delta(
            s,
            friend_id,
            {
                "appraisal": "他刚才那句话像在否定我",
                "affect_delta": {"valence": -2.0, "security": -2.0, "connection": -0.2},
                "energy": 2.0,
                "intent": "怼",
                "inner_note": "我又不是只会讲道理。",
                "relationship_note": "有点被推远。",
                "concerns": ["他是不是觉得我没用", "他是不是觉得我没用", "刚才那句话"],
                "open_loops_add": ["他说要重构整个系统"],
            },
        )
        snapshot = character_state_service.state_to_dict(state)
        assert snapshot["affect"]["valence"] == pytest.approx(-1.0)
        assert snapshot["affect"]["security"] == pytest.approx(-0.45)
        assert snapshot["energy"] == pytest.approx(1.0)
        assert snapshot["intent"] == "怼"
        assert snapshot["concerns"] == ["他是不是觉得我没用", "刚才那句话"]
        assert len(snapshot["open_loops"]) == 1


def test_open_loop_can_be_resolved(friend_id):
    with db.session_scope() as s:
        character_state_service.apply_delta(
            s,
            friend_id,
            {"open_loops_add": ["他说要重构整个系统"]},
        )
        character_state_service.apply_delta(
            s,
            friend_id,
            {"open_loops_resolve": ["重构"]},
        )
        state = character_state_service.get_or_create(s, friend_id)
        assert character_state_service.state_to_dict(state)["open_loops"] == []


def test_state_prompt_is_natural_language(friend_id):
    with db.session_scope() as s:
        state = character_state_service.apply_delta(
            s,
            friend_id,
            {
                "affect_delta": {"valence": -0.4, "connection": -0.3},
                "energy": 0.2,
                "intent": "沉默",
                "inner_note": "先不想解释。",
                "relationship_note": "有点累。",
                "open_loops_add": ["他刚才说我不懂他"],
            },
        )
        prompt = character_state_service.to_prompt(state)
        assert "# 你现在" in prompt
        assert "心里有点发沉" in prompt
        assert "有点想退开" in prompt
        assert "先不想解释。" in prompt
        assert "他刚才说我不懂他" in prompt
        for internal in ("valence", "arousal", "security", "connection", "affect_delta"):
            assert internal not in prompt


def test_runtime_rules_keep_inner_note_optional():
    """D313：inner_note 有想法就自然说，没有不硬造下文。"""
    rules = character_state_service.STATE_RUNTIME_RULES
    assert "没有也不必硬造" in rules
    assert "不按检查表凑" in rules
    assert "不要像任务清单" in rules


def test_active_thread_is_maintained_and_cleared(friend_id):
    with db.session_scope() as s:
        state = character_state_service.apply_delta(
            s,
            friend_id,
            {
                "active_thread": {
                    "goal": "决定要不要重构日程系统",
                    "constraints": ["不能打断灵感"],
                    "candidates": [
                        {"idea": "只设两个检查点", "note": "不按天排"},
                        {"idea": "完全取消日程", "note": "风险高"},
                    ],
                    "decisions": ["先不动学习计划"],
                    "open_questions": ["ddl 和灵感怎么共存"],
                }
            },
        )
        prompt = character_state_service.to_prompt(state)
        assert "# 你们正在一起想" in prompt
        assert "决定要不要重构日程系统" in prompt
        assert "只设两个检查点" in prompt
        assert "ddl 和灵感怎么共存" in prompt
        assert character_state_service.state_to_dict(state)["active_thread"]["goal"].startswith(
            "决定要不要"
        )

        character_state_service.apply_delta(s, friend_id, {"active_thread": None})
        cleared = character_state_service.get_or_create(s, friend_id)
        assert cleared.active_thread is None


def test_state_block_parser_handles_chunked_tags():
    parser = StateBlockParser()
    events = []
    for chunk in (
        "<state_",
        'delta>{"affect_delta":{"valence":0.2},"intent":"逗"}</state_',
        "delta>\n\n在。",
    ):
        events.extend(parser.feed(chunk))
    events.extend(parser.flush())
    assert events[0]["type"] == "state_delta"
    assert events[0]["delta"]["intent"] == "逗"
    assert "".join(e["delta"] for e in events if e["type"] == "content") == "在。"


def test_state_block_parser_passes_plain_text():
    parser = StateBlockParser()
    events = parser.feed("在。")
    assert events == [{"type": "content", "delta": "在。"}]


def test_state_block_parser_strips_trailing_block():
    """正常正文在前、状态块在后时也不能漏到用户眼前（2026-10-25 实机回归）。"""
    parser = StateBlockParser()
    events = []
    for chunk in ("先说一句。", "\n\n<state_del", 'ta>{"intent":"逗"}</state_delta>'):
        events.extend(parser.feed(chunk))
    events.extend(parser.flush())
    text = "".join(item["delta"] for item in events if item["type"] == "content")
    assert text.strip() == "先说一句。"
    assert "<state_delta>" not in text
    assert any(item["type"] == "state_delta" for item in events)


def test_strip_state_blocks_removes_residue():
    raw = "正文\n<state_delta>{bad}</state_delta>\n尾巴"
    assert "state_delta" not in strip_state_blocks(raw)
    assert "正文" in strip_state_blocks(raw)


def test_segment_parser_handles_chunked_break():
    from backend.agent.segment_block import SegmentParser

    parser = SegmentParser()
    events = []
    for chunk in ("好。<<<M", "SG>>>再一", "句。"):
        events.extend(parser.feed(chunk))
    events.extend(parser.flush())
    assert "".join(e["delta"] for e in events if e["type"] == "content") == "好。再一句。"
    assert any(e["type"] == "segment_break" for e in events)


def test_context_injects_state_and_runtime_rules(friend_id):
    with db.session_scope() as s:
        state = character_state_service.apply_delta(
            s,
            friend_id,
            {"intent": "观察", "inner_note": "先看看他想说什么。"},
        )
        friend = s.get(Friend, friend_id)
        messages = ctx.build_messages(
            friend,
            user_input="在吗",
            now=datetime(2026, 10, 21, 21, 0),
            character_state=state,
            tool_lines=[],
        )
        system = messages[0]["content"]
        assert "# 你现在" in system
        assert "先看看他想说什么。" in system
        assert "state_delta" in system
        assert "你不是先判断“该怎么回”" in system


async def test_loop_emits_state_delta_and_hides_it_from_content(friend_id):
    provider = ScriptedProvider(
        '<state_delta>{"affect_delta":{"valence":0.2},"intent":"逗",'
        '"inner_note":"他今天有点意思。"}</state_delta>\n\n在。'
    )
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        state = character_state_service.get_or_create(s, friend_id)
        events = [
            e
            async for e in loop.stream_reply(
                friend=friend,
                user_input="在吗",
                provider=provider,
                session=s,
                character_state=state,
            )
        ]
    assert any(e.get("type") == "state_delta" for e in events)
    text = "".join(e["delta"] for e in events if e.get("type") == "content")
    assert text == "在。"
    assert "<state_delta>" not in text
    # 状态块也要在模型上下文里，而不是只用于事后展示
    assert "# 你现在" in provider.calls[0][0]["content"]


async def test_loop_emits_segment_break_and_strips_marker(friend_id):
    provider = ScriptedProvider(
        '<state_delta>{"intent":"逗"}</state_delta>\n'
        "好。<<<MSG>>>再一句。"
    )
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        state = character_state_service.get_or_create(s, friend_id)
        events = [
            e
            async for e in loop.stream_reply(
                friend=friend,
                user_input="在吗",
                provider=provider,
                session=s,
                character_state=state,
            )
        ]
    text = "".join(e["delta"] for e in events if e.get("type") == "content")
    assert text == "好。再一句。"
    assert not any("<<<MSG>>>" in e.get("delta", "") for e in events)
    assert sum(1 for e in events if e.get("type") == "segment_break") == 1


def test_message_to_dict_includes_segments(friend_id):
    with db.session_scope() as s:
        row = Message(
            id="m1",
            friend_id=friend_id,
            role="assistant",
            content="好。\n再一句。",
            segments=json.dumps(["好。", "再一句。"], ensure_ascii=False),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        s.add(row)
        s.flush()
        assert message_service.to_dict(row)["segments"] == ["好。", "再一句。"]


async def test_sse_stream_persists_state_and_segments(friend_id, monkeypatch):
    monkeypatch.setattr(messages_api.voice_reply_service, "open_stream", lambda _mid: None)
    monkeypatch.setattr(messages_api.voice_reply_service, "schedule", lambda _mid: None)
    provider = ScriptedProvider(
        '<state_delta>{"intent":"逗","inner_note":"他今天有点意思。"}</state_delta>'
        "好。<<<MSG>>>再一句。"
    )
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        state = character_state_service.get_or_create(s, friend_id)
        user = message_service.create(
            s,
            friend_id,
            role="user",
            content="在吗",
            stream_status=message_service.COMPLETED,
        )
        assistant = message_service.create(
            s,
            friend_id,
            role="assistant",
            content="",
            stream_status=message_service.STREAMING,
        )
        s.commit()
        async for _frame in messages_api.sse_stream(
            friend=friend,
            user_input="在吗",
            provider=provider,
            history=[],
            memories=[],
            user_message_id=user.id,
            assistant_id=assistant.id,
            deep_thinking=False,
            session=s,
            character_state=state,
        ):
            pass

    with db.session_scope() as s:
        saved = s.get(Message, assistant.id)
        assert saved is not None
        assert saved.content == "好。\n再一句。"
        assert json.loads(saved.segments or "[]") == ["好。", "再一句。"]
        saved_state = s.get(FriendState, friend_id)
        assert saved_state is not None
        assert saved_state.intent == "逗"


async def test_sse_stream_keeps_paragraphs_in_content(friend_id, monkeypatch):
    """模型没写 <<<MSG>>> 时，正文保留自然段；落库结构不在这里强切。"""
    monkeypatch.setattr(messages_api.voice_reply_service, "open_stream", lambda _mid: None)
    monkeypatch.setattr(messages_api.voice_reply_service, "schedule", lambda _mid: None)
    provider = ScriptedProvider(
        '<state_delta>{"intent":"哄"}</state_delta>\n'
        "先别急着解释。\n\n你这声「哎」听着不对。\n\n说吧，怎么了？"
    )
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        state = character_state_service.get_or_create(s, friend_id)
        user = message_service.create(
            s, friend_id, role="user", content="哎", stream_status=message_service.COMPLETED
        )
        assistant = message_service.create(
            s, friend_id, role="assistant", content="", stream_status=message_service.STREAMING
        )
        s.commit()
        async for _frame in messages_api.sse_stream(
            friend=friend,
            user_input="哎",
            provider=provider,
            history=[],
            memories=[],
            user_message_id=user.id,
            assistant_id=assistant.id,
            deep_thinking=False,
            session=s,
            character_state=state,
        ):
            pass

    with db.session_scope() as s:
        saved = s.get(Message, assistant.id)
        assert saved is not None
        assert "先别急着解释。" in saved.content
        assert "你这声「哎」听着不对。" in saved.content
        assert "说吧，怎么了？" in saved.content
        assert json.loads(saved.segments or "[]") == []
