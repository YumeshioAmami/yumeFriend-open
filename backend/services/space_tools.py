"""小世界工具集（D396）。

参考游戏模式的 `game_do` 设计：模型不是普通聊天，而是先看见当前世界里
真正可用的高层动作、目标和前置条件，再决定这一轮要不要 `space_do`。
这里只负责把世界快照翻译成工具说明，不执行动作。
"""

from __future__ import annotations

import re
from typing import Any


MOVE_ANCHORS = ("center", "window", "bed", "desk", "shelf", "cushion")
ACTION_REQUEST_RE = re.compile(
    r"(帮我|给我|你去|你来|把|拿一下|放一下|搬|推|拖|打开|关上|关掉|开灯|"
    r"关灯|拉开|拉上|坐到|坐在|坐一下|躺到|躺在|走到|过去|过来|碰一下|"
    r"摸一下|整理|收拾|捡|抱|用一下|看看|看一眼|移到|挪到|换个位置)"
)


def _entity_line(item: dict[str, Any]) -> str:
    line = f"- {item.get('key')}（{item.get('label')}）"
    zone = (item.get("zone") or {}).get("label")
    if zone:
        line += f"：{zone}"
    if item.get("support"):
        line += f"，在{item['support']}"
    if item.get("distance_to_me") is not None:
        line += f"，离你 {float(item['distance_to_me']):.1f}m"
    state = item.get("state") or {}
    if state:
        bits = "、".join(f"{key}={value}" for key, value in state.items() if value)
        if bits:
            line += f"，状态 {bits}"
    affordances = [str(value) for value in item.get("affordances") or [] if value]
    if affordances:
        line += "；可做：" + " / ".join(affordances[:8])
    return line


def toolset_prompt(snapshot: dict[str, Any]) -> str:
    """生成她这一轮真正能用的空间工具清单。"""
    entities = [
        item
        for item in snapshot.get("entities") or []
        if isinstance(item, dict) and item.get("key")
    ]
    entity_keys = ", ".join(str(item["key"]) for item in entities[:16])
    outfit = (snapshot.get("self") or {}).get("outfit") or {}
    lines = [
        "# 你的小世界工具箱",
        "你不是只在聊天。下面这些是你现在真的能对自己空间做的事；",
        "TA 让你动手时，必须把动作放进 space_do，不能只口头答应。",
        "",
        "- space_plan(goal, plan?)：给自己设一个短目标和 2~5 步计划；已有计划可改。",
        "- space_do(kind, target?, state?, duration_seconds?, strength?)：执行下一步身体动作。",
        "- space_say(text, emotion?)：在小世界里对 TA 说话。对话时接住他的话并给出"
        "自己的判断/联想/追问，通常 2~4 句；纯动作时才一句。不说话就不填。"
        "emotion 用当前真实情绪；问句会让她停下等待回答。",
        "",
        "## space_do 动作与前置条件",
        f"- approach(target)：走向一个实体或 TA；target 可填：{entity_keys or '当前实体'} / user。",
        f"- go_to(anchor)：走到固定区域；anchor = {' / '.join(MOVE_ANCHORS)}。",
        "- sit(target)：游戏式坐下；target = floor / 可坐 surface key / 可坐物件 id。"
        "只要目标真实可坐，前端会把她自动对齐到正确座面和朝向，不要再用 approach+use 模拟。",
        "- lie(target)：游戏式躺下；target = floor / 可躺 surface key / 可躺物件 id。",
        "  坐/躺只能用 sit/lie；不要用 use/arrange 表示坐下或躺下。",
        "- pick_up(target)：拿起实体；必须已经离它足够近（约 1.8m 内）。",
        "- put_down(target?)：放下手里的东西；手里没东西会失败。",
        "- push(target, strength?)：推实体；必须离它约 2.2m 内。",
        "- use(target)：使用/操作实体；target 用下面的真实 key。",
        "- arrange(target)：整理/摆弄实体；target 用下面的真实 key。",
        "- set_state(target, state)：改变实体、房间设施或自己的穿着状态，例如 "
        '{"on":true} / {"open":true} / {"state":"closed"}；'
        '穿衣服用 target="outfit"，state={"top":"black_tee","bottom":"blue_skirt"} 等。',
        "- idle(target?) / wait(duration_seconds?)：待着、观察、等身体停稳。",
        "",
        "## 现在能碰的实体",
    ]
    if outfit:
        lines.append(
            "你现在穿着："
            + "、".join(
                f"{key}={value}" for key, value in outfit.items() if value != "none"
            )
        )
    lines.extend(_entity_line(item) for item in entities)
    surfaces = [
        item
        for item in snapshot.get("surfaces") or []
        if isinstance(item, dict) and item.get("key")
    ]
    if surfaces:
        lines.extend(["", "## 可以坐/躺的支撑面"])
        for surface in surfaces:
            flags = []
            if surface.get("sittable"):
                flags.append("可坐")
            if surface.get("lieable"):
                flags.append("可躺")
            if not flags:
                flags.append("只能放东西")
            lines.append(
                f"- {surface.get('key')}（{surface.get('label')}）："
                f"顶面 {float(surface.get('top') or 0):.2f}m，"
                + " / ".join(flags)
            )
    lines.extend(
        [
            "",
            "硬规则：target 必须用上面真实存在的 key；不要编不在清单里的东西。",
            "坐/躺必须用 sit/lie；use/arrange 只用于使用或整理物件。",
            "动作失败时先看失败原因，换位置/换目标/改计划，不要原样机械重试。",
            "说话和身体动作可以同时发生；但问 TA 问题后要停下，保持当前情绪动作等待回答。",
        ]
    )
    return "\n".join(lines)


def looks_like_action_request(text: str) -> bool:
    """TA 是不是在让她动小世界，而不是纯聊天。"""
    return bool(ACTION_REQUEST_RE.search(str(text or "")))

