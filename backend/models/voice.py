"""voices 表：音色库（v2.0 / T2.0-4，见 data_model.md 的【v2】4）。

一个音色 = 一组**外部路径引用**（GPT 权重 + SoVITS 权重 + 参考音频 + 它的台词）。
**不把几 GB 的模型复制进 user_data** —— 文件不在就标"路径失效"，别把库搞坏。
"""

from sqlalchemy import Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class Voice(Base):
    __tablename__ = "voices"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)

    # 三个外部路径（绝对路径）。GPT-SoVITS 就是靠这三样零样本克隆的。
    gpt_weights_path: Mapped[str] = mapped_column(Text, nullable=False)
    sovits_weights_path: Mapped[str] = mapped_column(Text, nullable=False)
    ref_audio_path: Mapped[str] = mapped_column(Text, nullable=False)
    # 参考音频的台词：**不能空**（D77 实测：空着直接 400）
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)

    speed: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
