"""messages 表：消息（直接属于朋友，无 conversation 概念，D15）。"""

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String, ForeignKey("friends.id", ondelete="CASCADE"), nullable=False
    )

    role: Mapped[str] = mapped_column(String, nullable=False)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)  # 干净正文（工具块已剥离）

    mode_at_send: Mapped[str | None] = mapped_column(String, nullable=True)
    is_proactive: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stream_status: Mapped[str | None] = mapped_column(String, nullable=True)  # NULL/streaming/completed/error/partial
    tools_used: Mapped[str | None] = mapped_column(Text, nullable=True)  # 完整工具调用记录 JSON（D31）

    # v2（D85）：这条助手回复的语音（**附在文字上**，不是独立语音消息）。
    # NULL=没有 / pending=合成中 / ready=有 / failed=失败
    audio_status: Mapped[str | None] = mapped_column(String, nullable=True)
    # 音频文件，**相对 user_data/**（如 audio/<id>.wav）；按消息存，历史能一直回放
    audio_path: Mapped[str | None] = mapped_column(String, nullable=True)
    audio_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audio_error: Mapped[str | None] = mapped_column(Text, nullable=True)  # 失败原因（人话）

    # v2（T2.0-6 / D82）：用户发的图。`image_path` 相对 user_data（如 images/<id>.png）；
    # `image_no` 是**这个朋友的**第几张图（从 1 开始）——提示词里的占位 `[图片#12 · 09-19]`
    # 和 `look_image(12)` 都用它。
    image_path: Mapped[str | None] = mapped_column(String, nullable=True)
    image_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # v3.10（P4 / D176）：这张图的**一句内容索引**（"一只橘猫趴在键盘上"）——
    # 占位符 / list_images / look_image 都读它，这样"那张猫的图"才找得到。
    # best-effort 后台生成（看得见图的模型），失败就是 NULL（退回"只有编号和时间"）。
    image_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # v2（T2.0-7 / D92）：这轮搜网用到的来源，JSON 数组 `[{"no":1,"title":...,"url":...}]`，
    # 前端渲染成气泡下面的小链接（api.md 的【v2】6）。
    sources: Mapped[str | None] = mapped_column(Text, nullable=True)

    # v2.2（D102）：她这条回复用的表情（`[立绘:高兴]` 摘出来的；NULL = 正常脸）
    mood: Mapped[str | None] = mapped_column(String, nullable=True)

    # v2.2（D113）：她**每段**给的情绪（JSON 数组，按段顺序）：["正常","生气",…]
    moods: Mapped[str | None] = mapped_column(Text, nullable=True)

    # v6.0（D298）：一条回复内部的多个气泡（JSON 字符串数组）。NULL/单元素 = 普通单气泡。
    segments: Mapped[str | None] = mapped_column(Text, nullable=True)

    # v7.12（D342）：这条消息由哪台设备生成/发出。服务器模式用它保证
    # “一条用户消息只有一个回复者”，也方便排查跨端冲突。
    origin_device_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # v2.2（D105）：分段语音清单（JSON）：`[{"index","path","duration_ms","text"}]`。
    # 沉浸模式靠它"一段一段说"（第一段好了就播）；NULL = 只有整段的那一个 wav。
    audio_segments: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[str] = mapped_column(String, nullable=False)
    # v7.17.1（D361）：消息不是 append-only —— 流式时有占位、结束后要回填正文/情绪；
    # 增量同步用它判断“这条消息后来更新过”。旧行由迁移回填为 created_at。
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (Index("idx_messages_friend_created", "friend_id", "created_at"),)
