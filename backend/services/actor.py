"""操作者身份：区分"用户"与"LLM 工具"（api.md §8）。

service 层每个方法都显式接收 actor，越权直接抛 ServiceError —— **不允许"默认放行"的分支**。
"""

from enum import Enum


class Actor(str, Enum):
    USER = "user"  # HTTP 层（用户界面），全权限
    AGENT = "agent"  # LLM 工具调用，受限（M3 起真正用上）
