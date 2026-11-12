"""settings 表：全局设置（单行）。"""

from sqlalchemy import CheckConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 恒为 1

    active_message_frequency: Mapped[int] = mapped_column(Integer, nullable=False, default=60)  # 分钟（D22）
    deep_thinking: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 思考链开关（D23）
    show_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 调试显示（D31）

    llm_bases: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON：多端点（D13/D19）
    llm_active: Mapped[str | None] = mapped_column(String, nullable=True)

    # v2（D80）：**按用途**存的模型配置，JSON 形如
    # {"chat": {...}, "vision": {"follow_chat": true}, "tts": {...}}
    # 老的 llm_bases / llm_active 保留不动（当备份），迁移时搬一次，见 D80。
    purposes: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")

    # v2（D87）：**定时预热**——隔一会儿发一个"列模型"的空请求（不花 token），
    # 把连接池和上游都捂热，免得"隔了十分钟说第一句话"慢得要命。
    warmup_enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    warmup_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )

    # v2.1（D99）：文件**写操作**要不要逐笔让用户点头（1 = 要，默认）
    file_write_approval: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    # v2.1（D100）+ v2.2（D103）：**看屏**——她主动找你说话之前，先瞟一眼屏幕（有话题再开口）。
    # 频率跟 `active_message_frequency` 共用一个数字，所以不再有独立的随机区间。
    screen_watch_enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    memory_token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 注入预算（D25）

    # v3.0（D141）：**消息语音保留多少天**（0 = 永久留着，只按总量上限清）。
    # 语音文件很占地方（48kHz 单声道 ≈ 每分钟 5.6 MB），不清理几天就上 GB。
    voice_keep_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )

    # v3.0（B9 / D142）：沉浸模式里"我"叫什么。
    # 以前存在浏览器 localStorage（**每个壳各叫各的**），挪到 DB 后两端一致。
    my_name: Mapped[str] = mapped_column(String, nullable=False, default="我", server_default="我")

    # v3.15（2026-09-26）：开机自启（用户要求"做成 yumeFriend 自己的设置项"）。
    # 生效方式 = 在 Windows 启动文件夹放/删一个 .cmd（见 services/autostart_service.py）。
    # 默认 **关**：不替用户做决定，也不当"塞自启"的流氓软件。
    auto_start: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # v5.0（D255）：一起玩游戏时，她主动在游戏里说话的间隔（秒；0 = 关）。
    # 默认 90 秒 —— 用户要"频率很高"，但也要避免刷屏。
    game_chat_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default="90"
    )

    # v5.0（D256）：**已废弃（D262）**。字段保留避免迁移破坏，不再驱动行为。
    game_autonomy_enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # v5.0（D257）：**已废弃（D262）**。字段保留，不再驱动行为。
    game_auto_free_delay_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20, server_default="20"
    )

    # v5.0（D258）：**已废弃（D262）**。字段保留，不再驱动行为。
    game_autonomy_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )

    # v5.0（D268）：**已废弃（D272）**。改为常驻自主循环，字段保留不再使用。
    game_idle_self_start_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=180, server_default="180"
    )

    # v5.0（D268）：**已废弃（D272）**。
    game_idle_self_start_cooldown_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300, server_default="300"
    )

    # v5.1（D290）：作弊 / 难度 profile。
    # JSON 形如 {"profile":"survival","master_enabled":false,"abilities":{...}}。
    # 默认生存 + 总开关关；只有用户能改，LLM 没有任何写入入口。
    game_cheats: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )

    # v5.1（D291）：反射 mode 配置。
    game_reflexes: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )

    # v7.0（D303）：当前负责主动消息 / 到点提醒 / 反思的设备 id。
    # NULL = 还没选过（手机端首次启动会选自己）。
    autonomy_owner_device_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # v7.20（D373）：静态功能模块开关。JSON 形如 {"life": false, "game": true}。
    # 没写的功能用 catalog 里的默认值；核心功能始终为开。
    features: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )

    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (CheckConstraint("id = 1", name="ck_settings_single_row"),)
