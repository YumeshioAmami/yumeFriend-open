"""Anthropic Messages API adapter（D453）。

只负责把 Anthropic 的 SSE 归一化成 yumeFriend 的统一事件；工具调用仍走
提示词级协议（D14 的基线），不强依赖 Anthropic 原生 tool use。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator

import httpx
from loguru import logger

from backend.llm.base import ILLMProvider, ProviderConfig
from backend.llm.errors import LLMCallError
from backend.services import usage_service


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
        )
    return str(content or "")


def _image_part(part: dict[str, Any]) -> dict[str, Any] | None:
    image = part.get("image_url")
    url = image.get("url") if isinstance(image, dict) else image
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, _, data = url.partition(",")
    media_type = header.removeprefix("data:").split(";", 1)[0] or "image/png"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "system":
            system_parts.append(_text_of(content))
            continue
        if role == "tool":
            role = "user"
        if role not in {"user", "assistant"}:
            role = "user"
        blocks: list[dict[str, Any]] = []
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"text", "input_text"}:
                    blocks.append({"type": "text", "text": str(part.get("text", ""))})
                elif part.get("type") in {"image_url", "input_image"}:
                    image = _image_part(part)
                    if image:
                        blocks.append(image)
        else:
            blocks.append({"type": "text", "text": str(content or "")})
        if blocks:
            converted.append({"role": role, "content": blocks})
    return "\n\n".join(item for item in system_parts if item), converted


class AnthropicProvider(ILLMProvider):
    def __init__(
        self, config: ProviderConfig, client: httpx.AsyncClient | None = None
    ) -> None:
        self._config = config
        self._client = client

    @property
    def config(self) -> ProviderConfig:
        return self._config

    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "x-api-key": self._config.api_key or "",
            "anthropic-version": "2023-06-01",
        }

    def _url(self, suffix: str) -> str:
        return f"{self._config.base_url.rstrip('/')}/{suffix.lstrip('/')}"

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        usage_purpose: str = "chat",
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        system, contents = _convert_messages(messages)
        payload: dict[str, Any] = {
            "model": self._config.model,
            "max_tokens": int(kwargs.get("max_tokens") or 4096),
            "messages": contents,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if kwargs.get("temperature") is not None:
            payload["temperature"] = kwargs["temperature"]
        request_id = uuid.uuid4().hex[:8]
        usage: dict[str, Any] = {}
        stop_reason: str | None = None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(8.0, read=180.0))
        owns_client = self._client is None
        try:
            async with client.stream(
                "POST", self._url("messages"), headers=self._headers(), json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event_type = event.get("type")
                    if event_type == "message_start":
                        message_usage = (event.get("message") or {}).get("usage") or {}
                        usage.update(message_usage)
                    elif event_type == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield {"type": "content", "delta": str(delta["text"])}
                        elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                            yield {"type": "reasoning", "delta": str(delta["thinking"])}
                    elif event_type == "message_delta":
                        delta = event.get("delta") or {}
                        if delta.get("stop_reason"):
                            stop_reason = str(delta["stop_reason"])
                        delta_usage = event.get("usage") or {}
                        if delta_usage.get("output_tokens") is not None:
                            usage["output_tokens"] = delta_usage["output_tokens"]
                    elif event_type == "message_stop":
                        break
        except Exception as exc:  # noqa: BLE001
            raise LLMCallError(f"Anthropic 调用失败 request_id={request_id}：{exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
        usage_service.record_call(
            provider=self._config.name,
            model=self._config.model,
            purpose=usage_purpose,
            usage=usage,
            request_id=request_id,
        )
        yield {
            "type": "done",
            "usage": usage_service.normalize_usage(usage),
            "stop_reason": stop_reason,
        }

    async def probe(self) -> None:
        client = self._client or httpx.AsyncClient(timeout=20.0)
        owns_client = self._client is None
        try:
            response = await client.get(self._url("models"), headers=self._headers())
            response.raise_for_status()
        except Exception as models_error:  # noqa: BLE001
            try:
                response = await client.post(
                    self._url("messages"),
                    headers=self._headers(),
                    json={
                        "model": self._config.model,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                )
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Anthropic probe models failed: {}", models_error)
                raise exc
        finally:
            if owns_client:
                await client.aclose()
