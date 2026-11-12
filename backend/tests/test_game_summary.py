"""P3：游戏摘要的解析与兜底（不调真 LLM）。"""

import json

import pytest

from backend.game.summary import format_events, summarize_events


class FakeProvider:
    def __init__(self, chunks):
        self.chunks = chunks

    async def chat_stream(self, _messages, **_kwargs):
        for chunk in self.chunks:
            yield {"type": "content", "delta": chunk}
        yield {"type": "done", "stop_reason": "stop"}


class FakeEvent:
    def __init__(self, event_type, payload, created_at="2026-10-18T12:00:00+00:00"):
        self.type = event_type
        self.payload = json.dumps(payload, ensure_ascii=False)
        self.created_at = created_at


def test_format_events_keeps_meaningful_events_only():
    text = format_events(
        [
            FakeEvent("heartbeat", {}),
            FakeEvent("chat", {"from": "梦汐", "text": "挖点铁"}),
            FakeEvent("task_done", {"task_id": "task-1", "ok": True, "result": {"mined": 3}}),
        ]
    )
    assert "挖点铁" in text
    assert "task-1" in text
    assert "heartbeat" not in text


@pytest.mark.asyncio
async def test_summary_parses_json():
    provider = FakeProvider(
        [
            '{"summary":"2026-10-18 和梦汐一起挖了铁。",'
            '"memories":[{"content":"她挖到 3 个铁矿","importance":0.7}]}'
        ]
    )
    summary, memories = await summarize_events(provider, "[12:00] 梦汐 说：挖点铁")
    assert summary.startswith("2026-10-18")
    assert memories == [{"content": "她挖到 3 个铁矿", "importance": 0.7}]


@pytest.mark.asyncio
async def test_summary_falls_back_on_bad_json():
    provider = FakeProvider(["这就是一段普通摘要，不是 JSON。"])
    summary, memories = await summarize_events(provider, "[12:00] 梦汐 说：挖点铁")
    assert summary == "这就是一段普通摘要，不是 JSON。"
    assert memories == []
