"""设置相关的请求体（api.md §5.2）。"""

from pydantic import BaseModel, Field


class LlmBaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    base_url: str = ""
    model: str = ""
    protocol: str = "openai"
    # None = 沿用旧值（前端拿不到明文 key，不回传就是"不改"）；"" = 清空
    api_key: str | None = None
    # "" = 跟随系统；"none" = 只直连；"auto" = 直连优先、连不上自动走系统代理（D52）
    proxy: str = ""
    max_retries: int = 1
    capabilities: dict = Field(default_factory=dict)


class SettingsPatch(BaseModel):
    active_message_frequency: int | None = Field(default=None, ge=1, le=24 * 60)
    deep_thinking: bool | None = None
    show_tool_calls: bool | None = None
    llm_active: str | None = None
    llm_bases: list[LlmBaseIn] | None = None
    memory_token_budget: int | None = Field(default=None, ge=500, le=100_000)
    # v2（D80）：按用途的模型配置。`api_key` 省略 = 沿用旧值（同 llm_bases 的规矩）。
    purposes: dict[str, dict] | None = None
    # v2（D87）：定时预热（开关 + 间隔分钟）。改完**立刻生效**，不用重启。
    warmup_enabled: bool | None = None
    warmup_interval_minutes: int | None = Field(default=None, ge=1, le=120)
    # v2.1（D99）：文件写操作要不要逐笔点头（默认 true）
    file_write_approval: bool | None = None
    # v2.1（D100）+ v2.2（D103）：看屏（她主动找你说话之前先瞟一眼屏幕）。
    # 频率跟 `active_message_frequency` 共用一个数字，所以没有独立的间隔区间了。
    screen_watch_enabled: bool | None = None
    # v3.0（D141）：消息语音保留多少天（0 = 永久留着，只按总量上限清）
    voice_keep_days: int | None = Field(default=None, ge=0, le=365)
    # v3.0（B9 / D142）：沉浸模式里"我"叫什么（两端一致）
    my_name: str | None = Field(default=None, max_length=20)
    # v3.15（2026-09-26）：开机自启（做成设置项，默认关；见 services/autostart_service.py）
    auto_start: bool | None = None
    # v5.0（D255）：一起玩游戏时她主动说话的间隔（秒；0 = 关）
    game_chat_interval_seconds: int | None = Field(default=None, ge=0, le=3600)
    # v5.0（D256）：**已废弃（D262）**，保留只为兼容旧客户端
    game_autonomy_enabled: bool | None = None
    # v5.0（D257）：**已废弃（D262）**
    game_auto_free_delay_seconds: int | None = Field(default=None, ge=0, le=3600)
    # v5.0（D258）：**已废弃（D262）**
    game_autonomy_interval_seconds: int | None = Field(default=None, ge=0, le=3600)
    # v5.0（D268）：**已废弃（D272）**，保留只为兼容旧客户端
    game_idle_self_start_seconds: int | None = Field(default=None, ge=0, le=86400)
    # v5.0（D268）：**已废弃（D272）**
    game_idle_self_start_cooldown_seconds: int | None = Field(
        default=None, ge=0, le=86400
    )
    # v7.0（D303）：主设备（主动消息 / 提醒 / 反思的唯一执行者）
    autonomy_owner_device_id: str | None = None


class PurposeTestIn(BaseModel):
    """按用途测试连通（api.md §5.3）。"""

    purpose: str = Field(default="chat", min_length=1, max_length=20)
    # 前端把"还没保存的草稿"传上来测：不然用户改了 key 得先保存才能测（体验很别扭）。
    # 省略 api_key = 沿用已保存的那个（跟 PATCH 的规矩一致）。
    config: dict | None = None
