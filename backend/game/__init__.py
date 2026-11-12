"""游戏陪伴（Game Companion）—— P0 只做契约层。

设计见 `docs/game_adapter.md` / D250 / D251。这里**不碰**对话、LLM、记忆；
只负责：契约类型 · 适配器客户端 · 会话状态机 · 进程启动器。
"""

from backend.game.contract import (
    PROTOCOL_VERSION,
    Capabilities,
    ContractError,
    GameEvent,
    UnsupportedVersionError,
)

__all__ = [
    "PROTOCOL_VERSION",
    "Capabilities",
    "ContractError",
    "GameEvent",
    "UnsupportedVersionError",
]
