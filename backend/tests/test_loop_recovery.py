"""v4.1（D246）：空回复 / 被截断的兜底重试。

背景（真实数据：112/1090 条 assistant 消息是空的）：`deep_thinking` 开着时，思考链和正文
**共用 max_tokens** —— 模型偶尔整轮只思考不说话（正文空），或者思考吃满预算把正文截断。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.agent import loop
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Friend


class ScriptedProvider:
    """按脚本吐事件（每次调用取下一段脚本，越界就复用最后一段）。"""

    def __init__(self, scripts: list[list[dict]]):
        self._scripts = scripts
        self.calls: list[dict] = []
        self.config = ProviderConfig(
            name="fake",
            base_url="http://x",
            api_key="k",
            model="m",
            capabilities=ProviderCapabilities(reasoning=True),
        )

    async def chat_stream(self, messages, tools=None, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self._scripts) - 1)
        for event in self._scripts[index]:
            yield event

    async def health_check(self) -> bool:
        return True


@pytest.fixture()
def friend_id(tmp_path, monkeypatch):
    db_file = tmp_path / "recovery.db"
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


def _text(events: list[dict]) -> str:
    return "".join(e["delta"] for e in events if e["type"] == "content")


async def test_tool_rounds_can_be_capped_for_game_turns(friend_id):
    """D270：游戏回合可以只给 1 轮工具，避免一轮里跑 5 轮拖慢。"""
    tool_block = '<tool>{"name":"get_time","arguments":{}}</tool>'
    provider = ScriptedProvider(
        [
            [
                {"type": "content", "delta": tool_block},
                {"type": "done", "usage": {}, "stop_reason": "stop"},
            ],
            [
                {"type": "content", "delta": "收尾。"},
                {"type": "done", "usage": {}, "stop_reason": "stop"},
            ],
        ]
    )
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        events = [
            e
            async for e in loop.stream_reply(
                friend=friend,
                user_input="现在几点",
                provider=provider,
                session=s,
                max_tool_rounds=1,
            )
        ]
    assert _text(events) == "收尾。"
    assert len(provider.calls) == 2


async def test_usage_is_accumulated_across_all_provider_calls(friend_id):
    tool_block = '<tool>{"name":"get_time","arguments":{}}</tool>'
    provider = ScriptedProvider(
        [
            [
                {"type": "content", "delta": tool_block},
                {
                    "type": "done",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "stop_reason": "stop",
                },
            ],
            [
                {"type": "content", "delta": "收尾。"},
                {
                    "type": "done",
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 7,
                        "completion_tokens_details": {"reasoning_tokens": 4},
                    },
                    "stop_reason": "stop",
                },
            ],
        ]
    )
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        events = [
            event
            async for event in loop.stream_reply(
                friend=friend,
                user_input="现在几点",
                provider=provider,
                session=s,
                max_tool_rounds=2,
                usage_purpose="test",
            )
        ]
    done = events[-1]
    assert done["type"] == "done"
    assert done["usage"]["provider_calls"] == 2
    assert done["usage"]["input_tokens"] == 30
    assert done["usage"]["output_tokens"] == 12
    assert done["usage"]["reasoning_tokens"] == 4
    assert done["usage"]["total_tokens"] == 42


async def test_same_tool_error_twice_stops_retrying(friend_id):
    """D281：同一个工具连续撞同一个错误，不许无限换参数重试。"""
    bad = '<tool>{"name":"game_do","arguments":{"action":"sequence","args":{%s}}}</tool>'
    provider = ScriptedProvider(
        [
            [
                {"type": "content", "delta": bad % '"a":1'},
                {"type": "done", "usage": {}, "stop_reason": "stop"},
            ],
            [
                {"type": "content", "delta": bad % '"b":2'},
                {"type": "done", "usage": {}, "stop_reason": "stop"},
            ],
            [
                {"type": "content", "delta": bad % '"c":3'},
                {"type": "done", "usage": {}, "stop_reason": "stop"},
            ],
            [
                {"type": "content", "delta": "这一步做不了。"},
                {"type": "done", "usage": {}, "stop_reason": "stop"},
            ],
        ]
    )
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        events = [
            event
            async for event in loop.stream_reply(
                friend=friend,
                user_input="试试",
                provider=provider,
                session=s,
                game_turn=True,
                max_tool_rounds=5,
            )
        ]
    assert len(provider.calls) == 3  # 两轮失败 + 收尾；第三轮工具块不再执行
    assert any(event.get("type") == "tool_result" for event in events)


async def test_empty_reply_is_retried_without_thinking(friend_id):
    """整轮只有思考链、一个字都没说 → 关掉思考链重试一次，拿到正文。"""
    provider = ScriptedProvider(
        [
            [
                {"type": "reasoning", "delta": "（想了一大堆但没说出口）"},
                {"type": "done", "usage": {}, "stop_reason": "stop"},
            ],
            [
                {"type": "content", "delta": "我听见了。"},
                {"type": "done", "usage": {}, "stop_reason": "stop"},
            ],
        ]
    )
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        events = [
            e
            async for e in loop.stream_reply(
                friend=friend, user_input="在吗", provider=provider, session=s, deep_thinking=True
            )
        ]
    assert _text(events) == "我听见了。"
    assert len(provider.calls) == 2
    assert provider.calls[0].get("reasoning_effort") == "high"  # 第一次带思考
    assert "reasoning_effort" not in provider.calls[1]  # 重试关掉思考，预算全留给正文


async def test_truncated_reply_is_continued(friend_id):
    """被 max_tokens 截断（停止=length）→ 让它接着说完，两段拼成一句。"""
    provider = ScriptedProvider(
        [
            [
                {"type": "content", "delta": "前半句，"},
                {"type": "done", "usage": {}, "stop_reason": "length"},
            ],
            [
                {"type": "content", "delta": "后半句。"},
                {"type": "done", "usage": {}, "stop_reason": "stop"},
            ],
        ]
    )
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        events = [
            e
            async for e in loop.stream_reply(
                friend=friend, user_input="讲个长的", provider=provider, session=s, deep_thinking=True
            )
        ]
    assert _text(events) == "前半句，后半句。"
    assert len(provider.calls) == 2
