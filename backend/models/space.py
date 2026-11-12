"""小世界：每个朋友的房间状态与互动事件（v7.21 / D374）。"""

from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class FriendSpace(Base):
    """每个朋友一行：摆位 / 模式 / 当前自主动作。"""

    __tablename__ = "friend_spaces"

    friend_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("friends.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # JSON：{"book":[x,y,z], "mug":[...], ...}
    layout: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    # free（默认，自己找事做）/ together（更注意用户）
    mode: Mapped[str] = mapped_column(
        String, nullable=False, default="free", server_default="free"
    )
    # JSON：当前自主动作 {"kind":"read","target":"book","started_at":"..."}
    current_action: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    # JSON：道具状态（杯子满/空、书开/合、玩偶被捏）
    prop_states: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    # JSON：可编辑世界 {"environment":"room|white","objects":[...]}
    world: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    # JSON：身体/衣物槽位 {"outfit":{"top":"...","bottom":"...","shoes":"...","accessory":"..."}}
    appearance: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class SpaceEvent(Base):
    """小世界互动事件；只记有意义的事，idle 不记。"""

    __tablename__ = "space_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("friends.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # enter / leave / touch / move / use / call / arrange / autonomy / mode
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)
    # head / tail / book / mug / window / bed ...
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    # 额外信息：{"from":[...],"to":[...],"state":"open"} 等
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    # 1 = 可以进聊天摘要；0 = 只留档（如 leave）
    notable: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False, index=True)


class FriendSpaceRuntime(Base):
    """Phase B：她在这个房间里的持续注意力 / 意图 / 开场状态。"""

    __tablename__ = "friend_space_runtime"

    friend_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("friends.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # busy / notice / engaged
    attention: Mapped[str] = mapped_column(
        String, nullable=False, default="busy", server_default="busy"
    )
    # JSON：当前意图 + 最近一次发射信号（防止 2Hz tick 重复说同一句）
    intent: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    # JSON：打断/恢复栈
    interrupt_stack: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    # waiting / observing / understood / realized / daily
    opening_stage: Mapped[str] = mapped_column(
        String, nullable=False, default="waiting", server_default="waiting"
    )
    opening_target: Mapped[str | None] = mapped_column(String, nullable=True)
    opening_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Phase B v2：经历积累出的“这里是我的世界”理解，不是固定收尾阶段。
    discovery_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0"
    )
    wonder_said: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_discovery_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # v2.1：micro 自主控制的节拍；本地 2Hz tick 只提示“该想一下了”。
    last_deliberation_at: Mapped[str | None] = mapped_column(String, nullable=True)
    next_deliberation_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # D392：她自己的长期目标 / 多步计划 / 上一条动作的执行反馈。
    goal: Mapped[str | None] = mapped_column(String, nullable=True)
    plan: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    plan_step: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_action_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_perception_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class SpaceObjectKnowledge(Base):
    """按朋友隔离的物件知识：不知道 / 观察到 / 理解了。"""

    __tablename__ = "space_object_knowledge"
    __table_args__ = (UniqueConstraint("friend_id", "object_key", name="uq_space_knowledge"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    friend_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("friends.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    object_key: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False, default="")
    # unknown / observed / understood
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="unknown", server_default="unknown"
    )
    user_explanation: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    # Phase B v2：好奇/提问冲动。all 由本地 2Hz tick 推进，不靠固定剧本。
    novelty: Mapped[float] = mapped_column(Float, nullable=False, default=1.0, server_default="1")
    salience: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    ask_pressure: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0"
    )
    ask_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    ignored_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_seen_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_observed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_asked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_answered_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
