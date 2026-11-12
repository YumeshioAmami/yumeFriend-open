"""ILLMProvider 接口与配置模型（LLM协议§1.1 / §1.2）。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass(frozen=True)
class ProviderCapabilities:
    """能力由**配置声明**，不在代码里按厂商名写死（LLM协议§1.2）。"""

    native_tool_calls: bool = False
    reasoning: bool = False
    reasoning_streamable: bool = False
    max_context: int = 32768

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProviderCapabilities":
        data = data or {}
        return cls(
            native_tool_calls=bool(data.get("native_tool_calls", False)),
            reasoning=bool(data.get("reasoning", False)),
            reasoning_streamable=bool(data.get("reasoning_streamable", False)),
            max_context=int(data.get("max_context", 32768)),
        )


@dataclass(frozen=True)
class ProviderConfig:
    """一个模型端点的完整配置（OpenAI 兼容 / Anthropic / Gemini）。"""

    name: str
    base_url: str
    api_key: str
    model: str
    protocol: str = "openai"
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    # 代理策略（LLM协议§9.3）。空 = 跟随系统/环境变量；"none"/"direct" = 强制直连；
    # 其它值当代理 URL 用（如 http://127.0.0.1:8080）。
    proxy: str = ""
    # 网络类错误重试次数（LLM协议§9.5）。openai SDK 只对连接错误 / 408 / 409 / 429 / 5xx 重试。
    # 默认 1 = 最多试 2 次：上游抖动时约 15s 内报错，而不是干等 60~80s（D45）。
    max_retries: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProviderConfig":
        return cls(
            name=data["name"],
            base_url=data.get("base_url", ""),
            api_key=data.get("api_key", ""),
            model=data.get("model", ""),
            protocol=str(data.get("protocol") or "openai").lower(),
            capabilities=ProviderCapabilities.from_dict(data.get("capabilities")),
            proxy=data.get("proxy", "") or "",
            max_retries=int(data.get("max_retries", 1)),
        )


class ILLMProvider(ABC):
    """LLM 调用的统一接口。所有实现都必须归一化成同一套事件（LLM协议§3.2）。"""

    @property
    @abstractmethod
    def config(self) -> ProviderConfig:
        """当前 provider 的配置。"""

    @abstractmethod
    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式对话。产出归一化事件：

            {"type": "content",  "delta": "..."}
            {"type": "reasoning","delta": "..."}
            {"type": "tool_call","id": "...", "name": "...", "arguments": "..."}
            {"type": "done",     "usage": {...}, "stop_reason": "..."}
        """

    @abstractmethod
    async def probe(self) -> None:
        """发一次最小请求验证 base_url / key / model。

        **失败时必须抛**（带原始错误信息）——设置里的"测试连接"要拿到具体原因
        （api.md §5.3：`{"ok": false, "error": "401 Unauthorized"}`）。
        """

    async def health_check(self) -> bool:
        """连通性检测的布尔版（内部就是 probe 的 try/except）。"""
        try:
            await self.probe()
        except Exception:  # noqa: BLE001
            return False
        return True
