"""同一轮里重复的工具调用只执行一次（实测 add_schedule 被吐两遍 → 建了两条重复日程）。"""

from datetime import datetime, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.agent import loop
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Friend
from backend.services import schedule_service


class DuplicatingProvider:
    """把同一个工具块吐两遍（模拟实测里模型的行为）。"""

    def __init__(self, block: str):
        self._block = block
        self.calls = 0
        self.config = ProviderConfig(
            name="fake", base_url="http://x", api_key="k", model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "content", "delta": self._block + "\n" + self._block}
            yield {"type": "done", "usage": {}, "stop_reason": "stop"}
            return
        yield {"type": "content", "delta": "行。"}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


@pytest.fixture()
def friend_id(tmp_path, monkeypatch):
    db_file = tmp_path / "dedupe.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as s:
        s.add(Friend(id="f1", name="阿月", created_at=now, updated_at=now))
    return "f1"


async def test_identical_tool_calls_run_once(friend_id):
    block = '<tool>{"name":"add_schedule","arguments":{"title":"喝水","date":"10m"}}</tool>'
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        events = [
            e
            async for e in loop.stream_reply(
                friend=friend, user_input="10 分钟后提醒我喝水",
                provider=DuplicatingProvider(block), session=s,
            )
        ]
        results = [e for e in events if e["type"] == "tool_result"]
        assert len(results) == 1, "重复的调用不该执行两遍"
        assert len(schedule_service.list_for(s, friend_id=friend_id)) == 1


class RepeatingProvider:
    """**跨轮**重复：第 1 轮调工具，喂回结果后第 2 轮又调一遍（实测就是这个）。"""

    def __init__(self, block: str):
        self._block = block
        self.calls = 0
        self.config = ProviderConfig(
            name="fake", base_url="http://x", api_key="k", model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, messages, tools=None, **kwargs):
        self.calls += 1
        yield {"type": "content", "delta": self._block}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


async def test_repeated_call_across_rounds_runs_once(friend_id):
    block = '<tool>{"name":"add_schedule","arguments":{"title":"喝水","date":"10m"}}</tool>'
    provider = RepeatingProvider(block)
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        events = [
            e
            async for e in loop.stream_reply(
                friend=friend, user_input="10 分钟后提醒我喝水", provider=provider, session=s
            )
        ]
        # 第一轮执行一次；第二轮全是重复 → 直接收尾，不再执行
        assert len([e for e in events if e["type"] == "tool_result"]) == 1
        assert len(schedule_service.list_for(s, friend_id=friend_id)) == 1
