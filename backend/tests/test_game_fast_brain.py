"""D279：本地快脑的 JSON 契约 / 升级规则。"""

from types import SimpleNamespace

import pytest

from backend.game import fast_brain


class FakeProvider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.config = SimpleNamespace(model="fake-local")

    async def chat_stream(self, _messages, **_kwargs):
        yield {"type": "content", "delta": self.text}
        yield {
            "type": "done",
            "usage": {"prompt_tokens": 20, "completion_tokens": 8},
        }


async def _decide(text: str):
    return await fast_brain.decide(
        FakeProvider(text),
        friend_name="汐音",
        personality="温柔",
        state="位置：(0,64,0)｜血量：20",
        plan="挖 32 块原木",
        plan_steps=["找树", "挖够 32 块"],
        last_task={
            "action": "mine",
            "ok": True,
            "result": {"mined": 7, "requested": 32, "remaining": 25},
        },
    )


@pytest.mark.asyncio
async def test_valid_json_is_accepted():
    decision, _raw, usage = await _decide(
        '{"action":"mine","args":{"block":"oak_log","count":8},"say":""}'
    )
    assert decision is not None
    assert decision["action"] == "mine"
    assert decision["args"]["count"] == 8
    assert usage["prompt_tokens"] == 20
    assert usage["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_markdown_fence_is_tolerated():
    decision, _raw, _usage = await _decide(
        '```json\n{"action":"goto","args":{"x":1,"y":64,"z":2}}\n```'
    )
    assert decision is not None
    assert decision["action"] == "goto"


@pytest.mark.asyncio
async def test_non_json_escalates():
    decision, _raw, _usage = await _decide("我去挖木头")
    assert decision is None


@pytest.mark.asyncio
async def test_explicit_escalation():
    decision, _raw, _usage = await _decide(
        '{"escalate":true,"reason":"目标不可达"}'
    )
    assert decision is None


@pytest.mark.asyncio
async def test_disallowed_action_escalates():
    decision, _raw, _usage = await _decide(
        '{"action":"sequence","args":{"steps":[]}}'
    )
    assert decision is None
