"""反射 mode 配置（v5.1-P6 / D291 / D323）。

只保留**身体层面**的一件事：掉进危险地形时先脱身。
打怪 / 吃东西 / 插火把 / 捡东西都是目标决策，必须由她根据快照与 plan 思考后调用工具。
"""

from __future__ import annotations

from typing import Any, Mapping

REFLEX_MODES = (
    "self_preservation",
)

DEFAULT_MODES: dict[str, dict[str, Any]] = {
    "self_preservation": {
        "enabled": True,
        "priority": 100,
        "interrupts": True,
        "cooldown": 0.5,
    },
}


def default_config() -> dict[str, Any]:
    return {"modes": {name: dict(values) for name, values in DEFAULT_MODES.items()}}


def normalize_config(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, Mapping) else {}
    modes_raw = data.get("modes")
    modes_raw = modes_raw if isinstance(modes_raw, Mapping) else {}
    modes: dict[str, dict[str, Any]] = {}
    for name in REFLEX_MODES:
        defaults = DEFAULT_MODES[name]
        item = modes_raw.get(name)
        item = item if isinstance(item, Mapping) else {}
        try:
            priority = int(item.get("priority", defaults["priority"]))
        except (TypeError, ValueError):
            priority = defaults["priority"]
        try:
            cooldown = float(item.get("cooldown", defaults["cooldown"]))
        except (TypeError, ValueError):
            cooldown = defaults["cooldown"]
        modes[name] = {
            "enabled": bool(item.get("enabled", defaults["enabled"])),
            "priority": max(0, min(999, priority)),
            "interrupts": bool(item.get("interrupts", defaults["interrupts"])),
            "cooldown": max(0.0, min(300.0, cooldown)),
        }
    return {"modes": modes}


def active_modes(config: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    normalized = normalize_config(config)
    items = [
        (name, values)
        for name, values in normalized["modes"].items()
        if values["enabled"]
    ]
    items.sort(key=lambda item: (-item[1]["priority"], item[0]))
    return items
