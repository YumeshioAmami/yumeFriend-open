"""作弊 / 难度 profile（v5.1-P2 / D290）。

这里只做**纯配置和命令构造**，不碰数据库、不碰适配器。
权限判断和审计在 `GameManager`：LLM 永远不能自己打开作弊。
"""

from __future__ import annotations

from typing import Any, Mapping

PROFILES = ("survival", "assisted", "operator", "creative")

ABILITIES = (
    "command",
    "give",
    "teleport",
    "gamemode",
    "time",
    "weather",
    "heal",
    "feed",
    "instant_mine",
    "fly",
    "no_damage",
    "reveal_map",
)

PROFILE_LABELS = {
    "survival": "生存",
    "assisted": "辅助",
    "operator": "管理员",
    "creative": "创造",
}

PROFILE_PRESETS: dict[str, dict[str, bool]] = {
    "survival": {name: False for name in ABILITIES},
    "assisted": {
        **{name: False for name in ABILITIES},
        "heal": True,
        "feed": True,
        "time": True,
        "weather": True,
    },
    "operator": {
        **{name: False for name in ABILITIES},
        "command": True,
        "give": True,
        "teleport": True,
        "gamemode": True,
        "time": True,
        "weather": True,
        "heal": True,
        "feed": True,
        "instant_mine": True,
        "reveal_map": True,
    },
    "creative": {name: True for name in ABILITIES},
}

GAMEMODES = ("survival", "creative", "adventure", "spectator")
TIMES = ("day", "noon", "night", "midnight")
WEATHERS = ("clear", "rain", "thunder")


def default_config() -> dict[str, Any]:
    return {
        "profile": "survival",
        "master_enabled": False,
        "abilities": {name: False for name in ABILITIES},
    }


def normalize_config(raw: Any) -> dict[str, Any]:
    """把前端 / DB 里可能残缺的 JSON 归一成稳定形状。"""
    data = raw if isinstance(raw, Mapping) else {}
    profile = str(data.get("profile") or "survival").strip().lower()
    if profile not in PROFILES:
        profile = "survival"
    raw_abilities = data.get("abilities")
    abilities: dict[str, bool] = {}
    if isinstance(raw_abilities, Mapping):
        for name in ABILITIES:
            abilities[name] = bool(raw_abilities.get(name, False))
    else:
        abilities = {name: False for name in ABILITIES}
    return {
        "profile": profile,
        "master_enabled": bool(data.get("master_enabled", False)),
        "abilities": abilities,
    }


def is_active(config: Mapping[str, Any]) -> bool:
    """总开关打开且至少选中一项能力，才算“她看得到 game_cheat”。"""
    normalized = normalize_config(config)
    return bool(
        normalized["master_enabled"]
        and any(normalized["abilities"].values())
    )


def enabled_abilities(config: Mapping[str, Any]) -> list[str]:
    normalized = normalize_config(config)
    if not normalized["master_enabled"]:
        return []
    return [name for name in ABILITIES if normalized["abilities"].get(name)]


def _text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} 不能为空")
    return text


def _number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not (-30_000_000 <= number <= 30_000_000):
        raise ValueError(f"{name} 超出可接受范围")
    return number


def _coords(args: Mapping[str, Any]) -> tuple[float, float, float]:
    return (
        _number(args.get("x"), "x"),
        _number(args.get("y"), "y"),
        _number(args.get("z"), "z"),
    )


def build_commands(action: str, args: Mapping[str, Any] | None = None) -> list[str]:
    """把一项作弊能力翻译成 Minecraft 命令（不带前导 `/`）。"""
    action = str(action or "").strip().lower()
    data = dict(args or {})
    if action not in ABILITIES:
        raise ValueError(f"不认识的作弊能力：{action}")

    if action == "command":
        raw = data.get("commands", data.get("command"))
        items = raw if isinstance(raw, list) else [raw]
        commands = []
        for item in items:
            text = _text(item, "command").lstrip("/")
            if text:
                commands.append(text)
        if not commands:
            raise ValueError("command 不能为空")
        return commands

    if action == "give":
        item = _text(data.get("item"), "item")
        count = int(data.get("count", 1) or 1)
        count = max(1, min(6400, count))
        return [f"give @s {item} {count}"]

    if action == "teleport":
        x, y, z = _coords(data)
        return [f"tp @s {x:g} {y:g} {z:g}"]

    if action == "gamemode":
        mode = _text(data.get("mode"), "mode").lower()
        if mode not in GAMEMODES:
            raise ValueError("mode 只能是 survival / creative / adventure / spectator")
        return [f"gamemode {mode} @s"]

    if action == "time":
        value = _text(data.get("value", data.get("time")), "value").lower()
        if value not in TIMES:
            try:
                value = str(int(value))
            except ValueError as exc:
                raise ValueError("time 只能是 day / noon / night / midnight / 整数 tick") from exc
        return [f"time set {value}"]

    if action == "weather":
        value = _text(data.get("value", data.get("weather")), "value").lower()
        if value not in WEATHERS:
            raise ValueError("weather 只能是 clear / rain / thunder")
        duration = data.get("duration")
        if duration in (None, ""):
            return [f"weather {value}"]
        seconds = max(1, min(86_400, int(duration)))
        return [f"weather {value} {seconds}"]

    if action == "heal":
        return [
            "effect give @s instant_health 1 10 true",
            "effect give @s saturation 1 10 true",
        ]

    if action == "feed":
        return ["effect give @s saturation 1 10 true"]

    if action == "instant_mine":
        x, y, z = _coords(data)
        return [f"setblock {x:g} {y:g} {z:g} air destroy"]

    if action == "fly":
        return ["ability @s mayfly true"]

    if action == "no_damage":
        return ["effect give @s resistance 999999 4 true"]

    if action == "reveal_map":
        # MC 没有“揭示地图”的通用命令；给夜视是诚实的近似（不是矿透）。
        return ["effect give @s night_vision 999999 0 true"]

    raise ValueError(f"不认识的作弊能力：{action}")
