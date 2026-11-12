"""T3.4 验收：停歇补漏（D24 / architecture.md §5.2）。

重点验两件事：**水位线不重复提取**、**任务失败不炸**。
"""

from datetime import datetime, timedelta, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Friend, Message
from backend.services import catchup_service, memory_service
from backend.services.actor import Actor

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _iso(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


class FakeProvider:
    def __init__(self, tool_blocks: list[str] | None = None, error: Exception | None = None):
        self._blocks = tool_blocks or []
        self._error = error
        self.calls = 0
        self.config = ProviderConfig(
            name="fake", base_url="http://x", api_key="k", model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self._error is not None:
            raise self._error
        for block in self._blocks:
            yield {"type": "content", "delta": block}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


@pytest.fixture()
def friend_id(tmp_path, monkeypatch):
    db_file = tmp_path / "catchup.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    with db.session_scope() as s:
        s.add(Friend(id="f1", name="阿月", created_at=_iso(60), updated_at=_iso(60)))
    return "f1"


def _add_messages(fid: str, count: int, *, minutes_ago_start: int) -> None:
    with db.session_scope() as s:
        for i in range(count):
            s.add(
                Message(
                    id=f"m{i}",
                    friend_id=fid,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"第 {i} 句",
                    created_at=_iso(minutes_ago_start - i),
                )
            )


def block(content: str, layer: str = "long_term") -> str:
    return (
        '<tool>{"name":"save_memory","arguments":{"content":"%s","layer":"%s"}}</tool>'
        % (content, layer)
    )


# ---------- 谁该被补漏 ----------


def test_skips_friends_still_chatting(friend_id):
    _add_messages(friend_id, 2, minutes_ago_start=1)  # 1 分钟前还在聊
    with db.session_scope() as s:
        assert catchup_service._find_candidates(s, idle_seconds=300, now=NOW) == []


def test_picks_friends_idle_with_new_messages(friend_id):
    _add_messages(friend_id, 2, minutes_ago_start=10)  # 10 分钟前聊完 → 停歇够了
    with db.session_scope() as s:
        assert [f.id for f in catchup_service._find_candidates(s, idle_seconds=300, now=NOW)] == [
            "f1"
        ]


def test_watermark_prevents_reprocessing(friend_id):
    _add_messages(friend_id, 2, minutes_ago_start=10)
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        friend.last_catchup_at = _iso(5)  # 已经整理到"5 分钟前"
        assert catchup_service._find_candidates(s, idle_seconds=300, now=NOW) == []


def test_friend_without_messages_is_skipped(friend_id):
    with db.session_scope() as s:
        assert catchup_service._find_candidates(s, idle_seconds=300, now=NOW) == []


def test_first_run_only_takes_the_most_recent_messages(friend_id, monkeypatch):
    """首轮整理不要翻远古记录：只看最近的 N 条（否则会把几十条老对话全丢给模型）。"""
    monkeypatch.setattr(catchup_service, "CATCHUP_MAX_MESSAGES", 2)
    _add_messages(friend_id, 5, minutes_ago_start=30)  # m0 最老 … m4 最新
    with db.session_scope() as s:
        picked = catchup_service._messages_since(s, friend_id, None)
        assert [m.content for m in picked] == ["第 3 句", "第 4 句"]

    with db.session_scope() as s:
        picked_after = catchup_service._messages_since(s, friend_id, _iso(28))
        # 水位线之后按时间**正序**取（老的在前），且不含水位线那一条本身
        assert [m.content for m in picked_after] == ["第 3 句", "第 4 句"]


# ---------- 跑一次补漏 ----------


async def test_run_catchup_saves_memories_and_advances_watermark(friend_id):
    _add_messages(friend_id, 4, minutes_ago_start=10)
    provider = FakeProvider([block("用户在学 Rust"), block("用户的下周要去日本出差")])
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        stats = await catchup_service.run_catchup(s, friend, provider)
        assert stats == {"scanned": 4, "saved": 2, "merged": 0}
        assert friend.last_catchup_at == _iso(7)  # 推到"看见过的最后一条"

    with db.session_scope() as s:
        saved = memory_service.list_alive(s, friend_id)
        assert {m.content for m in saved} == {"用户在学 Rust", "用户的下周要去日本出差"}
        assert all(m.layer == "long_term" for m in saved)


async def test_second_run_does_nothing_without_new_messages(friend_id):
    """幂等：水位线之后没有新消息 → 不再调用模型、不重复写入。"""
    _add_messages(friend_id, 2, minutes_ago_start=10)
    provider = FakeProvider([block("用户在学 Rust")])
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        await catchup_service.run_catchup(s, friend, provider)
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        stats = await catchup_service.run_catchup(s, friend, provider)
    assert stats["saved"] == 0
    assert provider.calls == 1, "没有新消息就不该再调模型"
    with db.session_scope() as s:
        assert len(memory_service.list_alive(s, friend_id)) == 1


async def test_model_outputting_no_tools_saves_nothing(friend_id):
    _add_messages(friend_id, 2, minutes_ago_start=10)
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        stats = await catchup_service.run_catchup(s, friend, FakeProvider([]))
        assert stats["saved"] == 0
        assert friend.last_catchup_at is not None, "就算一条没记，水位线也要推"


async def test_prose_around_tool_blocks_is_ignored(friend_id):
    """模型不听话、话里混着正文 → 只取工具块。"""
    _add_messages(friend_id, 2, minutes_ago_start=10)
    provider = FakeProvider(["好的，我来整理：\n", block("用户养了只猫叫团子"), "\n整理完毕。"])
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        stats = await catchup_service.run_catchup(s, friend, provider)
        assert stats["saved"] == 1
    with db.session_scope() as s:
        assert [m.content for m in memory_service.list_alive(s, friend_id)] == ["用户养了只猫叫团子"]


async def test_duplicate_from_extraction_merges(friend_id):
    """补漏也走过去重（D28）：模型重复输出同一件事不会写两条。"""
    _add_messages(friend_id, 2, minutes_ago_start=10)
    provider = FakeProvider([block("用户的下周要去日本出差"), block("用户下周要去日本出差")])
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        stats = await catchup_service.run_catchup(s, friend, provider)
        assert stats["saved"] == 2 and stats["merged"] == 1
        assert len(memory_service.list_alive(s, friend_id)) == 1


async def test_extraction_prompt_includes_existing_memories(friend_id):
    """从源头防重复：把"已经记过的"一并给模型，让它别再输出一遍。

    （只靠字符串去重会漏：实测"用户叫天海梦汐" vs "用户的名字是天海梦汐" 相似度 0.67）
    """
    with db.session_scope() as s:
        memory_service.save(s, Actor.AGENT, friend_id, content="用户叫天海梦汐", layer="pinned")
    _add_messages(friend_id, 2, minutes_ago_start=10)

    captured: list[list[dict]] = []

    class RecordingProvider(FakeProvider):
        async def chat_stream(self, messages, tools=None, **kwargs):
            captured.append(messages)
            async for event in super().chat_stream(messages, tools, **kwargs):
                yield event

    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        await catchup_service.run_catchup(s, friend, RecordingProvider([]))

    prompt = captured[0][1]["content"]
    assert "已经记过这些了" in prompt
    assert "用户叫天海梦汐" in prompt


async def test_catchup_failure_does_not_advance_watermark(friend_id):
    """失败时水位线不动 → 下一轮会重试（不丢、不炸）。"""
    _add_messages(friend_id, 2, minutes_ago_start=10)
    with db.session_scope() as s:
        friend = s.get(Friend, friend_id)
        with pytest.raises(RuntimeError):
            await catchup_service.run_catchup(s, friend, FakeProvider(error=RuntimeError("boom")))
    with db.session_scope() as s:
        assert s.get(Friend, friend_id).last_catchup_at is None
