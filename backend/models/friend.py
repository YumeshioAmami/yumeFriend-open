"""friends 表：朋友（人格 + 在场 + 当前 mode）。"""

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class Friend(Base):
    __tablename__ = "friends"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # 人格就是普通文本字段，不是独立文件（D11）
    personality: Mapped[str] = mapped_column(Text, nullable=False, default="")
    custom_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")

    presence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # 在场 = 主动消息授权（D21）
    current_mode: Mapped[str] = mapped_column(String, nullable=False, default="normal")  # normal/roleplay/tool（D4）
    current_role: Mapped[str | None] = mapped_column(String, nullable=True)

    unread_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 未读主动消息数（D30）
    last_catchup_at: Mapped[str | None] = mapped_column(String, nullable=True)  # 记忆补漏水位线（D24）
    # v6.0（D297）：停歇反思水位线（情节 / 理解 / 纠正）
    last_reflection_at: Mapped[str | None] = mapped_column(String, nullable=True)

    # v2（T2.0-3）：头像文件，**相对 user_data 的路径**（如 avatars/<id>.png）；
    # NULL = 用首字母色块（**绝不出现空框**）
    avatar_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # v2（D84）：「她的样子」——两三句外貌描述，上传头像时自动生成一次（也可用户手改）。
    # 注入 system prompt 的 `# 你的样子`；NULL = 整段不注入。
    avatar_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # v2（T2.0-4）：这个朋友用哪个音色（voices.id）。删音色时**置空**而不是连坐删朋友
    voice_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("voices.id", ondelete="SET NULL"), nullable=True
    )
    # v2（D85）：**她的回复都用语音**。开了且配了音色 → 每条助手回复都合成音频；
    # 关 = 纯文字。这是**后端硬决定**的，不给模型开关（不然它只会"偶尔发一条"）。
    voice_reply_enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # v2.2（D110）：立绘的缩放 / 位移（沉浸模式里拖出来的），JSON 文本：
    # {"scale":1.0,"x":0,"y":0}；NULL = 用默认位置（贴底居中、不缩放）
    portrait_transform: Mapped[str | None] = mapped_column(Text, nullable=True)

    # v3.5（D151）：这个朋友用哪个**命名的模型端点**（`settings.llm_bases` 里的 `name`）。
    # NULL / 空 = 跟随全局「聊天」用途（D80 的 purposes.chat）。
    # 为什么放在朋友上：聪明的模型（DeepSeek）会拒答，不设限的模型又不如它聪明 ——
    # 那就让**每个朋友自己选嗓子**，而不是全局二选一。
    llm_base: Mapped[str | None] = mapped_column(String, nullable=True)

    # v4.3（D249）：这个朋友的"温度"（越高越跳、越低越稳）。
    # NULL = 跟随默认（主对话 0.7 / 主动消息 0.8，跟以前完全一样）；
    # 设了就用她的 —— 像 Rick 这种"疯"的角色可以调到 1.0~1.1。
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)

    # v6.0（D298）：声音样本（JSON 字符串数组）。学语气/句长/用词，不是套句式。
    voice_examples: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )

    # v7.12（D341）：生活线开关。默认关；第一期只给汐音打开。
    # 只有电脑端会推进生活活动，手机端只读（D342）。
    life_enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # 手机端不拥有 FriendLife 行；它把“正在聊天”的暂停窗口写在这里，
    # 通过普通 friends 同步到电脑，避免覆盖电脑的 current_activity_id（D342）。
    chat_pause_until: Mapped[str | None] = mapped_column(String, nullable=True)

    # v7.13（D344）：对话人味增强开关。默认关，先给汐音开。
    chat_pacing_enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    humanizer_enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # v7.43（D441）：这个朋友的呈现方式：Live2D 模型 / 立绘包二选一。
    visual_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="live2d", server_default="live2d"
    )
    # 选 Live2D 时用哪个模型（sha256(moc3)[:16]）；NULL = 旧版按朋友目录 / 默认模型。
    live2d_model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 选立绘时的全局立绘包；NULL = 旧版按朋友自己的 portraits/<fid>/。
    portrait_pack_id: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    deleted_at: Mapped[str | None] = mapped_column(String, nullable=True)
