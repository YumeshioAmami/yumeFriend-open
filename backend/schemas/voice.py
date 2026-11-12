"""音色相关的请求体（api.md 的【v2】4）。"""

from pydantic import BaseModel, Field


class VoiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    gpt_weights_path: str = Field(min_length=1)
    sovits_weights_path: str = Field(min_length=1)
    ref_audio_path: str = Field(min_length=1)
    # 参考音频的台词：**不能空**（D77 实测：空着上游直接 400）
    prompt_text: str = Field(min_length=1)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


class VoiceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    prompt_text: str | None = None
    speed: float | None = Field(default=None, ge=0.5, le=2.0)


class ScanIn(BaseModel):
    """扫目录时可以让前端**把当前草稿里的目录**传上来（不用先保存）。"""

    sovits_dir: str | None = None
