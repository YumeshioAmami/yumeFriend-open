"""D453：聊天多协议适配器（OpenAI / Anthropic / Gemini）。"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.llm.anthropic_compat import AnthropicProvider, _convert_messages as anthropic_messages
from backend.llm.base import ProviderConfig
from backend.llm.factory import create_provider_for_purpose
from backend.llm.google_compat import GeminiProvider, _convert_messages as gemini_messages
from backend.services import usage_service


def _config(protocol: str) -> ProviderConfig:
    return ProviderConfig(
        name="test",
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        protocol=protocol,
    )


def test_factory_selects_protocol():
    for protocol, expected in (
        ("openai", "OpenAICompatProvider"),
        ("anthropic", "AnthropicProvider"),
        ("gemini", "GeminiProvider"),
    ):
        raw = json.dumps(
            {
                "chat": {
                    "base_url": "https://example.com/v1",
                    "api_key": "k",
                    "model": "m",
                    "protocol": protocol,
                }
            }
        )
        provider = create_provider_for_purpose(raw, "chat")
        assert type(provider).__name__ == expected


def test_anthropic_message_conversion_keeps_system_and_image():
    system, messages = anthropic_messages(
        [
            {"role": "system", "content": "人设"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这个"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            },
        ]
    )
    assert system == "人设"
    assert messages[0]["role"] == "user"
    assert messages[0]["content"][0] == {"type": "text", "text": "看这个"}
    assert messages[0]["content"][1]["type"] == "image"


def test_gemini_message_conversion():
    system, contents = gemini_messages(
        [
            {"role": "system", "content": "人设"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在"},
        ]
    )
    assert system == {"parts": [{"text": "人设"}]}
    assert contents[0] == {"role": "user", "parts": [{"text": "你好"}]}
    assert contents[1] == {"role": "model", "parts": [{"text": "在"}]}


@pytest.mark.asyncio
async def test_anthropic_stream_normalizes_events(monkeypatch):
    monkeypatch.setattr(usage_service, "record_call", lambda **_: None)
    body = (
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"喵"}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":2}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        provider = AnthropicProvider(_config("anthropic"), client=client)
        events = [event async for event in provider.chat_stream([{"role": "user", "content": "hi"}])]
    assert events[0] == {"type": "content", "delta": "喵"}
    assert events[-1]["type"] == "done"
    assert events[-1]["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_gemini_stream_normalizes_events(monkeypatch):
    monkeypatch.setattr(usage_service, "record_call", lambda **_: None)
    body = (
        'data: {"candidates":[{"content":{"parts":[{"text":"汪"}]},"finishReason":"STOP"}],'
        '"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":2,"totalTokenCount":5}}\n\n'
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        provider = GeminiProvider(_config("gemini"), client=client)
        events = [event async for event in provider.chat_stream([{"role": "user", "content": "hi"}])]
    assert events[0] == {"type": "content", "delta": "汪"}
    assert events[-1]["type"] == "done"
    assert events[-1]["usage"]["total_tokens"] == 5
