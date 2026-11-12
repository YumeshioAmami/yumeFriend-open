"""小世界语义世界模型（Phase D / D392）。

把数据库里的 layout / world / prop_states / knowledge / events 合成一份 LLM 可读的
“世界快照”。这里不调模型，也不做动作；只负责让她知道自己周围有什么、是什么状态、
在哪里、能做什么、最近发生了什么。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import (
    FriendState,
    FriendSpace,
    FriendSpaceRuntime,
    Message,
    SpaceEvent,
    SpaceObjectKnowledge,
)
from backend.services.space_touch import TOUCH_LABELS, touch_sensation

OBJECT_LABELS = {
    "book": "书",
    "mug": "杯子",
    "cushion": "靠垫",
    "notebook": "笔记本",
    "plush": "鲸鱼玩偶",
}
DEFAULT_LAYOUT: dict[str, list[float]] = {
    "book": [0.42, 0.05, -0.55],
    "mug": [0.95, 0.11, -0.9],
    "cushion": [-0.95, 0.16, 0.15],
    "notebook": [-1.35, 0.06, -0.5],
    "plush": [0.5, 0.18, 0.32],
}
WORLD_ASSET_LABELS = {
    "table": "桌子",
    "chair": "椅子",
    "shelf": "书架",
    "bed": "床",
    "rug": "地毯",
    "plant": "植物",
    "lamp": "灯",
    "box": "箱子",
    "cushion": "靠垫",
    "blanket": "毯子",
    "nest": "小窝",
    "procedural": "程序化物件",
}
OUTFIT_LABELS = {
    "wear": "穿着",
    "none": "没有",
}
ZONES: tuple[dict[str, Any], ...] = (
    {"key": "window", "label": "窗边", "center": [0.2, 0, -4.0], "radius": 2.0},
    {"key": "bed", "label": "床边", "center": [2.8, 0, -3.8], "radius": 2.1},
    {"key": "desk", "label": "桌边", "center": [3.45, 0, 3.35], "radius": 2.0},
    {"key": "shelf", "label": "书架边", "center": [-3.95, 0, -3.65], "radius": 1.9},
    {"key": "center", "label": "房间中央", "center": [0, 0, 0], "radius": 2.8},
)
FIXED_SURFACES: tuple[dict[str, Any], ...] = (
    {
        "key": "fixed-bed",
        "label": "固定床面",
        "kind": "bed",
        "center": [2.8, 0, -4.3],
        "top": 0.34,
        "half": [0.72, 0.33],
        "rotation": 0.0,
        "walkable": False,
        "sittable": True,
        "lieable": True,
        "source": "fixed",
    },
    {
        "key": "fixed-desk",
        "label": "固定桌面",
        "kind": "table",
        "center": [3.45, 0, 4.22],
        "top": 0.76,
        "half": [0.58, 0.24],
        "rotation": 0.0,
        "walkable": False,
        "sittable": False,
        "lieable": False,
        "source": "fixed",
    },
)

PROP_AFFORDANCES: dict[str, tuple[str, ...]] = {
    "book": ("看", "拿起来", "翻开", "读", "移动"),
    "mug": ("看", "拿起来", "喝水", "移动"),
    "cushion": ("看", "拿起来", "坐", "靠", "移动"),
    "notebook": ("看", "拿起来", "翻开", "写字", "移动"),
    "plush": ("看", "拿起来", "抱", "揉", "移动"),
}
ASSET_AFFORDANCES: dict[str, tuple[str, ...]] = {
    "table": ("移动", "推", "把东西放上去"),
    "chair": ("移动", "坐"),
    "shelf": ("移动", "放东西", "打开"),
    "bed": ("移动", "坐", "躺"),
    "rug": ("移动", "踩上去", "把东西放上去"),
    "plant": ("看", "移动", "浇水"),
    "lamp": ("移动", "打开", "关上"),
    "box": ("移动", "打开", "把东西放进去"),
    "cushion": ("移动", "坐", "抱"),
    "blanket": ("移动", "拿起来", "盖"),
    "nest": ("移动", "坐", "躺"),
    "procedural": ("看", "移动", "推"),
}
FIXTURES: tuple[dict[str, Any], ...] = (
    {
        "key": "room_light",
        "label": "房间灯",
        "position": [0, 3.1, 0.2],
        "affordances": ("打开", "关上"),
    },
    {
        "key": "curtain",
        "label": "窗帘",
        "position": [0.2, 1.9, -4.87],
        "affordances": ("拉开", "拉上"),
    },
    {
        "key": "window",
        "label": "窗户",
        "position": [0.2, 1.9, -4.94],
        "affordances": ("打开", "关上"),
    },
    {
        "key": "door",
        "label": "门",
        "position": [-1.4, 1.15, 4.97],
        "affordances": ("打开", "关上"),
    },
)


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="minutes")


def _time_phase(now: datetime) -> dict[str, str]:
    hour = now.hour
    if 5 <= hour < 7:
        return {"key": "dawn", "label": "清晨"}
    if 7 <= hour < 11:
        return {"key": "morning", "label": "上午"}
    if 11 <= hour < 14:
        return {"key": "noon", "label": "中午"}
    if 14 <= hour < 17:
        return {"key": "afternoon", "label": "下午"}
    if 17 <= hour < 19:
        return {"key": "dusk", "label": "傍晚"}
    return {"key": "night", "label": "深夜" if hour >= 22 or hour < 5 else "晚上"}


def _build_senses(
    prop_states: dict[str, Any],
    entities: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """把时间、灯、窗、门和正在动的物件折成她能直接感受的环境线索。"""
    now = now or datetime.now().astimezone()
    phase = _time_phase(now)
    room_light_on = str(prop_states.get("room_light") or "on") == "on"
    curtain_open = str(prop_states.get("curtain") or "open") == "open"
    window_open = str(prop_states.get("window") or "closed") == "open"
    door_open = str(prop_states.get("door") or "closed") == "open"

    daylight = {
        "dawn": 0.38,
        "morning": 0.82,
        "noon": 1.0,
        "afternoon": 0.88,
        "dusk": 0.35,
        "night": 0.04,
    }[phase["key"]]
    if not curtain_open:
        daylight *= 0.12
    light_level = max(daylight, 0.74 if room_light_on else 0.0)
    if light_level >= 0.72:
        light_text = "光线很足"
    elif light_level >= 0.38:
        light_text = "光线偏柔"
    elif light_level >= 0.12:
        light_text = "屋里有点暗"
    else:
        light_text = "屋里很暗，只能看清近处"
    if room_light_on:
        light_text += "，房间灯开着"
    elif phase["key"] == "night":
        light_text += "，房间灯关着"
    if not curtain_open:
        light_text += "，窗帘拉着"

    base_temp = {
        "dawn": 20.2,
        "morning": 22.0,
        "noon": 24.2,
        "afternoon": 24.8,
        "dusk": 22.4,
        "night": 19.6,
    }[phase["key"]]
    temperature = base_temp
    if window_open:
        temperature -= 1.4
    if door_open:
        temperature -= 0.5
    if not curtain_open:
        temperature += 0.3
    if room_light_on:
        temperature += 0.2
    if temperature <= 20.0:
        temperature_text = "空气有点凉"
    elif temperature >= 24.5:
        temperature_text = "空气偏暖"
    else:
        temperature_text = "温度正好"

    sound_parts: list[str] = []
    if window_open:
        sound_parts.append(
            "窗外有风声和很远的人声"
            if phase["key"] not in ("night", "dawn")
            else "窗外偶尔有风声"
        )
    elif curtain_open and phase["key"] in ("morning", "noon", "afternoon"):
        sound_parts.append("隔着窗能听见一点外面的动静")
    else:
        sound_parts.append("房间很安静，能听见自己的呼吸和衣料摩擦")
    if door_open:
        sound_parts.append("门开着，走廊那边偶尔有回音")
    moving = [
        str(item.get("label") or item.get("key") or "东西")
        for item in entities
        if item.get("moving")
    ]
    if moving:
        sound_parts.append("、".join(moving[:4]) + "还在轻轻碰响")

    if window_open and door_open:
        air_text = "有穿堂风，空气在流动"
    elif window_open:
        air_text = "窗边有新鲜空气慢慢进来"
    elif door_open:
        air_text = "门口有一点流动的空气"
    else:
        air_text = "空气安静，几乎不流动"

    summary = [
        f"现在是{phase['label']}，{light_text}。",
        f"{temperature_text}，大约 {temperature:.1f}°C。",
        "你听见：" + "；".join(sound_parts) + "。",
        air_text + "。",
    ]
    return {
        "time_phase": phase,
        "light": {
            "level": round(light_level, 3),
            "room_light": "on" if room_light_on else "off",
            "curtain": "open" if curtain_open else "closed",
            "window": "open" if window_open else "closed",
            "description": light_text,
        },
        "sound": {"description": "；".join(sound_parts), "moving": moving[:6]},
        "temperature": {
            "celsius": round(temperature, 1),
            "description": temperature_text,
        },
        "air": {"description": air_text},
        "summary": summary,
    }


def _loads(raw: str | None, default: Any) -> Any:
    try:
        data = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _point(value: Any) -> list[float] | None:
    try:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return None
        return [float(value[0]), float(value[1]), float(value[2])]
    except (TypeError, ValueError):
        return None


def _distance(a: list[float] | None, b: list[float] | None) -> float | None:
    if a is None or b is None:
        return None
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def _zone_for(position: list[float] | None) -> dict[str, Any]:
    if position is None:
        return {"key": "room", "label": "房间里"}
    best = None
    best_dist = float("inf")
    for zone in ZONES:
        dist = _distance(position, zone["center"])
        if dist is not None and dist < best_dist:
            best = zone
            best_dist = dist
    if best is None:
        return {"key": "room", "label": "房间里"}
    if best_dist <= float(best["radius"]):
        return {"key": best["key"], "label": best["label"]}
    return {"key": "room", "label": "房间里"}


def _surfaces_from_world(
    world: dict[str, Any],
    layout: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    surfaces: list[dict[str, Any]] = [
        {
            "key": "floor",
            "label": "地板",
            "kind": "floor",
            "center": [0.0, 0.0, 0.0],
            "top": 0.0,
            "half": [4.95, 4.95],
            "rotation": 0.0,
            "walkable": True,
            "sittable": True,
            "lieable": True,
            "source": "room",
        },
        *[dict(item) for item in FIXED_SURFACES],
    ]
    rugs: list[dict[str, Any]] = []
    cushion = _point((layout or {}).get("cushion"))
    if cushion is not None:
        surfaces.append(
            {
                "key": "cushion",
                "label": "靠垫面",
                "kind": "cushion",
                "center": cushion,
                "top": cushion[1] + 0.14,
                "half": [0.32, 0.24],
                "rotation": 0.0,
                "walkable": True,
                "sittable": True,
                "lieable": False,
                "source": "room",
            }
        )
    surface_specs: dict[str, dict[str, Any]] = {
        "table": {
            "kind": "table", "top": 0.77, "half": [0.65, 0.375],
            "walkable": False, "sittable": False, "lieable": False,
        },
        "chair": {
            "kind": "seat", "top": 0.51, "half": [0.23, 0.23],
            "walkable": False, "sittable": True, "lieable": False,
        },
        "shelf": {
            "kind": "other", "top": 1.8, "half": [0.48, 0.18],
            "walkable": False, "sittable": False, "lieable": False,
        },
        "bed": {
            "kind": "bed", "top": 0.5, "half": [0.62, 0.32],
            "walkable": False, "sittable": True, "lieable": True,
        },
        "rug": {
            "kind": "floor", "top": 0.024, "half": [0.75, 0.75],
            "walkable": True, "sittable": True, "lieable": True,
        },
        "plant": {
            "kind": "other", "top": 0.78, "half": [0.24, 0.24],
            "walkable": False, "sittable": False, "lieable": False,
        },
        "lamp": {
            "kind": "other", "top": 1.58, "half": [0.18, 0.18],
            "walkable": False, "sittable": False, "lieable": False,
        },
        "box": {
            "kind": "other", "top": 0.56, "half": [0.28, 0.28],
            "walkable": False, "sittable": False, "lieable": False,
        },
        "cushion": {
            "kind": "cushion", "top": 0.22, "half": [0.3, 0.23],
            "walkable": True, "sittable": True, "lieable": False,
        },
        "blanket": {
            "kind": "floor", "top": 0.07, "half": [0.42, 0.3],
            "walkable": True, "sittable": True, "lieable": True,
        },
        "nest": {
            "kind": "nest", "top": 0.32, "half": [0.48, 0.48],
            "walkable": False, "sittable": True, "lieable": True,
        },
    }
    for item in world.get("objects") or []:
        if not isinstance(item, dict):
            continue
        position = _point(item.get("position"))
        if position is None:
            continue
        asset = str(item.get("asset") or "")
        scale = max(0.2, float(item.get("scale") or 1))
        spec = surface_specs.get(asset)
        if spec is not None:
            key = str(item.get("id") or asset)
            surfaces.append(
                {
                    "key": key,
                    "label": {
                        "table": "桌面",
                        "chair": "椅子面",
                        "bed": "床面",
                        "rug": "地毯",
                        "cushion": "靠垫面",
                        "blanket": "毯子面",
                        "nest": "小窝面",
                    }.get(asset, "家具面"),
                    "kind": spec["kind"],
                    "center": [position[0], position[1], position[2]],
                    "top": position[1] + float(spec["top"]) * scale,
                    "half": [float(spec["half"][0]) * scale, float(spec["half"][1]) * scale],
                    "rotation": float(item.get("rotation") or 0),
                    "walkable": bool(spec["walkable"]),
                    "sittable": bool(spec["sittable"]),
                    "lieable": bool(spec["lieable"]),
                    "source": "world",
                    "object_id": key,
                }
            )
        if asset == "rug":
            rugs.append(
                {
                    "key": str(item.get("id") or "rug"),
                    "label": "地毯",
                    "center": [position[0], 0, position[2]],
                    "radius": 0.85 * scale,
                }
            )
    return surfaces, rugs


def _support_for(
    position: list[float] | None,
    surfaces: list[dict[str, Any]],
    rugs: list[dict[str, Any]],
) -> str:
    if position is None:
        return "不知道在哪里"
    for surface in surfaces:
        if surface.get("kind") == "floor":
            continue
        center = surface["center"]
        half = surface["half"]
        top = float(surface["top"])
        dx = position[0] - float(center[0])
        dz = position[2] - float(center[2])
        rotation = float(surface.get("rotation") or 0)
        cos = math.cos(rotation)
        sin = math.sin(rotation)
        local_x = dx * cos - dz * sin
        local_z = dx * sin + dz * cos
        if (
            abs(local_x) <= float(half[0])
            and abs(local_z) <= float(half[1])
            and top - 0.18 <= position[1] <= top + 0.9
        ):
            return str(surface["label"])
    for rug in rugs:
        center = rug["center"]
        if (
            position[1] <= 0.22
            and _distance(position, [center[0], 0, center[2]]) is not None
            and float(_distance(position, [center[0], 0, center[2]]) or 999)
            <= float(rug["radius"])
        ):
            return str(rug["label"])
    if position[1] <= 0.25:
        return "地板"
    return "半空/家具上"


def _status_text(row: SpaceObjectKnowledge | None) -> str:
    if row is None or row.status == "unknown":
        return "还不知道是什么"
    if row.status == "observed":
        return "见过，但还没完全弄明白"
    if row.user_explanation:
        return f"已经明白：{row.user_explanation}"
    return "已经明白"


def _event_text(event: SpaceEvent) -> str:
    detail = _loads(event.detail, {})
    if event.kind == "say":
        text = str(detail.get("text") or "").strip()
        reply = str(detail.get("reply") or "").strip()
        if detail.get("speaker") == "assistant":
            return f"你主动说：{text}" if text else ""
        return f"TA 说：{text}；你回：{reply}" if text else ""
    if event.kind == "move":
        return f"有东西移动了：{event.target or '未知物件'}"
    if event.kind == "use":
        return f"用过：{event.target or '某样东西'}"
    if event.kind == "autonomy":
        text = str(detail.get("text") or "").strip()
        return f"你自己做了：{text}" if text else ""
    if event.kind == "touch":
        return f"TA 碰了你的{TOUCH_LABELS.get(str(event.target or ''), event.target or '身体')}"
    if event.kind == "action_result":
        ok = detail.get("ok")
        text = str(detail.get("detail") or "").strip()
        return f"你刚才的动作{'完成' if ok else '没完成'}" + (f"：{text}" if text else "")
    return ""


def build_world_snapshot(
    session: Session,
    friend_id: str,
    *,
    viewer: dict | None = None,
    self_state: dict | None = None,
    objects: list[dict] | None = None,
    activity: str | None = None,
    mood: str | None = None,
) -> dict[str, Any]:
    """合成当前语义世界；objects / self_state 是本次前端最新快照。"""
    state = session.get(FriendSpace, friend_id)
    layout = {**DEFAULT_LAYOUT, **_loads(state.layout if state else None, {})}
    prop_states = _loads(state.prop_states if state else None, {})
    world = _loads(state.world if state else None, {})
    appearance = _loads(state.appearance if state else None, {})
    outfit = appearance.get("outfit") if isinstance(appearance, dict) else {}
    outfit = outfit if isinstance(outfit, dict) else {}
    runtime = session.get(FriendSpaceRuntime, friend_id)
    surfaces, rugs = _surfaces_from_world(world, layout)

    perceived: dict[str, dict] = {}
    for item in objects or []:
        if isinstance(item, dict) and item.get("key"):
            perceived[str(item["key"])] = item

    knowledge_rows = list(
        session.scalars(
            select(SpaceObjectKnowledge).where(SpaceObjectKnowledge.friend_id == friend_id)
        ).all()
    )
    knowledge = {row.object_key: row for row in knowledge_rows}

    self_position = _point((self_state or {}).get("position"))
    self_hand = _point((self_state or {}).get("hand"))
    user = viewer if isinstance(viewer, dict) else {}
    user_position = _point(user.get("camera"))
    hand_position = _point(user.get("hand"))

    entities: list[dict[str, Any]] = []
    for key, label in OBJECT_LABELS.items():
        seen = perceived.get(key, {})
        position = _point(seen.get("position")) or _point(layout.get(key))
        row = knowledge.get(key)
        entities.append(
            {
                "key": key,
                "label": label,
                "kind": "builtin_prop",
                "position": position,
                "zone": _zone_for(position),
                "support": _support_for(position, surfaces, rugs),
                "state": {"state": prop_states.get(key, "normal")},
                "moving": bool(seen.get("moving")),
                "distance_to_me": _distance(self_position, position),
                "distance_to_user": _distance(user_position, position),
                "known": _status_text(row),
                "affordances": list(PROP_AFFORDANCES.get(key, ("看", "移动"))),
                "last_event": None,
            }
        )

    for fixture in FIXTURES:
        position = _point(fixture["position"])
        entities.append(
            {
                "key": fixture["key"],
                "label": fixture["label"],
                "kind": "fixture",
                "position": position,
                "zone": _zone_for(position),
                "support": "固定在房间里",
                "state": {"state": prop_states.get(fixture["key"], "normal")},
                "moving": False,
                "distance_to_me": _distance(self_position, position),
                "distance_to_user": _distance(user_position, position),
                "known": "房间的一部分",
                "affordances": list(fixture["affordances"]),
                "last_event": None,
            }
        )

    for item in world.get("objects") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or "")
        if not key:
            continue
        seen = perceived.get(key, {})
        position = _point(seen.get("position")) or _point(item.get("position"))
        asset = str(item.get("asset") or "procedural")
        label = WORLD_ASSET_LABELS.get(asset, "导入的物件" if asset.startswith("glb:") else asset)
        row = knowledge.get(key)
        entities.append(
            {
                "key": key,
                "label": label,
                "kind": "world_object",
                "asset": asset,
                "position": position,
                "zone": _zone_for(position),
                "support": _support_for(position, surfaces, rugs),
                "state": item.get("state") if isinstance(item.get("state"), dict) else {},
                "moving": bool(seen.get("moving")),
                "distance_to_me": _distance(self_position, position),
                "distance_to_user": _distance(user_position, position),
                "known": _status_text(row),
                "affordances": list(
                    ASSET_AFFORDANCES.get(asset, ("看", "拿起来", "使用", "移动", "推"))
                ),
                "last_event": None,
            }
        )

    events = list(
        session.scalars(
            select(SpaceEvent)
            .where(SpaceEvent.friend_id == friend_id)
            .order_by(SpaceEvent.created_at.desc())
            .limit(30)
        ).all()
    )
    recent = [
        {"kind": event.kind, "target": event.target, "text": _event_text(event)}
        for event in events
        if _event_text(event)
    ][:8]
    event_by_target: dict[str, str] = {}
    for event in events:
        text = _event_text(event)
        if event.target and text and event.target not in event_by_target:
            event_by_target[event.target] = text
    for item in entities:
        item["last_event"] = event_by_target.get(item["key"])

    space_chat: list[dict[str, Any]] = []
    for event in reversed(events):
        if event.kind != "say":
            continue
        detail = _loads(event.detail, {})
        text = re.sub(r"\s+", " ", str(detail.get("text") or "")).strip()[:240]
        reply = re.sub(r"\s+", " ", str(detail.get("reply") or "")).strip()[:240]
        emission = str(detail.get("emotion") or "").strip()
        if text:
            space_chat.append(
                {
                    "role": "你" if detail.get("speaker") == "assistant" else "TA",
                    "content": text,
                    "at": event.created_at,
                    "emotion": emission,
                    "proactive": bool(detail.get("proactive")),
                }
            )
        if reply:
            space_chat.append(
                {
                    "role": "你",
                    "content": reply,
                    "at": event.created_at,
                    "emotion": emission,
                    "proactive": False,
                }
            )
    space_chat = space_chat[-12:]
    chat_rows = list(
        session.scalars(
            select(Message)
            .where(Message.friend_id == friend_id)
            .order_by(Message.created_at.desc())
            .limit(10)
        ).all()
    )
    recent_chat = [
        {
            "role": "TA" if row.role == "user" else "你",
            "content": re.sub(r"\s+", " ", row.content or "").strip()[:240],
            "at": row.created_at,
        }
        for row in reversed(chat_rows)
        if (row.content or "").strip()
    ]
    shared = session.get(FriendState, friend_id)
    loops = _loads(shared.open_loops if shared else None, [])
    shared_state = {
        "intent": (shared.intent or "") if shared else "",
        "inner_note": (shared.inner_note or "") if shared else "",
        "open_loops": [
            str(item.get("text") if isinstance(item, dict) else item)
            for item in loops
            if str(item.get("text") if isinstance(item, dict) else item).strip()
        ][:6],
        "energy": float(shared.energy or 0.5) if shared else 0.5,
        "affect": _loads(shared.affect if shared else None, {}),
    }

    senses = _build_senses(prop_states, entities)
    entities.sort(
        key=lambda item: (
            item["distance_to_me"] is None,
            item["distance_to_me"] if item["distance_to_me"] is not None else 999,
        )
    )
    return {
        "friend_id": friend_id,
        "time": _now_text(),
        "environment": world.get("environment") or "room",
        "room": {"width": 10.2, "depth": 10.2, "height": 3.6},
        "zones": [dict(zone) for zone in ZONES],
        "surfaces": surfaces,
        "senses": senses,
        "self": {
            "position": self_position,
            "hand": self_hand,
            "holding": (self_state or {}).get("holding"),
            "posture": (self_state or {}).get("posture") or "stand",
            "sitting": bool((self_state or {}).get("sitting")),
            "lying": bool((self_state or {}).get("lying")),
            "support_key": (self_state or {}).get("support_key"),
            "outfit": {
                "top": str(outfit.get("top") or "wear"),
                "bottom": str(outfit.get("bottom") or "wear"),
                "shoes": str(outfit.get("shoes") or "none"),
                "accessory": str(outfit.get("accessory") or "none"),
            },
            "moving": bool((self_state or {}).get("moving")),
            "still_seconds": float((self_state or {}).get("still_seconds") or 0),
            "target": (self_state or {}).get("target"),
            "at_target": bool((self_state or {}).get("at_target")),
            "activity": activity,
            "mood": mood,
        },
        "user": {
            "camera": user_position,
            "hand": hand_position,
            "hand_active": bool(user.get("hand_active")),
            "hand_holding": user.get("hand_holding"),
            "hand_contact_zone": user.get("hand_contact_zone"),
            "hand_contact_kind": user.get("hand_contact_kind"),
            "hand_contact_speed": float(user.get("hand_contact_speed") or 0),
            "hand_contact_seconds": float(user.get("hand_contact_seconds") or 0),
            "look": _point(user.get("look")),
        },
        "entities": entities,
        "recent_events": recent,
        "recent_chat": recent_chat[-8:],
        "space_chat": space_chat,
        "shared_state": shared_state,
        "goal": runtime.goal if runtime else None,
        "plan": _loads(runtime.plan if runtime else None, []),
        "plan_step": int(runtime.plan_step or 0) if runtime else 0,
        "last_action_result": _loads(runtime.last_action_result if runtime else None, {}),
    }


def to_prompt(snapshot: dict[str, Any]) -> str:
    """把世界快照压成 LLM 能直接读的自然语言。"""
    self_data = snapshot.get("self") or {}
    user = snapshot.get("user") or {}
    lines = [
        "# 你所在的世界",
        f"现在：{snapshot.get('time')}",
        f"空间：约 10.2×10.2m 的房间，高 3.6m；有窗边、床边、桌边、书架边和中央区域。",
    ]
    senses = snapshot.get("senses") or {}
    if isinstance(senses, dict) and senses.get("summary"):
        lines.append("# 你现在感觉到")
        lines.extend(f"- {item}" for item in senses["summary"])
    position = self_data.get("position")
    if position:
        lines.append(f"你自己：在 {_zone_for(position)['label']}，坐标 ({position[0]:.2f}, {position[1]:.2f}, {position[2]:.2f})")
    if self_data.get("moving"):
        lines.append("你现在正在走动。")
    else:
        lines.append(f"你现在停着，已经静止约 {float(self_data.get('still_seconds') or 0):.1f} 秒。")
    posture = str(self_data.get("posture") or "stand")
    posture_text = {"stand": "站着", "sit": "坐着", "lie": "躺着"}.get(posture, posture)
    support_key = self_data.get("support_key")
    lines.append(f"你现在的姿势：{posture_text}" + (f"，支撑面是 {support_key}" if support_key else ""))
    surfaces = [
        item for item in snapshot.get("surfaces") or [] if isinstance(item, dict)
    ]
    if surfaces:
        usable = []
        for surface in surfaces:
            flags = []
            if surface.get("sittable"):
                flags.append("可坐")
            if surface.get("lieable"):
                flags.append("可躺")
            if not flags:
                continue
            usable.append(
                f"{surface.get('label')}（{surface.get('key')}，"
                f"顶面 {float(surface.get('top') or 0):.2f}m，{'/'.join(flags)}）"
            )
        if usable:
            lines.append("真实支撑面：" + "；".join(usable[:10]))
    if self_data.get("activity"):
        lines.append(f"你刚才在做：{self_data['activity']}")
    if self_data.get("holding"):
        lines.append(f"你手里拿着：{self_data['holding']}")
    if self_data.get("sitting"):
        lines.append("你现在坐着。")
    outfit = self_data.get("outfit") or {}
    if outfit:
        worn = [
            f"{label}：{OUTFIT_LABELS.get(str(outfit.get(key) or 'none'), outfit.get(key))}"
            for key, label in (
                ("top", "上衣"),
                ("bottom", "下装"),
                ("shoes", "鞋"),
                ("accessory", "配饰"),
            )
            if outfit.get(key) and outfit.get(key) != "none"
        ]
        if worn:
            lines.append("你穿着：" + "；".join(worn))
    if self_data.get("mood"):
        lines.append(f"你现在的情绪/表情：{self_data['mood']}")
    if user.get("camera"):
        text = "TA 在房间里"
        contact_zone = str(user.get("hand_contact_zone") or "").strip()
        contact_kind = str(user.get("hand_contact_kind") or "").strip()
        if contact_zone:
            contact_text = {
                "hold": "贴着你在轻轻抚摸/揉，没有松开",
                "drag": "抓着你，正把你带着移动",
                "push": "抓着你，正把你往外推",
                "pull": "抓着你，正把你往自己身边拉",
            }.get(contact_kind, "正抓着你")
            text += (
                f"，手{contact_text}（{TOUCH_LABELS.get(contact_zone, contact_zone)}，"
                f"持续约 {float(user.get('hand_contact_seconds') or 0):.1f}s）"
            )
            sensation = touch_sensation(contact_zone)
            if sensation:
                text += f"；身体感受：{sensation.rstrip('。')}"
        elif user.get("hand_active"):
            text += f"，手正碰着 {user.get('hand_holding') or '某样东西'}"
        elif user.get("hand"):
            text += "，手空着"
        lines.append(text + "。")

    space_chat = snapshot.get("space_chat") or []
    if space_chat:
        lines.extend(["", "# 你们在这个小世界里说过"])
        lines.extend(
            f"- {item['role']}：{item['content']}" for item in space_chat[-8:]
        )

    recent_chat = snapshot.get("recent_chat") or []
    if recent_chat:
        lines.extend(["", "# 你们最近在普通聊天里说过"])
        lines.extend(
            f"- {item['role']}：{item['content']}" for item in recent_chat[-6:]
        )

    lines.append("")
    lines.append("# 你看得见的东西")
    for item in snapshot.get("entities") or []:
        line = f"- {item['label']}（{item['key']}）：在{item['zone']['label']}"
        if item.get("support"):
            line += f"，放在{item['support']}上"
        if item.get("distance_to_me") is not None:
            line += f"，离你 {float(item['distance_to_me']):.1f}m"
        if item.get("distance_to_user") is not None:
            line += f"，离 TA {float(item['distance_to_user']):.1f}m"
        state = item.get("state") or {}
        state_parts = [f"{key}={value}" for key, value in state.items() if value]
        if state_parts:
            line += "；状态：" + "、".join(state_parts)
        if item.get("moving"):
            line += "；正在动"
        line += f"；{item.get('known')}"
        if item.get("affordances"):
            line += "；可以做：" + "、".join(item["affordances"][:5])
        if item.get("last_event"):
            line += f"；最近：{item['last_event']}"
        lines.append(line)

    recent = snapshot.get("recent_events") or []
    if recent:
        lines.extend(["", "# 最近发生的事"])
        lines.extend(f"- {item['text']}" for item in recent)

    if snapshot.get("goal"):
        lines.extend(["", "# 你现在自己的目标", str(snapshot["goal"])])
    plan = snapshot.get("plan") or []
    if plan:
        step = int(snapshot.get("plan_step") or 0)
        lines.append("你的计划：" + " → ".join(str(item) for item in plan))
        if 0 <= step < len(plan):
            lines.append(f"下一步：{plan[step]}")
        else:
            lines.append("计划已经走完，等你自己决定要不要继续。")
    last_action = snapshot.get("last_action_result") or {}
    if isinstance(last_action, dict) and last_action.get("status"):
        status = str(last_action.get("status") or "")
        ok = last_action.get("ok")
        detail = str(last_action.get("detail") or "").strip()
        if status == "issued":
            lines.extend(["", "# 上一条动作", "动作已经发出，你还没收到结果。"])
        elif status == "finished":
            lines.extend(
                [
                    "",
                    "# 上一条动作的结果",
                    "结果：" + ("完成" if ok else "没完成"),
                ]
            )
            if detail:
                lines.append(f"具体：{detail}")
        failures = int(last_action.get("failure_streak") or 0)
        if failures > 0:
            lines.append(f"这条路线已经失败 {failures} 次。别机械重复同一个动作；先想想够不够得到、位置对不对，必要时改计划。")
    return "\n".join(lines)
