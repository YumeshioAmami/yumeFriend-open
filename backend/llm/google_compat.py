"""Google Gemini adapter（D453）。

支持 `streamGenerateContent?alt=sse`；工具调用仍走提示词级协议。
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


def _parts_of(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}]
    parts: list[dict[str, Any]] = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"text", "input_text"}:
                parts.append({"text": str(part.get("text", ""))})
            elif part.get("type") in {"image_url", "input_image"}:
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else image
                if isinstance(url, str) and url.startswith("data:"):
                    header, _, data = url.partition(",")
                    mime = header.removeprefix("data:").split(";", 1)[0] or "image/png"
                    parts.append({"inlineData": {"mimeType": mime, "data": data}})
    return parts


def _convert_messages(
    messages: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    system_parts: list[dict[str, Any]] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "system":
            system_parts.extend(_parts_of(content))
            continue
        if role == "tool":
            role = "user"
        contents.append(
            {
                "role": "model" if role == "assistant" else "user",
                "parts": _parts_of(content),
            }
        )
    system = {"parts": system_parts} if system_parts else None
    return system, contents


class GeminiProvider(ILLMProvider):
    def __init__(
        self, config: ProviderConfig, client: httpx.AsyncClient | None = None
    ) -> None:
        self._config = config
        self._client = client

    @property
    def config(self) -> ProviderConfig:
        return self._config

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
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = system
        generation_config: dict[str, Any] = {}
        if kwargs.get("temperature") is not None:
            generation_config["temperature"] = kwargs["temperature"]
        if kwargs.get("max_tokens") is not None:
            generation_config["maxOutputTokens"] = int(kwargs["max_tokens"])
        if generation_config:
            payload["generationConfig"] = generation_config
        model = self._config.model
        url = self._url(f"models/{model}:streamGenerateContent")
        params = {"alt": "sse"}
        if self._config.api_key:
            params["key"] = self._config.api_key
        request_id = uuid.uuid4().hex[:8]
        usage: dict[str, Any] = {}
        stop_reason: str | None = None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(8.0, read=180.0))
        owns_client = self._client is None
        try:
            async with client.stream("POST", url, params=params, json=payload) as response:
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
                    feedback = event.get("promptFeedback") or {}
                    if feedback.get("blockReason"):
                        raise LLMCallError(
                            f"Gemini 拒绝了这次请求：{feedback['blockReason']}"
                        )
                    for candidate in event.get("candidates") or []:
                        if candidate.get("finishReason"):
                            stop_reason = str(candidate["finishReason"])
                        content = candidate.get("content") or {}
                        for part in content.get("parts") or []:
                            if part.get("thought") and part.get("text"):
                                yield {"type": "reasoning", "delta": str(part["text"])}
                            elif part.get("text"):
                                yield {"type": "content", "delta": str(part["text"])}
                    usage_event = event.get("usageMetadata") or {}
                    if usage_event:
                        usage = {
                            "prompt_tokens": usage_event.get("promptTokenCount"),
                            "completion_tokens": usage_event.get("candidatesTokenCount"),
                            "total_tokens": usage_event.get("totalTokenCount"),
                        }
        except LLMCallError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMCallError(f"Gemini 调用失败 request_id={request_id}：{exc}") from exc
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
            response = await client.get(
                self._url("models"),
                params={"key": self._config.api_key} if self._config.api_key else None,
            )
            response.raise_for_status()
        except Exception as models_error:  # noqa: BLE001
            try:
                response = await client.post(
                    self._url(
                        f"models/{self._config.model}:generateContent"
                    ),
                    params={"key": self._config.api_key} if self._config.api_key else None,
                    json={
                        "contents": [{"role": "user", "parts": [{"text": "ping"}]}]
                    },
                )
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Gemini probe models failed: {}", models_error)
                raise exc
        finally:
            if owns_client:
                await client.aclose()
