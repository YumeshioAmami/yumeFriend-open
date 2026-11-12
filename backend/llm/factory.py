"""按用户配置造 provider（LLM协议§1.2）。

配置来自 `settings.llm_bases`（JSON 数组）+ `settings.llm_active`（名字）。
用户想加任何 OpenAI 兼容端点，只改设置，不动代码。
"""

import json

from backend.llm.base import ILLMProvider, ProviderConfig
from backend.llm.errors import LLMConfigError
from backend.llm.anthropic_compat import AnthropicProvider
from backend.llm.google_compat import GeminiProvider
from backend.llm.openai_compat import OpenAICompatProvider


def _provider_for(config: ProviderConfig) -> ILLMProvider:
    if config.protocol == "anthropic":
        return AnthropicProvider(config)
    if config.protocol == "gemini":
        return GeminiProvider(config)
    return OpenAICompatProvider(config)


def load_provider_configs(llm_bases_json: str | None) -> list[ProviderConfig]:
    if not llm_bases_json:
        raise LLMConfigError("还没配置任何 LLM 端点（请在设置里添加 provider）")
    try:
        raw = json.loads(llm_bases_json)
    except json.JSONDecodeError as exc:
        raise LLMConfigError(f"llm_bases 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise LLMConfigError("llm_bases 是空的（请在设置里添加至少一个 provider）")
    return [ProviderConfig.from_dict(item) for item in raw]


def pick_provider_config(configs: list[ProviderConfig], active: str | None) -> ProviderConfig:
    if active:
        for cfg in configs:
            if cfg.name == active:
                return cfg
        raise LLMConfigError(f"llm_active 指向的 provider 不存在：{active}")
    return configs[0]


def create_provider(llm_bases_json: str | None, llm_active: str | None = None) -> ILLMProvider:
    """造出当前要用的 provider；配置不全时抛 LLMConfigError。"""
    configs = load_provider_configs(llm_bases_json)
    config = pick_provider_config(configs, llm_active)
    if not config.model:
        raise LLMConfigError(f"provider「{config.name}」没填 model")
    if not config.base_url:
        raise LLMConfigError(f"provider「{config.name}」没填 base_url")
    return _provider_for(config)


# ── v2（D80）：按用途造 provider ──────────────────────────────────

# 报错信息会直接显示给用户（设置页的"测试连接"），所以别写代码术语
_PURPOSE_LABELS = {
    "chat": "聊天",
    "game_fast": "游戏快脑",
    "vision": "视觉",
    "tts": "语音合成",
    "stt": "语音识别",
    "image": "图片生成",
}


def _purposes_dict(purposes_json: str | None) -> dict:
    if not purposes_json:
        return {}
    try:
        data = json.loads(purposes_json)
    except json.JSONDecodeError as exc:
        raise LLMConfigError(f"purposes 不是合法 JSON：{exc}") from exc
    return data if isinstance(data, dict) else {}


def resolve_purpose_config(purposes_json: str | None, purpose: str) -> ProviderConfig:
    """按用途取配置。`vision` 这类可以 `follow_chat`（D79：chat 模型本身就能看图）。

    **只有 `chat` 是必须的**（D83）：其它用途没配 = "没这个能力"，不是错误。
    所以这里抛的错分两种口径，界面直接用：
      · 完全没配 → "没有启用——它是可选能力，不配也不影响聊天"
      · 配了一半 → "还差一点：base_url 和 model 都要填"
    """
    purposes = _purposes_dict(purposes_json)
    label = _PURPOSE_LABELS.get(purpose, purpose)
    conf = purposes.get(purpose)
    if isinstance(conf, dict) and conf.get("enabled") is False:
        raise LLMConfigError(f"「{label}」被关掉了（要用就去设置里打开）")
    if isinstance(conf, dict) and conf.get("follow_chat"):
        conf = purposes.get("chat")
        label = f"{label}（跟随聊天）"
    if not conf:
        raise LLMConfigError(
            f"「{label}」没有启用——这项是可选的，不配也不影响聊天；要用就去设置里配一个"
        )
    if not isinstance(conf, dict) or not conf.get("base_url") or not conf.get("model"):
        raise LLMConfigError(f"「{label}」还差一点：base_url 和 model 都要填")
    return ProviderConfig.from_dict(
        {
            "name": f"{purpose}:follow_chat" if label != purpose else purpose,
            "base_url": conf.get("base_url", ""),
            "api_key": conf.get("api_key", ""),
            "model": conf.get("model", ""),
            "protocol": conf.get("protocol", "openai"),
            "capabilities": conf.get("capabilities") or {},
            "proxy": conf.get("proxy", ""),
            "max_retries": conf.get("max_retries", 1),
        }
    )


def create_provider_for_purpose(purposes_json: str | None, purpose: str = "chat") -> ILLMProvider:
    """按用途造 provider（chat / vision / ……）。"""
    return _provider_for(resolve_purpose_config(purposes_json, purpose))


# ── v3.5（D151）：按**朋友**造 provider ────────────────────────────


def resolve_base_config(llm_bases_json: str | None, name: str) -> ProviderConfig:
    """按名字取「模型预设」（`settings.llm_bases` 里的一个端点）。

    **配错要说话、绝不静默回落**（D151）：找不到 / 没填全 → 抛 `LLMConfigError`（人话）。
    静默用回全局模型的话，用户会以为"这个朋友已经用上不设限的模型了" —— 那是骗人。
    """
    wanted = (name or "").strip()
    for config in load_provider_configs(llm_bases_json):
        if config.name == wanted:
            if not config.base_url or not config.model:
                raise LLMConfigError(
                    f"给这个朋友选的模型「{wanted}」还差一点：服务地址和模型名都要填"
                    "（设置 → 模型）"
                )
            return config
    raise LLMConfigError(
        f"给这个朋友选的模型「{wanted}」在设置里找不到了 —— 去「设置 → 模型」重新选一个，"
        "或者把它改成「跟随全局」"
    )


def create_provider_for_llm_base(llm_bases_json: str | None, name: str) -> ILLMProvider:
    """这个朋友自己的"嗓子"（D151）。名字对不上就抛人话错误。"""
    return _provider_for(resolve_base_config(llm_bases_json, name))
