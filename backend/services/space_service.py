"""小世界：房间状态、互动事件、自由模式与动作选择（v7.21 / D374）。

边界：
- 这里只管"房间里发生了什么"和"她下一步想做什么"；
- 不直接操作 Three.js，也不把 LLM 的自由文本当动作执行；
- 动作必须先归一化成固定词表（go_to / sit / lie / use / arrange / idle / wait），
  再由前端执行。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import config as backend_config
from backend.agent.parser import MoodTagParser
from backend.llm.errors import LLMConfigError
from backend.llm.factory import create_provider, create_provider_for_purpose
from backend.models import Friend, FriendSpace, FriendSpaceRuntime, SpaceEvent, SpaceObjectKnowledge
from backend.services import memory_service, settings_service
from backend.services import space_tools, space_world_service
from backend.services.actor import Actor
from backend.services.errors import NotFoundError
from backend.services.space_touch import touch_sensation

MODES = ("free", "together")
DEFAULT_MODE = "free"

LAYOUT_KEYS = ("book", "mug", "cushion", "notebook", "plush")
DEFAULT_LAYOUT: dict[str, list[float]] = {
    "book": [0.42, 0.05, -0.55],
    "mug": [0.95, 0.11, -0.9],
    "cushion": [-0.95, 0.16, 0.15],
    "notebook": [-1.35, 0.06, -0.5],
    "plush": [0.5, 0.18, 0.32],
}
DEFAULT_PROP_STATES: dict[str, str] = {
    "book": "closed",
    "mug": "full",
    "cushion": "normal",
    "notebook": "closed",
    "plush": "normal",
    "room_light": "on",
    "curtain": "open",
    "window": "closed",
    "door": "closed",
}
DEFAULT_APPEARANCE: dict[str, Any] = {
    "outfit": {
        "top": "wear",
        "bottom": "wear",
        "shoes": "wear",
        "accessory": "wear",
    },
}
OUTFIT_OPTIONS: dict[str, tuple[str, ...]] = {
    # D469：小世界不再提供裸露/成人向衣橱状态。上下装永远穿着；
    # 鞋和配饰仍可切换，避免把“换装”直接砍成只剩一套。
    "top": ("wear",),
    "bottom": ("wear",),
    "shoes": ("wear", "none"),
    "accessory": ("wear", "none"),
}
OUTFIT_LABELS: dict[str, str] = {
    "wear": "穿着",
    "none": "没有",
}
FIXTURE_STATE_KEYS = ("room_light", "curtain", "window", "door")
WORLD_ENVIRONMENTS = ("room", "white")
WORLD_ASSETS: dict[str, str] = {
    "table": "一张桌子",
    "chair": "一把椅子",
    "shelf": "一个书架",
    "bed": "一张床",
    "rug": "一块地毯",
    "plant": "一盆植物",
    "lamp": "一盏灯",
    "box": "一个箱子",
    "cushion": "一个靠垫",
    "blanket": "一条毯子",
    "nest": "一个小窝",
    "procedural": "一个程序化物件",
}
DEFAULT_WORLD: dict[str, Any] = {"environment": "room", "objects": []}

EVENT_KINDS = {
    "enter",
    "leave",
    "touch",
    "move",
    "use",
    "call",
    "say",
    "arrange",
    "autonomy",
    "action_result",
    "mode",
    "memory",
}
ACTION_KINDS = (
    "go_to",
    "sit",
    "lie",
    "use",
    "arrange",
    "idle",
    "wait",
    "approach",
    "set_state",
    "pick_up",
    "put_down",
    "push",
)
ATTENTIONS = ("busy", "notice", "engaged")
OPENING_STAGES = ("waiting", "observing", "understood", "realized", "daily")
KNOWLEDGE_STATUSES = ("unknown", "observed", "understood")
POST_ANSWER_QUIET_SECONDS = 25.0
# 小世界不是每条动作都要重新开一次会。自主循环放慢，动作执行期间
# 不再几秒一次地叫模型自言自语；用户说话/触碰仍然即时响应。
DELIBERATION_MIN_SECONDS = 6.0
DELIBERATION_MAX_SECONDS = 20.0
DELIBERATION_APPROACH_SECONDS = 4.0
DELIBERATION_SETTLE_SECONDS = 0.6
DELIBERATION_AT_TARGET_DISTANCE = 0.95
FREE_SPEECH_COOLDOWN_SECONDS = 30.0
TOUCH_SPEECH_COOLDOWN_SECONDS = 8.0
SPEECH_DUPLICATE_THRESHOLD = 0.55
RECENT_SPEECH_LIMIT = 8
# 问句不是普通台词：她停下来等 TA 回答，但也不是永久冻结。
# 90 秒内没人回、或 TA 已离开，就回到自己的生活。
AWAITING_REPLY_SECONDS = 90.0
SPACE_EMOTIONS = (
    "happy",
    "excited",
    "shy",
    "curious",
    "surprised",
    "sad",
    "angry",
    "tired",
    "calm",
)
ANCHORS = {
    "center": [0.0, 0.0, 0.0],
    "window": [1.45, 0.0, -2.02],
    "bed": [1.45, 0.0, -2.02],
    "desk": [1.7, 0.0, 2.25],
    "shelf": [-2.1, 0.0, -1.9],
    "cushion": [-0.95, 0.0, 0.15],
}
ZONE_LABELS = {
    "head": "头",
    "ear": "耳朵",
    "tail": "尾巴",
    "hand": "手",
    "body": "身体",
    "neck": "脖子",
    "chest": "胸口",
    "waist": "腰",
    "thigh": "腿",
    "butt": "后面",
}
OBJECT_LABELS = {
    "book": "书",
    "mug": "杯子",
    "cushion": "靠垫",
    "notebook": "笔记本",
    "plush": "鲸鱼玩偶",
}

_DEDUPE_SECONDS = 1.5
_PROMPT_HOURS = 24
_PROMPT_LIMIT = 5
_MEMORY_KINDS = {"move", "arrange", "use"}
# 小世界每 500ms 都会写运行态；SQLite 的读事务再升级写事务时，如果别的请求恰好
# 提交过，会直接 `database is locked`（busy_timeout 救不了 WAL 快照冲突）。
# 同一个进程里把空间写操作排成一条队，并在锁内提交，避免 2Hz tick 互相踩。
_SPACE_WRITE_LOCK = threading.RLock()
_DELIBERATION_LOCKS: dict[str, asyncio.Lock] = {}
_TOUCH_REACTION_TIMES: dict[str, float] = {}


def _serialized_space_write(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _SPACE_WRITE_LOCK:
            result = fn(*args, **kwargs)
            session = args[0] if args else kwargs.get("session")
            if isinstance(session, Session):
                session.commit()
            return result

    return wrapper


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _normalize_emotion(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in SPACE_EMOTIONS else ""


def _looks_like_question(text: str | None) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if value.endswith(("?", "？")):
        return True
    return bool(re.search(r"(吗|呢|么|是不是|要不要|能不能|可不可以)[。！!]?$", value))


def _store_proactive_line(
    session: Session,
    friend_id: str,
    *,
    line: str,
    emotion: str = "",
) -> None:
    """把她主动说出的话记进小世界自己的事件流，不污染主聊天。"""
    _store_space_say(
        session,
        friend_id,
        speaker="assistant",
        text=line,
        emotion=emotion,
        proactive=True,
    )


def _store_space_say(
    session: Session,
    friend_id: str,
    *,
    speaker: str,
    text: str,
    emotion: str = "",
    proactive: bool = False,
) -> None:
    """把小世界对话的一方记进事件流；用户那一半也必须落库。"""
    text = str(text or "").strip()
    if not text:
        return
    try:
        session.add(
            SpaceEvent(
                id=_new_id(),
                friend_id=friend_id,
                kind="say",
                target=None,
                detail=json.dumps(
                    {
                        "speaker": speaker,
                        "text": text,
                        "emotion": emotion,
                        "proactive": proactive,
                    },
                    ensure_ascii=False,
                ),
                notable=1,
                created_at=_now(),
            )
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 —— 说话已经发生，消息桥失败不能倒扣
        logger.warning("小世界对话写空间事件失败：{}", exc)


def _save_space_memory(
    session: Session,
    friend_id: str,
    note: str,
    *,
    context: str = "小世界 · 对话",
) -> None:
    """只有模型明确标成值得记住的内容才进入长期记忆。"""
    text = str(note or "").strip()[:500]
    if not text:
        return
    try:
        from backend.services import memory_service  # noqa: PLC0415

        memory_service.save(
            session,
            Actor.AGENT,
            friend_id,
            content=text,
            tag="normal",
            layer="long_term",
            context=context,
            importance=0.62,
        )
    except Exception as exc:  # noqa: BLE001 —— 记忆失败不能毁掉当前互动
        logger.warning("小世界模型指定记忆写入失败：{}", exc)


def _mark_awaiting_reply(
    session: Session,
    friend_id: str,
    *,
    line: str,
    emotion: str = "",
) -> bool:
    if not _looks_like_question(line):
        return False
    runtime = get_or_create_runtime(session, friend_id)
    now_dt = datetime.now(timezone.utc)
    runtime.intent = json.dumps(
        {
            "kind": "awaiting_reply",
            "line": line,
            "emotion": emotion,
            "since": now_dt.isoformat(),
            "expires_at": (
                now_dt + timedelta(seconds=AWAITING_REPLY_SECONDS)
            ).isoformat(),
        },
        ensure_ascii=False,
    )
    runtime.next_deliberation_at = (
        now_dt + timedelta(seconds=AWAITING_REPLY_SECONDS)
    ).isoformat()
    runtime.attention = "engaged"
    runtime.updated_at = now_dt.isoformat()
    session.flush()
    return True


def _parse(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _json_list(raw: str | None) -> list:
    try:
        data = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _clean_plan(value: Any, *, limit: int = 8) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = re.sub(r"\s+", " ", str(item or "")).strip()[:160]
        if text and text not in seen:
            seen.add(text)
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _age_seconds(value: str | None) -> float:
    if not value:
        return 1e9
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()
    except (TypeError, ValueError):
        return 1e9


def _knowledge_row(
    session: Session, friend_id: str, object_key: str
) -> SpaceObjectKnowledge | None:
    return session.scalars(
        select(SpaceObjectKnowledge).where(
            SpaceObjectKnowledge.friend_id == friend_id,
            SpaceObjectKnowledge.object_key == object_key,
        )
    ).first()


def _voice_examples(friend: Friend) -> list[str]:
    return [
        str(item).strip()[:200]
        for item in _json_list(friend.voice_examples)
        if str(item).strip()
    ][:6]


def _focus_candidate(
    rows: list[tuple[dict, SpaceObjectKnowledge]],
) -> tuple[dict, SpaceObjectKnowledge] | None:
    """返回当前最值得注意的物件；只给深思一个焦点，不替她说话。"""
    if not rows:
        return None
    item, row = max(rows, key=lambda entry: float(entry[1].salience or 0))
    return item, row


def _world_from_row(row: FriendSpace) -> dict:
    raw = _parse(row.world)
    environment = raw.get("environment")
    objects = raw.get("objects")
    return {
        "environment": environment if environment in WORLD_ENVIRONMENTS else "room",
        "objects": objects if isinstance(objects, list) else [],
    }


def _valid_world(world: dict) -> dict:
    if not isinstance(world, dict):
        raise ValueError("world 必须是对象")
    environment = str(world.get("environment") or "room")
    if environment not in WORLD_ENVIRONMENTS:
        raise ValueError("world.environment 只能是 room / white")
    objects = world.get("objects") or []
    if not isinstance(objects, list):
        raise ValueError("world.objects 必须是数组")
    if len(objects) > 24:
        raise ValueError("房间里的物件最多 24 个")
    cleaned: list[dict] = []
    for item in objects:
        if not isinstance(item, dict):
            raise ValueError("world.objects 里每一项都必须是对象")
        asset = str(item.get("asset") or "")
        if asset.startswith("glb:"):
            filename = asset[4:]
            if not re.fullmatch(r"[0-9a-fA-F-]+\.glb", filename):
                raise ValueError("GLB 资源文件名不合法")
        elif asset not in WORLD_ASSETS:
            raise ValueError(f"不认识的物件资源：{asset}")
        position = item.get("position") or [0, 0, 0]
        if (
            not isinstance(position, (list, tuple))
            or len(position) != 3
            or not all(isinstance(value, (int, float)) for value in position)
        ):
            raise ValueError("物件 position 必须是三个数字")
        position = [max(-4.8, min(4.8, float(value))) for value in position]
        try:
            rotation = float(item.get("rotation") or 0)
            scale = float(item.get("scale") or 1)
        except (TypeError, ValueError) as exc:
            raise ValueError("物件 rotation / scale 必须是数字") from exc
        rotation = max(-6.283, min(6.283, rotation))
        scale = max(0.35, min(2.5, scale))
        state = item.get("state")
        cleaned.append(
            {
                "id": str(item.get("id") or uuid.uuid4()),
                "asset": asset,
                "position": position,
                "rotation": rotation,
                "scale": scale,
                "state": state if isinstance(state, dict) else {},
            }
        )
    return {"environment": environment, "objects": cleaned}


def _require_friend(session: Session, friend_id: str) -> Friend:
    friend = session.get(Friend, friend_id)
    if friend is None or friend.deleted_at:
        raise NotFoundError("朋友不存在")
    return friend


@_serialized_space_write
def get_or_create(session: Session, friend_id: str) -> FriendSpace:
    _require_friend(session, friend_id)
    row = session.get(FriendSpace, friend_id)
    if row is None:
        row = FriendSpace(
            friend_id=friend_id,
            layout=json.dumps(DEFAULT_LAYOUT, ensure_ascii=False),
            mode=DEFAULT_MODE,
            current_action="{}",
            prop_states=json.dumps(DEFAULT_PROP_STATES, ensure_ascii=False),
            world=json.dumps(DEFAULT_WORLD, ensure_ascii=False),
            appearance=json.dumps(DEFAULT_APPEARANCE, ensure_ascii=False),
            updated_at=_now(),
        )
        session.add(row)
        session.flush()
    return row


def state_dict(row: FriendSpace) -> dict:
    return {
        "friend_id": row.friend_id,
        "layout": {**DEFAULT_LAYOUT, **_parse(row.layout)},
        "mode": row.mode if row.mode in MODES else DEFAULT_MODE,
        "current_action": _parse(row.current_action),
        "prop_states": {**DEFAULT_PROP_STATES, **_parse(row.prop_states)},
        "world": _world_from_row(row),
        "appearance": _normalize_appearance(_parse(row.appearance), friend_id=row.friend_id),
        "updated_at": row.updated_at,
    }


def get_state(session: Session, friend_id: str) -> dict:
    return state_dict(get_or_create(session, friend_id))


@_serialized_space_write
def get_or_create_runtime(session: Session, friend_id: str) -> FriendSpaceRuntime:
    _require_friend(session, friend_id)
    row = session.get(FriendSpaceRuntime, friend_id)
    if row is None:
        row = FriendSpaceRuntime(
            friend_id=friend_id,
            attention="busy",
            intent="{}",
            interrupt_stack="[]",
            opening_stage="waiting",
            opening_target=None,
            opening_seen=0,
            discovery_score=0,
            wonder_said=0,
            last_discovery_at=None,
            last_deliberation_at=None,
            next_deliberation_at=None,
            goal=None,
            plan="[]",
            plan_step=0,
            last_action_result=None,
            last_perception_at=None,
            updated_at=_now(),
        )
        session.add(row)
        session.flush()
    return row


def runtime_dict(row: FriendSpaceRuntime) -> dict:
    try:
        stack = json.loads(row.interrupt_stack or "[]")
    except (json.JSONDecodeError, TypeError):
        stack = []
    return {
        "friend_id": row.friend_id,
        "attention": row.attention if row.attention in ATTENTIONS else "busy",
        "intent": _parse(row.intent),
        "interrupt_stack": stack if isinstance(stack, list) else [],
        "opening_stage": (
            row.opening_stage if row.opening_stage in OPENING_STAGES else "waiting"
        ),
        "opening_target": row.opening_target,
        "opening_seen": bool(row.opening_seen),
        "discovery_score": float(row.discovery_score or 0),
        "wonder_said": bool(row.wonder_said),
        "last_discovery_at": row.last_discovery_at,
        "last_deliberation_at": row.last_deliberation_at,
        "next_deliberation_at": row.next_deliberation_at,
        "goal": row.goal,
        "plan": _json_list(row.plan),
        "plan_step": int(row.plan_step or 0),
        "last_action_result": _parse(row.last_action_result),
        "last_perception_at": row.last_perception_at,
        "updated_at": row.updated_at,
    }


def _knowledge_dict(row: SpaceObjectKnowledge) -> dict:
    return {
        "id": row.id,
        "friend_id": row.friend_id,
        "object_key": row.object_key,
        "label": row.label,
        "status": row.status,
        "user_explanation": row.user_explanation,
        "novelty": float(row.novelty or 0),
        "salience": float(row.salience or 0),
        "ask_pressure": float(row.ask_pressure or 0),
        "ask_count": int(row.ask_count or 0),
        "ignored_count": int(row.ignored_count or 0),
        "last_seen_at": row.last_seen_at,
        "last_observed_at": row.last_observed_at,
        "last_asked_at": row.last_asked_at,
        "last_answered_at": row.last_answered_at,
        "updated_at": row.updated_at,
    }


def _read_stack(row: FriendSpaceRuntime) -> list[dict]:
    try:
        stack = json.loads(row.interrupt_stack or "[]")
    except (json.JSONDecodeError, TypeError):
        stack = []
    return stack if isinstance(stack, list) else []


def _write_stack(row: FriendSpaceRuntime, stack: list[dict]) -> None:
    row.interrupt_stack = json.dumps(stack[-8:], ensure_ascii=False)


def list_knowledge(session: Session, friend_id: str) -> list[dict]:
    _require_friend(session, friend_id)
    rows = session.scalars(
        select(SpaceObjectKnowledge)
        .where(SpaceObjectKnowledge.friend_id == friend_id)
        .order_by(SpaceObjectKnowledge.updated_at.asc())
    ).all()
    return [_knowledge_dict(row) for row in rows]


@_serialized_space_write
def upsert_knowledge(
    session: Session,
    friend_id: str,
    object_key: str,
    label: str,
    *,
    status: str = "observed",
    user_explanation: str = "",
) -> dict:
    _require_friend(session, friend_id)
    key = str(object_key or "").strip()[:80]
    if not key:
        raise ValueError("object_key 不能为空")
    if status not in KNOWLEDGE_STATUSES:
        raise ValueError("不认识的知识状态")
    row = session.scalars(
        select(SpaceObjectKnowledge).where(
            SpaceObjectKnowledge.friend_id == friend_id,
            SpaceObjectKnowledge.object_key == key,
        )
    ).first()
    if row is None:
        row = SpaceObjectKnowledge(
            id=_new_id(),
            friend_id=friend_id,
            object_key=key,
            label=str(label or key).strip()[:40],
            status=status,
            user_explanation=str(user_explanation or "").strip()[:500],
            updated_at=_now(),
        )
        session.add(row)
    else:
        if label:
            row.label = str(label).strip()[:40]
        if status == "understood" or row.status != "understood":
            row.status = status
        if user_explanation:
            row.user_explanation = str(user_explanation).strip()[:500]
        row.updated_at = _now()
    session.flush()
    return _knowledge_dict(row)


def space_context(session: Session, friend_id: str) -> dict:
    _require_friend(session, friend_id)
    runtime = session.get(FriendSpaceRuntime, friend_id)
    if runtime is None:
        runtime = FriendSpaceRuntime(
            friend_id=friend_id,
            attention="busy",
            intent="{}",
            interrupt_stack="[]",
            opening_stage="waiting",
            opening_target=None,
            opening_seen=0,
            discovery_score=0,
            wonder_said=0,
            last_discovery_at=None,
            last_deliberation_at=None,
            next_deliberation_at=None,
            goal=None,
            plan="[]",
            plan_step=0,
            last_action_result=None,
            last_perception_at=None,
            updated_at=_now(),
        )
    return {
        "runtime": runtime_dict(runtime),
        "knowledge": list_knowledge(session, friend_id),
    }


def _valid_layout(layout: dict) -> dict[str, list[float]]:
    if not isinstance(layout, dict):
        raise ValueError("layout 必须是对象")
    result: dict[str, list[float]] = {}
    for key in LAYOUT_KEYS:
        value = layout.get(key)
        if value is None:
            continue
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 3
            or not all(isinstance(item, (int, float)) for item in value)
        ):
            raise ValueError(f"layout.{key} 必须是三个数字")
        if any(abs(float(item)) > 10 for item in value):
            raise ValueError(f"layout.{key} 超出房间范围")
        result[key] = [float(item) for item in value]
    return result


def _normalize_appearance(raw: dict | None, *, friend_id: str | None = None) -> dict[str, Any]:
    outfit_raw = raw.get("outfit") if isinstance(raw, dict) else None
    outfit = outfit_raw if isinstance(outfit_raw, dict) else {}
    result = {
        slot: str(outfit.get(slot) or default)
        for slot, default in DEFAULT_APPEARANCE["outfit"].items()
    }
    for slot, value in result.items():
        if value not in OUTFIT_OPTIONS[slot]:
            # 旧数据（white_blouse / black_shorts …）统一折算成“穿”，
            # 只有显式 none 才是不穿。
            result[slot] = "none" if value == "none" else "wear"
    # D469：旧数据里的异常穿着状态一律折算成正常穿着。
    result["top"] = "wear"
    result["bottom"] = "wear"
    model = raw.get("model") if isinstance(raw, dict) else None
    model = str(model).strip() if isinstance(model, str) and model.strip() else None
    # 2026-11-07：上传过 VRM 但状态里 model 为空/被重置时，自动捡起
    # user_data/avatar_models/<friend_id>.vrm。这样大肥鱼 3D 不会“文件在、界面却用默认”。
    if model is None and friend_id:
        model_path = backend_config.AVATAR_MODEL_DIR / f"{friend_id}.vrm"
        if model_path.is_file():
            try:
                version = int(model_path.stat().st_mtime * 1000)
            except OSError:
                version = 0
            model = f"/api/friends/{friend_id}/avatar-model?v={version}"
    return {"outfit": result, "model": model}


@_serialized_space_write
def update_state(
    session: Session,
    friend_id: str,
    *,
    layout: dict | None = None,
    mode: str | None = None,
    current_action: dict | None = None,
    prop_states: dict | None = None,
    world: dict | None = None,
    appearance: dict | None = None,
) -> dict:
    row = get_or_create(session, friend_id)
    if layout is not None:
        merged = {**DEFAULT_LAYOUT, **_parse(row.layout), **_valid_layout(layout)}
        row.layout = json.dumps(merged, ensure_ascii=False)
    if mode is not None:
        if mode not in MODES:
            raise ValueError("mode 只能是 free / together")
        row.mode = mode
    if current_action is not None:
        if not isinstance(current_action, dict):
            raise ValueError("current_action 必须是对象")
        row.current_action = json.dumps(current_action, ensure_ascii=False)
    if prop_states is not None:
        if not isinstance(prop_states, dict):
            raise ValueError("prop_states 必须是对象")
        merged_states = {**DEFAULT_PROP_STATES, **_parse(row.prop_states)}
        for key, value in prop_states.items():
            if (
                key in LAYOUT_KEYS or key in FIXTURE_STATE_KEYS
            ) and isinstance(value, str) and value.strip():
                merged_states[key] = value.strip()[:30]
        row.prop_states = json.dumps(merged_states, ensure_ascii=False)
    if world is not None:
        row.world = json.dumps(_valid_world(world), ensure_ascii=False)
    if appearance is not None:
        if not isinstance(appearance, dict):
            raise ValueError("appearance 必须是对象")
        current_appearance = _normalize_appearance(_parse(row.appearance), friend_id=friend_id)
        incoming_outfit = appearance.get("outfit")
        incoming_outfit = (
            incoming_outfit if isinstance(incoming_outfit, dict) else {}
        )
        merged_outfit = {
            **current_appearance["outfit"],
            **{
                slot: value
                for slot, value in incoming_outfit.items()
                if slot in OUTFIT_OPTIONS and isinstance(value, str)
            },
        }
        current_model = current_appearance.get("model")
        incoming_model = appearance.get("model", current_model)
        row.appearance = json.dumps(
            _normalize_appearance(
                {"outfit": merged_outfit, "model": incoming_model},
                friend_id=friend_id,
            ),
            ensure_ascii=False,
        )
    row.updated_at = _now()
    session.flush()
    return state_dict(row)


def _event_dict(event: SpaceEvent) -> dict:
    return {
        "id": event.id,
        "friend_id": event.friend_id,
        "kind": event.kind,
        "target": event.target,
        "detail": _parse(event.detail),
        "notable": bool(event.notable),
        "created_at": event.created_at,
    }


def _recent_assistant_says(
    session: Session, friend_id: str, *, limit: int = RECENT_SPEECH_LIMIT
) -> list[dict[str, Any]]:
    """最近她自己说过的话。只用于反重复和可见的连续内心，不做内容审查。"""
    rows = session.scalars(
        select(SpaceEvent)
        .where(
            SpaceEvent.friend_id == friend_id,
            SpaceEvent.kind == "say",
        )
        .order_by(SpaceEvent.created_at.desc())
        .limit(max(1, min(40, limit * 4)))
    ).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        detail = _parse(row.detail)
        if str(detail.get("speaker") or "") != "assistant":
            continue
        text = str(detail.get("text") or "").strip()
        if not text:
            continue
        result.append(
            {
                "text": text[:400],
                "created_at": row.created_at,
                "emotion": str(detail.get("emotion") or "").strip(),
                "proactive": bool(detail.get("proactive")),
            }
        )
        if len(result) >= limit:
            break
    return result


def _speech_gate(
    session: Session,
    friend_id: str,
    text: str,
    *,
    cooldown_seconds: float,
) -> tuple[bool, str]:
    """自主/触碰台词的反重复闸；直接对用户说话不走这里。"""
    clean = str(text or "").strip()
    if not clean:
        return False, "empty"
    recent = _recent_assistant_says(session, friend_id)
    if recent:
        age = _age_seconds(str(recent[0].get("created_at") or ""))
        if age < cooldown_seconds:
            return False, f"cooldown:{age:.1f}s"
        for item in recent:
            score = memory_service.similarity(clean, str(item.get("text") or ""))
            if score >= SPEECH_DUPLICATE_THRESHOLD:
                return False, f"duplicate:{score:.2f}"
    return True, "ok"


def _recent_say_prompt(session: Session, friend_id: str) -> str:
    recent = _recent_assistant_says(session, friend_id, limit=6)
    if not recent:
        return ""
    return (
        "# 你最近已经说过的话（不要复述这些句子或意思）\n"
        + "\n".join(
            f"- {str(item['text']).strip()[:220]}" for item in reversed(recent)
        )
    )


def _clip_text(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


class SpaceDialogueStreamParser:
    """把角色流式输出的轻量 XML 段拆成 thought / speak / emotion / memory。

    模型可能把标签拆到多个 chunk；这里保留未闭合的后缀，避免把 `<spe`
    当成台词喷给用户。没有标签时退化为纯文本 speak。
    """

    _TAGS: tuple[tuple[str, str | None], ...] = (
        ("<thought>", "thought"),
        ("</thought>", None),
        ("<emotion>", "emotion"),
        ("</emotion>", None),
        ("<speak>", "speak"),
        ("</speak>", None),
        ("<memory>", "memory"),
        ("</memory>", None),
    )

    def __init__(self) -> None:
        self.buffer = ""
        self.raw = ""
        self.mode: str | None = None
        self.fields: dict[str, str] = {
            "thought": "",
            "emotion": "",
            "speak": "",
            "memory": "",
        }
        self.saw_tag = False

    @classmethod
    def _partial_suffix(cls, value: str) -> bool:
        for tag, _field in cls._TAGS:
            for index in range(1, len(tag)):
                if value.endswith(tag[:index]):
                    return True
        return False

    def _append(self, field: str | None, text: str) -> list[tuple[str, str]]:
        if not text:
            return []
        if field in ("thought", "speak", "emotion", "memory"):
            self.fields[field] += text
        if field in ("thought", "speak"):
            return [(field, text)]
        return []

    def feed(self, delta: str) -> list[tuple[str, str]]:
        if not delta:
            return []
        self.buffer += delta
        self.raw += delta
        events: list[tuple[str, str]] = []
        while self.buffer:
            matches = [
                (self.buffer.find(tag), tag, field)
                for tag, field in self._TAGS
                if self.buffer.find(tag) >= 0
            ]
            if not matches:
                if self.mode is None:
                    stripped = self.buffer.strip()
                    if not stripped:
                        self.buffer = ""
                        break
                    if stripped.startswith("{"):
                        # 模型没按标签、而是回了 JSON：先缓冲，结束时再兜底解析。
                        break
                    if self._partial_suffix(self.buffer):
                        break
                    self.mode = "speak"
                if self._partial_suffix(self.buffer):
                    break
                events.extend(self._append(self.mode, self.buffer))
                self.buffer = ""
                break
            position, tag, field = min(matches, key=lambda item: item[0])
            before = self.buffer[:position]
            events.extend(self._append(self.mode, before))
            self.buffer = self.buffer[position + len(tag) :]
            self.mode = field
            self.saw_tag = True
        return events

    def finish(self) -> list[tuple[str, str]]:
        events = self._append(self.mode, self.buffer)
        self.buffer = ""
        if not self.fields["speak"] and not self.saw_tag and not self.raw.strip().startswith("{"):
            self.fields["speak"] = self.raw.strip()
        return events


def _recent_duplicate(
    session: Session, friend_id: str, kind: str, target: str | None
) -> SpaceEvent | None:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_DEDUPE_SECONDS)).isoformat()
    stmt = (
        select(SpaceEvent)
        .where(
            SpaceEvent.friend_id == friend_id,
            SpaceEvent.kind == kind,
            SpaceEvent.created_at >= cutoff,
        )
        .order_by(SpaceEvent.created_at.desc())
        .limit(1)
    )
    if target is None:
        stmt = stmt.where(SpaceEvent.target.is_(None))
    else:
        stmt = stmt.where(SpaceEvent.target == target)
    return session.scalars(stmt).first()


@_serialized_space_write
def log_event(
    session: Session,
    friend_id: str,
    *,
    kind: str,
    target: str | None = None,
    detail: dict | None = None,
    notable: bool | None = None,
    write_memory: bool = True,
) -> dict:
    _require_friend(session, friend_id)
    get_or_create(session, friend_id)
    kind = str(kind or "").strip()
    if kind not in EVENT_KINDS:
        raise ValueError(f"不认识的小世界事件：{kind}")
    target = (str(target).strip()[:40] or None) if target is not None else None
    duplicate = _recent_duplicate(session, friend_id, kind, target)
    if duplicate is not None:
        return _event_dict(duplicate)

    if notable is None:
        notable = kind != "leave"
    event = SpaceEvent(
        id=_new_id(),
        friend_id=friend_id,
        kind=kind,
        target=target,
        detail=json.dumps(detail or {}, ensure_ascii=False),
        notable=1 if notable else 0,
        created_at=_now(),
    )
    session.add(event)
    session.flush()
    if kind in {"touch", "call", "use", "move"}:
        runtime = get_or_create_runtime(session, friend_id)
        now = _now()
        runtime.next_deliberation_at = now
        runtime.updated_at = now
    if write_memory:
        if kind == "autonomy":
            action = (detail or {}).get("action")
            action = action if isinstance(action, dict) else {}
            act_kind = str(action.get("kind") or "")
            act_target = str(action.get("target") or "") or None
            if act_kind in ("use", "arrange") and act_target in OBJECT_LABELS:
                maybe_write_memory(
                    session, friend_id, act_kind, act_target, autonomous=True
                )
        else:
            maybe_write_memory(session, friend_id, kind, target)
    return _event_dict(event)


def recent_events(
    session: Session,
    friend_id: str,
    *,
    limit: int = 20,
    hours: int | None = None,
    notable_only: bool = False,
) -> list[dict]:
    stmt = select(SpaceEvent).where(SpaceEvent.friend_id == friend_id)
    if notable_only:
        stmt = stmt.where(SpaceEvent.notable == 1)
    if hours is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        stmt = stmt.where(SpaceEvent.created_at >= cutoff)
    stmt = stmt.order_by(SpaceEvent.created_at.desc()).limit(max(1, min(100, limit)))
    return [_event_dict(item) for item in session.scalars(stmt).all()]


def _event_line(event: dict) -> str | None:
    kind = str(event.get("kind") or "")
    target = str(event.get("target") or "")
    if kind == "enter":
        return "TA 来小世界看你了"
    if kind == "touch":
        return f"TA 碰了你的{ZONE_LABELS.get(target, target)}"
    if kind == "move":
        return f"TA 把{OBJECT_LABELS.get(target, target)}挪到了别处"
    if kind == "use":
        return f"TA 让你用了{OBJECT_LABELS.get(target, target)}"
    if kind == "call":
        return "TA 叫了你"
    if kind == "say":
        detail = event.get("detail") or {}
        text = str(detail.get("text") or "").strip()
        return f"TA 在小世界里对你说：{text[:60]}" if text else "TA 在小世界里跟你说话"
    if kind == "arrange":
        return f"你自己整理了{OBJECT_LABELS.get(target, target)}"
    if kind == "autonomy":
        detail = event.get("detail") or {}
        text = str(detail.get("text") or "").strip()
        return text or "你自己在房间里做自己的事"
    if kind == "mode":
        detail = event.get("detail") or {}
        return "TA 把小世界切成了自由模式" if detail.get("mode") == "free" else "TA 把小世界切成了陪伴模式"
    return None


def prompt_note(session: Session, friend_id: str) -> str:
    """给聊天注入一小段"最近小世界发生了什么"。"""
    state = get_state(session, friend_id)
    world = state.get("world") or {}
    runtime = session.get(FriendSpaceRuntime, friend_id)
    body: list[str] = []
    if world.get("environment") == "white":
        body.append("房间现在是纯白空间，只有你、地板和摆着的东西")
    objects = world.get("objects") if isinstance(world.get("objects"), list) else []
    labels = [
        WORLD_ASSETS.get(str(item.get("asset") or ""))
        or ("一个导入的物件" if str(item.get("asset") or "").startswith("glb:") else "")
        for item in objects
        if isinstance(item, dict)
    ]
    labels = [label for label in labels if label]
    if labels:
        body.append("房间里现在有：" + "、".join(labels[:12]))

    events = recent_events(
        session, friend_id, limit=16, hours=_PROMPT_HOURS, notable_only=True
    )
    action_lines = [
        line
        for line in (
            _event_line(item) for item in events if item.get("kind") != "say"
        )
        if line and "TA 碰了" not in line and "TA 来了" not in line
    ]
    if action_lines:
        body.extend(action_lines[:3])
    touch_lines = [
        line
        for line in (_event_line(item) for item in events)
        if line and "TA 碰了" in line
    ]
    if touch_lines:
        body.extend(touch_lines[:2])

    if runtime is not None:
        goal = str(runtime.goal or "").strip()
        plan = _json_list(runtime.plan)
        if goal:
            body.append(f"你此刻在空间里想做的事：{goal}")
        if plan:
            step = max(0, min(int(runtime.plan_step or 0), len(plan)))
            progress = f"（现在走到“{plan[step]}”）" if step < len(plan) else "（已经走完）"
            body.append("你的空间计划：" + " → ".join(plan) + progress)
        last_result = _parse(runtime.last_action_result)
        if last_result.get("status") == "finished":
            detail = str(last_result.get("detail") or "").strip()
            ok = bool(last_result.get("ok"))
            body.append(
                "你刚才在空间里"
                + ("做成了：" if ok else "没做成：")
                + (detail or "那件事")
            )
    if not body:
        return ""
    return (
        "# 小世界里刚发生的事\n"
        + "- 你和小世界里的你是同一个人；下面这些是你亲身看到、亲口说过、亲手做过的事。\n"
        + "\n".join(f"- {line}" for line in body[:10])
        + "\n（聊天时可以自然接起这些经历，但别像监控日志一样逐条播报。）"
    )


def maybe_write_memory(
    session: Session,
    friend_id: str,
    kind: str,
    target: str | None,
    *,
    autonomous: bool = False,
) -> None:
    """只有少数事件写长期记忆；最多一天一条，失败绝不影响事件本身。"""
    try:
        if kind not in _MEMORY_KINDS or target not in OBJECT_LABELS:
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        recent_memory = session.scalars(
            select(SpaceEvent)
            .where(
                SpaceEvent.friend_id == friend_id,
                SpaceEvent.kind == "memory",
                SpaceEvent.created_at >= cutoff,
            )
            .limit(1)
        ).first()
        if recent_memory is not None:
            return
        label = OBJECT_LABELS[target]
        if autonomous:
            if kind == "arrange":
                content = f"她自己在小世界里整理了{label}"
            else:
                content = f"她自己在小世界里用了{label}"
        elif kind == "move":
            content = f"用户在小世界里把{label}挪到了别的地方"
        elif kind == "arrange":
            content = f"用户看到她在小世界里整理了{label}"
        else:
            content = f"用户在小世界里让她用了{label}"
        memory_service.save(
            session,
            Actor.AGENT,
            friend_id,
            content=content,
            layer="long_term",
            importance=0.45,
            context="小世界 · 房间互动",
        )
        session.add(
            SpaceEvent(
                id=_new_id(),
                friend_id=friend_id,
                kind="memory",
                target=target,
                detail=json.dumps({"content": content}, ensure_ascii=False),
                notable=0,
                created_at=_now(),
            )
        )
        session.flush()
    except Exception as exc:  # noqa: BLE001 —— 记忆桥坏掉不能影响房间
        logger.warning("小世界记忆桥失败（已忽略）：{}", exc)


def _last_autonomy(session: Session, friend_id: str) -> tuple[str, str]:
    event = session.scalars(
        select(SpaceEvent)
        .where(SpaceEvent.friend_id == friend_id, SpaceEvent.kind == "autonomy")
        .order_by(SpaceEvent.created_at.desc())
        .limit(1)
    ).first()
    if event is None:
        return "", ""
    detail = _parse(event.detail)
    action = detail.get("action") if isinstance(detail.get("action"), dict) else {}
    return str(action.get("kind") or ""), str(action.get("target") or "")


def _normalize_action(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip()
    if kind not in ACTION_KINDS:
        return None
    target = str(raw.get("target") or "").strip() or None
    if kind == "go_to" and target not in ANCHORS:
        return None
    if kind in ("sit", "lie"):
        target = target or "floor"
    if kind in ("use", "arrange") and not target:
        return None
    if kind == "approach" and not target:
        return None
    if kind in ("pick_up", "put_down", "push") and not target:
        return None
    state = raw.get("state")
    if kind == "set_state":
        if not target or not isinstance(state, dict) or not state:
            return None
        state = {
            str(key).strip()[:40]: value
            for key, value in state.items()
            if str(key).strip()
        }
        if not state:
            return None
    else:
        state = None
    if kind == "idle" and target not in (None, "window", "read", "tail", "phone", "stretch"):
        return None
    try:
        duration = float(raw.get("duration_seconds") or 8)
    except (TypeError, ValueError):
        duration = 8.0
    duration = max(3.0, min(90.0, duration))
    try:
        strength = max(0.2, min(3.0, float(raw.get("strength") or 1.0)))
    except (TypeError, ValueError):
        strength = 1.0
    return {
        "kind": kind,
        "target": target,
        "duration_seconds": duration,
        "text": str(raw.get("text") or "").strip()[:80] or None,
        **({"state": state} if state is not None else {}),
        **({"strength": strength} if kind in ("push", "pick_up") else {}),
    }


def _coerce_posture_action(
    action: dict | None,
    world_model: dict[str, Any],
    *texts: str | None,
) -> dict | None:
    """模型偶尔会把“坐下”写成 use/arrange；按真实支撑面纠正成 sit/lie。"""
    if action is None or action.get("kind") not in {"use", "arrange"}:
        return action
    target = str(action.get("target") or "").strip()
    if not target:
        return action
    surfaces = [
        item
        for item in world_model.get("surfaces") or []
        if isinstance(item, dict)
    ]
    surface = next(
        (
            item
            for item in surfaces
            if str(item.get("key") or "") == target
            or (
                target in {"bed", "chair", "cushion", "nest", "floor"}
                and str(item.get("kind") or "") == ("seat" if target == "chair" else target)
            )
        ),
        None,
    )
    if surface is None:
        return action
    phrase = " ".join(str(item or "") for item in texts)
    if "躺" in phrase and surface.get("lieable"):
        return {**action, "kind": "lie", "target": str(surface.get("key") or target)}
    if any(word in phrase for word in ("坐", "坐下", "坐好", "坐稳")) and surface.get("sittable"):
        return {**action, "kind": "sit", "target": str(surface.get("key") or target)}
    return action


def local_action(session: Session, friend_id: str) -> dict:
    """本地规则：不花 token 的"她下一步做什么"。"""
    hour = datetime.now().astimezone().hour
    last_kind, last_target = _last_autonomy(session, friend_id)
    friend = session.get(Friend, friend_id)
    if friend is not None and friend.life_enabled:
        try:
            from backend.services import life_service  # noqa: PLC0415

            status = life_service.status_for(session, friend)
            current = status.get("current_activity")
            current = current if isinstance(current, dict) else {}
            title = str(current.get("title") or current.get("summary") or "")
            hint = action_for_life_text(title)
            if hint is not None and random.random() < 0.65:
                return hint
        except Exception as exc:  # noqa: BLE001 —— 生活线坏了不能拖垮小世界
            logger.debug("小世界读取生活线失败（回退本地规则）：{}", exc)
    if 6 <= hour < 10:
        pool = [
            {"kind": "idle", "target": "stretch", "text": "伸了个懒腰"},
            {"kind": "go_to", "target": "window", "text": "走到窗边看早上"},
            {"kind": "use", "target": "book", "text": "拿起书翻了两页"},
        ]
    elif 10 <= hour < 18:
        pool = [
            {"kind": "use", "target": "book", "text": "坐在一边看书"},
            {"kind": "use", "target": "notebook", "text": "翻开笔记本写点什么"},
            {"kind": "arrange", "target": "cushion", "text": "把靠垫摆好"},
            {"kind": "go_to", "target": "desk", "text": "走到桌边"},
            {"kind": "sit", "target": "bed", "text": "坐到床边"},
            {"kind": "sit", "target": "cushion", "text": "坐到靠垫上"},
            {"kind": "idle", "target": "phone", "text": "刷了一会儿手机"},
        ]
    else:
        pool = [
            {"kind": "sit", "target": "bed", "text": "坐到床边"},
            {"kind": "lie", "target": "floor", "text": "在地板上躺一会儿"},
            {"kind": "use", "target": "book", "text": "靠着床看书"},
            {"kind": "idle", "target": "phone", "text": "窝着刷手机"},
            {"kind": "go_to", "target": "window", "text": "走到窗边"},
            {"kind": "use", "target": "plush", "text": "抱起鲸鱼玩偶"},
        ]
    filtered = [
        item for item in pool if (item["kind"], item.get("target") or "") != (last_kind, last_target)
    ]
    # 别一直站着/一直走：上一次在动，这一次就做点手上的事；反之亦然。
    movement = {"go_to", "sit", "lie"}
    stationary = {"use", "arrange", "idle"}
    if last_kind in movement:
        filtered = [item for item in filtered if item["kind"] in stationary] or filtered
    elif last_kind in stationary:
        filtered = [item for item in filtered if item["kind"] in movement] or filtered
    choice = random.choice(filtered or pool)
    duration = {
        "go_to": 4,
        "sit": 12,
        "lie": 18,
        "use": 8,
        "arrange": 5,
        "idle": 7,
        "wait": 3,
        "approach": 4,
    }[choice["kind"]]
    return {
        "kind": choice["kind"],
        "target": choice.get("target"),
        "duration_seconds": duration,
        "text": choice["text"],
    }


def action_for_life_text(text: str) -> dict | None:
    """把生活线当前活动映射成房间里能做的动作（D378）。"""
    value = (text or "").strip().lower()
    if not value:
        return None
    if any(word in value for word in ("笔记", "新闻", "科技", "编程", "ai", "人工智能")):
        return {
            "kind": "use",
            "target": "notebook",
            "duration_seconds": 18,
            "text": "接着写刚才想到的东西",
        }
    if any(word in value for word in ("书", "读", "文章", "小说", "论文", "资料")):
        return {
            "kind": "use",
            "target": "book",
            "duration_seconds": 20,
            "text": "接着看刚才那本书",
        }
    if any(word in value for word in ("视频", "b站", "电影", "动画", "番", "游戏")):
        return {
            "kind": "idle",
            "target": "phone",
            "duration_seconds": 16,
            "text": "缩在一边刷视频",
        }
    if any(word in value for word in ("音乐", "歌", "专辑")):
        return {
            "kind": "sit",
            "target": "cushion",
            "duration_seconds": 24,
            "text": "靠着靠垫听歌",
        }
    return None


def _provider_for_space(session: Session, friend: Friend, purpose: str = "game_fast"):
    row = settings_service.get(session)
    try:
        return create_provider_for_purpose(row.purposes, purpose)
    except LLMConfigError:
        bases, active = settings_service.llm_config(session)
        return create_provider(bases, active)


def _parse_json_object(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _llm_json(
    session: Session,
    friend: Friend,
    system: str,
    user: str,
    *,
    max_tokens: int = 220,
    purpose: str = "chat",
) -> dict | None:
    provider = _provider_for_space(session, friend, purpose)
    settings_row = settings_service.get(session)
    deep_thinking = bool(settings_row.deep_thinking) and provider.config.capabilities.reasoning
    reasoning_kwargs: dict[str, Any] = {}
    if deep_thinking:
        reasoning_kwargs["reasoning_effort"] = "high"
    chunks: list[str] = []
    async for event in provider.chat_stream(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=float(friend.temperature if friend.temperature is not None else 0.8),
        # 思考链和正文共用输出预算；开着 deep_thinking 时多留一截给 reasoning，
        # 否则 JSON 很容易被思考过程吃光、只剩空回复。
        max_tokens=max_tokens + (700 if deep_thinking else 0),
        **reasoning_kwargs,
    ):
        if event.get("type") == "content":
            chunks.append(str(event.get("delta") or ""))
    return _parse_json_object("".join(chunks))


def _point_text(value: Any) -> str | None:
    try:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return None
        x, y, z = (float(item) for item in value)
        if not all(abs(item) < 100 for item in (x, y, z)):
            return None
        return f"({x:.2f}, {y:.2f}, {z:.2f})"
    except (TypeError, ValueError):
        return None


def _presence_brief(viewer: dict | None) -> str:
    """把用户相机/视线/手翻译成她能读懂的一句空间状态。"""
    if not isinstance(viewer, dict):
        return ""
    parts: list[str] = []
    camera = _point_text(viewer.get("camera"))
    look = _point_text(viewer.get("look"))
    hand = _point_text(viewer.get("hand"))
    if camera:
        parts.append(f"TA 的相机在 {camera}")
    if look:
        parts.append(f"视线方向 {look}")
    if hand:
        parts.append(f"手在 {hand}")
    contact_zone = str(viewer.get("hand_contact_zone") or "").strip()
    contact_kind = str(viewer.get("hand_contact_kind") or "").strip()
    if contact_zone:
        contact_text = {
            "hold": "抓着你没放，手一直贴在你身上",
            "drag": "抓着你，正把你整体带着移动（可能把你提离地面）",
            "push": "抓着你，正把你往外推",
            "pull": "抓着你，正把你往自己身边拉",
        }.get(contact_kind, "正抓着你")
        try:
            contact_speed = float(viewer.get("hand_contact_speed") or 0.0)
        except (TypeError, ValueError):
            contact_speed = 0.0
        parts.append(
            f"{contact_text}（{ZONE_LABELS.get(contact_zone, contact_zone)}，"
            f"{float(viewer.get('hand_contact_seconds') or 0):.1f}s，"
            f"速度 {contact_speed:.2f}m/s）"
        )
        sensation = touch_sensation(contact_zone)
        if sensation:
            parts.append(f"这个部位的身体感受：{sensation}")
    elif viewer.get("hand_active"):
        holding = str(viewer.get("hand_holding") or "").strip()
        parts.append(f"手正抓/推着{holding}" if holding else "手正伸向房间里的东西")
    elif hand:
        parts.append("手现在空着")
    if not parts:
        return ""
    return "TA 现在在小世界里：" + "；".join(parts) + "。"


def _clean_perception_objects(objects: list[dict] | None) -> list[dict]:
    cleaned: list[dict] = []
    for item in objects or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()[:80]
        if not key:
            continue
        position = _point_tuple(item.get("position"))
        if position is None:
            continue
        cleaned.append(
            {
                "key": key,
                "label": str(item.get("label") or key).strip()[:40],
                "position": position,
                "moving": bool(item.get("moving")),
            }
        )
    return cleaned[:40]


def _point_tuple(value: Any) -> tuple[float, float, float] | None:
    try:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            return None
        result = tuple(float(item) for item in value)
        if not all(abs(item) < 100 for item in result):
            return None
        return result  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def _nearest_object(
    objects: list[dict], point: tuple[float, float, float] | None
) -> dict | None:
    if not objects:
        return None
    if point is None:
        return objects[0]
    return min(
        objects,
        key=lambda item: sum(
            (float(item["position"][index]) - point[index]) ** 2 for index in range(3)
        ),
    )


def _knowledge_brief(session: Session, friend_id: str) -> str:
    rows = list_knowledge(session, friend_id)
    known = [
        f"{row['label']}（{row['user_explanation']}）"
        for row in rows
        if row["status"] == "understood" and row["user_explanation"]
    ]
    if not known:
        return ""
    return "你已经弄明白的东西：" + "；".join(known[:8])


def _character_brief(session: Session, friend: Friend, *, query: str = "") -> str:
    """把聊天用的同一套人格状态 / 情节 / 记忆带进小世界。

    最近对话由 `space_world_service` 统一放进世界快照，避免两处重复；
    这里专注那些“她本来就知道”的长期状态和回忆。
    """
    from backend.services import (  # noqa: PLC0415 —— 避开模块循环
        character_state_service,
        episode_service,
    )

    parts: list[str] = []
    state = character_state_service.get_or_create(session, friend.id)
    state_text = character_state_service.to_prompt(state)
    if state_text:
        parts.append(state_text)
    episode_text = episode_service.prompt_note(session, friend.id, limit=8)
    if episode_text:
        parts.append(episode_text)

    if query.strip():
        recalled = memory_service.recall(session, friend.id, query, limit=4)
        if recalled:
            parts.append(
                "# 相关长期记忆\n"
                + "\n".join(
                    f"- {_clip_text(item.content, 360)}" for item in recalled
                )
            )
    return "\n\n".join(parts)


def world_snapshot(
    session: Session,
    friend_id: str,
    *,
    viewer: dict | None = None,
    self_state: dict | None = None,
    objects: list[dict] | None = None,
) -> dict:
    _require_friend(session, friend_id)
    return space_world_service.build_world_snapshot(
        session,
        friend_id,
        viewer=viewer,
        self_state=self_state,
        objects=objects,
    )


def _approach_action(target: str, text: str) -> dict:
    return {
        "kind": "approach",
        "target": target,
        "duration_seconds": 6,
        "text": text,
    }


def _discovery_dict(session: Session, runtime: FriendSpaceRuntime) -> dict:
    rows = list_knowledge(session, runtime.friend_id)
    understood = sum(1 for row in rows if row["status"] == "understood")
    intent = _parse(runtime.intent)
    pending = intent if intent.get("kind") == "ask" else None
    return {
        "score": float(runtime.discovery_score or 0),
        "understood": understood,
        "pending_ask": pending,
        "wonder_said": bool(runtime.wonder_said),
    }


@_serialized_space_write
def handle_ask_explanation(
    session: Session, friend_id: str, text: str
) -> dict | None:
    """用户解释了当前 pending ask 的物件；没有 ask intent 就不写知识。"""
    runtime = get_or_create_runtime(session, friend_id)
    intent = _parse(runtime.intent)
    target = str(intent.get("target") or "").strip()
    if intent.get("kind") != "ask" or not target:
        return None
    row = _knowledge_row(session, friend_id, target)
    label = str(intent.get("label") or (row.label if row else target))
    knowledge = upsert_knowledge(
        session,
        friend_id,
        target,
        label,
        status="understood",
        user_explanation=text,
    )
    try:
        memory_service.save(
            session,
            Actor.AGENT,
            friend_id,
            content=f"在小世界里，TA 告诉我「{label}」是：{text}",
            tag="normal",
            layer="long_term",
            context="小世界",
            importance=0.55,
        )
    except Exception as exc:  # noqa: BLE001 —— 记忆写失败不能丢解释
        logger.warning("小世界解释写长期记忆失败（不影响知识）：{}", exc)
    current = _knowledge_row(session, friend_id, target)
    if current is not None:
        current.ask_pressure = 0
        current.novelty = 0.08
        current.salience = 0
        current.last_answered_at = _now()
        current.updated_at = _now()
    # 刚弄明白一样东西，不会立刻像清单一样追问下一件。
    for other in session.scalars(
        select(SpaceObjectKnowledge).where(
            SpaceObjectKnowledge.friend_id == friend_id,
            SpaceObjectKnowledge.object_key != target,
        )
    ).all():
        other.ask_pressure = max(0.0, float(other.ask_pressure or 0) * 0.55)
        other.updated_at = _now()
    runtime.discovery_score = float(runtime.discovery_score or 0) + 1
    runtime.last_discovery_at = _now()
    runtime.last_deliberation_at = _now()
    runtime.next_deliberation_at = (
        datetime.now(timezone.utc) + timedelta(seconds=POST_ANSWER_QUIET_SECONDS)
    ).isoformat()
    runtime.attention = "engaged"
    runtime.intent = json.dumps(
        {
            "kind": "understanding",
            "target": target,
            "label": label,
            "since": _now(),
        },
        ensure_ascii=False,
    )
    runtime.updated_at = _now()
    session.flush()
    return {
        "target": target,
        "label": label,
        "explanation": text,
        "knowledge": knowledge,
        "discovery": _discovery_dict(session, runtime),
    }


@_serialized_space_write
def perceive(
    session: Session,
    friend_id: str,
    *,
    viewer: dict | None,
    objects: list[dict] | None,
    self_state: dict | None = None,
    paused: bool = False,
    personality_curiosity: float = 0.5,
    activity_kind: str | None = None,
    activity_target: str | None = None,
    activity_label: str | None = None,
    mood: str | None = None,
) -> dict:
    """2Hz 本地感知 tick：只推进 salience / 焦点，不调 LLM、不生成台词。"""
    _require_friend(session, friend_id)
    runtime = get_or_create_runtime(session, friend_id)
    cleaned = _clean_perception_objects(objects)
    now = _now()
    runtime.last_perception_at = now
    runtime.updated_at = now
    focus_target = str(activity_target or "").strip()
    hand = _point_tuple((viewer or {}).get("hand"))
    camera = _point_tuple((viewer or {}).get("camera"))
    look = _point_tuple((viewer or {}).get("look"))
    hand_active = bool((viewer or {}).get("hand_active"))
    hand_holding = str((viewer or {}).get("hand_holding") or "")
    contact_zone = str((viewer or {}).get("hand_contact_zone") or "").strip()[:20]
    contact_kind = str((viewer or {}).get("hand_contact_kind") or "hold").strip()[:20]
    try:
        contact_seconds = max(
            0.0, float((viewer or {}).get("hand_contact_seconds") or 0.0)
        )
    except (TypeError, ValueError):
        contact_seconds = 0.0
    if paused:
        return {
            **space_context(session, friend_id),
            "line": None,
            "action": None,
            "ask_intent": None,
            "deliberation_due": False,
            "focus": None,
            "awaiting_reply": False,
            "discovery": _discovery_dict(session, runtime),
        }

    current_intent = _parse(runtime.intent)
    if contact_zone:
        if current_intent.get("kind") != "being_touched":
            stack = _read_stack(runtime)
            stack.append(current_intent or {"kind": "daily_life"})
            _write_stack(runtime, stack)
        runtime.attention = "notice"
        runtime.intent = json.dumps(
            {
                "kind": "being_touched",
                "zone": contact_zone,
                "motion": contact_kind,
                "seconds": round(contact_seconds, 2),
                "since": current_intent.get("since") or now,
            },
            ensure_ascii=False,
        )
        runtime.updated_at = now
        session.flush()
        return {
            **space_context(session, friend_id),
            "line": None,
            "action": None,
            "ask_intent": None,
            "deliberation_due": False,
            "focus": None,
            "awaiting_reply": False,
            "discovery": _discovery_dict(session, runtime),
        }
    if current_intent.get("kind") == "being_touched":
        stack = _read_stack(runtime)
        restored = stack.pop() if stack else {"kind": "daily_life"}
        _write_stack(runtime, stack)
        runtime.intent = json.dumps(restored, ensure_ascii=False)
        runtime.attention = "busy"
        current_intent = restored
    if current_intent.get("kind") == "awaiting_reply":
        waiting_since = str(current_intent.get("since") or "")
        if _age_seconds(waiting_since) < AWAITING_REPLY_SECONDS:
            return {
                **space_context(session, friend_id),
                "line": None,
                "action": None,
                "ask_intent": None,
                "deliberation_due": False,
                "focus": None,
                "awaiting_reply": True,
                "discovery": _discovery_dict(session, runtime),
            }
        runtime.intent = json.dumps(
            {"kind": "daily_life", "since": now},
            ensure_ascii=False,
        )
        runtime.next_deliberation_at = now
        runtime.attention = "busy"
        current_intent = {}
    if current_intent.get("kind") == "notice_user":
        stack = _read_stack(runtime)
        if not hand_active:
            restored = stack.pop() if stack else {"kind": "daily_life"}
            _write_stack(runtime, stack)
            runtime.intent = json.dumps(restored, ensure_ascii=False)
            runtime.attention = "busy"
            current_intent = restored

    rows: list[tuple[dict, SpaceObjectKnowledge]] = []
    for item in cleaned:
        row = _knowledge_row(session, friend_id, item["key"])
        if row is None:
            upsert_knowledge(session, friend_id, item["key"], item["label"], status="observed")
            row = _knowledge_row(session, friend_id, item["key"])
        if row is None:
            continue
        distance = 1e9
        if hand is not None:
            distance = sum(
                (float(item["position"][i]) - hand[i]) ** 2 for i in range(3)
            ) ** 0.5
        near = 1.0 if distance < 1.35 else 0.0
        moving = 1.0 if item.get("moving") else 0.0
        looking = 0.0
        if camera is not None and look is not None:
            vx = float(item["position"][0]) - camera[0]
            vy = float(item["position"][1]) - camera[1]
            vz = float(item["position"][2]) - camera[2]
            length = max(1e-6, (vx * vx + vy * vy + vz * vz) ** 0.5)
            dot = (vx * look[0] + vy * look[1] + vz * look[2]) / length
            looking = max(0.0, min(1.0, (dot - 0.82) / 0.18))
        held = 1.0 if hand_active and hand_holding and hand_holding in item["label"] else 0.0
        activity_interest = 1.0 if focus_target and focus_target == item["key"] else 0.0
        if row.status == "understood":
            novelty = 0.06
        elif row.status == "observed":
            novelty = max(0.05, float(row.novelty or 0.65))
        else:
            novelty = 1.0
        salience = min(
            1.0,
            0.1 * novelty
            + 0.4 * near
            + 0.2 * moving
            + 0.2 * looking
            + 0.1 * held
            + 0.15 * activity_interest,
        )
        if row.status == "understood":
            salience *= 0.25
        row.novelty = max(0.03, novelty * 0.985)
        row.salience = salience
        row.last_seen_at = now
        row.updated_at = now
        rows.append((item, row))

    if hand_active and current_intent.get("kind") != "ask":
        stack = _read_stack(runtime)
        if current_intent.get("kind") != "notice_user":
            stack.append(current_intent or {"kind": "daily_life"})
            _write_stack(runtime, stack)
        runtime.attention = "notice"
        runtime.intent = json.dumps({"kind": "notice_user", "since": now}, ensure_ascii=False)

    focus_pair = _focus_candidate(rows)
    focus_new = False
    if focus_pair is not None:
        item, row = focus_pair
        target = str(item["key"])
        if current_intent.get("kind") != "considering" or str(
            current_intent.get("target") or ""
        ) != target:
            runtime.attention = "notice"
            runtime.intent = json.dumps(
                {
                    "kind": "considering",
                    "target": target,
                    "label": item["label"],
                    "reason": "现在最吸引你注意",
                    "since": now,
                },
                ensure_ascii=False,
            )
            current_intent = _parse(runtime.intent)
            focus_new = True
        focus = {
            "object_key": target,
            "label": item["label"],
            "salience": float(row.salience or 0),
            "ask_pressure": 0.0,
            "reason": "现在最吸引你注意",
        }
    elif current_intent.get("kind") == "considering" and current_intent.get("target"):
        focus = {
            "object_key": str(current_intent.get("target")),
            "label": str(current_intent.get("label") or current_intent.get("target")),
            "salience": 0.0,
            "ask_pressure": 0.0,
            "reason": str(current_intent.get("reason") or "还在想刚才的东西"),
        }
    else:
        focus = None

    next_at = runtime.next_deliberation_at
    due_clock = not next_at or _age_seconds(next_at) >= 0
    session.flush()
    return {
        **space_context(session, friend_id),
        "line": None,
        "action": None,
        "ask_intent": None,
        "deliberation_due": bool(due_clock or focus_new),
        "focus": focus,
        "awaiting_reply": False,
        "discovery": _discovery_dict(session, runtime),
    }


async def _deliberate_inner(
    session: Session,
    friend_id: str,
    *,
    viewer: dict | None,
    self_state: dict | None,
    objects: list[dict] | None,
    personality_curiosity: float = 0.5,
    activity_kind: str | None = None,
    activity_target: str | None = None,
    activity_label: str | None = None,
    mood: str | None = None,
    allow_llm: bool = True,
) -> dict:
    """micro 深思一次：决定她接下来怎么动、要不要开口。

    2Hz 本地 tick 只提示“该想一下了”；这里才允许模型接管身体动作。
    走路中永远不说话；只有站定并靠近关注物后，才可能产生 ask。
    """
    friend = _require_friend(session, friend_id)
    runtime = get_or_create_runtime(session, friend_id)
    cleaned = _clean_perception_objects(objects)
    curiosity = max(0.0, min(1.0, float(personality_curiosity or 0.5)))
    contact_zone = str((viewer or {}).get("hand_contact_zone") or "").strip()[:20]
    zone_sensation = touch_sensation(contact_zone) if contact_zone else ""
    if contact_zone:
        # 被 TA 抓着/推拉时，身体控制权在 TA 手里；不让她自己再规划走路。
        allow_llm = False

    rows: list[tuple[dict, SpaceObjectKnowledge]] = []
    for item in cleaned:
        row = _knowledge_row(session, friend_id, item["key"])
        if row is not None:
            rows.append((item, row))

    current_intent = _parse(runtime.intent)
    focus_target = (
        str(current_intent.get("target") or "")
        if current_intent.get("kind") in {"considering", "experiment"}
        else ""
    )
    focus_pair = next(
        ((item, row) for item, row in rows if item["key"] == focus_target),
        None,
    )
    if focus_pair is None:
        focus_pair = _focus_candidate(rows)
        if focus_pair is not None:
            focus_target = str(focus_pair[0]["key"])

    focus_label = ""
    focus_explanation = ""
    if focus_pair is not None:
        focus_item, focus_row = focus_pair
        focus_target = str(focus_item["key"])
        focus_label = str(focus_item["label"] or focus_row.label or focus_target)
        focus_explanation = str(focus_row.user_explanation or "还不知道")

    visible_keys = {str(item["key"]) for item, _row in rows}
    world_model = space_world_service.build_world_snapshot(
        session,
        friend_id,
        viewer=viewer,
        self_state=self_state,
        objects=objects,
        activity=activity_label or activity_kind,
        mood=mood,
    )
    world_prompt = space_world_service.to_prompt(world_model)
    toolset_prompt = space_tools.toolset_prompt(world_model)
    entity_keys = {
        str(item.get("key") or "")
        for item in world_model.get("entities") or []
        if item.get("key")
    }
    visible_labels = [str(item["label"]) for item, _row in rows]
    recent_chat_text = " ".join(
        str(item.get("content") or "")
        for item in (world_model.get("recent_chat") or [])[-4:]
    )
    memory_query = " ".join(
        [
            focus_label,
            *visible_labels,
            activity_label or "",
            mood or "",
            recent_chat_text,
        ]
    ).strip()
    character_brief = _character_brief(session, friend, query=memory_query)
    recent_say_prompt = _recent_say_prompt(session, friend_id)
    existing_goal = str(runtime.goal or "").strip()
    existing_plan = _json_list(runtime.plan)
    existing_step = int(runtime.plan_step or 0)
    previous_action_result = _parse(runtime.last_action_result)

    self_state = self_state if isinstance(self_state, dict) else {}
    self_position = _point_tuple(self_state.get("position"))
    moving = bool(self_state.get("moving"))
    try:
        still_seconds = max(0.0, float(self_state.get("still_seconds") or 0.0))
    except (TypeError, ValueError):
        still_seconds = 0.0
    settled = not moving and still_seconds >= DELIBERATION_SETTLE_SECONDS
    posture = str(self_state.get("posture") or "stand")
    posture_text = {"stand": "站着", "sit": "坐着", "lie": "躺着"}.get(
        posture, posture
    )
    support_key = str(self_state.get("support_key") or "").strip()

    focus_distance: float | None = None
    if focus_pair is not None and self_position is not None:
        focus_position = _point_tuple(focus_pair[0].get("position"))
        if focus_position is not None:
            focus_distance = sum(
                (focus_position[i] - self_position[i]) ** 2 for i in range(3)
            ) ** 0.5
    near_focus = (
        focus_distance is not None
        and focus_distance <= DELIBERATION_AT_TARGET_DISTANCE
    )

    line: str | None = None
    thought = ""
    reflection = ""
    preference = ""
    emotion = ""
    space_memory = ""
    source = "local"
    action: dict | None = None
    next_think_in = 10.0
    llm_ok = False
    goal = existing_goal
    plan = existing_plan
    plan_step = existing_step

    if allow_llm:
        voice = _voice_examples(friend)
        voice_note = (
            "\n她平时说话的样本（只学语气和句长，不要照抄）：\n- "
            + "\n- ".join(voice)
            if voice
            else ""
        )
        system = (
            f"你是「{friend.name}」。{friend.personality or ''}\n"
            f"{friend.custom_instructions or ''}{voice_note}\n"
            "你在自己的小房间里生活，不是在回答客服问题。你有自己的节奏："
            "可以继续手里的事、观察、靠近、拿起来、问 TA，或者什么都不做。"
            "房间里的东西默认都属于你，或者属于你和 TA 两个人；你不需要先问许可，"
            "只有太重、够不到、被挡住这种物理条件才会阻止你。"
            "这不是普通聊天：你有一组小世界工具，动作必须落进 space_do；"
            "只说话不动手不算完成 TA 交给你的事。"
            "这不是聊天模式：即使 TA 不说话，你也会继续生活、继续做自己的事。"
            "说话只是你在这个世界里做的一件事，不需要先把聊天框打开。"
            "自由活动默认可以安静：space_say 不是每次思考的旁白，不是把心里话说出来；"
            "只有你确实有一句想对 TA 说、说了会改变此刻气氛的话才填。"
            "自由活动时不要用 space_say 播报动作、姿势或位置（“我坐下了 / 我到了 / 我靠着”）；"
            "只有你确实有想对 TA 说的话才开口。和 TA 对话时可以说 1~3 句，"
            "不要只丢一个单次反应就停。"
            "始终用你和 TA 最近对话使用的语言说话，不要突然切成英文。"
            "自由活动时默认一直在找下一件自己想做的事；没有目标就自己定一个小目标，"
            "不要停在原地等 TA 发话。只有真的累、想休息或需要观察时才 idle / wait。"
            "现在先在心里想一下，再决定接下来几秒做什么。\n"
            "硬规则：\n"
            "1) 你可以边走边说话，也可以在做自己的事时突然想对 TA 说一句；"
            "但如果你问了一个需要 TA 回答的问题，就停下来，用当前表情和动作等他回答。\n"
            "2) 不要客服腔、清单腔、解释内心；每句话都必须是你此刻真的想说，"
            "没有真话就保持安静。\n"
            "3) 不要为了填安静而按固定间隔问问题。只有你看到、想到、正在做的事"
            "真的让你产生了对 TA 说话的冲动，才开口。\n"
            "4) 不要把工具调用/动作过程说成台词；不要用「让我看看 / I'll check / "
            "Let me…」这种旁白。动作直接放进 space_do，space_say 只写你真想对 TA 说的话。\n"
            "5) 动作只能从固定词表里选，不能凭空操作 3D 场景。\n"
            "6) 上一条动作失败时，不要机械重复同一个动作；先判断距离、遮挡和位置，"
            "必要时改计划或换一种做法。\n"
        )
        state_text = (
            f"身体：{posture_text}；{'正在走' if moving else '停着'}；"
            f"已静止 {still_seconds:.1f}s；"
            f"支撑面：{support_key or '脚下地面'}；"
            f"关注物距离 {focus_distance:.2f}m" if focus_distance is not None
            else (
                f"身体：{posture_text}；{'正在走' if moving else '停着'}；"
                f"已静止 {still_seconds:.1f}s；支撑面：{support_key or '脚下地面'}"
            )
        )
        sensation_line = (
            f"这个部位的身体感受：{zone_sensation}\n" if zone_sensation else ""
        )
        user = (
            f"{character_brief}\n\n"
            f"{world_prompt}\n\n"
            f"{toolset_prompt}\n\n"
            f"{recent_say_prompt}\n\n"
            f"当前关注：{focus_label or '没有特别关注的东西'}"
            f"（{focus_target or '-'}）；你对它的了解：{focus_explanation or '还不知道'}\n"
            f"你当前的身体判断：{state_text}\n"
            f"你之前给自己的目标：{existing_goal or '还没有'}\n"
            f"你之前的计划：{' → '.join(existing_plan) if existing_plan else '还没有'}\n"
            f"计划走到第 {existing_step + 1 if existing_plan else 0} 步。\n\n"
            "你要像住在这里的人一样，决定接下来几秒做什么；如果事情需要多步，"
            "可以给自己一个短目标和 2~5 步计划，下一次再继续。\n"
            "默认优先继续已有的计划和动作；不要因为安静就自动说话。"
            "如果最近说过相近的意思，这一拍就保持沉默，只更新 thought/动作。\n"
            "只输出 JSON："
            '{"thought":"用你自己的话写此刻的具体判断，不要照抄字段说明",'
            '"space_plan":{"goal":"你此刻具体想完成什么；没有就空字符串",'
            '"steps":["具体步骤，最多5步"]} 或 null,'
            '"space_do":{"kind":"approach|go_to|sit|lie|use|arrange|idle|wait|set_state|pick_up|put_down|push",'
            '"target":"物件 key / 锚点 / null","duration_seconds":4,'
            '"state":{"可选：你要把物件改成什么状态，如 open:true"}} 或 null,'
            '"space_say":{"text":"可选：想对 TA 说的话，1~3 句；只想做事就 null","emotion":"happy|excited|shy|curious|surprised|sad|angry|tired|calm"} 或 null,'
            '"reflection":"可选：从刚才发生的事里意识到什么",'
            '"preference":"可选：你发现自己更喜欢空间里的什么；没有就空字符串",'
            '"space_memory":"可选：只有这次对话/经历真的值得长期记住时，写一条第一人称记忆；普通闲聊留空",'
            '"next_think_in":10}'
            "\n字段值是具体内容，禁止把说明文字原样写回；"
            "thought / goal / steps / reflection 都用你和 TA 最近使用的语言，不要用英文枚举词。"
        )
        try:
            data = await _llm_json(
                session, friend, system, user, purpose="chat", max_tokens=420
            )
            if data:
                llm_ok = True
                thought = str(data.get("thought") or "").strip()[:240]
                space_plan = (
                    data.get("space_plan")
                    if isinstance(data.get("space_plan"), dict)
                    else {}
                )
                action = _normalize_action(
                    data.get("space_do") or data.get("action")
                )
                if (
                    action is not None
                    and action.get("kind")
                    in {"use", "arrange", "set_state", "pick_up", "put_down", "push"}
                    and action.get("target") not in entity_keys
                    and action.get("target") != "outfit"
                ):
                    action = None
                reflection = str(data.get("reflection") or "").strip()[:240]
                preference = str(data.get("preference") or "").strip()[:200]
                space_memory = str(data.get("space_memory") or "").strip()[:500]
                space_say = data.get("space_say")
                if isinstance(space_say, dict):
                    say = str(space_say.get("text") or "").strip()[:400]
                    emotion = _normalize_emotion(space_say.get("emotion"))
                else:
                    say = str(data.get("say") or data.get("line") or "").strip()[:400]
                    emotion = _normalize_emotion(data.get("emotion"))
                line = say or None
                if line:
                    allowed, gate_reason = _speech_gate(
                        session,
                        friend_id,
                        line,
                        cooldown_seconds=FREE_SPEECH_COOLDOWN_SECONDS,
                    )
                    if not allowed:
                        logger.debug(
                            "小世界自主台词被反重复闸拦下：{}（{}）",
                            gate_reason,
                            line[:80],
                        )
                        line = None
                        emotion = ""
                try:
                    proposed = float(data.get("next_think_in") or next_think_in)
                except (TypeError, ValueError):
                    proposed = next_think_in
                next_think_in = max(
                    DELIBERATION_MIN_SECONDS,
                    min(DELIBERATION_MAX_SECONDS, proposed),
                )
                source = "llm"
                proposed_goal = str(
                    space_plan.get("goal") or data.get("goal") or ""
                ).strip()[:200]
                proposed_plan = _clean_plan(
                    space_plan.get("steps")
                    or space_plan.get("plan")
                    or data.get("plan")
                )
                if proposed_goal:
                    goal = proposed_goal
                if proposed_plan:
                    plan = proposed_plan
                if goal != existing_goal or plan != existing_plan:
                    plan_step = 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("小世界 micro 深思失败，回退本地：{}", exc)

    if contact_zone:
        action = {
            "kind": "wait",
            "target": None,
            "duration_seconds": 2,
            "text": "被 TA 抓着，先不自己动",
        }
        next_think_in = 2.2
    elif action is None:
        if focus_pair is not None and not near_focus:
            action = _approach_action(focus_target, f"走近看看{focus_label}")
        elif focus_pair is not None and not settled:
            action = {
                "kind": "wait",
                "target": focus_target,
                "duration_seconds": 2,
                "text": "先站定看看",
            }
        else:
            action = local_action(session, friend_id)

    action = _coerce_posture_action(action, world_model, line, thought, goal)
    moving_action = action is not None and action.get("kind") in {
        "approach",
        "go_to",
        "sit",
        "lie",
    }
    if moving_action:
        next_think_in = min(next_think_in, DELIBERATION_APPROACH_SECONDS)
    elif action is not None and action.get("kind") == "wait":
        next_think_in = min(next_think_in, 2.2)

    if line and _looks_like_question(line):
        # 问句是一个真实的社会动作：她停下身体、保持情绪，等 TA 回应。
        # 自主活动随后由 awaiting_reply 超时或 TA 的回答恢复。
        action = {
            "kind": "wait",
            "target": None,
            "duration_seconds": max(2.0, min(30.0, AWAITING_REPLY_SECONDS)),
            "text": "停下来等 TA 回答",
        }

    plan_step = max(0, min(plan_step, len(plan)))
    action_id = _new_id() if action is not None else None
    if action is not None and action_id is not None:
        action = {**action, "id": action_id}

    now = _now()
    with _SPACE_WRITE_LOCK:
        session.rollback()
        runtime = get_or_create_runtime(session, friend_id)
        runtime.last_deliberation_at = now
        runtime.updated_at = now
        runtime.goal = goal or None
        runtime.plan = json.dumps(plan, ensure_ascii=False)
        runtime.plan_step = plan_step
        if thought or (goal and goal != existing_goal):
            try:
                from backend.services import character_state_service  # noqa: PLC0415

                delta: dict[str, Any] = {}
                if thought:
                    delta["inner_note"] = thought
                if goal and goal != existing_goal:
                    delta["intent"] = goal[:40]
                    delta["open_loops_add"] = [f"在自由空间里：{goal}"]
                character_state_service.apply_delta(session, friend_id, delta)
            except Exception as exc:  # noqa: BLE001
                logger.warning("小世界目标同步到角色状态失败：{}", exc)

        if line:
            _store_proactive_line(
                session, friend_id, line=line, emotion=emotion
            )
        _save_space_memory(session, friend_id, space_memory)
        waiting = bool(
            line
            and _mark_awaiting_reply(
                session,
                friend_id,
                line=line,
                emotion=emotion,
            )
        )
        if not waiting:
            next_at = (
                datetime.now(timezone.utc) + timedelta(seconds=next_think_in)
            ).isoformat()
            runtime.next_deliberation_at = next_at
            runtime.attention = "notice" if action or line else "busy"
            runtime.intent = json.dumps(
                {
                    "kind": "considering" if focus_target else "daily_life",
                    "target": focus_target or None,
                    "label": focus_label or None,
                    "thought": thought,
                    "line": line,
                    "emotion": emotion,
                    "reflection": reflection,
                    "preference": preference,
                    "since": now,
                },
                ensure_ascii=False,
            )

        if action is not None and action_id is not None:
            runtime.last_action_result = json.dumps(
                {
                    "action_id": action_id,
                    "status": "issued",
                    "ok": None,
                    "detail": "",
                    "goal": goal or None,
                    "reason": thought,
                    "reflection": reflection,
                    "preference": preference,
                    "failure_streak": int(
                        previous_action_result.get("failure_streak") or 0
                    ),
                    "at": now,
                },
                ensure_ascii=False,
            )

        discovery = _discovery_dict(session, runtime)
        context = space_context(session, friend_id)
        session.commit()

    return {
        "source": source,
        "thought": thought,
        "emotion": emotion,
        "reflection": reflection,
        "preference": preference,
        "space_memory": space_memory or None,
        "line": line,
        "action": action,
        "ask_intent": None,
        "next_think_in": next_think_in,
        "awaiting_reply": bool(
            runtime and _parse(runtime.intent).get("kind") == "awaiting_reply"
        ),
        "discovery": discovery,
        **context,
    }


async def deliberate(
    session: Session,
    friend_id: str,
    *,
    viewer: dict | None,
    self_state: dict | None,
    objects: list[dict] | None,
    personality_curiosity: float = 0.5,
    activity_kind: str | None = None,
    activity_target: str | None = None,
    activity_label: str | None = None,
    mood: str | None = None,
    allow_llm: bool = True,
) -> dict:
    """单飞锁：同一朋友同一时间只允许一轮深思。"""
    lock = _DELIBERATION_LOCKS.setdefault(friend_id, asyncio.Lock())
    if lock.locked():
        runtime = session.get(FriendSpaceRuntime, friend_id)
        return {
            "source": "local",
            "thought": "",
            "reflection": "",
            "preference": "",
            "line": None,
            "action": None,
            "ask_intent": None,
            "next_think_in": 1.0,
            "discovery": _discovery_dict(
                session,
                runtime or get_or_create_runtime(session, friend_id),
            ),
            "busy": True,
            **space_context(session, friend_id),
        }
    async with lock:
        return await _deliberate_inner(
            session,
            friend_id,
            viewer=viewer,
            self_state=self_state,
            objects=objects,
            personality_curiosity=personality_curiosity,
            activity_kind=activity_kind,
            activity_target=activity_target,
            activity_label=activity_label,
            mood=mood,
            allow_llm=allow_llm,
        )


@_serialized_space_write
def record_action_result(
    session: Session,
    friend_id: str,
    *,
    action_id: str,
    ok: bool,
    detail: str = "",
) -> dict:
    """前端执行完一条高层动作后回报结果；下一次深思会看到它。"""
    runtime = get_or_create_runtime(session, friend_id)
    previous = _parse(runtime.last_action_result)
    now = _now()
    accepted = str(previous.get("action_id") or "") == str(action_id)
    new_result = accepted and previous.get("status") != "finished"
    plan_completed = False
    goal = str(runtime.goal or "").strip()
    if new_result:
        failure_streak = (
            0 if ok else int(previous.get("failure_streak") or 0) + 1
        )
        runtime.last_action_result = json.dumps(
            {
                "action_id": action_id,
                "status": "finished",
                "ok": bool(ok),
                "detail": str(detail or "").strip()[:500],
                "goal": previous.get("goal") or goal or None,
                "reason": previous.get("reason") or "",
                "reflection": previous.get("reflection") or "",
                "preference": previous.get("preference") or "",
                "failure_streak": failure_streak,
                "at": now,
            },
            ensure_ascii=False,
        )
        if ok:
            plan = _json_list(runtime.plan)
            if plan and int(runtime.plan_step or 0) < len(plan):
                runtime.plan_step = int(runtime.plan_step or 0) + 1
                plan_completed = int(runtime.plan_step or 0) >= len(plan)
            # 游戏模式的续跑：一步做完就很快想下一步，不靠长时间定时器。
            runtime.next_deliberation_at = (
                datetime.now(timezone.utc) + timedelta(seconds=0.9)
            ).isoformat()
        else:
            runtime.next_deliberation_at = (
                datetime.now(timezone.utc) + timedelta(seconds=1.5)
            ).isoformat()
            if failure_streak == 2 and goal:
                try:
                    reason = str(previous.get("reason") or "").strip()
                    reflection = str(previous.get("reflection") or "").strip()
                    memory_service.save(
                        session,
                        Actor.AGENT,
                        friend_id,
                        content=(
                            f"在自由空间里，我试了几次也没做成「{goal}」："
                            f"{str(detail or '').strip()[:240]}"
                            + (f"。我意识到：{reflection}" if reflection else "")
                            + (f"。我当时是这么想的：{reason}" if reason else "")
                        ),
                        tag="normal",
                        layer="long_term",
                        context="小世界 · 改计划",
                        importance=0.56,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("小世界失败反思写记忆失败：{}", exc)
        runtime.updated_at = now
    if plan_completed and goal:
        try:
            reason = str(previous.get("reason") or "").strip()
            reflection = str(previous.get("reflection") or "").strip()
            preference = str(previous.get("preference") or "").strip()
            detail_text = str(detail or "").strip()[:300]
            memory_parts = [f"在自由空间里，我完成了「{goal}」：{detail_text}"]
            if reason:
                memory_parts.append(f"我这么做是因为：{reason}")
            if reflection:
                memory_parts.append(f"做的时候我意识到：{reflection}")
            if preference:
                memory_parts.append(f"这让我发现，我更喜欢：{preference}")
            memory_service.save(
                session,
                Actor.AGENT,
                friend_id,
                content="。".join(memory_parts),
                tag="normal",
                layer="long_term",
                context="小世界",
                importance=0.62,
            )
            from backend.services import character_state_service  # noqa: PLC0415

            character_state_service.apply_delta(
                session,
                friend_id,
                {
                    "open_loops_resolve": [f"在自由空间里：{goal}"],
                    "inner_note": (
                        f"我刚刚在自由空间里完成了：{goal}"
                        + (f"。我更喜欢：{preference}" if preference else "")
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("小世界完成目标写记忆失败：{}", exc)
    log_event(
        session,
        friend_id,
        kind="action_result",
        detail={
            "action_id": action_id,
            "accepted": accepted,
            "ok": bool(ok),
            "detail": str(detail or "").strip()[:500],
            "failure_streak": int(
                _parse(runtime.last_action_result).get("failure_streak") or 0
            )
            if accepted
            else 0,
        },
        notable=True,
    )
    return {
        "runtime": runtime_dict(runtime),
        "knowledge": list_knowledge(session, friend_id),
    }


@_serialized_space_write
def record_space_dialogue(
    session: Session,
    friend_id: str,
    *,
    text: str,
    reply: str,
    emotion: str = "",
) -> dict:
    """更新共享角色状态；小世界对话本身只留在空间事件流里。"""
    from backend.services import character_state_service  # noqa: PLC0415

    _require_friend(session, friend_id)
    user_text = str(text or "").strip()
    reply_text = str(reply or "").strip()
    if not user_text:
        return {}
    try:
        character_state_service.apply_delta(
            session,
            friend_id,
            {
                "inner_note": (
                    f"在小世界里，TA 对我说：{user_text[:100]}；我回：{reply_text[:140]}"
                ),
                "intent": "陪 TA",
            },
        )
    except Exception as exc:  # noqa: BLE001 —— 对话已经落库，状态失败不能倒扣
        logger.warning("小世界对话写回角色状态失败：{}", exc)
    session.flush()
    return {"space_message": True}


async def ask_about_object(
    session: Session,
    friend_id: str,
    *,
    object_key: str,
    viewer: dict | None,
    activity_kind: str | None = None,
    activity_target: str | None = None,
    activity_label: str | None = None,
    mood: str | None = None,
    allow_llm: bool = True,
) -> dict:
    """ask intent 出现后才调用：让 micro-LLM 生成“她具体怎么问”。"""
    friend = _require_friend(session, friend_id)
    runtime = get_or_create_runtime(session, friend_id)
    row = _knowledge_row(session, friend_id, object_key)
    if row is None:
        raise ValueError("这个物件还没有被注意到")
    label = row.label or object_key
    line = ""
    source = "local"
    presence = _presence_brief(viewer)
    if allow_llm:
        system = (
            f"你是「{friend.name}」。{friend.personality or ''}\n"
            "你刚刚自己产生了好奇，想问 TA 一个关于房间里某样东西的问题。"
            "不要解释内心，不要列问题清单，不要 markdown；只问一个自然、短的问题。"
        )
        user = (
            f"{presence}\n\n"
            f"你注意到的是：{label}\n"
            f"你目前对它的了解：{row.user_explanation or '还不知道'}\n"
            f"你想问的原因：{_parse(runtime.intent).get('reason') or '好奇/不确定'}\n"
            f"你刚才正在做的事：{activity_label or activity_kind or '待在自己的房间里'}\n"
            f"你现在的情绪/表情：{mood or '说不上来'}\n"
            '只输出 JSON：{"line":"一句自然的问题"}；如果没有真的想问，line 输出空字符串。'
        )
        try:
            data = await _llm_json(
                session,
                friend,
                system,
                user,
                max_tokens=140,
                purpose="chat",
            )
            if data and str(data.get("line") or "").strip():
                line = str(data["line"]).strip()[:120]
                source = "llm"
        except Exception as exc:  # noqa: BLE001
            logger.warning("小世界提问生成失败（不生成模板台词）：{}", exc)
    # 前面为了拿到 label / 已知解释读过库；LLM 期间其他 2Hz tick 会继续提交。
    # 先把旧读事务结束，再在空间写锁里重新读、写、提交，避免 WAL 快照升级冲突。
    with _SPACE_WRITE_LOCK:
        session.rollback()
        runtime = get_or_create_runtime(session, friend_id)
        intent = _parse(runtime.intent)
        if line and intent.get("kind") == "ask" and str(intent.get("target") or "") == object_key:
            intent.update(
                {
                    "line": line,
                    "source": source,
                    "said_at": _now(),
                }
            )
            runtime.intent = json.dumps(intent, ensure_ascii=False)
            runtime.updated_at = _now()
            session.flush()
        discovery = _discovery_dict(session, runtime)
        session.commit()
    return {
        "source": source,
        "line": line,
        "action": _approach_action(object_key, f"走近看看{label}"),
        "discovery": discovery,
    }


async def react_to_touch(
    session: Session,
    friend_id: str,
    *,
    zone: str,
    viewer: dict | None = None,
    self_state: dict | None = None,
    objects: list[dict] | None = None,
    activity_label: str | None = None,
    mood: str | None = None,
    interaction: str | None = None,
    sustained_seconds: float = 0.0,
    allow_llm: bool = True,
) -> dict:
    """对真实触碰做一次即时、模型驱动的短反应；失败就只保留身体脉冲。"""
    friend = _require_friend(session, friend_id)
    clean_zone = str(zone or "").strip()[:20]
    if not clean_zone:
        raise ValueError("zone 不能为空")
    now_mono = time.monotonic()
    if now_mono - _TOUCH_REACTION_TIMES.get(friend_id, 0.0) < 0.9:
        runtime = get_or_create_runtime(session, friend_id)
        return {
            "source": "local",
            "thought": "",
            "emotion": "",
            "line": None,
            "action": None,
            "busy": True,
            "awaiting_reply": _parse(runtime.intent).get("kind") == "awaiting_reply",
            "discovery": _discovery_dict(session, runtime),
        }
    _TOUCH_REACTION_TIMES[friend_id] = now_mono

    zone_label = ZONE_LABELS.get(clean_zone, clean_zone)
    zone_sensation = touch_sensation(clean_zone)
    contact_kind = str(interaction or "tap").strip().lower()[:20]
    contact_text = {
        "tap": "刚刚碰了你一下",
        "grab": "正抓着你，没有松开",
        "hold": "一直贴着你在轻轻抚摸/揉，没有松开",
        "drag": "正抓着你把你整体带着移动（可能把你提离地面）",
        "push": "正抓着你把你往外推",
        "pull": "正抓着你把你往自己身边拉",
    }.get(contact_kind, "正碰着你")
    held_seconds = max(0.0, float(sustained_seconds or 0.0))
    world_model = space_world_service.build_world_snapshot(
        session,
        friend_id,
        viewer=viewer,
        self_state=self_state,
        objects=objects,
        activity=activity_label,
        mood=mood,
    )
    world_prompt = space_world_service.to_prompt(world_model)
    toolset_prompt = space_tools.toolset_prompt(world_model)
    character_brief = _character_brief(
        session, friend, query=f"{zone_label} 触碰 {activity_label or ''} {mood or ''}"
    )
    recent_say_prompt = _recent_say_prompt(session, friend_id)
    line = ""
    thought = ""
    emotion = ""
    space_memory = ""
    source = "local"
    action: dict | None = None
    hold_text = (
        f"这已经是持续接触约 {held_seconds:.1f} 秒。\n"
        if held_seconds > 0.8
        else ""
    )
    sensation_line = (
        f"这个部位的身体感受：{zone_sensation}\n" if zone_sensation else ""
    )
    if allow_llm:
        system = (
            f"你是「{friend.name}」。{friend.personality or ''}\n"
            f"{friend.custom_instructions or ''}\n"
            "TA 刚刚在小世界里真实地碰了你的身体。你不是客服，也不是只会播固定反应；"
            "你可以只是动一下、可以说话、也可以什么都不说。"
            "用你和 TA 最近对话使用的语言。不要把动作过程说成台词。"
            "如果想说，可说一两句自然短话；不要汇报动作。"
            "如果碰得突然/不舒服/害羞/开心，"
            "用 emotion 标出此刻真实情绪。不要解释内心。"
            "如果最近已经对同一种触碰说过相近的话，这次就保持安静，只做身体反应。"
            "TA 碰你时，默认保持你正在做的事和当前姿势：不要因为被碰就站起来、"
            "坐下、躺下或走过来。只有你自己真的想改变动作时才填 space_do，"
            "否则 space_do 保持 null。"
        )
        user = (
            f"{character_brief}\n\n{world_prompt}\n\n{toolset_prompt}\n\n"
            f"{recent_say_prompt}\n\n"
            f"TA 的手{contact_text}；碰的是你的：{zone_label}（zone={clean_zone}）。\n"
            f"{sensation_line}"
            f"{hold_text}"
            "如果有人碰你而你确实不想回应，就保持沉默。\n"
            "只输出 JSON："
            '{"thought":"用你自己的话写此刻的具体判断，不要照抄字段说明",'
            '"space_say":{"text":"可选一两句自然短话","emotion":"happy|excited|shy|curious|surprised|sad|angry|tired|calm"} 或 null,'
            '"space_do":{"kind":"idle|wait|approach|sit|lie","target":null,"duration_seconds":2} 或 null,'
            '"space_memory":"可选：这次触碰真的值得长期记住时写一条第一人称记忆；否则空字符串"}'
            "\n字段值是具体内容，禁止把说明文字原样写回；用最近对话的语言。"
        )
        try:
            data = await _llm_json(
                session,
                friend,
                system,
                user,
                max_tokens=220,
                purpose="chat",
            )
            if data:
                thought = str(data.get("thought") or "").strip()[:240]
                space_say = data.get("space_say")
                if isinstance(space_say, dict):
                    line = str(space_say.get("text") or "").strip()[:240]
                    emotion = _normalize_emotion(space_say.get("emotion"))
                else:
                    line = str(data.get("say") or data.get("line") or "").strip()[:240]
                    emotion = _normalize_emotion(data.get("emotion"))
                if line:
                    allowed, gate_reason = _speech_gate(
                        session,
                        friend_id,
                        line,
                        cooldown_seconds=TOUCH_SPEECH_COOLDOWN_SECONDS,
                    )
                    if not allowed:
                        logger.debug(
                            "小世界触碰台词被反重复闸拦下：{}（{}）",
                            gate_reason,
                            line[:80],
                        )
                        line = ""
                        emotion = ""
                space_memory = str(data.get("space_memory") or "").strip()[:500]
                action = _normalize_action(data.get("space_do") or data.get("action"))
                source = "llm"
        except Exception as exc:  # noqa: BLE001
            logger.warning("小世界触碰反应失败（只保留身体反馈）：{}", exc)

    action, runtime_state = _attach_dialogue_action(
        session,
        friend_id,
        action,
        thought=thought,
        line=line or None,
        emotion=emotion,
        space_memory=space_memory,
        store_proactive=True,
    )
    return {
        "source": source,
        "thought": thought,
        "emotion": emotion,
        "space_memory": space_memory or None,
        "line": line or None,
        "action": action,
        "awaiting_reply": (runtime_state.get("intent") or {}).get("kind")
        == "awaiting_reply",
        "runtime": runtime_state,
        "discovery": _discovery_dict(
            session, session.get(FriendSpaceRuntime, friend_id)
        ),
    }


def _attach_dialogue_action(
    session: Session,
    friend_id: str,
    action: dict | None,
    *,
    thought: str = "",
    goal: str = "",
    plan: list[str] | None = None,
    line: str | None = None,
    emotion: str = "",
    space_memory: str = "",
    store_proactive: bool = False,
) -> tuple[dict | None, dict]:
    """把世界对话里产生的 space_do 接上 action id / 计划 / 结果回流。"""
    now = _now()
    runtime = get_or_create_runtime(session, friend_id)
    existing_goal = str(runtime.goal or "").strip()
    existing_plan = _json_list(runtime.plan)
    with _SPACE_WRITE_LOCK:
        session.rollback()
        runtime = get_or_create_runtime(session, friend_id)
        if line and _looks_like_question(line):
            # 问句一律先停下身体、保持表情和动作等回应；
            # 是否恢复自主活动由 awaiting_reply 的超时/回答处理。
            action = {
                "kind": "wait",
                "target": None,
                "duration_seconds": max(2.0, min(30.0, AWAITING_REPLY_SECONDS)),
                "text": "停下来等 TA 回答",
            }
        if _parse(runtime.intent).get("kind") == "awaiting_reply":
            runtime.intent = json.dumps(
                {"kind": "daily_life", "since": now},
                ensure_ascii=False,
            )
        if goal:
            runtime.goal = goal[:200]
            if goal != existing_goal:
                runtime.plan_step = 0
        if plan:
            runtime.plan = json.dumps(_clean_plan(plan), ensure_ascii=False)
            if plan != existing_plan:
                runtime.plan_step = 0
        if action is not None:
            action = {**action, "id": _new_id()}
            runtime.last_action_result = json.dumps(
                {
                    "action_id": action["id"],
                    "status": "issued",
                    "ok": None,
                    "detail": "",
                    "goal": runtime.goal,
                    "reason": thought,
                    "failure_streak": int(
                        _parse(runtime.last_action_result).get("failure_streak") or 0
                    ),
                    "at": now,
                },
                ensure_ascii=False,
            )
            runtime.attention = "notice"
        if line:
            # 直接对话也要留痕；否则下一轮只看到用户和她的自主独白，
            # 看不到她刚才实际回过什么。proactive 只标记是不是她主动开口。
            _store_space_say(
                session,
                friend_id,
                speaker="assistant",
                text=line,
                emotion=emotion,
                proactive=store_proactive,
            )
            runtime.intent = json.dumps(
                {
                    "kind": "considering",
                    "line": line,
                    "emotion": emotion,
                    "thought": thought,
                    "since": now,
                },
                ensure_ascii=False,
            )
            runtime.attention = "notice"
            if _looks_like_question(line):
                _mark_awaiting_reply(
                    session,
                    friend_id,
                    line=line,
                    emotion=emotion,
                )
        _save_space_memory(session, friend_id, space_memory)
        runtime.updated_at = now
        session.flush()
        state = runtime_dict(runtime)
        session.commit()
    return action, state


async def think(
    session: Session,
    friend_id: str,
    *,
    text: str | None = None,
    allow_llm: bool = True,
    viewer: dict | None = None,
) -> dict:
    """小世界的一次"想"。

    - 给了 text：TA 在小世界里对她说的话 → micro-LLM 短回应；
    - 没给 text：自由模式 → 本地规则优先，偶尔让 micro-LLM 选下一步。
    失败一律回退本地规则，绝不因为模型不可用让她僵住。
    """
    friend = _require_friend(session, friend_id)
    clean = (text or "").strip()
    if clean:
        # D416 修复：小世界对话必须保存“用户那一半”，否则下一轮只看到
        # 她自己的独白，模型会自然地越说越像自言自语。
        _store_space_say(session, friend_id, speaker="user", text=clean)
    presence = _presence_brief(viewer)
    world_model = space_world_service.build_world_snapshot(
        session,
        friend_id,
        viewer=viewer,
    )
    world_prompt = space_world_service.to_prompt(world_model)
    toolset_prompt = space_tools.toolset_prompt(world_model)
    entity_keys = {
        str(item.get("key") or "")
        for item in world_model.get("entities") or []
        if item.get("key")
    }
    character_brief = _character_brief(
        session,
        friend,
        query=" ".join(
            [
                clean,
                *[
                    str(item.get("content") or "")
                    for item in (world_model.get("recent_chat") or [])[-4:]
                ],
            ]
        ),
    )
    explained = handle_ask_explanation(session, friend_id, clean) if clean else None
    knowledge = _knowledge_brief(session, friend_id)
    discovery = _discovery_dict(session, runtime=get_or_create_runtime(session, friend_id))
    emotion = ""
    space_memory = ""
    recent_say_prompt = _recent_say_prompt(session, friend_id)
    if clean:
        needs_action = space_tools.looks_like_action_request(clean)
        fallback_action = (
            _approach_action(explained["target"], f"再看看{explained['label']}")
            if explained and needs_action
            else (
                local_action(session, friend_id)
                if needs_action
                else {
                    "kind": "wait",
                    "target": None,
                    "duration_seconds": 2,
                    "text": "留在原地听 TA 说话",
                }
            )
        )
        line = ""
        if allow_llm:
            voice = _voice_examples(friend)
            voice_note = (
                "\n你平时说话的样本（只学语气和句长，不要照抄）：\n- "
                + "\n- ".join(voice)
                if voice
                else ""
            )
            explained_note = (
                f"\nTA 刚刚向你解释了「{explained['label']}」：{clean}。"
                "你已经记住，不要装作没听见。"
                if explained
                else ""
            )
            if needs_action:
                system = (
                    f"你是「{friend.name}」。{friend.personality or ''}\n"
                    f"{friend.custom_instructions or ''}{voice_note}\n"
                    "你正在自己的房间里，和 TA 待在一起。小世界里的你和聊天里的你是同一个人，"
                    "不是另一个 NPC。你要带着你们共同的记忆、情绪和最近聊过的事回应，"
                    "不要像第一次见面。这不是普通聊天：你有自己的小世界工具，"
                    "TA 让你动手时必须用 space_do，不能只口头答应。"
                    "用你和 TA 最近对话使用的语言。不要把动作过程说成台词；"
                    "动作直接放进 space_do，space_say 只写你真想对 TA 说的话。"
                    "TA 是在跟你对话，不是在给你下一条工单：先接住他具体说的东西，"
                    "再给出你自己的判断、联想、偏好或追问。通常 2~4 句自然话；"
                    "只有纯动作或极短回应才一句。"
                )
                user = (
                    f"{character_brief}\n\n"
                    f"{world_prompt}\n\n"
                    f"{toolset_prompt}\n\n"
                    f"{recent_say_prompt}\n\n"
                    f"{presence}\n\n"
                    f"{knowledge}\n"
                    f"{explained_note}\n"
                    f"TA 对你说：{clean}\n\n"
                    "只输出 JSON："
                    '{"thought":"用你自己的话写此刻的具体判断，不要照抄字段说明",'
                    '"space_plan":{"goal":"具体目标；没有就空","steps":["具体步骤"]} 或 null,'
                    '"space_say":{"text":"2~4 句自然对话，接住他的话并给出你自己的话；不要用模板，不要只做单次反应"},'
                    '"space_do":{"kind":"go_to|sit|lie|use|arrange|idle|wait|set_state|pick_up|put_down|push",'
                    '"target":"必须用工具箱里的真实 key","duration_seconds":8} 或 null,'
                    '"space_memory":"可选：只有这次对话真的值得长期记住时，写一条第一人称记忆；普通闲聊留空"}'
                    "\n字段值是具体内容，禁止把说明文字原样写回；用最近对话的语言。"
                )
            else:
                system = (
                    f"你是「{friend.name}」。{friend.personality or ''}\n"
                    f"{friend.custom_instructions or ''}{voice_note}\n"
                    "你现在和 TA 在同一个房间里，正在对话，不是在写演讲稿。"
                    "小世界里的你和普通聊天里的你是同一个人：你带着共同记忆、当前情绪、"
                    "最近的聊天和你们一起在做的事。"
                    "先接住 TA 具体说的内容；如果它接上了你们之前聊过的事或共同目标，"
                    "要自然接上，不要重新开场，也不要复述上一轮。"
                    "提到记忆和最近聊天时只接你确实看到的具体内容；不确定就直说，不要编。"
                    "说自己的判断、联想、偏好或追问，通常 2~4 句；"
                    "不要用模板、客服腔、清单腔或动作旁白。"
                    "如果这一轮没有新的真实反应，可以短，但不要只回“嗯/是/我在”。"
                )
                user = (
                    f"{character_brief}\n\n"
                    f"{world_prompt}\n\n"
                    f"{recent_say_prompt}\n\n"
                    f"{presence}\n\n"
                    f"{knowledge}\n"
                    f"{explained_note}\n"
                    f"TA 对你说：{clean}\n\n"
                    "只输出 JSON："
                    '{"thought":"用你自己的话写你此刻真正接住了什么、怎么想；不要照抄字段说明",'
                    '"space_say":{"text":"2~4 句自然对话，接住他的话并给出你自己的话"},'
                    '"space_memory":"可选：只有这次对话真的值得长期记住时，写一条第一人称记忆；普通闲聊留空"}'
                    "\n字段值是具体内容，禁止把说明文字原样写回；用最近对话的语言。"
                )
            try:
                data = await _llm_json(
                    session, friend, system, user, purpose="chat", max_tokens=260
                )
                if data:
                    raw_action: dict | None = None
                    space_plan = (
                        data.get("space_plan")
                        if isinstance(data.get("space_plan"), dict)
                        else {}
                    )
                    if needs_action:
                        raw_action = _normalize_action(
                            data.get("space_do") or data.get("action")
                        )
                        if (
                            raw_action is not None
                            and raw_action.get("kind")
                            in {"use", "arrange", "set_state", "pick_up", "put_down", "push"}
                            and raw_action.get("target") not in entity_keys
                            and raw_action.get("target") != "outfit"
                        ):
                            raw_action = None
                        if raw_action is None:
                            repair_user = (
                                f"{user}\n\n"
                                "[工具纠正] TA 刚才是在让你动小世界里的东西，不是在纯聊天。"
                                "你上一轮没有给 space_do。重新输出 JSON，必须根据工具箱里的"
                                "真实 key 给一个 space_do；如果确实做不到，在 space_say 里"
                                "说明物理原因，但仍要给出你尝试的动作。"
                            )
                            repaired = await _llm_json(
                                session,
                                friend,
                                system,
                                repair_user,
                                purpose="chat",
                                max_tokens=300,
                            )
                            if repaired:
                                data = repaired
                                space_plan = (
                                    data.get("space_plan")
                                    if isinstance(data.get("space_plan"), dict)
                                    else {}
                                )
                                raw_action = _normalize_action(
                                    data.get("space_do") or data.get("action")
                                )
                                if (
                                    raw_action is not None
                                    and raw_action.get("kind")
                                    in {
                                        "use",
                                        "arrange",
                                        "set_state",
                                        "pick_up",
                                        "put_down",
                                        "push",
                                    }
                                    and raw_action.get("target") not in entity_keys
                                    and raw_action.get("target") != "outfit"
                                ):
                                    raw_action = None
                    action = (raw_action or fallback_action) if needs_action else None
                    space_say = data.get("space_say")
                    if isinstance(space_say, dict):
                        line = (
                            str(space_say.get("text") or "").strip()[:400] or line
                        )
                        emotion = _normalize_emotion(space_say.get("emotion"))
                        space_memory = str(data.get("space_memory") or "").strip()[:500]
                    else:
                        line = (
                            str(data.get("say") or data.get("line") or "").strip()[:400]
                            or line
                        )
                        emotion = _normalize_emotion(data.get("emotion"))
                        space_memory = str(data.get("space_memory") or "").strip()[:500]
                    thought = str(data.get("thought") or "").strip()[:240]
                    goal = str(
                        space_plan.get("goal") or data.get("goal") or ""
                    ).strip()[:200]
                    plan = _clean_plan(
                        space_plan.get("steps")
                        or space_plan.get("plan")
                        or data.get("plan")
                    )
                    if needs_action:
                        action = _coerce_posture_action(
                            action, world_model, line, thought, goal, clean
                        )
                    else:
                        action = {
                            "kind": "wait",
                            "target": None,
                            "duration_seconds": 2,
                            "text": "留在原地听 TA 说话",
                        }
                    action, runtime_state = _attach_dialogue_action(
                        session,
                        friend_id,
                        action,
                        thought=thought or line,
                        goal=goal,
                        plan=plan,
                        line=line or None,
                        emotion=emotion,
                        space_memory=space_memory,
                    )
                    return {
                        "source": "llm",
                        "thought": thought,
                        "emotion": emotion,
                        "space_memory": space_memory or None,
                        "line": line or None,
                        "action": action,
                        "discovery": discovery,
                        "runtime": runtime_state,
                        **space_context(session, friend_id),
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning("小世界 micro-LLM 回应失败，回退本地：{}", exc)
        fallback_action, runtime_state = _attach_dialogue_action(
            session, friend_id, fallback_action
        )
        return {
            "source": "local",
            "emotion": "",
            "space_memory": None,
            "line": line or None,
            "action": fallback_action,
            "discovery": discovery,
            "runtime": runtime_state,
            **space_context(session, friend_id),
        }

    action = local_action(session, friend_id)
    source = "local"
    # 不设次数上限；只是"偶尔让模型挑一次"，避免每拍都调用。
    if allow_llm and random.random() < 0.28:
        system = (
            f"你是「{friend.name}」。{friend.personality or ''}\n"
            "你在自己的房间里，没人指挥你。你要决定接下来做什么。"
            "房间里的东西默认都属于你，或者属于你和 TA 两个人；不需要先问许可，"
            "只有物理条件会让你失败。只能从固定动作里选。"
        )
        user = (
            f"现在：{datetime.now().astimezone().strftime('%H:%M')}\n"
            f"{character_brief}\n\n"
            f"{world_prompt}\n\n"
            f"{toolset_prompt}\n\n"
            f"{presence}\n\n"
            "只输出 JSON："
            '{"space_do":{"kind":"go_to|sit|lie|use|arrange|idle|wait|set_state|pick_up|put_down|push",'
            '"target":"必须用工具箱里的真实 key","duration_seconds":8}}'
        )
        try:
            data = await _llm_json(session, friend, system, user, purpose="chat")
            if data:
                normalized = _normalize_action(
                    data.get("space_do") or data.get("action")
                )
                if (
                    normalized is not None
                    and normalized.get("kind")
                    in {"use", "arrange", "set_state", "pick_up", "put_down", "push"}
                    and normalized.get("target") not in entity_keys
                    and normalized.get("target") != "outfit"
                ):
                    normalized = None
                if normalized:
                    action = normalized
                    source = "llm"
        except Exception as exc:  # noqa: BLE001
            logger.warning("小世界自主思考失败，回退本地规则：{}", exc)
    action, runtime_state = _attach_dialogue_action(session, friend_id, action)
    return {
        "source": source,
        "line": None,
        "action": action,
        "discovery": discovery,
        "runtime": runtime_state,
        **space_context(session, friend_id),
    }


async def stream_chat(
    session: Session,
    friend_id: str,
    *,
    text: str,
    viewer: dict | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """小世界房间对话的 token 级流式入口。

    普通聊天走轻量标签协议，边生成边给前端；明确动作请求则复用
    `think()` 的完整工具规划器，最后一次性回放文本/动作结果。
    """
    clean = str(text or "").strip()
    if not clean:
        yield {"type": "error", "message": "没有收到要对她说的话"}
        return
    friend = _require_friend(session, friend_id)
    if space_tools.looks_like_action_request(clean):
        result = await think(session, friend_id, text=clean, viewer=viewer)
        if result.get("thought"):
            yield {"type": "thought", "delta": result["thought"]}
        if result.get("line"):
            yield {"type": "speak", "delta": result["line"]}
        yield {
            "type": "done",
            "line": result.get("line"),
            "thought": result.get("thought") or "",
            "emotion": result.get("emotion") or "",
            "action": result.get("action"),
            "runtime": result.get("runtime"),
            "knowledge": result.get("knowledge"),
            "discovery": result.get("discovery"),
        }
        return

    _store_space_say(session, friend_id, speaker="user", text=clean)
    world_model = space_world_service.build_world_snapshot(
        session,
        friend_id,
        viewer=viewer,
    )
    world_prompt = space_world_service.to_prompt(world_model)
    presence = _presence_brief(viewer)
    knowledge = _knowledge_brief(session, friend_id)
    recent_say_prompt = _recent_say_prompt(session, friend_id)
    character_brief = _character_brief(
        session,
        friend,
        query=" ".join(
            [
                clean,
                *[
                    str(item.get("content") or "")
                    for item in (world_model.get("recent_chat") or [])[-4:]
                ],
            ]
        ),
    )
    voice = _voice_examples(friend)
    voice_note = (
        "\n你平时说话的样本（只学语气和句长，不要照抄）：\n- "
        + "\n- ".join(voice)
        if voice
        else ""
    )
    system = (
        f"你是「{friend.name}」。{friend.personality or ''}\n"
        f"{friend.custom_instructions or ''}{voice_note}\n"
        "你现在和 TA 在同一个房间里说话。先接住 TA 具体说的内容；"
        "如果它接上了你们最近聊天、共同记忆或一起在做的事，就自然接上，"
        "不要重新开场，不要复述上一轮。"
        "说自己的判断、联想、偏好或追问，通常 2~4 句自然话。"
        "只输出这三段，不要 Markdown、不要 JSON、不要解释格式：\n"
        "不要写 `**情绪**：`、`## 情绪`、`[立绘:情绪]` 这类标签；"
        "情绪只能写进 <emotion>。\n"
        "<thought>一句你此刻真实接住了什么、怎么想；不要写给别人看</thought>\n"
        "<emotion>happy|excited|shy|curious|surprised|sad|angry|tired|calm</emotion>\n"
        "<speak>2~4 句你真想对 TA 说的话</speak>\n"
        "<memory>可选：只有真的值得长期记住才写一句，否则留空</memory>"
    )
    user = (
        f"{character_brief}\n\n"
        f"{world_prompt}\n\n"
        f"{recent_say_prompt}\n\n"
        f"{presence}\n\n"
        f"{knowledge}\n"
        f"TA 对你说：{clean}\n\n"
        "现在直接按标签输出；thought / speak 之间不要写别的内容。"
    )
    parser = SpaceDialogueStreamParser()
    mood_parser = MoodTagParser()
    speak_parts: list[str] = []
    provider = _provider_for_space(session, friend, purpose="chat")
    try:
        async for event in provider.chat_stream(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=float(
                friend.temperature if friend.temperature is not None else 0.8
            ),
            max_tokens=500,
        ):
            if event.get("type") != "content":
                continue
            for kind, delta in parser.feed(str(event.get("delta") or "")):
                if not delta:
                    continue
                if kind == "speak":
                    visible = mood_parser.feed(delta)
                    if visible:
                        speak_parts.append(visible)
                        yield {"type": "speak", "delta": visible}
                else:
                    yield {"type": kind, "delta": delta}
        for kind, delta in parser.finish():
            if not delta:
                continue
            if kind == "speak":
                visible = mood_parser.feed(delta)
                if visible:
                    speak_parts.append(visible)
                    yield {"type": "speak", "delta": visible}
            else:
                yield {"type": kind, "delta": delta}
        tail = mood_parser.flush()
        if tail:
            speak_parts.append(tail)
            yield {"type": "speak", "delta": tail}
        # 模型偶尔不按 XML 标签，而写成 `**疑惑**：` / `## 疑惑`。
        # 流式阶段就已经由 MoodTagParser 摘掉，这里只把干净台词写回解析结果。
        parser.fields["speak"] = "".join(speak_parts).strip()
        if mood_parser.moods and not parser.fields["emotion"].strip():
            parser.fields["emotion"] = mood_parser.moods[-1]
        if not parser.fields["speak"] and parser.raw.strip().startswith("{"):
            # 标签格式失手时兼容 JSON；不把半截 JSON 当台词显示。
            fallback = _parse_json_object(parser.raw)
            if fallback:
                say = (
                    fallback.get("space_say")
                    if isinstance(fallback.get("space_say"), dict)
                    else {}
                )
                parser.fields["speak"] = str(
                    say.get("text") or fallback.get("line") or ""
                ).strip()
                parser.fields["thought"] = str(
                    fallback.get("thought") or ""
                ).strip()
                parser.fields["emotion"] = _normalize_emotion(
                    say.get("emotion") or fallback.get("emotion")
                )
                parser.fields["memory"] = str(
                    fallback.get("space_memory") or ""
                ).strip()
                if parser.fields["speak"]:
                    yield {"type": "speak", "delta": parser.fields["speak"]}
        line = parser.fields["speak"].strip()[:400]
        thought = parser.fields["thought"].strip()[:240]
        emotion = _normalize_emotion(parser.fields["emotion"])
        space_memory = parser.fields["memory"].strip()[:500]
        if not line:
            yield {
                "type": "error",
                "message": "她这次没有组织出可播放的台词",
            }
            return
        action, runtime_state = _attach_dialogue_action(
            session,
            friend_id,
            {
                "kind": "wait",
                "target": None,
                "duration_seconds": 2,
                "text": "留在原地听 TA 说话",
            },
            thought=thought or line,
            line=line,
            emotion=emotion,
            space_memory=space_memory,
        )
        yield {
            "type": "done",
            "line": line,
            "thought": thought,
            "emotion": emotion,
            "action": action,
            "runtime": runtime_state,
            "knowledge": list_knowledge(session, friend_id),
            "discovery": _discovery_dict(
                session, get_or_create_runtime(session, friend_id)
            ),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("小世界流式对话失败：{}", exc)
        yield {"type": "error", "message": str(exc) or "流式对话失败"}
