"""朋友相关的请求体。"""

from typing import Literal

from pydantic import BaseModel, Field


class FriendCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    personality: str = ""
    custom_instructions: str = ""
    # v6.0（D298）：声音样本（对话片段，不是格式模板）
    voice_examples: list[str] | None = None
    # v4.3（D249）：她的"温度"（0.0~1.5）。不传 = 跟随默认
    temperature: float | None = Field(default=None, ge=0.0, le=1.5)
    life_enabled: bool = False
    chat_pacing_enabled: bool = False
    humanizer_enabled: bool = False
    visual_mode: Literal["live2d", "portrait"] = "live2d"
    live2d_model_id: str | None = None
    portrait_pack_id: str | None = None


class FriendUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    personality: str | None = None
    custom_instructions: str | None = None
    # v6.0（D298）：传空数组 = 清空；None = 这次不改
    voice_examples: list[str] | None = None
    # v2（D84）：用户也可以自己写/改"她的样子"（生成的那段只是草稿）
    avatar_description: str | None = None
    # v2（T2.0-4）：用哪个音色。**传空字符串 = 取消配音色**（None = 这次不改）
    voice_id: str | None = None
    # v2（D85）：开了就每条回复都配音（None = 这次不改）
    voice_reply_enabled: bool | None = None
    # v2.2（D110）：立绘的缩放 / 位移（沉浸模式里拖出来的），None = 这次不改
    portrait_transform: dict | None = None
    # v3.5（D151）：这个朋友用哪个命名端点（`settings.llm_bases` 的 name）。
    # **传空字符串 = 跟随全局「聊天」用途**；None = 这次不改
    llm_base: str | None = None
    # v4.3（D249）：她的"温度"（0.0~1.5）。
    # ⚠️ 这里**故意要区分三态**：不给这个字段 = 不改；给数字 = 设成它；
    # 给 null（显式）= 恢复默认。api 层靠 `model_fields_set` 判断，别用 `is not None`。
    temperature: float | None = Field(default=None, ge=0.0, le=1.5)
    # v7.12（D341）：生活线开关（电脑端唯一生活主体）
    life_enabled: bool | None = None
    chat_pacing_enabled: bool | None = None
    humanizer_enabled: bool | None = None
    # v7.43（D441）：呈现方式 + 选中的 Live2D 模型 / 立绘包
    visual_mode: Literal["live2d", "portrait"] | None = None
    live2d_model_id: str | None = None
    portrait_pack_id: str | None = None


class PresenceUpdate(BaseModel):
    presence: bool


class ModeUpdate(BaseModel):
    mode: Literal["normal", "roleplay", "tool"]
    role: str | None = None
