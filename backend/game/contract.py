"""游戏适配器契约 v1（D250 / D251）。

主代码与适配器之间**唯一**的共同语言。这里只有类型与校验，不含任何业务逻辑：
· 适配器不认识 LLM / 数据库 / 人格；
· 主代码不认识任何具体游戏。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

PROTOCOL_VERSION = 1

# 契约里认可的事件类型。未知类型**不崩**，原样收下（前向兼容）。
KNOWN_EVENT_TYPES = frozenset(
    {
        "chat",
        "damage",
        "death",
        "task_done",
        "player_near",
        "error",
        "status",
        "heartbeat",
    }
)


class ContractError(ValueError):
    """适配器给的契约不合法。消息会直接显示给用户，所以必须是人话。"""


class UnsupportedVersionError(ContractError):
    """服务器版本不在适配器的 `supported_versions` 里（D251）。"""


def _as_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{what} 必须是一个 JSON 对象")
    return value


@dataclass(frozen=True)
class Capabilities:
    """适配器自报的能力清单（`GET /capabilities`）。"""

    game: str
    display_name: str
    protocol_version: int
    adapter_version: str
    supported_versions: tuple[str, ...]
    actions: Mapping[str, Mapping[str, Any]]
    limits: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> "Capabilities":
        data = _as_mapping(payload, "能力清单")

        version = data.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise ContractError(
                f"适配器协议版本是 {version!r}，主程序只认 {PROTOCOL_VERSION}（D250）"
            )

        game = str(data.get("game") or "").strip()
        if not game:
            raise ContractError("能力清单缺少 game")

        raw_actions = data.get("actions")
        actions = _as_mapping(raw_actions, "actions")
        if not actions:
            raise ContractError("能力清单缺少 actions（至少要有一种动作）")
        normalized: dict[str, dict[str, Any]] = {}
        for name, spec in actions.items():
            normalized[str(name)] = dict(_as_mapping(spec, f"动作 {name!r} 的说明"))

        raw_versions = data.get("supported_versions") or []
        if isinstance(raw_versions, (str, bytes)) or not isinstance(raw_versions, Sequence):
            raise ContractError("supported_versions 要是字符串数组")

        limits = data.get("limits") or {}
        return cls(
            game=game,
            display_name=str(data.get("display_name") or game),
            protocol_version=PROTOCOL_VERSION,
            adapter_version=str(data.get("adapter_version") or "unknown"),
            supported_versions=tuple(str(item) for item in raw_versions),
            actions=normalized,
            limits=dict(_as_mapping(limits, "limits")),
        )

    def action_names(self) -> tuple[str, ...]:
        return tuple(self.actions)

    def supports_version(self, version: str | None) -> bool:
        """服务器版本是不是适配器能进的（没探测到就不拦，交给适配器判断）。"""
        if not version:
            return True
        return version in self.supported_versions

    def prompt_lines(self) -> list[str]:
        """给 system prompt 的能力清单（P3 用；现在先定下来，免得两处漂移）。"""
        lines: list[str] = []
        for name, spec in self.actions.items():
            params = spec.get("params") or {}
            if isinstance(params, Mapping):
                rendered = ", ".join(str(key) for key in params)
            else:
                rendered = ""
            description = str(spec.get("description") or "").strip()
            line = f"- {name}({rendered})"
            lines.append(f"{line}  {description}".rstrip())
        return lines


@dataclass(frozen=True)
class GameEvent:
    """适配器上报的一条事实（`WS /events`）。"""

    type: str
    event_id: str
    payload: Mapping[str, Any]
    ts: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> "GameEvent":
        data = _as_mapping(payload, "事件")
        event_type = str(data.get("type") or "").strip()
        if not event_type:
            raise ContractError("事件缺少 type")
        # 契约要求适配器带 event_id；万一没带，主代码自己补一个，别把事件丢了。
        event_id = str(data.get("event_id") or "").strip() or f"{event_type}-{uuid.uuid4().hex[:12]}"
        return cls(
            type=event_type,
            event_id=event_id,
            payload=dict(data),
            ts=str(data["ts"]) if data.get("ts") else None,
        )
