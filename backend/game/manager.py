"""游戏会话管理器（P3）：把适配器会话接到数据库、记忆、prompt 和 proactively。

职责（都只属于"主代码"）：
· 起 / 停一局，持久化 session / task / event；
· 把事件喂给 prompt（`# 你在游戏里`）和"最近一起玩过"；
· 一局结束 → 用**事件日志**提炼摘要 + 记忆（D236：不许编）；
· 值得说一句的事件 → 让她主动冒个泡（限流）。

适配器仍然只执行、只上报；这里不碰任何具体游戏。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable, Mapping

from loguru import logger

from backend.agent.context import build_messages
from backend.agent.parser import MoodTagParser, strip_mood_marks
from backend.db import session_scope
from backend.game import (
    blueprints,
    cheats,
    fast_brain,
    knowledge,
    preflight,
    postconditions,
    reflexes,
    skills,
)
from backend.game.contract import GameEvent
from backend.game.plans import PlanStore
from backend.game.service import GameService
from backend.game.summary import format_events, summarize_events
from backend.llm.errors import LLMConfigError
from backend.llm.factory import (
    create_provider,
    create_provider_for_llm_base,
    create_provider_for_purpose,
)
from backend.models import Friend
from backend.services import (
    blueprint_store,
    game_store,
    memory_service,
    minecraft_version_service,
    message_service,
    proactive_service,
    schedule_service,
    settings_service,
    skill_store,
)
from backend.services.actor import Actor

Summarizer = Callable[[Any, str], Awaitable[tuple[str, list[dict[str, Any]]]]]

TALK_HINT = (
    "主动说一句：看看 TA 在干嘛 / 提一句你注意到的事 / 问一句要不要一起做点什么"
)
AUTONOMY_BUFFER_SECONDS = 6.0
PATROL_SECONDS = 6.0
WATCHDOG_INTERVAL = 0.5
CHAT_GRACE_SECONDS = 20.0
GAME_MODES = ("follow", "chat", "free")
DEFAULT_GAME_MODE = "follow"
MODE_SWITCH_COOLDOWN = 300.0
MODE_STOP_PAUSE_SECONDS = 15.0
FAST_BRAIN_FAILURE_COOLDOWN = 60.0
MAX_SAME_FAILURES = 3
PLAN_REVIEW_RETRY_SECONDS = 8.0
PLAN_REVIEW_MAX_ATTEMPTS = 3
AUTONOMY_BACKOFF_BASE_SECONDS = 6.0
AUTONOMY_BACKOFF_MAX_SECONDS = 60.0
INCIDENT_LLM_COOLDOWN = 5.0
INCIDENT_MAX_ATTEMPTS = 3
INCIDENT_ACTIONS = {
    "goto",
    "escape",
    "pillar",
    "place",
    "boat",
    "eat",
    "equip",
    "use_item",
    "attack",
}
MODE_LABELS = {
    "follow": "跟随（跟着 TA）",
    "chat": "聊天（待在 TA 附近）",
    "free": "自由（自己找事做）",
}
STALL_ANSWER_WORDS = ("查查", "看看", "稍等", "等一下", "等等", "待会", "一会儿")
# 蓝图原位修复时，只有“普通地形 / 建筑材料”允许清掉；容器和玩家功能方块不动，
# 避免为了补一格把箱子、床、门、告示牌之类的东西拆了。
_BLUEPRINT_PROTECTED_BLOCK = re.compile(
    r"(chest|barrel|shulker|furnace|smoker|hopper|dispenser|dropper|"
    r"brewing_stand|beacon|spawner|sign|banner|bed|door|trapdoor|button|"
    r"lever|pressure_plate|lectern|enchanting_table|anvil|"
    r"respawn_anchor|end_portal|command_block|barrier|bedrock)"
)


def _looks_like_stall(text: str) -> bool:
    """她只说了"我去查查/等等"这种空头承诺 → 需要兜底把结果说出来（D286）。"""
    cleaned = re.sub(r"[\s，。！？!?~…、,.：:；;\"'“”‘’（）()\[\]]", "", text or "")
    if not cleaned:
        return True
    if len(cleaned) > 24:
        return False
    if len(cleaned) <= 12 and any(word in cleaned for word in STALL_ANSWER_WORDS):
        return True
    return any(cleaned.endswith(word) for word in STALL_ANSWER_WORDS)


_WORLD_ACTION_REQUEST_RE = re.compile(
    r"(建造|建一|建个|建座|建房|搭|盖|挖|砍|采集|采(?:矿|树|石头)|"
    r"做个|制作|合成|放置|放下|放个|放一|拿来|拿出|带到|带上|做成|"
    r"修(?:一下|好|建|墙|门)|拆|填|清理|清掉|走过去|走到|跑过去|过来|"
    r"钓鱼|射箭|骑|种树|种地|种菜|种下|收获|搬|整理|攻击|打怪|杀|吃|喝|睡)"
)

_STOP_REQUEST_RE = re.compile(
    r"(停下|停手|停一下|停止|别动|别做|不要做|先不做|先不建|不建了|别建了|"
    r"别挖了|不挖了|回来|别搞了|休息一下)"
)

_RESUME_REQUEST_RE = re.compile(r"(继续|接着|恢复|别停|接着做|继续建|继续挖)")


def _looks_like_action_request(text: str) -> bool:
    """用户是不是在交办一个会改变世界/需要动手的事。"""
    return bool(_WORLD_ACTION_REQUEST_RE.search(str(text or "")))


def _looks_like_stop_request(text: str) -> bool:
    """用户是否明确叫停；用于确定性暂停/取消，不交给 LLM 猜。"""
    return bool(_STOP_REQUEST_RE.search(str(text or "")))


def _looks_like_resume_request(text: str) -> bool:
    return bool(_RESUME_REQUEST_RE.search(str(text or "")))


SELF_START_HINT = (
    "现在没人指挥你。先看看周围、背包、当前模式和最近事件，再决定这一小段要做什么。"
    "自由模式下可以自己定目标；跟随/聊天模式下可以主动提醒、提建议，或者明确继续跟着。"
    "如果确实没事，用 wait/rest 明确表示你在待着，不要无意义站着。"
    "需要动手就用工具；这一轮安静做事，只有真的想和 TA 说话才 game_say。"
)
CONTINUE_HINT = (
    "你刚完成一步：{task}。现在先看上面的状态，再对照你的计划："
    "① 如果计划还没定，或者该改了，先用 game_plan 写出目标 + 2~5 个具体步骤；"
    "② 有 active plan 后，用 game_do 执行**下一步具体动作**"
    "（不要重复刚才已经完成的事）；"
    "③ 如果计划真的完成了，才用 game_say 说一句收尾。"
    "这一轮**安静做事**，不要向 TA 汇报进度；只有真的想和 TA 说话时才用 game_say。"
    "不要站在原地只说话；也不要把同一个方块挖掉又放回去。"
    "如果刚才失败，先判断：错误里说「格式错误 / 需要 steps 数组 / 需要 block / 需要 x y z」"
    "→ 按正确格式重写一次再试；如果是「没有吃的 / 没找到 / 路被挡住 / 没有工具」"
    "→ 不要原样重试，换目标、换地方或先补齐工具/食物。"
    "如果一步里要做几件事，用 game_do(action=sequence) 一次排好，不要每个小动作都停。"
)
CONTINUE_RETRY_HINT = (
    "你刚才只说话，没有动手。现在先确认有 active plan；没有就用 game_plan 创建，"
    "然后用 game_do 推进计划（mine / craft / goto / place / sequence …）；"
    "如果计划已经完成或做不了，"
    "用 game_plan(done=true) 结束它，并说明原因。"
    "刚才失败过的调用不要原样重试：格式错误就按正确格式重写，"
    "世界状态错误就换动作/换目标。这一轮安静做事，不要汇报。"
)


def _provider_for(session, friend: Friend):  # noqa: ANN001, ANN202
    """跟 `scheduler/jobs._provider_for` 同一口径（D151）。"""
    name = (getattr(friend, "llm_base", None) or "").strip()
    if name:
        row = settings_service.get(session)
        return create_provider_for_llm_base(row.llm_bases, name)
    bases, active = settings_service.llm_config(session)
    return create_provider(bases, active)


def _short(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _compact_value(value: Any, depth: int = 0) -> Any:
    """给状态/事件做体积压缩；工具结果再大也不能整段进模型上下文。"""
    if depth >= 3:
        text = str(value)
        return text[:300] + ("…" if len(text) > 300 else "")
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in ("positions", "blocks") and isinstance(item, (list, tuple)):
                out[key] = {
                    "count": len(item),
                    "sample": [_compact_value(x, depth + 1) for x in item[:3]],
                }
            elif key == "columns" and isinstance(item, (list, tuple)):
                sample = []
                for column in item[:3]:
                    if isinstance(column, Mapping):
                        blocks = column.get("blocks")
                        sample.append(
                            {
                                **{
                                    k: _compact_value(v, depth + 1)
                                    for k, v in column.items()
                                    if k != "blocks"
                                },
                                **(
                                    {
                                        "blocks": {
                                            "count": len(blocks),
                                            "sample": [
                                                _compact_value(x, depth + 1)
                                                for x in blocks[:3]
                                            ],
                                        }
                                    }
                                    if isinstance(blocks, list)
                                    else {}
                                ),
                            }
                        )
                    else:
                        sample.append(_compact_value(column, depth + 1))
                out[key] = {"count": len(item), "sample": sample}
            elif key == "steps" and isinstance(item, (list, tuple)):
                out[key] = [
                    {
                        "action": step.get("action"),
                        "ok": step.get("ok"),
                        "error": _short(step.get("error"), 160),
                    }
                    if isinstance(step, Mapping)
                    else _short(step, 160)
                    for step in item[:12]
                ]
                if len(item) > 12:
                    out[key].append({"more": len(item) - 12})
            elif isinstance(item, (list, tuple)) and len(item) > 20:
                out[key] = {
                    "count": len(item),
                    "sample": [_compact_value(x, depth + 1) for x in item[:3]],
                }
            elif isinstance(item, str) and len(item) > 500:
                out[key] = item[:500] + "…"
            else:
                out[key] = _compact_value(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)) and len(value) > 20:
        return {
            "count": len(value),
            "sample": [_compact_value(x, depth + 1) for x in value[:3]],
        }
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


def _compact_task_view(task: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action": task.get("action"),
        "status": task.get("status"),
        "origin": task.get("origin"),
        "progress": _compact_value(task.get("progress")),
        "result": _compact_value(task.get("result")),
        "created_at": task.get("created_at"),
    }


def _clean_game_text(text: Any) -> str:
    """游戏里说的话：去掉模型学来的 `[游戏]` 前缀和情绪标签。"""
    cleaned = str(text or "")
    cleaned = re.sub(r"\[游戏\]", "", cleaned)
    cleaned = strip_mood_marks(cleaned)
    return cleaned.strip()


def _inventory_summary(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """把背包槽位按物品名合并；不能只给前 N 个槽位，否则重复堆叠会把后面的东西挤掉。"""
    counts: dict[str, int] = {}
    foods: set[str] = set()
    for entry in snapshot.get("inventory") or []:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("item") or "").strip()
        if not name:
            continue
        try:
            count = int(entry.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        counts[name] = counts.get(name, 0) + count
        if entry.get("food"):
            foods.add(name)
    return [
        {"item": name, "count": count, "food": name in foods}
        for name, count in sorted(counts.items())
    ]


def _inventory_lines(snapshot: Mapping[str, Any]) -> list[str]:
    """完整但可读的背包事实；详细槽位由 game_inventory 查。"""
    items = _inventory_summary(snapshot)
    lines: list[str] = []
    if items:
        lines.append(
            "背包（全部）："
            + "、".join(f"{item['item']}×{item['count']}" for item in items)
        )
    else:
        lines.append("背包（全部）：空")
    foods = [item for item in items if item.get("food")]
    if foods:
        lines.append(
            "可吃的："
            + "、".join(f"{item['item']}×{item['count']}" for item in foods)
        )
    empty = snapshot.get("inventory_empty_slots")
    if isinstance(empty, (int, float)):
        lines.append(
            f"背包空位：{int(empty)}"
            + ("（满了，捡不了新东西）" if int(empty) <= 0 else "")
        )
    tools = [
        item
        for item in (snapshot.get("inventory_tools") or [])
        if isinstance(item, Mapping)
    ]
    if tools:
        rendered = []
        for item in tools:
            name = str(item.get("name") or "工具")
            durability = item.get("durability")
            maximum = item.get("max_durability")
            if isinstance(durability, (int, float)) and isinstance(maximum, (int, float)):
                rendered.append(f"{name}（耐久 {int(durability)}/{int(maximum)}）")
            else:
                rendered.append(name)
        lines.append("工具：" + "、".join(rendered))
    ammo = [
        item
        for item in (snapshot.get("inventory_ammo") or [])
        if isinstance(item, Mapping)
    ]
    if ammo:
        lines.append(
            "弹药："
            + "、".join(
                f"{item.get('name')}×{item.get('count')}" for item in ammo
            )
        )
    boats = [
        item
        for item in (snapshot.get("inventory_boats") or [])
        if isinstance(item, Mapping)
    ]
    if boats:
        lines.append(
            "船：" + "、".join(f"{item.get('name')}×{item.get('count')}" for item in boats)
        )
    equipment = snapshot.get("equipment")
    if not isinstance(equipment, Mapping):
        held = snapshot.get("held")
        equipment = {"held": held} if isinstance(held, Mapping) else {}
    if isinstance(equipment, Mapping):
        held = equipment.get("held")
        offhand = equipment.get("offhand")
        armor = [
            item for item in (equipment.get("armor") or []) if isinstance(item, Mapping)
        ]
        bits = []
        if isinstance(held, Mapping):
            bits.append(f"手持 {held.get('name')}×{held.get('count')}")
        if isinstance(offhand, Mapping):
            bits.append(f"副手 {offhand.get('name')}×{offhand.get('count')}")
        if armor:
            bits.append("护甲 " + "、".join(str(item.get("name")) for item in armor))
        if bits:
            lines.append("装备：" + "；".join(bits))
    guidance = snapshot.get("tool_guidance")
    if isinstance(guidance, Mapping):
        labels = (
            ("pickaxe", "镐"),
            ("axe", "斧"),
            ("shovel", "锹"),
            ("shears", "剪"),
        )
        shown = [
            f"{label}={guidance.get(key) or '无'}"
            for key, label in labels
            if key in guidance
        ]
        if shown:
            lines.append("当前工具：" + "、".join(shown))
        lines.append("工具用途：镐挖石/矿，斧砍木，锹挖土/沙，剪树叶/羊毛")
    return lines


def _world_lines(snapshot: Mapping[str, Any]) -> list[str]:
    """把适配器采好的紧凑地图和地标渲染成模型能读的文字图。"""
    world = snapshot.get("world")
    if not isinstance(world, Mapping):
        return []
    lines: list[str] = []
    for key, label in (("near", "近景"), ("far", "远景")):
        view = world.get(key)
        if not isinstance(view, Mapping):
            continue
        rows = [str(row) for row in (view.get("rows") or []) if isinstance(row, str)]
        if not rows:
            continue
        origin = view.get("origin") or {}
        lines.append(
            f"{label}地图（北在上；原点 x={origin.get('x')} z={origin.get('z')}，"
            f"每格 {view.get('step')} 格）："
        )
        lines.extend(rows)
        lines.append(
            f"{label}高度：{view.get('height_min')} ~ {view.get('height_max')}"
            f"；水面格 {view.get('water_cells', 0)}"
            f"；树格 {view.get('tree_cells', 0)}"
            f"；石格 {view.get('stone_cells', 0)}"
        )
    lines.append(
        "图例：@ 她｜~ 水｜! 岩浆｜T 树/森林｜# 石/矿｜. 可走平地｜"
        "^ 高于她｜v 低于她｜? 未加载/未知"
    )
    for key, label in (
        ("nearest_water", "最近水面"),
        ("nearest_lava", "最近岩浆"),
        ("nearest_tree", "最近树林"),
        ("nearest_stone", "最近石地"),
    ):
        near = None
        for view_key in ("near", "far"):
            view = world.get(view_key)
            if isinstance(view, Mapping) and isinstance(view.get(key), Mapping):
                near = view[key]
                break
        if not isinstance(near, Mapping):
            continue
        lines.append(
            f"{label}：{near.get('direction') or '未知'}约 {near.get('distance')} 格，"
            f"坐标 ({near.get('x')}, {near.get('y')}, {near.get('z')})"
        )
    return lines


def _local_grid_text(snapshot: Mapping[str, Any]) -> str:
    grid = [
        item
        for item in (snapshot.get("local_grid") or [])
        if isinstance(item, Mapping)
    ]
    if not grid:
        return ""
    parts = []
    for item in grid[:35]:
        ground = item.get("ground") or {}
        parts.append(
            f"({item.get('x')},{item.get('z')}) "
            f"{ground.get('name') or '空'}@{ground.get('y', '?')}/"
            f"脚:{item.get('feet') or '空'}/头:{item.get('head') or '空'}"
        )
    return "；".join(parts)


def _entity_fact_text(item: Mapping[str, Any]) -> str:
    """把适配器的实体事实压成一句人话。只写事实，不写应该做什么。"""
    name = str(item.get("name") or item.get("type") or "未知")
    distance = item.get("distance")
    direction = str(item.get("direction") or "").strip()
    vertical = str(item.get("vertical") or "").strip()
    bits = [name]
    if isinstance(distance, (int, float)):
        bits.append(f"{distance:.1f} 格")
    if direction:
        bits.append(direction)
    if vertical:
        bits.append(vertical)
    if item.get("hostile"):
        bits.append("敌对")
    return "，".join(bits)


def _perception_lines(
    snapshot: Mapping[str, Any], speaker: str | None = None
) -> list[str]:
    """给提示词顶部的高信号感知块：谁在附近、有没有威胁、自己是否处在危险里。"""
    sense = snapshot.get("entity_sense")
    if not isinstance(sense, Mapping):
        entities = [
            item
            for item in (snapshot.get("nearby_entities") or [])
            if isinstance(item, Mapping)
        ]
        sense = {
            "players": [i for i in entities if i.get("type") == "player"],
            "hostiles": [i for i in entities if i.get("hostile")],
            "passive_mobs": [
                i for i in entities if i.get("type") == "mob" and not i.get("hostile")
            ],
        }
    lines: list[str] = []
    players = [i for i in (sense.get("players") or []) if isinstance(i, Mapping)]
    hostiles = [i for i in (sense.get("hostiles") or []) if isinstance(i, Mapping)]
    mobs = [
        i for i in (sense.get("passive_mobs") or []) if isinstance(i, Mapping)
    ]
    if players:
        rendered = []
        for item in players[:6]:
            name = str(item.get("name") or "玩家")
            label = "TA" if speaker and name == speaker else name
            rendered.append(f"{label}（{_entity_fact_text(item)}）")
        lines.append("身边的玩家：" + "；".join(rendered))
    else:
        lines.append("身边的玩家：64 格内没有看到其他玩家")
    if hostiles:
        lines.append("附近的敌对生物：" + "；".join(
            _entity_fact_text(item) for item in hostiles[:6]
        ))
    else:
        lines.append("附近的敌对生物：暂时没有")
    if mobs:
        lines.append("附近的普通生物：" + "；".join(
            _entity_fact_text(item) for item in mobs[:5]
        ))
    hazards = snapshot.get("hazards")
    if isinstance(hazards, Mapping):
        danger: list[str] = []
        if hazards.get("in_lava"):
            danger.append("在岩浆里")
        if hazards.get("in_water"):
            danger.append(f"在水里（氧气 {hazards.get('oxygen', '?')}）")
        if hazards.get("on_fire"):
            danger.append("身上着火")
        health = hazards.get("health")
        if isinstance(health, (int, float)) and health <= 8:
            danger.append(f"血量偏低（{health:g}）")
        food = hazards.get("food")
        if isinstance(food, (int, float)) and food <= 6:
            danger.append(f"饥饿值偏低（{food:g}）")
        if danger:
            lines.append("身体状态：" + "；".join(danger))
    return lines


class GameManager:
    def __init__(
        self,
        *,
        summarizer: Summarizer | None = None,
        comment_throttle: float = 60.0,
        provider_factory: Callable[[Any, Friend], Any] | None = None,
        auto_continue: bool = True,
        continuation_delay: float = 0.2,
        autonomy_enabled: bool = True,
    ) -> None:
        self._service: GameService | None = None
        self.session_id: str | None = None
        self.friend_id: str | None = None
        self.game_kind: str | None = None
        self._recent_events: list[Any] = []
        self._summarizer = summarizer or summarize_events
        self._comment_throttle = comment_throttle
        self._last_comment = 0.0
        self._provider_factory = provider_factory or _provider_for
        self._chat_busy = False
        self._chat_lock = asyncio.Lock()
        self._pending_user_chat: list[dict[str, Any]] = []
        self._plan = ""
        self._plan_steps: list[str] = []
        self._last_task: dict[str, Any] | None = None
        self._auto_continue = auto_continue
        self._continuation_delay = max(0.0, float(continuation_delay))
        self._continuation_task: asyncio.Task[None] | None = None
        self._continuation_generation = 0
        self._continuation_retry = 0
        self._last_turn_at = time.monotonic()
        self._last_user_activity_at = time.monotonic()
        self._chat_until = 0.0
        self._patrol_supported = False
        self._placeholder_task_id: str | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._autonomy_enabled = autonomy_enabled
        self._autonomy_task: asyncio.Task[None] | None = None
        self._last_autonomy_at = 0.0
        self._next_idle_think_at = 0.0
        self._autonomy_no_progress = 0
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        # D276：常态模式是会话状态，不是任务。
        self._mode = DEFAULT_GAME_MODE
        self._mode_supported = True
        self._version_compat: dict[str, Any] | None = None
        self._mode_applied = False
        self._mode_paused_until = 0.0
        self._mode_switching = False
        self._last_mode_switch_at = 0.0
        self._last_mode_apply_at = 0.0
        self._mode_restore_task: asyncio.Task[None] | None = None
        # D278：用户任务 vs 她自己的任务（用于"任务完成要不要回报"）。
        self._turn_origin = "user"
        self._user_task_attempts: dict[str, int] = {}
        self._last_brain_decision: dict[str, Any] | None = None
        self._fast_provider_cache: Any = None
        self._fast_provider_signature: str | None = None
        self._fast_brain_disabled_until = 0.0
        self._failure_history: list[dict[str, Any]] = []
        self._last_turn_said = False
        self._plans = PlanStore()
        self._plan_task_links: dict[str, tuple[str, int]] = {}
        self._plan_failure_counts: dict[str, int] = {}
        # D321：plan 暂停后如果复盘没有真正改计划，由看门狗隔一会儿再给一次机会；
        # 连续几次仍无改动才降级恢复，避免 paused plan 永远卡死、人物站着不动。
        self._plan_review_attempts: dict[str, int] = {}
        self._plan_review_running: set[str] = set()
        self._plan_review_retry_at: dict[str, float] = {}
        self._plan_task_auto_repairs: dict[tuple[str, int], int] = {}
        self._plan_completion_audits: set[str] = set()
        self._plan_answer_pending: str | None = None
        self._plan_nudge_pending: str | None = None
        self._recent_completed_plans: list[dict[str, Any]] = []
        self._plan_stop_hold: str | None = None
        self._active_turn_task: asyncio.Task[Any] | None = None
        self._user_preempting = False
        self._active_user_message_text: str | None = None
        self._active_incident: dict[str, Any] | None = None
        self._incident_task: asyncio.Task[Any] | None = None
        self._incident_last_llm_at = 0.0
        self._incident_attempts = 0
        self._incident_fallback_used = False
        self._incident_response_active = False
        self._incident_plan_id: str | None = None
        self._incident_actions: list[dict[str, Any]] = []
        self._incident_lock = asyncio.Lock()
        # D323：最近在游戏里说话的人；用于把“谁在附近”标成 TA，而不是瞎猜。
        self._last_player_speaker: str | None = None
        # D327：事件唤醒思考；跟随模式不是认知休眠。
        self._initiative_cooldown_until = 0.0
        self._plan_resume_after_incident: str | None = None
        self._attention_dirty = False
        # D324：只有“当前这一轮就是 TA 交办的”才允许非自由模式建老式 plan；
        # 不能拿默认 turn_origin=user 冒充用户请求。
        self._plan_user_request_active = False
        # D290：当前局生效的作弊配置；None = 还没加载，回退到 settings 里“记住”的那份。
        self._cheat_config: dict[str, Any] | None = None
        self._reflex_config: dict[str, Any] | None = None
        # D288：skill/plan 等“下发后还要等结果”的调用，靠这两个结构消除事件先到竞态。
        self._completed_task_results: dict[str, dict[str, Any]] = {}
        self._task_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # 确定性技能 / 蓝图内部会下发多个 place/inspect 任务；这些任务的 task_done
        # 不能再触发"续跑/主动回合"，否则会和执行器抢同一局。
        self._internal_task_depth = 0
        self._blueprint_job: dict[str, Any] | None = None

    # ---------- 状态 ----------

    @property
    def active(self) -> bool:
        session = self._service.session if self._service else None
        return session is not None and session.state in ("starting", "running", "stopping")

    def active_for(self, friend_id: str) -> bool:
        return self.active and self.friend_id == friend_id

    def has_active_plan(self) -> bool:
        return bool(self._plans.active())

    def world_actions_blocked(self) -> bool:
        """TA 明确叫停后，世界动作要一直锁住，直到她明确继续或新建计划。"""
        return self._plan_stop_hold is not None

    def incident_response_active(self) -> bool:
        """紧急事故处理中的单次动作豁免；只用于自保，不用于普通计划。"""
        return self._incident_response_active

    def incident_action_allowed(self, action: str) -> bool:
        return self._incident_response_active and action in INCIDENT_ACTIONS

    # ---------- 作弊 / 难度（D290） ----------

    def _load_cheat_config(self) -> dict[str, Any]:
        if self._cheat_config is not None:
            return cheats.normalize_config(self._cheat_config)
        try:
            with session_scope() as db:
                self._cheat_config = settings_service.game_cheats_from(
                    settings_service.get(db)
                )
        except Exception as exc:  # noqa: BLE001 —— 设置读不到不能拖垮游戏
            logger.warning("读取作弊配置失败，按全关处理：{}", exc)
            self._cheat_config = cheats.default_config()
        return cheats.normalize_config(self._cheat_config)

    def get_cheat_config(self) -> dict[str, Any]:
        config = self._load_cheat_config()
        return {
            **config,
            "active": cheats.is_active(config),
            "enabled_abilities": cheats.enabled_abilities(config),
            "profiles": list(cheats.PROFILES),
            "abilities_supported": list(cheats.ABILITIES),
            "profile_labels": dict(cheats.PROFILE_LABELS),
        }

    def set_cheat_config(
        self, config: Mapping[str, Any], *, persist: bool = False
    ) -> dict[str, Any]:
        current = self._load_cheat_config()
        merged = {**current, **dict(config)}
        incoming_abilities = config.get("abilities")
        if isinstance(incoming_abilities, Mapping):
            merged["abilities"] = {
                **current["abilities"],
                **dict(incoming_abilities),
            }
        normalized = cheats.normalize_config(merged)
        self._cheat_config = normalized
        if persist:
            with session_scope() as db:
                settings_service.save_game_cheats(db, normalized)
        logger.info(
            "游戏作弊配置：profile={} master={} abilities={} persist={}",
            normalized["profile"],
            normalized["master_enabled"],
            cheats.enabled_abilities(normalized),
            persist,
        )
        return self.get_cheat_config()

    def cheats_active_for(self, friend_id: str) -> bool:
        if not self.active_for(friend_id):
            return False
        return cheats.is_active(self._load_cheat_config())

    # ---------- 反射 mode（D291） ----------

    def _load_reflex_config(self) -> dict[str, Any]:
        if self._reflex_config is not None:
            return reflexes.normalize_config(self._reflex_config)
        try:
            with session_scope() as db:
                self._reflex_config = settings_service.game_reflexes_from(
                    settings_service.get(db)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取反射配置失败，用默认值：{}", exc)
            self._reflex_config = reflexes.default_config()
        return reflexes.normalize_config(self._reflex_config)

    def get_reflex_config(self) -> dict[str, Any]:
        config = self._load_reflex_config()
        return {
            **config,
            "modes_supported": list(reflexes.REFLEX_MODES),
            "active_modes": [
                name for name, _values in reflexes.active_modes(config)
            ],
        }

    async def set_reflex_config(
        self, config: Mapping[str, Any], *, persist: bool = True
    ) -> dict[str, Any]:
        normalized = reflexes.normalize_config(config)
        self._reflex_config = normalized
        if persist:
            with session_scope() as db:
                settings_service.save_game_reflexes(db, normalized)
        service = self._service
        if service is not None and self.active:
            try:
                await service.command(
                    "configure_reflex", {"config": normalized}
                )
            except Exception as exc:  # noqa: BLE001 —— 实时下发失败下次开局仍会用
                logger.warning("反射配置实时下发失败：{}", exc)
        return self.get_reflex_config()

    async def knowledge_query(self, query: str, name: str) -> dict[str, Any]:
        if not self.active:
            raise RuntimeError("现在没有进行中的游戏会话")
        query = str(query or "").strip()
        name = str(name or "").strip()
        if query not in ("recipe_for", "tool_tier_for") or not name:
            raise ValueError("knowledge 需要 recipe_for/tool_tier_for + name")
        cached = knowledge.get(query, name)
        if cached is not None:
            result = {**cached, "cached": True}
            return await self._augment_tool_knowledge(result, query)
        self._internal_task_depth += 1
        try:
            accepted = await self.command(
                "knowledge_query", {"query": query, "name": name}, source="auto"
            )
            task_id = str(accepted.get("task_id") or "")
            if not task_id:
                raise RuntimeError("适配器没有返回 knowledge task_id")
            done = await self._wait_task_result(task_id, 10.0)
        finally:
            self._internal_task_depth = max(0, self._internal_task_depth - 1)
        payload = done.get("result")
        if not done.get("ok", True) or not isinstance(payload, Mapping):
            raise RuntimeError(f"知识查询失败：{payload}")
        result = knowledge.remember(query, name, payload)
        result["cached"] = False
        return await self._augment_tool_knowledge(result, query)

    async def _augment_tool_knowledge(
        self, result: Mapping[str, Any], query: str
    ) -> dict[str, Any]:
        data = dict(result)
        if query != "tool_tier_for":
            return data
        try:
            snapshot = await self.snapshot()
        except Exception:  # noqa: BLE001
            return data
        inventory = _inventory_summary(snapshot)
        wanted = {str(item) for item in (data.get("tools") or [])}
        owned = [
            item
            for item in inventory
            if str(item.get("item") or "") in wanted
        ]
        data["your_matching_tools"] = owned
        data["can_harvest_with_inventory"] = bool(owned) or not wanted
        if wanted and not owned:
            data["missing_tool_hint"] = (
                "背包里没有这个方块需要的工具类别；徒手或错误工具可能不掉落"
            )
        return data

    async def recipe_for(self, item: str) -> dict[str, Any]:
        return await self.knowledge_query("recipe_for", item)

    async def tool_tier_for(self, block: str) -> dict[str, Any]:
        return await self.knowledge_query("tool_tier_for", block)

    async def inventory_detail(self) -> dict[str, Any]:
        """完整背包事实：槽位、聚合、工具耐久、弹药、船、装备。"""
        if not self.active:
            raise RuntimeError("现在没有进行中的游戏会话")
        snapshot = await self.snapshot()
        details = snapshot.get("inventory_details")
        if not isinstance(details, Mapping):
            details = {}
        recent_changes = [
            self._event_view(event)
            for event in self._recent_events
            if getattr(event, "type", "") == "inventory_change"
        ][-5:]
        return {
            "inventory": _inventory_summary(snapshot),
            "details": dict(details),
            "empty_slots": snapshot.get("inventory_empty_slots"),
            "full": snapshot.get("inventory_full"),
            "tools": snapshot.get("inventory_tools") or [],
            "ammo": snapshot.get("inventory_ammo") or [],
            "boats": snapshot.get("inventory_boats") or [],
            "foods": snapshot.get("inventory_foods") or [],
            "equipment": snapshot.get("equipment") or {},
            "held": snapshot.get("held"),
            "recent_changes": recent_changes,
        }

    async def plugin_action(
        self, action: str, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.active:
            raise RuntimeError("现在没有进行中的游戏会话")
        action = str(action or "").strip().lower()
        data = dict(args or {})
        self._internal_task_depth += 1
        try:
            if action == "list":
                accepted = await self.command("plugin_list", {}, source="auto")
            elif action == "call":
                accepted = await self.command(
                    "plugin_call",
                    {
                        "plugin": str(data.get("plugin") or ""),
                        "action": str(data.get("action") or ""),
                        "args": data.get("args") if isinstance(data.get("args"), dict) else {},
                    },
                    source="auto",
                )
            else:
                raise ValueError("plugin action 只能是 list / call")
            task_id = str(accepted.get("task_id") or "")
            if not task_id:
                raise RuntimeError("适配器没有返回 plugin task_id")
            done = await self._wait_task_result(task_id, 12.0)
        finally:
            self._internal_task_depth = max(0, self._internal_task_depth - 1)
        payload = done.get("result")
        if not done.get("ok", True):
            if isinstance(payload, Mapping):
                return {**dict(payload), "ok": False}
            raise RuntimeError(str(payload or "插件调用失败"))
        return dict(payload) if isinstance(payload, Mapping) else {"ok": True, "result": payload}

    def status(self) -> dict[str, Any]:
        if self._service is None:
            config = self._load_cheat_config()
            return {
                "active": False,
                "current_plan": None,
                "plans": [],
                "incident": None,
                "profile": config["profile"],
                "cheat_enabled": cheats.is_active(config),
                "cheat_abilities": cheats.enabled_abilities(config),
                "reflexes": self._load_reflex_config(),
            }
        data = dict(self._service.status() or {})
        data["active"] = self.active
        data["friend_id"] = self.friend_id
        data["game_kind"] = self.game_kind
        raw_tasks = data.get("tasks")
        if isinstance(raw_tasks, Mapping):
            data["tasks"] = {
                str(task_id): _compact_task_view(task)
                for task_id, task in raw_tasks.items()
                if isinstance(task, Mapping)
            }
        active_plans = self._plans.active()
        current_plan = self._plans.executing() or (
            active_plans[-1] if active_plans else None
        )
        data["current_plan"] = current_plan.view() if current_plan else None
        data["plans"] = [plan.view() for plan in active_plans]
        # 旧字段保留给还没升级的调用方；新 UI 直接读 current_plan。
        data["plan"] = (
            str(current_plan.expected or current_plan.condition)
            if current_plan is not None
            else self._plan
        )
        data["plan_steps"] = (
            [
                f"{task.action} {json.dumps(task.args, ensure_ascii=False)}"
                for task in current_plan.tasks
                if task.status in ("pending", "executing")
            ]
            if current_plan is not None
            else list(self._plan_steps)
        )
        data["mode"] = self._mode
        data["mode_label"] = MODE_LABELS.get(self._mode, self._mode)
        data["mode_supported"] = self._mode_supported
        data["brain"] = dict(self._last_brain_decision) if self._last_brain_decision else None
        data["last_task"] = (
            _compact_task_view(self._last_task) if self._last_task else None
        )
        data["recent_events"] = [self._event_view(event) for event in self._recent_events[-30:]]
        cheat_config = self._load_cheat_config()
        data["profile"] = cheat_config["profile"]
        data["cheat_enabled"] = cheats.is_active(cheat_config)
        data["cheat_abilities"] = cheats.enabled_abilities(cheat_config)
        data["blueprint_job"] = _compact_value(self._blueprint_job)
        data["incident"] = _compact_value(self._active_incident)
        data["reflexes"] = self._load_reflex_config()
        data["version_compat"] = (
            dict(self._version_compat) if self._version_compat is not None else None
        )
        return data

    def recent_event_dicts(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self._event_view(event) for event in self._recent_events[-limit:]]

    def _event_view(self, event: Any) -> dict[str, Any]:
        """契约事件（有 event_id / ts）→ 前端/API 用的 dict。"""
        return {
            "id": event.event_id,
            "session_id": self.session_id,
            "friend_id": self.friend_id,
            "type": event.type,
            "payload": _compact_value(dict(event.payload)),
            "created_at": event.ts or "",
        }

    # ---------- 起 / 停 ----------

    async def start(
        self,
        *,
        friend_id: str,
        game_kind: str = "minecraft",
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.active:
            raise RuntimeError("同一时间只允许一局（D250）")
        requested = dict(config or {})
        version_resolution = None
        adapter_env: dict[str, str] | None = None
        if game_kind == "minecraft":
            version_resolution = await asyncio.to_thread(
                minecraft_version_service.resolve_for_server,
                str(requested.get("host") or "127.0.0.1"),
                int(requested.get("port") or 25565),
                requested_version=str(
                    requested.get("minecraft_version")
                    or requested.get("version")
                    or "auto"
                ),
            )
            requested.update(
                {
                    "version": version_resolution.pack_version,
                    "server_version": version_resolution.server_version,
                    "server_protocol": version_resolution.server_protocol,
                    "pack_protocol": version_resolution.pack_protocol,
                    "allow_version_fallback": version_resolution.mode
                    == "experimental",
                    "compat_mode": version_resolution.mode,
                    "compat_warning": version_resolution.warning,
                }
            )
            adapter_env = {
                "YF_MC_DATA_PACKAGE": version_resolution.package_path,
            }
        session_config = {
            **requested,
            "reflexes": self._load_reflex_config(),
        }

        async def persist_session(sess) -> None:  # noqa: ANN001
            with session_scope() as db:
                binding = game_store.upsert_binding(
                    db,
                    friend_id,
                    game_kind,
                    account_name=str((config or {}).get("username") or "") or None,
                    settings=dict(session_config),
                )
                game_store.create_session(
                    db,
                    session_id=sess.id,
                    friend_id=friend_id,
                    game_kind=game_kind,
                    binding_id=binding.id,
                    config=dict(session_config),
                )

        service = GameService(heartbeat_timeout=20.0, on_event=self._on_event)
        try:
            session = await service.start(
                friend_id=friend_id,
                game_kind=game_kind,
                env=adapter_env,
                config=session_config,
                on_session_ready=persist_session,
            )
        except Exception:
            self._service = None
            raise

        self._service = service
        self.session_id = session.id
        self.friend_id = friend_id
        self.game_kind = game_kind
        self._version_compat = (
            version_resolution.as_dict() if version_resolution is not None else None
        )
        self._recent_events = []
        self._plan = ""
        self._plan_steps = []
        self._last_task = None
        self._pending_user_chat = []
        self._continuation_retry = 0
        self._continuation_generation += 1
        self._last_turn_at = time.monotonic()
        self._last_user_activity_at = time.monotonic()
        self._last_autonomy_at = 0.0
        self._next_idle_think_at = time.monotonic() + AUTONOMY_BUFFER_SECONDS
        self._autonomy_no_progress = 0
        self._chat_until = 0.0
        self._placeholder_task_id = None
        self._mode = DEFAULT_GAME_MODE
        self._mode_applied = False
        self._mode_paused_until = 0.0
        self._mode_switching = False
        self._last_mode_switch_at = 0.0
        self._last_mode_apply_at = 0.0
        self._turn_origin = "user"
        self._user_task_attempts = {}
        self._last_brain_decision = None
        self._failure_history = []
        self._last_turn_said = False
        self._plans.clear()
        self._plan_task_links = {}
        self._plan_failure_counts = {}
        self._plan_review_attempts = {}
        self._plan_review_running = set()
        self._plan_review_retry_at = {}
        self._plan_task_auto_repairs = {}
        self._plan_completion_audits = set()
        self._plan_answer_pending = None
        self._plan_nudge_pending = None
        self._recent_completed_plans = []
        self._plan_stop_hold = None
        self._active_turn_task = None
        self._user_preempting = False
        self._active_user_message_text = None
        self._active_incident = None
        self._incident_task = None
        self._incident_last_llm_at = 0.0
        self._incident_attempts = 0
        self._incident_fallback_used = False
        self._incident_response_active = False
        self._incident_plan_id = None
        self._incident_actions = []
        self._last_player_speaker = None
        self._initiative_cooldown_until = 0.0
        self._plan_resume_after_incident = None
        self._attention_dirty = False
        self._plan_user_request_active = False
        self._completed_task_results = {}
        self._task_waiters = {}
        self._internal_task_depth = 0
        self._blueprint_job = None
        self._blueprint_previews: dict[str, dict[str, Any]] = {}
        self._last_blueprint_preview: dict[str, Any] | None = None
        # 新一局从“记住”的配置开始；上一局临时改的作弊设置不跨局。
        try:
            with session_scope() as db:
                self._cheat_config = settings_service.game_cheats_from(
                    settings_service.get(db)
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载本局作弊配置失败，按全关处理：{}", exc)
            self._cheat_config = cheats.default_config()
        session.mode = self._mode
        caps = session.capabilities
        self._patrol_supported = bool(
            caps is not None and "patrol" in caps.action_names()
        )
        self._mode_supported = bool(
            caps is not None and "mode" in caps.action_names()
        )
        if not self._mode_supported:
            # 旧适配器没有 mode：退回“自由”，保持 D272 的旧行为。
            self._mode = "free"
        else:
            try:
                await self._apply_mode()
            except Exception as exc:  # noqa: BLE001 —— 模式下发失败不该挡住开局
                logger.warning("初始游戏模式下发失败，退回自由模式：{}", exc)
                self._mode = "free"
                self._mode_supported = False
        session.mode = self._mode

        self._heartbeat_task = asyncio.create_task(
            self._game_chat_heartbeat_loop(), name="game-chat-heartbeat"
        )
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="game-no-idle-watchdog"
        )
        if self._autonomy_enabled:
            self._spawn_autonomy()
        return self.status()

    async def stop(self) -> None:
        service = self._service
        session_id = self.session_id
        if service is None or session_id is None:
            return
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._heartbeat_task = None
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._watchdog_task = None
        if self._autonomy_task is not None:
            self._autonomy_task.cancel()
            try:
                await self._autonomy_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._autonomy_task = None
        if self._incident_task is not None:
            self._incident_task.cancel()
            try:
                await self._incident_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._incident_task = None
        self._placeholder_task_id = None
        self._cancel_continuation()
        if self._mode_restore_task is not None:
            self._mode_restore_task.cancel()
            try:
                await self._mode_restore_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._mode_restore_task = None
        try:
            await service.stop()
        finally:
            with session_scope() as db:
                game_store.finish_session(db, session_id, status="ended")
            self._service = None
            self.session_id = None
            self._cheat_config = None
            self._completed_task_results = {}
            self._task_waiters = {}
            self._internal_task_depth = 0
            self._blueprint_job = None
            self._blueprint_previews = {}
            self._last_blueprint_preview = None
            self._version_compat = None
            self._spawn(self._summarize(session_id))

    async def shutdown(self) -> None:
        await self.stop()
        for task in list(self._background):
            task.cancel()
        for task in list(self._background):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ---------- 指令 ----------

    async def command(
        self,
        action: str,
        args: Mapping[str, Any] | None = None,
        *,
        source: str = "user",
    ) -> dict[str, Any]:
        service = self._service
        if service is None or not self.active or self.session_id is None:
            raise RuntimeError("现在没有进行中的游戏会话")
        params = dict(args or {})
        # D276：模式是状态，不是任务；旧的 game_do(action="follow") 也归到模式。
        if action == "mode":
            return await self.set_mode(
                str(params.get("mode") or ""),
                player=params.get("player"),
                force=True,
            )
        if action == "follow":
            return await self.set_mode(
                "follow",
                player=params.get("player"),
                force=True,
            )
        if action == "patrol" and source != "auto":
            raise RuntimeError("patrol 只由系统的待机看门狗下发")
        if action == "command" and source != "cheat":
            raise RuntimeError(
                "[条件不满足] command 是作弊内部动作，只能由 game_cheat 在 TA 开启作弊后调用"
            )
        origin = (
            "auto"
            if source == "auto"
            else ("user" if source == "user" or self._turn_origin == "user" else "auto")
        )
        # D294：先做只读预检，明知会失败的动作不落到适配器，也不打乱现有任务。
        checked = await self._preflight_action(action, params, source=source)
        if not checked.ok:
            raise RuntimeError(f"[预检:{checked.kind}] {checked.error_text()}")
        if action not in ("stop", "mode", "say") and self._has_running_task():
            # D321：plan 期间，普通聊天 / 工具回合不能抢走正在跑的 plan task。
            # 只有计划执行器（source=plan）能推进；用户要改计划必须显式走
            # game_plan(update/add_tasks/cancel)，不能靠一句闲聊把 task 取消。
            if self._plans.active():
                raise RuntimeError(
                    "[条件不满足] 当前 plan 正在推进，手上的 task 不能被动"
                    "（要改/停计划，先用 game_plan；不要另派 game_do 覆盖它）"
                )
            if source in ("user", "cheat") or (
                source == "agent" and self._turn_origin == "user"
            ):
                logger.info("新任务优先：取消手上正在跑的任务")
                await self._cancel_real_tasks()
            elif source != "plan":
                raise RuntimeError(
                    "[条件不满足] 手上还有任务，等它结束再派下一个（不要并发改寻路目标）"
                )
        if source in ("user", "cheat"):
            self._cancel_autonomy()
        if source != "auto" and action != "patrol":
            await self._cancel_patrol()
            if action != "say":
                await self._cancel_holding_tasks()
        if source in ("user", "cheat"):
            # 用户亲自下指令 → 取消待续跑，先听人
            self._cancel_continuation()
            self._last_user_activity_at = time.monotonic()
        if source in ("user", "agent", "cheat", "plan") and action != "say":
            # 真实任务优先于常态模式：先暂停跟随/聊天，任务结束再恢复。
            await self._suspend_mode()

        # 先落一条 task（adapter_task_id 稍后补），避免 task_done 先到、找不到行
        with session_scope() as db:
            row = game_store.add_task(
                db,
                session_id=self.session_id,
                adapter_task_id=None,
                action=action,
                args=params,
            )
            row_id = row.id
        try:
            result = await service.command(action, args, origin=origin)
        except Exception as exc:
            with session_scope() as db:
                game_store.finish_task(
                    db, row_id, status="failed", result={"error": str(exc)}
                )
            raise
        with session_scope() as db:
            game_store.attach_adapter_task_id(
                db, row_id, str(result.get("task_id") or "") or None
            )
        return result

    async def cheat(self, action: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """执行一项用户已开启的作弊能力，并把这次调用写进审计事件（D290）。"""
        if not self.active or self.session_id is None:
            raise RuntimeError("现在没有进行中的游戏会话")
        config = self._load_cheat_config()
        ability = str(action or "").strip().lower()
        if not cheats.is_active(config):
            raise PermissionError("[作弊未开启] 需要 TA 在游戏模式的「作弊」页手动开启")
        if not config["abilities"].get(ability):
            raise PermissionError(f"[作弊未开启] TA 没有开启「{ability}」这一项")

        params = dict(args or {})
        commands = cheats.build_commands(ability, params)
        try:
            result = await self.command(
                "command",
                {"commands": commands},
                source="cheat",
            )
        except Exception as exc:
            self._audit_cheat(
                ability,
                params,
                commands,
                ok=False,
                error=str(exc),
            )
            raise
        self._audit_cheat(ability, params, commands, ok=True, result=result)
        return {
            "cheat": ability,
            "commands": commands,
            "task": result,
        }

    def _audit_cheat(
        self,
        action: str,
        args: Mapping[str, Any],
        commands: list[str],
        *,
        ok: bool,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        if self.session_id is None or self.friend_id is None:
            return
        payload: dict[str, Any] = {
            "type": "cheat",
            "source": "cheat",
            "action": action,
            "args": dict(args),
            "commands": list(commands),
            "ok": ok,
        }
        if result is not None:
            payload["result"] = result
        if error:
            payload["error"] = error
        event = GameEvent.from_payload(payload)
        self._recent_events.append(event)
        if len(self._recent_events) > 80:
            del self._recent_events[: len(self._recent_events) - 80]
        try:
            with session_scope() as db:
                game_store.add_event(
                    db,
                    event_id=event.event_id,
                    session_id=self.session_id,
                    friend_id=self.friend_id,
                    event_type=event.type,
                    payload=event.payload,
                )
        except Exception as exc:  # noqa: BLE001 —— 审计失败不能反过来搞崩游戏
            logger.warning("作弊审计事件落库失败：{}", exc)

    async def _wait_task_result(
        self, task_id: str, timeout: float
    ) -> dict[str, Any]:
        """等一个适配器任务的结果；处理 task_done 早于 command 返回的竞态。"""
        cached = self._completed_task_results.pop(task_id, None)
        if cached is not None:
            return cached
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._task_waiters[task_id] = waiter
        try:
            return await asyncio.wait_for(waiter, timeout=max(0.05, timeout))
        finally:
            self._task_waiters.pop(task_id, None)

    async def run_skill(
        self,
        skill: skills.Skill | Mapping[str, Any],
        params: Mapping[str, Any] | None = None,
        *,
        source: str = "auto",
    ) -> dict[str, Any]:
        """确定性执行一个技能；步骤之间只等真实 task_done，不调 LLM。"""
        if not self.active:
            raise RuntimeError("现在没有进行中的游戏会话")

        async def command(
            step: skills.SkillStep, args: dict[str, Any], timeout: float
        ) -> dict[str, Any]:
            if step.action == "blueprint":
                return await self.run_blueprint(
                    args.get("blueprint") or args,
                    args.get("anchor") or {
                        "x": 0,
                        "y": 64,
                        "z": 0,
                    },
                )
            if step.action == "collect":
                # 她的 mine/attack 本身就会收掉落；collect 只作为“给世界一点时间”的
                # 确定性步骤，再由 checks 决定到底有没有收到。
                await asyncio.sleep(min(0.4, max(0.1, timeout / 10)))
                return {"ok": True, "collected": True}
            result = await self.command(step.action, args, source=source)
            task_id = str(result.get("task_id") or "")
            if not task_id:
                return {"ok": False, "error": "适配器没有返回 task_id"}
            try:
                done = await self._wait_task_result(
                    task_id, max(0.05, timeout - 0.2)
                )
            except asyncio.TimeoutError:
                try:
                    await self.cancel(task_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("技能步骤超时后取消任务失败：{}", exc)
                raise
            payload = done.get("result")
            if not done.get("ok", True):
                if isinstance(payload, Mapping):
                    return {"ok": False, **dict(payload)}
                return {"ok": False, "error": str(payload or "任务失败")}
            if isinstance(payload, Mapping):
                return dict(payload)
            return {"ok": True, "value": payload}

        runner = skills.SkillRunner(command, self.snapshot)
        self._internal_task_depth += 1
        try:
            result = await runner.run(skill, params)
        finally:
            self._internal_task_depth = max(0, self._internal_task_depth - 1)
        return result.view()

    async def use_skill(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.active or not self.friend_id:
            raise RuntimeError("现在没有进行中的游戏会话")
        with session_scope() as db:
            skill = skill_store.get_skill(db, self.friend_id, name)
        if skill is None:
            raise ValueError(f"没有这个技能：{name}")
        return await self._execute_skill(skill, params)

    async def use_skill_by_id(
        self,
        skill_id: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.active or not self.friend_id:
            raise RuntimeError("现在没有进行中的游戏会话")
        with session_scope() as db:
            skill = skill_store.get_skill_by_id(db, skill_id)
        if skill is None:
            raise ValueError(f"没有这个技能：{skill_id}")
        if skill.get("friend_id") not in (None, self.friend_id):
            raise PermissionError("这个技能不属于当前朋友")
        return await self._execute_skill(skill, params)

    async def _execute_skill(
        self,
        skill: Mapping[str, Any],
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not skill.get("enabled"):
            raise ValueError(f"技能「{skill.get('name')}」已被禁用")
        result = await self.run_skill(skill, params, source="agent")
        with session_scope() as db:
            skill_store.record_run(db, str(skill["id"]), ok=bool(result.get("ok")))
        return result

    # ---------- 蓝图 / 自由创作（D289） ----------

    async def _inspect_positions(
        self, positions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for start in range(0, len(positions), 512):
            chunk = positions[start : start + 512]
            result = await self.command(
                "inspect_blocks", {"positions": chunk}, source="auto"
            )
            task_id = str(result.get("task_id") or "")
            if not task_id:
                raise RuntimeError("适配器没有返回 inspect_blocks task_id")
            done = await self._wait_task_result(task_id, 12.0)
            payload = done.get("result")
            if not done.get("ok", True) or not isinstance(payload, Mapping):
                raise RuntimeError(f"方块查询失败：{payload}")
            found = payload.get("blocks")
            if isinstance(found, list):
                blocks.extend(item for item in found if isinstance(item, dict))
        return blocks

    async def inspect_area(
        self,
        *,
        center: Mapping[str, Any] | None = None,
        radius: int = 4,
        height: int = 4,
        include_air: bool = False,
    ) -> dict[str, Any]:
        """精确读取玩家周围 / 指定中心的一块局部地形。"""
        if not self.active:
            raise RuntimeError("现在没有进行中的游戏会话")
        snapshot = await self.snapshot()
        player = snapshot.get("player") or {}
        if isinstance(center, Mapping):
            try:
                cx = int(float(center.get("x")))
                cy = int(float(center.get("y")))
                cz = int(float(center.get("z")))
            except (TypeError, ValueError):
                center = None
                cx = int(float(player.get("x") or 0))
                cy = int(float(player.get("y") or 64))
                cz = int(float(player.get("z") or 0))
        else:
            cx = int(float(player.get("x") or 0))
            cy = int(float(player.get("y") or 64))
            cz = int(float(player.get("z") or 0))
        radius = max(1, min(8, int(radius)))
        height = max(1, min(8, int(height)))
        positions = [
            {"x": cx + dx, "y": cy + dy, "z": cz + dz}
            for dx in range(-radius, radius + 1)
            for dz in range(-radius, radius + 1)
            for dy in range(0, height)
        ]
        self._internal_task_depth += 1
        try:
            found = await self._inspect_positions(positions)
        finally:
            self._internal_task_depth = max(0, self._internal_task_depth - 1)
        by_column: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for block in found:
            if not isinstance(block, Mapping):
                continue
            name = str(block.get("name") or "air")
            if not include_air and name == "air":
                continue
            try:
                key = (int(block["x"]), int(block["z"]))
                entry = {"y": int(block["y"]), "name": name}
            except (KeyError, TypeError, ValueError):
                continue
            by_column.setdefault(key, []).append(entry)
        columns = [
            {"x": x, "z": z, "blocks": sorted(items, key=lambda item: item["y"])}
            for (x, z), items in sorted(by_column.items())
        ]
        return {
            "center": {"x": cx, "y": cy, "z": cz},
            "radius": radius,
            "height": height,
            "columns": columns,
        }

    async def survey_area(self, *, radius: int = 64, step: int = 4) -> dict[str, Any]:
        """按需返回大范围紧凑地形图，不把整块方块表塞给模型。"""
        if not self.active:
            raise RuntimeError("现在没有进行中的游戏会话")
        radius = max(8, min(128, int(radius)))
        step = max(2, min(16, int(step)))
        self._internal_task_depth += 1
        try:
            result = await self.command(
                "survey_area",
                {"radius": radius, "step": step},
                source="auto",
            )
            task_id = str(result.get("task_id") or "")
            if not task_id:
                raise RuntimeError("适配器没有返回 survey_area task_id")
            done = await self._wait_task_result(task_id, 20.0)
            payload = done.get("result")
            if not done.get("ok", True) or not isinstance(payload, Mapping):
                raise RuntimeError(f"地形测绘失败：{payload}")
            return {"ok": True, **dict(payload)}
        finally:
            self._internal_task_depth = max(0, self._internal_task_depth - 1)

    async def _preflight_place_support(
        self, args: Mapping[str, Any]
    ) -> preflight.PreflightResult | None:
        """放置前只读查目标格 + 6 个邻居；查不到就放行，不假装知道。"""
        try:
            x = float(args.get("x"))
            y = float(args.get("y"))
            z = float(args.get("z"))
        except (TypeError, ValueError):
            return None
        target = {"x": x, "y": y, "z": z}
        positions = [{**target}]
        for dx, dy, dz in (
            (0, -1, 0),
            (0, 1, 0),
            (1, 0, 0),
            (-1, 0, 0),
            (0, 0, 1),
            (0, 0, -1),
        ):
            positions.append({"x": x + dx, "y": y + dy, "z": z + dz})
        self._internal_task_depth += 1
        try:
            blocks = await self._inspect_positions(positions)
        except Exception as exc:  # noqa: BLE001 —— 查不到就交给适配器判断
            logger.debug("放置预检查方块失败（放行）：{}", exc)
            return None
        finally:
            self._internal_task_depth = max(0, self._internal_task_depth - 1)
        return preflight.check_place_support(blocks, target)

    async def _preflight_action(
        self,
        action: str,
        args: Mapping[str, Any],
        *,
        source: str,
    ) -> preflight.PreflightResult:
        """动作下发前的只读检查；预检自己出错时放行，不阻断正常游戏。"""
        if source == "cheat" or action not in preflight.PREFLIGHT_ACTIONS:
            return preflight.PreflightResult(True)
        try:
            snapshot = await self.snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.debug("动作预检取快照失败（放行）：{}", exc)
            return preflight.PreflightResult(True)
        recipe: Mapping[str, Any] | None = None
        if action == "craft":
            item = str(
                args.get("item") or args.get("block") or args.get("target") or ""
            ).strip()
            if item:
                recipe = knowledge.get("recipe_for", item)
                if recipe is None:
                    try:
                        recipe = await self.recipe_for(item)
                    except Exception as exc:  # noqa: BLE001 —— 没配方事实就交给适配器
                        logger.debug("动作预检查配方失败（放行）：{}", exc)
        result = preflight.check_action(action, args, snapshot, recipe=recipe)
        if not result.ok:
            logger.info(
                "动作预检拦截：{} {} → {}（{}）",
                action,
                dict(args),
                result.kind,
                result.message,
            )
            return result
        if action == "place" and self._internal_task_depth == 0:
            support = await self._preflight_place_support(args)
            if support is not None and not support.ok:
                logger.info(
                    "放置预检拦截：{} → {}（{}）",
                    dict(args),
                    support.kind,
                    support.message,
                )
                return support
        return result

    async def run_blueprint(
        self,
        blueprint: Mapping[str, Any] | blueprints.Blueprint,
        anchor: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not self.active:
            raise RuntimeError("现在没有进行中的游戏会话")
        definition = (
            blueprint
            if isinstance(blueprint, blueprints.Blueprint)
            else blueprints.compile_design(blueprint)
        )
        anchor_data = {
            "x": int(anchor["x"]),
            "y": int(anchor["y"]),
            "z": int(anchor["z"]),
        }
        self._blueprint_job = {
            "state": "running",
            "name": definition.name,
            "anchor": anchor_data,
            "placed": 0,
            "checked": 0,
            "failures": 0,
        }

        async def place(target: dict[str, Any], timeout: float) -> dict[str, Any]:
            result = await self.command("place", target, source="auto")
            task_id = str(result.get("task_id") or "")
            if not task_id:
                return {"error": "适配器没有返回 task_id"}
            done = await self._wait_task_result(task_id, timeout)
            payload = done.get("result")
            if not done.get("ok", True):
                if isinstance(payload, Mapping):
                    return {"ok": False, **dict(payload)}
                return {"ok": False, "error": str(payload or "放块失败")}
            return dict(payload) if isinstance(payload, Mapping) else {"ok": True}

        async def clear(target: dict[str, Any]) -> dict[str, Any]:
            observed = str(target.get("observed") or "")
            if observed and _BLUEPRINT_PROTECTED_BLOCK.search(observed):
                return {
                    "ok": False,
                    "error": f"冲突格是 {observed}，属于受保护方块，不自动拆",
                }
            result = await self.command(
                "dig_at",
                {
                    "x": target["x"],
                    "y": target["y"],
                    "z": target["z"],
                },
                source="auto",
            )
            task_id = str(result.get("task_id") or "")
            if not task_id:
                return {"ok": False, "error": "适配器没有返回 task_id"}
            done = await self._wait_task_result(task_id, 25.0)
            payload = done.get("result")
            if not done.get("ok", True):
                if isinstance(payload, Mapping):
                    return {"ok": False, **dict(payload)}
                return {"ok": False, "error": str(payload or "清理方块失败")}
            return dict(payload) if isinstance(payload, Mapping) else {"ok": True}

        def progress(data: dict[str, Any]) -> None:
            if self._blueprint_job is not None:
                self._blueprint_job.update(data)

        runner = blueprints.BlueprintRunner(
            place,
            self._inspect_positions,
            clear=clear,
        )
        self._internal_task_depth += 1
        try:
            result = await runner.run(definition, anchor_data, on_progress=progress)
        except Exception as exc:  # noqa: BLE001 —— 蓝图失败要如实返回，不搞崩会话
            result = {
                "ok": False,
                "name": definition.name,
                "anchor": anchor_data,
                "placed": 0,
                "checked": 0,
                "failures": [],
                "error": str(exc),
            }
        finally:
            self._internal_task_depth = max(0, self._internal_task_depth - 1)
        self._blueprint_job = {
            **(self._blueprint_job or {}),
            **result,
            "state": "completed" if result.get("ok") else "failed",
        }
        return result

    def _anchor_from_snapshot(self, snapshot: Mapping[str, Any]) -> dict[str, int]:
        player = snapshot.get("player") or {}
        return {
            "x": int(float(player.get("x") or 0)),
            "y": int(float(player.get("y") or 64)),
            "z": int(float(player.get("z") or 0)),
        }

    def _build_anchor_from_snapshot(
        self,
        snapshot: Mapping[str, Any],
        *,
        width: int,
        depth: int,
    ) -> dict[str, Any] | None:
        """优先选适配器已经验证过的平地；没有就如实返回 None。"""
        key = "flat_sites" if width >= 5 and depth >= 5 else "flat_sites_3"
        sites = [
            item
            for item in (snapshot.get(key) or snapshot.get("flat_sites") or [])
            if isinstance(item, Mapping)
        ]
        if not sites:
            return None
        site = sites[0]
        return {
            "x": int(site["x"]),
            "y": int(site["y"]),
            "z": int(site["z"]),
            "source": "flat_site",
            "width": width,
            "depth": depth,
        }

    async def _site_fill_positions(
        self, anchor: Mapping[str, Any], size: Mapping[str, Any]
    ) -> list[dict[str, int]]:
        """地面低于 floor 时，地基下面需要补的方块坐标（按 y 从低到高）。"""
        x0 = int(anchor["x"])
        y0 = int(anchor["y"])
        z0 = int(anchor["z"])
        positions = [
            {"x": x0 + x, "y": y, "z": z0 + z}
            for x in range(int(size["x"]))
            for z in range(int(size["z"]))
            for y in range(y0 - 8, y0)
        ]
        self._internal_task_depth += 1
        try:
            blocks = await self._inspect_positions(positions)
        finally:
            self._internal_task_depth = max(0, self._internal_task_depth - 1)
        names = {
            (int(item["x"]), int(item["y"]), int(item["z"])): str(
                item.get("name") or "air"
            )
            for item in blocks
            if isinstance(item, Mapping)
            and all(axis in item for axis in ("x", "y", "z"))
        }
        fill: list[dict[str, int]] = []
        for x in range(int(size["x"])):
            for z in range(int(size["z"])):
                ground = y0 - 8
                for y in range(y0 - 1, y0 - 9, -1):
                    observed = names.get((x0 + x, y, z0 + z), "unknown")
                    if observed not in ("air", "unknown"):
                        ground = y
                        break
                for y in range(ground + 1, y0):
                    if names.get((x0 + x, y, z0 + z), "air") in ("air", "unknown"):
                        fill.append({"x": x0 + x, "y": y, "z": z0 + z})
        fill.sort(key=lambda item: (item["y"], item["x"], item["z"]))
        return fill

    async def _prepare_site(
        self,
        *,
        anchor: Mapping[str, Any],
        size: Mapping[str, Any],
        fill_block: str,
    ) -> dict[str, Any]:
        """把 floor 下面悬空的地方按层垫起来；只做支撑，不改蓝图目标格。"""
        positions = await self._site_fill_positions(anchor, size)
        placed = 0
        for target in positions:
            result = await self.command(
                "place",
                {"block": fill_block, **target},
                source="auto",
            )
            task_id = str(result.get("task_id") or "")
            if not task_id:
                return {"ok": False, "error": "适配器没有返回 task_id", "placed": placed}
            done = await self._wait_task_result(task_id, 25.0)
            payload = done.get("result")
            if not done.get("ok", True):
                return {
                    "ok": False,
                    "error": str(
                        (payload or {}).get("error")
                        if isinstance(payload, Mapping)
                        else payload or "垫地基失败"
                    ),
                    "placed": placed,
                }
            placed += 1
        return {"ok": True, "placed": placed, "fill_block": fill_block}

    def _find_blueprint(
        self, friend_id: str, args: Mapping[str, Any]
    ) -> blueprints.Blueprint:
        raw = args.get("blueprint")
        if isinstance(raw, Mapping):
            return blueprints.compile_design(raw)
        blueprint_id = str(args.get("blueprint_id") or "").strip()
        if not blueprint_id:
            raise ValueError("需要 blueprint 或 blueprint_id")
        with session_scope() as db:
            row = blueprint_store.get_blueprint(db, friend_id, blueprint_id)
        if row is None:
            raise ValueError(f"没有这个蓝图：{blueprint_id}")
        return blueprints.Blueprint.from_dict(row["blueprint"])

    def _active_blueprint_plan(
        self, blueprint: blueprints.Blueprint
    ) -> tuple[Any, Any] | None:
        """同一份蓝图已经有 active plan 时，返回它，避免重开一座。"""
        wanted = blueprint.view()
        for plan in self._plans.active():
            for task in plan.tasks:
                if task.action != "blueprint":
                    continue
                raw = task.args.get("blueprint")
                if isinstance(raw, Mapping) and dict(raw) == wanted:
                    return plan, task
        return None

    async def _blueprint_package(
        self,
        *,
        blueprint: blueprints.Blueprint,
        anchor: Mapping[str, Any],
        allow_give: bool = True,
    ) -> dict[str, Any]:
        """把蓝图编译成统一的“材料 + task + checks”施工包。"""
        snapshot = await self.snapshot()
        requirements = blueprint.requirements()
        fill_block = (
            max(requirements, key=lambda name: requirements[name])
            if requirements
            else "dirt"
        )
        fill_positions = await self._site_fill_positions(anchor, blueprint.size)
        fill_count = len(fill_positions)
        if fill_count:
            requirements[fill_block] = requirements.get(fill_block, 0) + fill_count
        gaps = blueprints.material_gap(requirements, snapshot)
        direct_give = False
        materials_given = False
        if gaps:
            config = self._load_cheat_config()
            direct_give = bool(
                cheats.is_active(config) and config["abilities"].get("give")
            )
        if gaps and direct_give and allow_give:
            for item, count in gaps.items():
                await self.cheat("give", {"item": item, "count": count})
                await asyncio.sleep(0.35)
            materials_given = True
            snapshot = await self.snapshot()
            gaps = blueprints.material_gap(requirements, snapshot)
        tasks = blueprints.procurement_tasks(gaps) if gaps else []
        if fill_count:
            tasks.append(
                {
                    "action": "prepare_site",
                    "args": {
                        "anchor": dict(anchor),
                        "size": dict(blueprint.size),
                        "fill_block": fill_block,
                    },
                }
            )
        tasks.append(
            {
                "action": "blueprint",
                "args": {
                    "blueprint": blueprint.view(),
                    "anchor": dict(anchor),
                },
            }
        )
        checks = blueprint.block_checks(anchor)
        condition = (
            "材料齐；原地可以放置方块。缺口："
            + ("、".join(f"{name}×{count}" for name, count in gaps.items()) or "无")
            + (f"；地基需补 {fill_count} 格" if fill_count else "")
        )
        return {
            "requirements": requirements,
            "gaps": gaps,
            "direct_give": direct_give,
            "materials_given": materials_given,
            "fill_count": fill_count,
            "fill_block": fill_block,
            "tasks": tasks,
            "checks": checks,
            "condition": condition,
            "expected": (
                f"按蓝图 {blueprint.name} 建好，并通过 {len(checks)} 个 block_at 校验"
            ),
        }

    async def _start_blueprint_plan(
        self,
        *,
        friend_id: str,
        blueprint: blueprints.Blueprint,
        anchor: Mapping[str, Any],
    ) -> dict[str, Any]:
        package = await self._blueprint_package(blueprint=blueprint, anchor=anchor)
        plan_result = self.plan_action(
            "create",
            condition=package["condition"],
            expected=package["expected"],
            checks=package["checks"],
            tasks=package["tasks"],
        )
        return {
            **plan_result,
            "ok": True,
            "status": "planned",
            "materials_ready": not bool(package["gaps"]),
            "gap": dict(package["gaps"]),
            "direct_give": package["direct_give"],
            "materials_given": package["materials_given"],
            "requirements": package["requirements"],
            "site_fill": package["fill_count"],
            "site_fill_block": (
                package["fill_block"] if package["fill_count"] else None
            ),
        }

    async def _blueprint_preview(
        self, action: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        """只返回设计稿和施工事实，不创建 plan（D333）。"""
        if action == "hut":
            material = str(data.get("material") or data.get("block") or "").strip()
            if not material:
                raise ValueError(
                    "hut 需要 material（TA 指定的建造材料，例如 dirt / oak_planks）"
                )
            try:
                width = int(data.get("width") or 5)
                depth = int(data.get("depth") or 5)
                height = int(data.get("height") or 2)
            except (TypeError, ValueError) as exc:
                raise ValueError("hut 的 width / depth / height 必须是整数") from exc
            definition = blueprints.simple_hut_blueprint(
                material,
                width=width,
                depth=depth,
                height=height,
            )
            snapshot = await self.snapshot()
            anchor = data.get("anchor")
            if not isinstance(anchor, Mapping):
                anchor = self._build_anchor_from_snapshot(
                    snapshot, width=width, depth=depth
                )
            if anchor is None and (width > 3 or depth > 3):
                width = depth = 3
                definition = blueprints.simple_hut_blueprint(
                    material,
                    width=width,
                    depth=depth,
                    height=height,
                )
                anchor = self._build_anchor_from_snapshot(
                    snapshot, width=width, depth=depth
                )
            if anchor is None:
                return {
                    "ok": False,
                    "status": "needs_flat_site",
                    "message": "附近没有可用的平地；先找一块平地，不要直接在坡上建。",
                    "blueprint": definition.view(),
                    "resolved_width": width,
                    "resolved_depth": depth,
                    "flat_sites": snapshot.get("flat_sites") or [],
                    "flat_sites_3": snapshot.get("flat_sites_3") or [],
                    "world": snapshot.get("world") or {},
                }
        elif action == "build":
            definition = self._find_blueprint(self.friend_id or "", data)
            snapshot = await self.snapshot()
            anchor = data.get("anchor")
            if not isinstance(anchor, Mapping):
                anchor = self._anchor_from_snapshot(snapshot)
            if anchor is None:
                return {
                    "ok": False,
                    "status": "needs_flat_site",
                    "message": "附近没有可用的平地；先找一块平地，不要直接在坡上建。",
                    "blueprint": definition.view(),
                    "flat_sites": snapshot.get("flat_sites") or [],
                    "flat_sites_3": snapshot.get("flat_sites_3") or [],
                    "world": snapshot.get("world") or {},
                }
        else:
            raise ValueError("预览只支持 hut / build")

        package = await self._blueprint_package(
            blueprint=definition,
            anchor=anchor,
            allow_give=False,
        )
        preview = {
            "ok": True,
            "status": "design_preview",
            "message": (
                "这是设计稿和施工事实，没有创建 plan。要施工就用 game_plan "
                "创建或更新计划，并把 plan_tasks 放进去。"
            ),
            "blueprint": definition.view(),
            "anchor": dict(anchor),
            "condition": package["condition"],
            "expected": package["expected"],
            "checks": package["checks"],
            "plan_tasks": package["tasks"],
            "requirements": package["requirements"],
            "gap": dict(package["gaps"]),
            "materials_ready": not bool(package["gaps"]),
            "direct_give": package["direct_give"],
            "materials_given": package["materials_given"],
            "site_fill": package["fill_count"],
            "site_fill_block": (
                package["fill_block"] if package["fill_count"] else None
            ),
        }
        name = str(definition.name or "").strip()
        if name:
            self._blueprint_previews[name] = preview
        self._last_blueprint_preview = preview
        return preview

    async def blueprint_action(
        self,
        action: str,
        args: Mapping[str, Any] | None = None,
        *,
        preview_only: bool = False,
    ) -> dict[str, Any]:
        if not self.active or not self.friend_id:
            raise RuntimeError("现在没有进行中的游戏会话")
        data = dict(args or {})
        action = str(action or "").strip().lower()
        if preview_only and action in ("hut", "build"):
            return await self._blueprint_preview(action, data)
        if action in ("design", "save"):
            raw = data.get("blueprint")
            definition = blueprints.compile_design(raw)
            with session_scope() as db:
                saved = blueprint_store.save_blueprint(
                    db,
                    friend_id=self.friend_id,
                    raw=definition.view(),
                    source="learned",
                )
            snapshot = await self.snapshot()
            gaps = blueprints.material_gap(definition.requirements(), snapshot)
            return {
                "blueprint": saved,
                "requirements": definition.requirements(),
                "gap": gaps,
                "procurement_tasks": blueprints.procurement_tasks(gaps),
            }
        if action == "hut":
            material = str(data.get("material") or data.get("block") or "").strip()
            if not material:
                raise ValueError(
                    "hut 需要 material（TA 指定的建造材料，例如 dirt / oak_planks）"
                )
            try:
                width = int(data.get("width") or 5)
                depth = int(data.get("depth") or 5)
                height = int(data.get("height") or 2)
            except (TypeError, ValueError) as exc:
                raise ValueError("hut 的 width / depth / height 必须是整数") from exc
            definition = blueprints.simple_hut_blueprint(
                material,
                width=width,
                depth=depth,
                height=height,
            )
            existing = self._active_blueprint_plan(definition)
            if existing is not None and not bool(data.get("new_plan")):
                plan, task = existing
                if (
                    plan.status == "paused"
                    and plan.id not in self._plan_review_running
                ):
                    self._plans.resume(plan.id)
                    self._spawn(self._execute_next_plan_task(self.friend_id))
                return {
                    "ok": True,
                    "status": "reused_existing_plan",
                    "plan": plan.view(),
                    "plans": [item.view() for item in self._plans.list()],
                    "executing": (
                        self._plans.executing().id
                        if self._plans.executing()
                        else None
                    ),
                    "action": "hut",
                    "material": material,
                    "anchor": dict(task.args.get("anchor") or {}),
                    "blueprint": task.args.get("blueprint"),
                }
            anchor = data.get("anchor")
            fallback_site = False
            snapshot: Mapping[str, Any] | None = None
            if not isinstance(anchor, Mapping):
                snapshot = await self.snapshot()
                anchor = self._build_anchor_from_snapshot(
                    snapshot,
                    width=width,
                    depth=depth,
                )
                if anchor is None and (width > 3 or depth > 3):
                    # D324：树林里 5×5 很难全空；3×3 是同一蓝图的最小尺寸。
                    width = depth = 3
                    definition = blueprints.simple_hut_blueprint(
                        material,
                        width=width,
                        depth=depth,
                        height=height,
                    )
                    existing = self._active_blueprint_plan(definition)
                    if existing is not None and not bool(data.get("new_plan")):
                        plan, task = existing
                        if (
                            plan.status == "paused"
                            and plan.id not in self._plan_review_running
                        ):
                            self._plans.resume(plan.id)
                            self._spawn(
                                self._execute_next_plan_task(self.friend_id)
                            )
                        return {
                            "ok": True,
                            "status": "reused_existing_plan",
                            "plan": plan.view(),
                            "plans": [item.view() for item in self._plans.list()],
                            "executing": (
                                self._plans.executing().id
                                if self._plans.executing()
                                else None
                            ),
                            "action": "hut",
                            "material": material,
                            "anchor": dict(task.args.get("anchor") or {}),
                            "blueprint": task.args.get("blueprint"),
                        }
                    anchor = self._build_anchor_from_snapshot(
                        snapshot,
                        width=width,
                        depth=depth,
                    )
                if anchor is None:
                    # 仍然没有完整空地也不许“什么都不做”：先在她旁边选一个
                    # 3×3 锚点建 plan。蓝图执行器会原位清理冲突；失败再复盘。
                    player = snapshot.get("player") or {}
                    try:
                        anchor = {
                            "x": int(float(player.get("x") or 0)) + 3,
                            "y": int(float(player.get("y") or 64)),
                            "z": int(float(player.get("z") or 0)) + 3,
                            "source": "player_offset_fallback",
                            "width": width,
                            "depth": depth,
                        }
                    except (TypeError, ValueError):
                        raise ValueError(
                            "[条件不满足] 附近没有可用平地，也拿不到她的坐标"
                        ) from None
                    fallback_site = True
            if not all(axis in anchor for axis in ("x", "y", "z")):
                raise ValueError("hut 的 anchor 需要 x / y / z")
            result = await self._start_blueprint_plan(
                friend_id=self.friend_id,
                blueprint=definition,
                anchor=anchor,
            )
            return {
                **result,
                "action": "hut",
                "material": material,
                "anchor": dict(anchor),
                "blueprint": definition.view(),
                "fallback_site": fallback_site,
                "resolved_width": width,
                "resolved_depth": depth,
            }
        if action == "list":
            with session_scope() as db:
                return {
                    "blueprints": blueprint_store.list_blueprints(
                        db, self.friend_id
                    ),
                    "creative": blueprint_store.list_creative(db, self.friend_id),
                    "job": self._blueprint_job,
                }
        if action == "build":
            definition = self._find_blueprint(self.friend_id, data)
            existing = self._active_blueprint_plan(definition)
            if existing is not None and not bool(data.get("new_plan")):
                plan, task = existing
                if (
                    plan.status == "paused"
                    and plan.id not in self._plan_review_running
                ):
                    self._plans.resume(plan.id)
                    self._spawn(self._execute_next_plan_task(self.friend_id))
                return {
                    "ok": True,
                    "status": "reused_existing_plan",
                    "plan": plan.view(),
                    "plans": [item.view() for item in self._plans.list()],
                    "executing": (
                        self._plans.executing().id
                        if self._plans.executing()
                        else None
                    ),
                    "anchor": dict(task.args.get("anchor") or {}),
                    "blueprint": task.args.get("blueprint"),
                }
            anchor = data.get("anchor")
            if not isinstance(anchor, Mapping):
                anchor = self._anchor_from_snapshot(await self.snapshot())
            return await self._start_blueprint_plan(
                friend_id=self.friend_id,
                blueprint=definition,
                anchor=anchor,
            )
        if action == "creative_start":
            validated = blueprints.validate_creative_design(data)
            anchor = data.get("anchor")
            if not isinstance(anchor, Mapping):
                anchor = self._anchor_from_snapshot(await self.snapshot())
            size = data.get("size")
            if not isinstance(size, Mapping):
                raise ValueError("creative_start 需要 size: {x,y,z}")
            anchor_data = {
                "x": int(anchor["x"]),
                "y": int(anchor["y"]),
                "z": int(anchor["z"]),
            }
            size_data = {
                axis: int(size[axis]) for axis in ("x", "y", "z")
            }
            positions = blueprints.cube_positions(anchor_data, size_data)
            self._internal_task_depth += 1
            try:
                before = await self._inspect_positions(positions)
            finally:
                self._internal_task_depth = max(0, self._internal_task_depth - 1)
            with session_scope() as db:
                record = blueprint_store.creative_start(
                    db,
                    friend_id=self.friend_id,
                    brief=validated["brief"],
                    design=validated["design"],
                    anchor=anchor_data,
                    size=size_data,
                    before=before,
                )
            return {"record": record, "positions": len(positions)}
        if action == "creative_finish":
            record_id = str(data.get("record_id") or "").strip()
            if not record_id:
                raise ValueError("creative_finish 需要 record_id")
            with session_scope() as db:
                record = blueprint_store.get_creative(db, record_id)
            if record is None or record["friend_id"] != self.friend_id:
                raise ValueError("没有这个创作记录")
            positions = blueprints.cube_positions(
                record["anchor"], record["size"]
            )
            self._internal_task_depth += 1
            try:
                after = await self._inspect_positions(positions)
            finally:
                self._internal_task_depth = max(0, self._internal_task_depth - 1)
            diff = blueprints.block_diff(record["before"], after)
            with session_scope() as db:
                finished = blueprint_store.creative_finish(
                    db, record_id, after=after, diff=diff["changed"]
                )
                if data.get("rating") is not None:
                    finished = blueprint_store.creative_rate(
                        db, record_id, rating=int(data["rating"])
                    )
            return {
                "record": finished,
                "diff": diff,
                "ok": True,
                "status": "creative",
            }
        if action == "creative_rate":
            record_id = str(data.get("record_id") or "").strip()
            rating = int(data.get("rating") or 0)
            if not record_id or not 1 <= rating <= 5:
                raise ValueError("creative_rate 需要 record_id + rating(1~5)")
            with session_scope() as db:
                record = blueprint_store.get_creative(db, record_id)
                if record is None or record["friend_id"] != self.friend_id:
                    raise ValueError("没有这个创作记录")
                row = blueprint_store.creative_rate(db, record_id, rating=rating)
            return {"record": row}
        if action == "status":
            return {
                "job": self._blueprint_job,
                "plan": self._plan_snapshot_text(),
            }
        raise ValueError(
            "action 只能是 design/save/list/build/creative_start/"
            "creative_finish/creative_rate/status"
        )

    async def cancel(self, task_id: str) -> dict[str, Any]:
        service = self._service
        if service is None or not self.active:
            raise RuntimeError("现在没有进行中的游戏会话")
        result = await service.cancel(task_id)
        if task_id == self._placeholder_task_id:
            self._placeholder_task_id = None
        return result

    # ---------- 常态模式（D276） ----------

    async def set_mode(
        self,
        mode: str,
        *,
        player: Any = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """切换跟随 / 聊天 / 自由。模式是状态，不是任务。"""
        if not self.active:
            raise RuntimeError("现在没有进行中的游戏会话")
        if not self._mode_supported:
            raise RuntimeError("当前适配器不支持模式切换")
        mode = str(mode or "").strip().lower()
        if mode not in GAME_MODES:
            raise ValueError("mode 只能是 follow / chat / free")

        changed = mode != self._mode
        now = time.monotonic()
        if (
            changed
            and not force
            and self._last_mode_switch_at > 0
            and now - self._last_mode_switch_at < MODE_SWITCH_COOLDOWN
        ):
            left = int(MODE_SWITCH_COOLDOWN - (now - self._last_mode_switch_at))
            raise ValueError(
                f"刚换过模式，{left} 秒后再换；TA 明确要求时可以 force=true"
            )

        has_active_plan = self._plans.executing() is not None
        has_running_task = self._has_running_task()
        self._cancel_autonomy()
        # D317：plan/task 的优先级高于常态模式。切换模式只记录状态，
        # 不能把正在推进 plan 的续跑任务一起取消。
        if not has_active_plan and not has_running_task:
            self._cancel_continuation()
        await self._cancel_patrol()
        self._mode_switching = True
        try:
            self._mode = mode
            self._mode_paused_until = 0.0
            if self._service is not None and self._service.session is not None:
                self._service.session.mode = mode
            # D286：模式不能打断 plan/task。手上有任务或 executing plan 时先只记状态，
            # 等 plan 结束/任务做完，看门狗再真正把跟随/聊天目标下发。
            if has_running_task or has_active_plan:
                self._mode_applied = False
                result = {}
            else:
                result = await self._apply_mode(player=player)
        finally:
            self._mode_switching = False
        if changed:
            self._last_mode_switch_at = time.monotonic()
        if mode == "free" and self._plans.executing() is None:
            # 刚切到自由模式时给一次机会；之前的空转退避不应带进来。
            self._autonomy_no_progress = 0
            self._last_autonomy_at = 0.0
            self._next_idle_think_at = 0.0
            self._spawn_autonomy()
        logger.info("游戏模式切换为 {}（force={}）", mode, force)
        return {"mode": mode, **dict(result or {})}

    async def _apply_mode(self, *, player: Any = None) -> dict[str, Any]:
        service = self._service
        if service is None or not self.active or not self._mode_supported:
            return {}
        args: dict[str, Any] = {"mode": self._mode}
        if player:
            args["player"] = str(player)
        result = await service.command("mode", args)
        self._mode_applied = True
        self._last_mode_apply_at = time.monotonic()
        # 跟随/聊天时如果适配器没找到玩家，先不认为模式已经生效：
        # 看门狗会在玩家进入视野后重试。
        if (
            self._mode in ("follow", "chat")
            and "following" in result
            and not result.get("following")
        ):
            self._mode_applied = False
        if service.session is not None:
            service.session.mode = self._mode
        return dict(result or {})

    async def _suspend_mode(self) -> None:
        """真实任务开始前，把跟随/聊天的寻路目标撤掉。"""
        if not self.active or not self._mode_supported:
            return
        if self._mode not in ("follow", "chat") or not self._mode_applied:
            return
        service = self._service
        if service is None:
            return
        try:
            await service.command("mode", {"mode": "none"})
        except Exception as exc:  # noqa: BLE001 —— 暂停失败不该挡住任务
            logger.warning("暂停游戏模式失败（忽略）：{}", exc)
        self._mode_applied = False

    async def _cancel_real_tasks(self) -> None:
        """force 换模式时，先取消手上的真实任务（patrol / say 不算）。"""
        service = self._service
        session = service.session if service else None
        if service is None or session is None:
            return
        for task_id, task in list((session.tasks or {}).items()):
            if str(task.get("status") or "") != "running":
                continue
            if str(task.get("action") or "") in ("patrol", "say"):
                continue
            try:
                await service.cancel(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("取消任务 {} 失败（忽略）：{}", task_id, exc)

    def _schedule_mode_restore(self, delay: float = 0.2) -> None:
        if (
            not self._mode_supported
            or self._mode not in ("follow", "chat")
            or self._plans.executing() is not None
        ):
            return
        task = self._mode_restore_task
        if task is not None and not task.done():
            task.cancel()
        self._mode_restore_task = asyncio.create_task(
            self._restore_mode_after(delay), name="game-mode-restore"
        )

    async def _restore_mode_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            if not self.active or self._mode not in ("follow", "chat"):
                return
            if self._plans.executing() is not None:
                return
            if self._has_running_task() or self._chat_busy or self._pending_user_chat:
                return
            if time.monotonic() < self._mode_paused_until:
                return
            if self._mode_applied:
                return
            await self._apply_mode()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("恢复游戏模式失败（已吞掉）：{}", exc)

    # ---------- 用户任务的计数 / 回报（D278） ----------

    @staticmethod
    def _task_count(result: Mapping[str, Any]) -> tuple[int | None, int | None]:
        """从适配器结果里取 `(已完成, 目标)`；取不到就返回 (None, None)。"""
        requested = result.get("requested")
        target = int(requested) if isinstance(requested, (int, float)) else None
        done = None
        for key in (
            "mined",
            "killed",
            "crafted_count",
            "smelted_count",
            "dropped_count",
            "placed_count",
        ):
            value = result.get(key)
            if isinstance(value, (int, float)):
                done = int(value)
                break
        if done is None and isinstance(result.get("count"), (int, float)):
            done = int(result["count"])
        return done, target

    @classmethod
    def _remaining(cls, result: Mapping[str, Any]) -> int:
        raw = result.get("remaining")
        if isinstance(raw, (int, float)):
            return max(0, int(raw))
        done, target = cls._task_count(result)
        if done is not None and target is not None:
            return max(0, target - done)
        return 0

    @classmethod
    def _is_partial(cls, result: Mapping[str, Any]) -> bool:
        if not isinstance(result, Mapping):
            return False
        if result.get("error") or result.get("failed"):
            return False
        return cls._remaining(result) > 0

    @staticmethod
    def _task_label(action: str, args: Mapping[str, Any]) -> str:
        target = (
            args.get("block")
            or args.get("item")
            or args.get("target")
            or args.get("mob")
        )
        count = args.get("count")
        if target:
            return f"{action} {target}" + (f" ×{count}" if count else "")
        return action or "任务"

    @classmethod
    def _task_detail(cls, task: Mapping[str, Any]) -> str:
        result = task.get("result") or {}
        if not isinstance(result, Mapping):
            return ""
        if result.get("error"):
            return f"失败：{_short(result.get('error'), 120)}"
        if result.get("failed"):
            return f"失败在 {result.get('failed')} 这一步"
        done, target = cls._task_count(result)
        if done is not None and target is not None:
            return f"完成 {done}/{target}"
        if result.get("warning"):
            return _short(result.get("warning"), 120)
        if isinstance(result.get("steps"), list):
            return f"连续做了 {len(result['steps'])} 步"
        if result.get("said"):
            return "说了一句"
        return "完成了"

    async def _report_user_task(
        self, friend_id: str, task: Mapping[str, Any]
    ) -> None:
        """TA 交给她的任务完成/失败 → 让她在游戏里回一句（她自己的任务不强制）。"""
        result = task.get("result") or {}
        ok = bool(task.get("ok")) and not (
            isinstance(result, Mapping) and result.get("failed")
        )
        label = self._task_label(
            str(task.get("action") or ""),
            task.get("args") or {},
        )
        detail = self._task_detail(task)
        state_text = "完成" if ok else "没做成"
        hint = (
            f"TA 交给你的任务{state_text}：{label}。"
            + (f"结果：{detail}。" if detail else "")
            + "用一句自然的话告诉 TA；只说话，不要再调用任何游戏动作。"
        )
        await self._proactive_game_line(friend_id, hint)

    async def _analyze_failure(
        self,
        friend_id: str,
        task: Mapping[str, Any],
        *,
        user_visible: bool,
        partial: bool = False,
    ) -> None:
        """失败/只完成一部分 → 让 DeepSeek 先分析原因，再决定下一步。

        这里**不用快脑、不默认重试**：重试只能是分析后的结论。
        """
        label = self._task_label(
            str(task.get("action") or ""),
            task.get("args") or {},
        )
        detail = self._task_detail(task)
        failure_kind = preflight.classify_failure(detail)
        recovery = preflight.recovery_hint(
            failure_kind,
            str(task.get("action") or ""),
            task.get("args") if isinstance(task.get("args"), Mapping) else {},
        )
        history_lines = []
        for item in self._failure_history[-3:]:
            history_lines.append(
                f"- {item.get('action') or '?'} "
                f"{(item.get('args') or {})} → {str(item.get('error') or '')[:100]}"
            )
        history = "\n".join(history_lines)
        state_text = "只完成了一部分" if partial else "失败了"
        hint = (
            f"刚才这一步{state_text}：{label}。\n"
            + (f"结果：{detail}。\n" if detail else "")
            + f"失败类型：{failure_kind}。恢复建议：{recovery}\n"
            + (f"最近失败：\n{history}\n" if history else "")
            + "先分析原因，再决定下一步。**不要原样重试**；"
            "只有你能说清“哪一点变了”才重试。\n"
            "- 格式错误 → 按正确格式改好再试；\n"
            "- 条件不满足（没吃的 / 没工具 / 没装备 / 没材料）→ 先补齐条件或换目标；\n"
            "- 目标不可达 / 附近没找到 → 换地方或换目标；\n"
            "- 策略不对 → 改计划。\n"
            "这一轮安静做事；如果决定放弃，用 game_say 如实告诉 TA。"
        )
        logger.info(
            "失败分析（快脑优先，接不住再 DeepSeek）：{} / {}",
            label,
            detail[:120],
        )
        async with self._chat_lock:
            self._chat_busy = True
            previous_origin = self._turn_origin
            self._turn_origin = "user" if user_visible else "auto"
            try:
                # 决策权归单一 LLM；本地快脑不再独立选择动作。
                acted = await self._run_game_turn(
                    friend_id,
                    {"text": hint},
                    persist_user=False,
                    hint=f"[游戏里] {hint}",
                    quiet=True,
                    deep_thinking_override=False,
                    max_tool_rounds=1,
                )
            finally:
                self._turn_origin = previous_origin
                self._chat_busy = False
                self._last_turn_at = time.monotonic()
        self._drain_pending_chat()
        if user_visible and self.active and not acted and not self._last_turn_said:
            # 她分析完但既没动手也没说话 → 兜底给 TA 一句失败说明。
            await self._report_user_task(friend_id, task)

    async def snapshot(self) -> dict[str, Any]:
        service = self._service
        if service is None or not self.active:
            raise RuntimeError("现在没有进行中的游戏会话")
        return await service.snapshot()

    async def agent_snapshot(self) -> dict[str, Any]:
        """给 `game_status` 工具的轻量快照；只保留决策需要的事实。"""
        snapshot = await self.snapshot()
        keys = (
            "state",
            "player",
            "inventory",
            "held",
            "equipment",
            "tool_guidance",
            "inventory_empty_slots",
            "inventory_full",
            "environment",
            "hazards",
            "nearby_entities",
            "nearby_hostiles",
            "nearby_players",
            "flat_sites",
            "flat_sites_3",
            "tasks",
        )
        result = {
            key: _compact_value(snapshot.get(key))
            for key in keys
            if key in snapshot
        }
        world = snapshot.get("world")
        if isinstance(world, Mapping):
            result["world"] = {}
            for key in ("near", "far"):
                view = world.get(key)
                if not isinstance(view, Mapping):
                    continue
                result["world"][key] = {
                    name: _compact_value(value)
                    for name, value in view.items()
                    if name != "rows"
                }
        return result

    # ---------- prompt / 记忆 ----------

    async def note_for_prompt(self, friend_id: str) -> str:
        """`# 你在游戏里` —— 只有这个朋友正在玩时才注入。"""
        if not self.active_for(friend_id) or self._service is None:
            return ""
        session = self._service.session
        if session is None:
            return ""
        caps = session.capabilities
        snapshot: dict[str, Any] = {}
        try:
            snapshot = await self._service.snapshot()
        except Exception as exc:  # noqa: BLE001 —— 快照失败不该拖垮这一轮
            logger.warning("游戏快照失败（不影响对话）：{}", exc)

        player = snapshot.get("player") or {}
        following = snapshot.get("following")
        raw_tasks = snapshot.get("tasks") or []
        if isinstance(raw_tasks, dict):
            tasks = [{"task_id": key, **value} for key, value in raw_tasks.items()]
        elif isinstance(raw_tasks, list):
            tasks = [item for item in raw_tasks if isinstance(item, dict)]
        else:
            tasks = []
        running = [task for task in tasks if task.get("status") == "running"]
        lines = [
            f"# 你正在和 TA 一起玩 {caps.display_name if caps else self.game_kind}",
            (
                f"状态：{snapshot.get('state', session.state)}"
                f"｜你的角色名：{player.get('name') or '未知'}"
                f"｜血量：{player.get('health', '?')}"
                f"｜坐标：({player.get('x', '?')}, {player.get('y', '?')}, {player.get('z', '?')})"
            ),
        ]
        lines.append(f"当前常态模式：{MODE_LABELS.get(self._mode, self._mode)}")
        if following:
            lines.append(f"你正跟着：{following}")
        # D323：把“身边有谁 / 有没有威胁 / 自己是否处在危险里”放在最前面，
        # 不再埋在一大段方块统计里。最近发生的事件也给出来，供她推理。
        lines.extend(_perception_lines(snapshot, self._last_player_speaker))
        recent = self._recent_perception_text()
        if recent:
            lines.append("刚刚发生：" + recent)
        mode = snapshot.get("game_mode")
        lines.extend(_inventory_lines(snapshot))
        if mode:
            lines.append(
                f"游戏模式：{mode}"
                + ("（创造模式：挖方块不掉落物品）" if mode == "creative" else "")
            )
        environment = snapshot.get("environment") or {}
        if environment:
            bits = []
            if environment.get("dimension"):
                bits.append(f"维度 {environment['dimension']}")
            if environment.get("time_of_day") is not None:
                if environment.get("is_day") is not None:
                    bits.append("白天" if environment["is_day"] else "夜晚")
                bits.append(f"时刻 {environment['time_of_day']}")
            if environment.get("weather"):
                bits.append(f"天气 {environment['weather']}")
            if environment.get("biome"):
                bits.append(f"群系 {environment['biome']}")
            if bits:
                lines.append("环境：" + "｜".join(bits))
        lines.extend(_world_lines(snapshot))
        blocks = snapshot.get("blocks") or {}
        below = (blocks.get("below") or {}).get("name")
        front = (blocks.get("front") or {}).get("name")
        lines.append(f"脚下：{below or '空气'}｜面前：{front or '空气'}")
        looking = snapshot.get("looking_at") or {}
        if looking.get("name"):
            lines.append(
                f"准星对着：{looking['name']}"
                + (
                    f"（{looking['distance']:.1f} 格）"
                    if isinstance(looking.get("distance"), (int, float))
                    else ""
                )
            )
        nearby_blocks = snapshot.get("nearby_blocks") or []
        if nearby_blocks:
            lines.append(
                "周围方块（约 96×96 范围，抽样）："
                + "、".join(
                    f"{item.get('name')}×{item.get('count')}"
                    for item in nearby_blocks[:18]
                    if item.get("name")
                )
            )
        dropped_items = snapshot.get("dropped_items") or []
        if dropped_items:
            lines.append(
                "附近掉落物（12 格内）："
                + "、".join(
                    f"{item.get('name') or 'item'}×{item.get('count') or 1}"
                    f"（{item.get('distance')} 格 @ "
                    f"{item.get('x')}, {item.get('y')}, {item.get('z')}）"
                    for item in dropped_items[:8]
                    if isinstance(item, Mapping)
                )
            )
        terrain = snapshot.get("terrain") or {}
        if terrain:
            if terrain.get("flat"):
                lines.append("周围地形：脚下这一片比较平，适合建造")
            else:
                edges = terrain.get("edges") or []
                drops = [
                    (
                        f"{edge.get('direction')}"
                        + (
                            f"（{edge['distance']} 格）"
                            if edge.get("distance") is not None
                            else ""
                        )
                        + f"落差 {edge.get('dy')} 格"
                    )
                    for edge in edges
                    if isinstance(edge.get("dy"), (int, float)) and abs(edge["dy"]) >= 2
                ]
                headroom = [
                    f"{edge.get('direction')}头顶只有 {edge.get('headroom')} 格"
                    for edge in edges
                    if isinstance(edge.get("headroom"), (int, float))
                    and edge["headroom"] <= 1
                ]
                detail = "；".join([*drops[:2], *headroom[:2]])
                lines.append("周围地形：" + (detail or "有起伏，建造前先找平地"))
        local_grid = _local_grid_text(snapshot)
        if local_grid:
            lines.append("身边 5×5 格子（x,z：地面@y / 脚 / 头）：" + local_grid)
        flat_sites = snapshot.get("flat_sites") or []
        if flat_sites:
            lines.append("附近平坦区域（48 格内，5×5 可落脚）：")
            for site in flat_sites[:3]:
                lines.append(
                    f"- 坐标 ({site.get('x')}, {site.get('y')}, {site.get('z')})"
                    f"，距离约 {site.get('distance')} 格"
                    f"，地面 {site.get('ground') or '未知'}"
                )
        else:
            lines.append(
                "附近 48 格内没找到 5×5 的平地；要活动/铺地基先走到开阔一点的地方。"
            )
        if running:
            now_ms = time.time() * 1000
            running_text = []
            for task in running[:3]:
                action = _short(task.get("action"), 20)
                ends_at = task.get("ends_at")
                if isinstance(ends_at, (int, float)):
                    left = max(0, int(round((ends_at - now_ms) / 1000)))
                    running_text.append(f"{action}（剩 {left}s）")
                else:
                    running_text.append(action)
            lines.append(
                "你手上正在做：" + "、".join(running_text)
            )
        if caps:
            lines.append("你能做：")
            # `command` 是适配器内部动作，只能由 game_cheat 走；不要在这里诱导她 game_do(command)。
            lines.extend(
                line
                for line in caps.prompt_lines()
                if not line.startswith("- command(")
            )
        cheat_config = self._load_cheat_config()
        if cheats.is_active(cheat_config):
            names = cheats.enabled_abilities(cheat_config)
            lines.append(
                "【TA 已手动开启作弊】"
                f"profile={cheat_config['profile']}；可用：{'、'.join(names)}。"
                "这些能力是 TA 给的，不是你自己开的：只有 TA 明确要求，或事情真的卡住且"
                "用普通玩法明显做不到时才用；不要主动建议开更多作弊，也不要谎称自己改过设置。"
            )
        if self._mode == "follow":
            lines.append(
                "模式规则（跟随）：优先完成 TA 的事；做完回到 TA 身边继续跟着。"
                "没人要求时不要自己找活；TA 明确让你做需要多个步骤的事（例如建房子）时，"
                "仍然要建 plan 并执行完，不能因为当前是跟随模式就拒绝做计划。"
                "跟着 TA 时也可以主动提醒危险、提建议、说注意到的事，不是只等指令。"
            )
        elif self._mode == "chat":
            lines.append(
                "模式规则（聊天）：待在 TA 附近（4~6 格），以聊天为主；"
                "没人要求时不要自己找活；TA 明确让你做多步任务时，仍然要建 plan。"
                "可以主动提建议、提醒危险，不用等 TA 先开口。"
            )
        else:
            lines.append(
                "模式规则（自由）：没人指挥时自己定计划、自己推进；"
                "只有这个模式可以自己给自己派活。"
            )
        if self._mode_supported:
            lines.append(
                "切换常态模式用 game_mode(follow/chat/free)；不要频繁切换，"
                "TA 明确要求时才 force=true。"
            )
        lines.append(
            "动作分两层：低层身体动作用 game_do（mine / place / craft / goto …），"
            "它只能在 active plan 里执行；需要结构的建造用 game_build（例如 action=hut）。"
            "工具参数和返回值就是世界反馈，按反馈决定下一步；"
            "如果一时不知道该做什么，就重新看周围、背包、当前 plan 和 TA 刚说的话。"
        )
        lines.append(
            "所有会改变世界的动作都必须先有 active plan：game_plan 承载目标和步骤，"
            "game_do 是 plan 里的执行器；两步以上、需要验收、或失败后要复盘的事，"
            "先建/改 plan 再动手。"
            "game_build 只产出设计稿、材料/选址事实和 plan_tasks，不会自动建 plan；"
            "建不建、怎么建由你决定。"
        )
        lines.append(
            "你是这个世界的玩家，不是执行指令的工具：TA 说的是请求，不是命令。"
            "你可以按自己的性格决定接不接、怎么做，也可以拒绝、吐槽或提条件。"
        )
        if self._mode == "free":
            lines.append(
                " 自由模式不要无故站着：累了就用 rest(seconds)，TA 让你待着就用 wait(seconds)；"
                "系统会在你思考时自动待机巡逻，你只需要继续推进计划。"
            )
        else:
            lines.append(
                " 跟随/聊天时待在 TA 附近是正常的；不要为了“不站着”去自己找活干。"
            )
        active_plans = self._plans.active()
        if active_plans:
            lines.append("你的 plan（plan 不会失败；只有 task 会）：")
            for plan in active_plans:
                lines.append(
                    f"- {plan.id} [{plan.status}]"
                    f" condition：{_short(plan.condition, 160) or '（空）'}"
                    f"｜expected：{_short(plan.expected, 160) or '（空）'}"
                    f"｜result：{_short(plan.result, 160) or '（空）'}"
                )
                for index, task in enumerate(plan.tasks, start=1):
                    lines.append(
                        f"  {index}. [{task.status}] {task.action} {task.args}"
                    )
            lines.append(
                "task 状态就是进度；要改计划用 game_plan。TA 问起 plan 时，"
                "直接根据上面的状态回答，不要只说“我去查查”。"
            )
        elif self._plan:
            lines.append(f"你给自己定的计划：{self._plan}")
            if self._plan_steps:
                lines.append("计划步骤：")
                lines.extend(
                    f"{index}. {_short(step, 120)}"
                    for index, step in enumerate(self._plan_steps, start=1)
                )
            if self._mode == "free":
                lines.append(
                    "推进方式：完成当前这一步后，继续看状态决定下一步；"
                    "计划完成 / 放弃时用 game_plan(done=true) 收尾。"
                )
            else:
                lines.append(
                    "（现在不是自由模式：先不推进这个计划；TA 让你做事时以 TA 的事为先。）"
                )
        elif self._mode == "free":
            lines.append(
                "你还没有给自己定计划。没人指挥你时，先用 game_plan 定一个"
                "有 2~5 个具体步骤的目标，再执行第一步；不要只站着说话。"
            )
        else:
            lines.append(
                "现在不是自由模式：没人要求时不要自己找活；但 TA 明确交给你一个"
                "需要多个动作才能完成的任务时，必须用 game_plan / game_build 建计划，"
                "做完再回到当前模式。"
            )
        active = self._plans.executing()
        skill_query = (
            str(active.expected or active.condition)
            if active is not None
            else (self._plan or str((self._last_task or {}).get("action") or ""))
        )
        try:
            with session_scope() as db:
                matched_skills = skill_store.search_skills(
                    db,
                    friend_id,
                    skill_query,
                    game_kind=self.game_kind,
                    limit=5,
                )
        except Exception as exc:  # noqa: BLE001 —— 技能检索失败不影响这一轮
            logger.warning("技能检索失败（不影响本轮）：{}", exc)
            matched_skills = []
        if matched_skills:
            lines.append("你会的相关技能（只列最相关的 5 条以内；用 game_skill 执行）：")
            for skill in matched_skills:
                params = skill.get("params") or {}
                param_text = (
                    "，参数 " + json.dumps(params, ensure_ascii=False)
                    if params
                    else ""
                )
                lines.append(
                    f"- {skill['name']}：{_short(skill.get('description'), 120)}"
                    f"（{skill.get('source')}{param_text}）"
                )
        lines.append(
            "要查配方或方块所需工具等级，用 game_knowledge(query=recipe_for/tool_tier_for, name=...)，"
            "不要凭记忆猜；它直接读打包的 minecraft-data。"
        )
        lines.append(
            "需要模组能力时先用 game_plugin(action=list) 看有哪些插件/是否可用；"
            "缺模组的插件会被安全跳过，不要反复尝试。"
        )
        if self._last_task:
            task = self._last_task
            ok = "成功" if task.get("ok") else "失败"
            result = task.get("result") or {}
            detail = (
                result.get("error")
                or result.get("warning")
                or result.get("note")
                or ""
            )
            if not detail and isinstance(result.get("placed"), int):
                detail = f"放了 {result['placed']} 块方块"
            if not detail and isinstance(result.get("steps"), list):
                detail = f"连续做了 {len(result['steps'])} 步"
            if not detail:
                done, target = self._task_count(result)
                if done is not None and target is not None:
                    detail = f"{done}/{target}"
            lines.append(
                f"你刚做完的一步：{task.get('action') or '?'}（{ok}）"
                + (f" — {_short(detail, 160)}" if detail else "")
            )
        if self._recent_completed_plans:
            lines.append("最近真正完成的计划（不要再从零重做）：")
            for item in self._recent_completed_plans[-3:]:
                lines.append(
                    f"- {item.get('expected') or item.get('id')}（completed，"
                    f"{item.get('tasks_done')} 个 task）"
                    f"｜{item.get('completed_at')}"
                    + (
                        f"｜{_short(item.get('result'), 160)}"
                        if item.get("result")
                        else ""
                    )
                )
        if self._failure_history:
            lines.append("最近失败（先分析原因，别原样重试）：")
            lines.extend(
                f"- {item.get('action') or '?'} {item.get('args') or {}}"
                f" → {_short(item.get('error'), 120)}"
                for item in self._failure_history[-3:]
            )
        lines.append(
            "（注意：没有「游戏模式」这个 mode；你仍然是 normal / roleplay / tool 之一。"
            "一起玩只是你现在在做的事，不要为了玩游戏去 switch_mode。"
            "游戏里的跟随 / 聊天 / 自由是另一套常态模式，用 game_mode 切换。）"
        )
        recent = self._recent_events[-10:]
        if recent:
            lines.append("最近在游戏里发生：")
            lines.extend(f"- {_short(self._describe_event(event), 140)}" for event in recent)
        lines.append("（这些是游戏里的事实；不用逐条复述，按你的性格自然反应。）")
        return "\n".join(lines)

    def recent_games_note(self, friend_id: str, *, days: int = 14, limit: int = 3) -> str:
        """`# 最近一起玩过` —— 最近 3 局 / 14 天的摘要（硬注入，不靠检索）。"""
        with session_scope() as db:
            rows = game_store.recent_summaries(db, friend_id, days=days, limit=limit)
            lines = []
            for row in rows:
                day = str(row.started_at or "")[:10]
                summary = _short(row.summary, 180)
                if summary:
                    lines.append(f"- {day} · {row.game_kind} · {summary}")
        return "\n".join(lines)

    # ---------- 事件 ----------

    async def _on_event(self, session, event) -> None:  # noqa: ANN001
        if event.type in ("heartbeat", "status"):
            return
        self._recent_events.append(event)
        if len(self._recent_events) > 80:
            del self._recent_events[: len(self._recent_events) - 80]

        try:
            with session_scope() as db:
                game_store.add_event(
                    db,
                    event_id=event.event_id,
                    session_id=session.id,
                    friend_id=session.friend_id,
                    event_type=event.type,
                    payload=event.payload,
                )
                if event.type == "task_done":
                    task_id = str(event.payload.get("task_id") or "")
                    status = "done" if event.payload.get("ok", True) else "failed"
                    updated = game_store.update_task_by_adapter_id(
                        db,
                        session.id,
                        task_id,
                        status=status,
                        result=event.payload.get("result"),
                    )
                    if updated is None:
                        game_store.update_latest_running_task(
                            db,
                            session.id,
                            status=status,
                            result=event.payload.get("result"),
                        )
        except Exception as exc:  # noqa: BLE001 —— 单条事件落库失败不该断整条事件流
            logger.warning("游戏事件落库失败（忽略，不影响事件流）：{}", exc)

        if event.type == "task_done":
            task_id = str(event.payload.get("task_id") or "")
            if task_id:
                done = {
                    "ok": bool(event.payload.get("ok", True)),
                    "result": event.payload.get("result"),
                }
                self._completed_task_results[task_id] = done
                while len(self._completed_task_results) > 200:
                    self._completed_task_results.pop(
                        next(iter(self._completed_task_results))
                    )
                waiter = self._task_waiters.pop(task_id, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(done)
            task = (session.tasks or {}).get(task_id) or {}
            action = str(task.get("action") or "")
            result = event.payload.get("result") or {}
            if action == "patrol":
                if task_id == self._placeholder_task_id:
                    self._placeholder_task_id = None
                return
            self._last_task = {
                "task_id": task_id,
                "action": action,
                "args": dict(task.get("args") or {}),
                "origin": str(task.get("origin") or self._turn_origin or "auto"),
                "ok": bool(event.payload.get("ok", True)),
                "result": dict(result) if isinstance(result, dict) else {"value": result},
            }
            link = self._plan_task_links.pop(task_id, None)
            if link is not None:
                plan_id, plan_index = link
                plan = self._plans.get(plan_id)
                if plan is not None:
                    ok = bool(event.payload.get("ok", True))
                    failed = (not ok) or (
                        isinstance(result, dict)
                        and bool(result.get("error") or result.get("failed"))
                    )
                    error_text = ""
                    if failed and isinstance(result, dict):
                        error_text = str(
                            result.get("error")
                            or result.get("failed")
                            or ""
                        )
                    if failed and await self._auto_repair_plan_task(
                        plan_id,
                        plan_index,
                        plan.tasks[plan_index - 1],
                        error_text or "task 失败",
                    ):
                        return
                    stored_result: Any = result
                    if not failed and plan_index <= len(plan.tasks):
                        task_checks = plan.tasks[plan_index - 1].checks
                        if task_checks:
                            try:
                                snapshot = await self.snapshot()
                                report = postconditions.evaluate_checks(
                                    task_checks, snapshot, result
                                )
                            except Exception as exc:  # noqa: BLE001
                                report = [
                                    {
                                        "type": "snapshot",
                                        "ok": False,
                                        "message": f"后置条件取快照失败：{exc}",
                                    }
                                ]
                            stored_result = {
                                "adapter_result": result,
                                "checks": report,
                            }
                            if not postconditions.passed(report):
                                failed = True
                                error_text = (
                                    "后置条件未通过："
                                    + postconditions.failure_text(report)
                                )
                    self._plans.mark_task(
                        plan_id,
                        plan_index,
                        status="failed" if failed else "success",
                        result=stored_result,
                    )
                    if not failed:
                        self._plan_failure_counts.pop(plan_id, None)
                    if failed:
                        self._plans.pause(plan_id)
                        self._spawn(
                            self._review_plan(
                                session.friend_id,
                                plan_id,
                                reason=(
                                    "postcondition_failed"
                                    if error_text.startswith("后置条件未通过")
                                    else "task_failed"
                                ),
                                task_index=plan_index,
                                error=error_text or "task 失败",
                            )
                        )
                    elif plan.status == "executing":
                        if self._plans.next_pending(plan_id) is None:
                            plan_report: list[dict[str, Any]] = []
                            plan_failed = False
                            if plan.checks:
                                try:
                                    snapshot = await self.snapshot()
                                    plan_report = postconditions.evaluate_checks(
                                        plan.checks, snapshot, result
                                    )
                                    plan_failed = not postconditions.passed(plan_report)
                                except Exception as exc:  # noqa: BLE001
                                    plan_failed = True
                                    plan_report = [
                                        {
                                            "type": "snapshot",
                                            "ok": False,
                                            "message": f"plan 后置条件取快照失败：{exc}",
                                        }
                                    ]
                                plan.result = {
                                    "checks": plan_report,
                                    "verified": not plan_failed,
                                }
                            if not plan_failed and plan.checks:
                                try:
                                    with session_scope() as db:
                                        learned = skill_store.learn_from_plan(
                                            db,
                                            friend_id=session.friend_id,
                                            plan=plan,
                                            game_kind=self.game_kind or "minecraft",
                                        )
                                    if learned is not None:
                                        plan.result = {
                                            **(
                                                plan.result
                                                if isinstance(plan.result, dict)
                                                else {}
                                            ),
                                            "skill_id": learned["id"],
                                            "skill_name": learned["name"],
                                        }
                                        logger.info(
                                            "plan {} 已存成技能 {}",
                                            plan.id,
                                            learned["name"],
                                        )
                                except Exception as exc:  # noqa: BLE001
                                    logger.warning("从 plan 学习技能失败：{}", exc)
                            if plan_failed:
                                self._plans.pause(plan_id)
                                self._spawn(
                                    self._review_plan(
                                        session.friend_id,
                                        plan_id,
                                        reason="postcondition_failed",
                                        error=postconditions.failure_text(
                                            plan_report
                                        ),
                                    )
                                )
                            else:
                                if not isinstance(plan.result, dict):
                                    plan.result = {}
                                if plan.checks:
                                    plan.result = {
                                        **plan.result,
                                        "verified": True,
                                        "tasks_done": len(plan.tasks),
                                    }
                                    self._plans.complete(
                                        plan_id, result=plan.result
                                    )
                                    self._remember_completed_plan(plan)
                                    self._spawn(
                                        self._announce_plan_completion(
                                            session.friend_id, plan_id
                                        )
                                    )
                                else:
                                    # task 跑完 ≠ expected 达成；没有 checks 时
                                    # 做一次短验收，通过后才允许 completed。
                                    self._plan_completion_audits.add(plan_id)
                                    self._spawn(
                                        self._audit_plan_completion(
                                            session.friend_id, plan_id
                                        )
                                    )
                        else:
                            self._spawn(
                                self._execute_next_plan_task(session.friend_id)
                            )
                    return
            if self._internal_task_depth > 0:
                return
            if self._mode_switching:
                return
            if action == "stop":
                # stop 只暂停模式，不换模式；15 秒后看门狗自动恢复。
                self._mode_paused_until = time.monotonic() + MODE_STOP_PAUSE_SECONDS
                return
            if action in ("follow", "say"):
                return
            if isinstance(result, dict) and result.get("cancelled"):
                return
            origin = str(self._last_task.get("origin") or "auto")
            failed = (not self._last_task.get("ok")) or (
                isinstance(result, dict)
                and bool(result.get("error") or result.get("failed"))
            )
            partial = self._is_partial(result) if isinstance(result, dict) else False
            if (failed or partial) and action not in ("rest", "wait"):
                detail = self._task_detail(self._last_task)
                kind = preflight.classify_failure(detail)
                self._failure_history.append(
                    {
                        "action": action,
                        "args": dict(self._last_task.get("args") or {}),
                        "error": detail,
                        "kind": kind,
                        "suggestion": preflight.recovery_hint(
                            kind,
                            action,
                            self._last_task.get("args") or {},
                        ),
                    }
                )
                self._failure_history = self._failure_history[-5:]
                same = sum(
                    1
                    for item in self._failure_history
                    if item.get("action") == action and item.get("error") == detail
                )
                if same >= MAX_SAME_FAILURES:
                    logger.warning(
                        "同一失败连续 {} 次，停止自动分析/重试：{} {} → {}",
                        same,
                        action,
                        self._last_task.get("args"),
                        detail[:100],
                    )
                    if origin == "user":
                        self._spawn(
                            self._report_user_task(
                                session.friend_id, dict(self._last_task)
                            )
                        )
                        if self._mode in ("follow", "chat"):
                            self._schedule_mode_restore()
                    return
                # D282：失败/部分完成不再默认重试——先让 DeepSeek 分析原因再决定。
                self._spawn(
                    self._analyze_failure(
                        session.friend_id,
                        dict(self._last_task),
                        user_visible=(origin == "user"),
                        partial=partial,
                    )
                )
                return
            if origin == "user" and action not in ("rest", "wait"):
                # TA 的任务成功 → 回一句；她自己的任务不强制。
                self._user_task_attempts.pop(task_id, None)
                self._spawn(
                    self._report_user_task(session.friend_id, dict(self._last_task))
                )
                if self._plans.active():
                    if self._plans.executing() is not None:
                        self._spawn(
                            self._execute_next_plan_task(session.friend_id)
                        )
                    return
                elif self._mode == "free":
                    self._schedule_continuation()
                else:
                    self._schedule_mode_restore()
                return
            if self._plans.active():
                # D315：这个 task 不是 plan 直接绑定的（例如 plan 创建时正好有别的
                # 任务在跑）。完成后面先把控制权交回计划执行器，不能开快脑。
                if self._plans.executing() is not None:
                    self._spawn(
                        self._execute_next_plan_task(session.friend_id)
                    )
            elif self._mode in ("follow", "chat"):
                # 任务做完 → 回到常态模式（跟随 / 待在附近聊天）。
                self._schedule_mode_restore()
            else:
                self._schedule_continuation()

        if event.type == "chat":
            # 游戏里有人直接跟她说话 → 走**完整工具循环**（能 follow / mine / say），
            # 而不是只发一句主动消息（那样她没法执行指令）。
            self._last_player_speaker = str(event.payload.get("from") or "").strip() or None
            self._cancel_continuation()
            self._spawn(self._respond_to_game_chat(session.friend_id, event.payload))
        elif event.type in ("incident", "incident_cleared"):
            self._spawn(self._handle_incident_event(session.friend_id, event))
        else:
            self._maybe_initiative(session.friend_id, event)

    def _describe_event(self, event) -> str:  # noqa: ANN001
        payload = event.payload or {}
        if event.type == "chat":
            return f"{payload.get('from') or 'TA'} 说：{payload.get('text') or ''}"
        if event.type == "death":
            return f"你死了（{payload.get('cause') or '未知'}）"
        if event.type == "damage":
            source = payload.get("source")
            extra = ""
            if isinstance(source, Mapping):
                extra = "，受伤时附近有：" + _entity_fact_text(source)
            return f"你受到 {payload.get('amount')} 点伤害{extra}"
        if event.type == "task_done":
            ok = "完成" if payload.get("ok") else "失败"
            return f"任务 {payload.get('task_id')} {ok}"
        if event.type == "player_near":
            return "玩家靠近：" + _entity_fact_text(
                {**payload, "name": payload.get("player")}
            )
        if event.type == "player_far":
            return f"玩家 {payload.get('player')} 走远了（{payload.get('distance')} 格）"
        if event.type == "hostile_near":
            return "敌对生物靠近：" + _entity_fact_text(payload)
        if event.type == "hostile_far":
            return f"敌对生物 {payload.get('name') or '?'} 离开了附近"
        if event.type == "inventory_change":
            added = "、".join(
                f"{item.get('item')}×{item.get('count')}"
                for item in (payload.get("added") or [])
                if isinstance(item, Mapping)
            )
            removed = "、".join(
                f"{item.get('item')}×{item.get('count')}"
                for item in (payload.get("removed") or [])
                if isinstance(item, Mapping)
            )
            return "背包变化：" + "；".join(
                bit for bit in (f"增加 {added}" if added else "", f"减少 {removed}" if removed else "") if bit
            )
        if event.type == "reflex":
            return f"身体本能：{payload.get('mode') or '?'} / {payload.get('detail') or ''}"
        if event.type == "incident":
            kind = payload.get("incident_kind") or "?"
            oxygen = payload.get("oxygen")
            health = payload.get("health")
            return (
                f"突发事故：{kind}"
                f"（氧气 {oxygen if oxygen is not None else '?'}"
                f"，血量 {health if health is not None else '?'}）"
            )
        if event.type == "incident_cleared":
            return f"事故解除：{payload.get('incident_kind') or '?'}"
        return event.type

    def _recent_perception_text(self, limit: int = 5) -> str:
        """最近发生的感知事件，按时间正序；忽略高频心跳和任务流水。"""
        wanted = {
            "chat",
            "damage",
            "death",
            "player_near",
            "player_far",
            "hostile_near",
            "hostile_far",
            "inventory_change",
            "reflex",
            "incident",
            "incident_cleared",
        }
        items = [
            self._describe_event(event)
            for event in self._recent_events
            if getattr(event, "type", "") in wanted
        ]
        return "；".join(items[-limit:])

    def _maybe_initiative(self, friend_id: str, event) -> None:  # noqa: ANN001
        """D327：高价值事件唤醒一轮思考；不是自动反射。"""
        if not self.active:
            return
        payload = event.payload or {}
        critical = False
        eligible = False
        if event.type == "damage":
            eligible = True
            critical = True
        elif event.type == "death":
            eligible = True
            critical = True
        elif event.type == "hostile_near":
            distance = payload.get("distance")
            eligible = True
            critical = isinstance(distance, (int, float)) and distance <= 6
        elif event.type == "player_near":
            eligible = True
        elif event.type == "reflex":
            # 落水/着火的自保是身体反射，本身已经在处理；不要再把 plan 暂停、
            # 也不要每 8 秒叫一次慢脑。只有低血撤退这种真正危险才唤醒思考。
            eligible = (
                str(payload.get("mode") or "") == "self_preservation"
                and str(payload.get("action") or "") == "retreat"
            )
            critical = eligible
        if not eligible:
            return
        busy = self._chat_busy or bool(self._pending_user_chat)
        if busy:
            # D328：思考期间不排队、不累积；只留一个“忙完后要重看”的脏标记。
            if critical:
                self._attention_dirty = True
                due = time.monotonic() + 1.0
                if self._next_idle_think_at <= 0 or due < self._next_idle_think_at:
                    self._next_idle_think_at = due
            return
        if self._plans.executing() is not None and not critical:
            return
        now = time.monotonic()
        cooldown = 8.0 if critical else max(0.0, self._comment_throttle)
        if now < self._initiative_cooldown_until:
            return
        self._initiative_cooldown_until = now + cooldown
        self._next_idle_think_at = now + AUTONOMY_BUFFER_SECONDS
        self._spawn(self._initiative_turn(friend_id, event, critical=critical))

    def _incident_has_progress(
        self,
        previous: Mapping[str, Any] | None,
        current: Mapping[str, Any],
    ) -> bool:
        if not previous:
            return False
        prev_pos = previous.get("position")
        curr_pos = current.get("position")
        if isinstance(prev_pos, Mapping) and isinstance(curr_pos, Mapping):
            try:
                moved = (
                    (float(curr_pos["x"]) - float(prev_pos["x"])) ** 2
                    + (float(curr_pos["y"]) - float(prev_pos["y"])) ** 2
                    + (float(curr_pos["z"]) - float(prev_pos["z"])) ** 2
                ) ** 0.5
                if moved >= 0.8:
                    return True
            except (KeyError, TypeError, ValueError):
                pass
        try:
            if float(current.get("oxygen") or 0) > float(previous.get("oxygen") or 0) + 10:
                return True
            if float(current.get("health") or 0) > float(previous.get("health") or 0) + 1:
                return True
        except (TypeError, ValueError):
            pass
        if previous.get("in_water") and not current.get("in_water"):
            return True
        if previous.get("in_lava") and not current.get("in_lava"):
            return True
        if previous.get("on_fire") and not current.get("on_fire"):
            return True
        return False

    def _incident_fact_text(self, payload: Mapping[str, Any]) -> str:
        kind = str(payload.get("incident_kind") or "?")
        labels = {
            "water": "落水",
            "lava": "掉进岩浆",
            "fire": "着火",
            "low_health": "血量过低",
        }
        position = payload.get("position")
        position = position if isinstance(position, Mapping) else {}
        parts = [
            f"事故：{labels.get(kind, kind)}",
            (
                f"位置：({position.get('x', '?')}, "
                f"{position.get('y', '?')}, "
                f"{position.get('z', '?')})"
            ),
            f"血量：{payload.get('health', '?')}｜氧气：{payload.get('oxygen', '?')}",
            (
                "状态："
                f"水={bool(payload.get('in_water'))} "
                f"岩浆={bool(payload.get('in_lava'))} "
                f"着火={bool(payload.get('on_fire'))}"
            ),
        ]
        shore = payload.get("nearest_shore")
        if isinstance(shore, Mapping):
            parts.append(
                f"最近岸边：({shore.get('x')},{shore.get('y')},{shore.get('z')})"
                f"，距离 {shore.get('distance')}"
            )
        player = payload.get("nearest_player")
        if isinstance(player, Mapping):
            parts.append(
                f"最近玩家：{player.get('name') or '?'}"
                f"（{player.get('x')},{player.get('y')},{player.get('z')}）"
                f"，距离 {player.get('distance')}，"
                f"水里={bool(player.get('in_water'))}"
            )
        task = payload.get("active_task")
        if isinstance(task, Mapping):
            parts.append(
                f"当前任务：{task.get('action') or '?'} ({task.get('task_id') or '?'})"
            )
        parts.append(
            f"手持：{payload.get('held') or '空手'}"
            f"｜有船：{bool(payload.get('has_boat'))}"
            f"｜可用垫脚方块：{payload.get('block_count', 0)}"
        )
        if self._incident_actions:
            recent = "；".join(
                f"{item.get('action') or '?'}→"
                f"{'成功' if item.get('ok') else '失败'}"
                + (f"（{_short(item.get('error'), 60)}）" if item.get("error") else "")
                for item in self._incident_actions[-3:]
            )
            parts.append(f"最近尝试：{recent}")
        return "；".join(parts)

    async def _handle_incident_event(self, friend_id: str, event: Any) -> None:
        if not self.active:
            return
        payload = dict(event.payload or {})
        incident_id = str(payload.get("incident_id") or "incident")
        if event.type == "incident_cleared":
            previous = self._active_incident or {}
            if not previous or previous.get("incident_id") == incident_id:
                self._active_incident = None
                self._incident_attempts = 0
                self._incident_last_llm_at = 0.0
                self._incident_fallback_used = False
                self._incident_actions = []
                task = self._incident_task
                if task is not None and not task.done():
                    task.cancel()
                self._incident_task = None
            plan_id = self._incident_plan_id
            self._incident_plan_id = None
            resumed = False
            if plan_id and not self._plan_stop_hold:
                plan = self._plans.get(plan_id)
                if plan is not None and plan.status == "paused":
                    self._plans.resume(plan_id)
                    self._spawn(self._execute_next_plan_task(friend_id))
                    resumed = True
                    logger.info("事故解除：自动恢复 plan {}", plan_id)
            if not resumed and self._mode in ("follow", "chat"):
                self._schedule_mode_restore()
            return
        if str(payload.get("incident_kind") or "") not in {
            "water",
            "lava",
            "fire",
            "low_health",
        }:
            return
        previous = self._active_incident
        same_incident = bool(
            previous and previous.get("incident_id") == incident_id
        )
        progress = self._incident_has_progress(previous, payload)
        if not same_incident:
            self._incident_attempts = 0
            self._incident_last_llm_at = 0.0
            self._incident_fallback_used = False
            self._incident_actions = []
            if self._plans.executing() is not None:
                self._incident_plan_id = await self._interrupt_plan_for_danger()
        elif progress:
            self._incident_attempts = 0
            self._incident_last_llm_at = time.monotonic()
            self._incident_fallback_used = False
        self._active_incident = payload
        async with self._incident_lock:
            task = self._incident_task
            if task is not None and not task.done():
                return
            if self._has_running_task():
                return
            now = time.monotonic()
            if now - self._incident_last_llm_at < INCIDENT_LLM_COOLDOWN:
                return
            if self._incident_attempts >= INCIDENT_MAX_ATTEMPTS:
                if not self._incident_fallback_used:
                    self._incident_fallback_used = True
                    self._spawn(self._incident_fallback(friend_id, payload))
                return
            self._incident_last_llm_at = now
            self._incident_attempts += 1
            self._incident_task = asyncio.create_task(
                self._incident_turn(friend_id, payload),
                name="game-incident-response",
            )

    async def _incident_turn(
        self, friend_id: str, payload: Mapping[str, Any]
    ) -> None:
        self._incident_response_active = True
        try:
            fact = self._incident_fact_text(payload)
            hint = (
                f"[紧急事故] {fact}\n"
                "这不是普通计划时间，先保命。只选择一个最可能让你脱离危险的动作。"
                "如果最近的玩家安全且看起来可达，优先 goto 到玩家身边；"
                "否则再考虑 goto 最近岸边 / escape / pillar / place 垫脚 / 坐船。"
                "不要调用 game_plan，不要复盘，不要长篇解释；动作成功或已经安全就停止。"
            )
            await self._run_game_turn(
                friend_id,
                {"text": hint},
                persist_user=False,
                hint=hint,
                quiet=True,
                deep_thinking_override=False,
                max_tool_rounds=1,
            )
            task = self._last_task or {}
            if task.get("action"):
                self._incident_actions.append(
                    {
                        "action": task.get("action"),
                        "ok": bool(task.get("ok")),
                        "error": self._task_detail(task),
                    }
                )
                self._incident_actions = self._incident_actions[-5:]
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("事故监督器思考失败（已吞掉）：{}", exc)
        finally:
            self._incident_response_active = False
            current = asyncio.current_task()
            if self._incident_task is current:
                self._incident_task = None

    async def _incident_fallback(
        self, friend_id: str, payload: Mapping[str, Any]
    ) -> None:
        player = payload.get("nearest_player")
        use_player = (
            isinstance(player, Mapping)
            and not player.get("in_water")
            and not player.get("in_lava")
            and not player.get("on_fire")
            and isinstance(player.get("distance"), (int, float))
            and float(player.get("distance")) <= 48
        )
        action = "goto" if use_player else "escape"
        args = (
            {
                "x": player.get("x"),
                "y": player.get("y"),
                "z": player.get("z"),
            }
            if use_player
            else {}
        )
        logger.warning(
            "事故监督器多次未脱困，执行最后一次保底 {}",
            action,
        )
        try:
            result = await self.command(action, args, source="auto")
            task_id = str(result.get("task_id") or "")
            if task_id:
                await self._wait_task_result(task_id, 20.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("保底 {} 失败：{}", action, exc)
        try:
            await self._quick_game_line(
                friend_id,
                fact=(
                    f"你还在危险里：{self._incident_fact_text(payload)}。"
                    "你已经尝试了多次自动脱困但还没有完全成功。"
                ),
                instruction=(
                    "用一句自然的话告诉 TA 你现在卡在哪里、正在尝试脱困；"
                    "不要假装已经安全，也不要继续承诺做不到的事。"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("事故保底汇报失败：{}", exc)

    async def _interrupt_plan_for_danger(self) -> str | None:
        """危险事件下把当前 plan task 放回 pending，处理完后再从原计划继续。"""
        plan = self._plans.executing()
        if plan is None:
            return None
        for index, task in enumerate(plan.tasks, start=1):
            if task.status != "executing":
                continue
            for adapter_id, link in list(self._plan_task_links.items()):
                if link == (plan.id, index):
                    self._plan_task_links.pop(adapter_id, None)
            self._plans.mark_task(plan.id, index, status="pending", result=None)
            break
        self._plans.pause(plan.id)
        self._cancel_continuation()
        await self._cancel_real_tasks()
        logger.warning("危险事件唤醒思考：plan {} 已暂停，处理完会继续", plan.id)
        return plan.id

    async def _interrupt_plan_for_user(self) -> str | None:
        """TA 开口时，先把正在执行的那一步放回 pending，再暂停整个 plan。"""
        plan = self._plans.executing()
        if plan is None:
            return None
        for index, task in enumerate(plan.tasks, start=1):
            if task.status != "executing":
                continue
            for adapter_id, link in list(self._plan_task_links.items()):
                if link == (plan.id, index):
                    self._plan_task_links.pop(adapter_id, None)
            self._plans.mark_task(plan.id, index, status="pending", result=None)
            break
        self._plans.pause(plan.id)
        self._cancel_continuation()
        await self._cancel_real_tasks()
        logger.info("TA 开口：plan {} 已暂停，当前动作已让位", plan.id)
        return plan.id

    async def _preempt_for_user(self, text: str) -> str | None:
        """用户消息抢占：停掉当前认知/自动复盘/世界动作，先听 TA 说。"""
        self._last_user_activity_at = time.monotonic()
        self._chat_until = time.monotonic() + CHAT_GRACE_SECONDS
        self._autonomy_no_progress = 0
        self._next_idle_think_at = time.monotonic() + AUTONOMY_BUFFER_SECONDS

        if _looks_like_resume_request(text):
            self._plan_stop_hold = None

        plan_id = await self._interrupt_plan_for_user()
        await self._cancel_real_tasks()
        if _looks_like_stop_request(text):
            active = self._plans.active()
            self._plan_stop_hold = (
                plan_id
                or self._plan_stop_hold
                or (active[0].id if active else None)
            )
            logger.info("TA 明确叫停：plan 保持在 paused，不自动恢复")

        self._cancel_continuation()
        self._cancel_autonomy()
        task = self._active_turn_task
        current = asyncio.current_task()
        if task is not None and not task.done() and task is not current:
            self._user_preempting = True
            task.cancel()
            try:
                await asyncio.gather(task, return_exceptions=True)
            finally:
                self._user_preempting = False
        return plan_id

    def _resume_after_user_interrupt(
        self, friend_id: str, plan_id: str | None
    ) -> None:
        """用户只是插话而不是叫停时，答完自动把刚才让位的计划接回去。"""
        if not plan_id or self._plan_stop_hold:
            return
        if self._plans.executing() is not None:
            return
        plan = self._plans.get(plan_id)
        if plan is None or plan.status != "paused":
            return
        self._plans.resume(plan.id)
        self._spawn(self._execute_next_plan_task(friend_id))
        logger.info("TA 的话已处理：自动恢复 plan {}", plan.id)

    def _remember_completed_plan(self, plan: Any) -> None:
        summary = {
            "id": getattr(plan, "id", ""),
            "expected": _short(getattr(plan, "expected", ""), 160),
            "tasks_done": sum(
                1
                for task in getattr(plan, "tasks", [])
                if getattr(task, "status", "") == "success"
            ),
            "result": _short(getattr(plan, "result", ""), 260),
            "completed_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        self._recent_completed_plans = [
            item
            for item in self._recent_completed_plans
            if item.get("id") != summary["id"]
        ][-2:] + [summary]
        logger.info(
            "plan {} 已完成：{} 个 task；记住完成事实避免下一轮重做",
            summary["id"],
            summary["tasks_done"],
        )

    async def _announce_plan_completion(
        self, friend_id: str, plan_id: str
    ) -> None:
        """计划完成后的唯一一次 LLM 调用：只说结果，不重新复盘。"""
        plan = self._plans.get(plan_id)
        if plan is None:
            return
        completed = next(
            (
                item
                for item in self._recent_completed_plans
                if item.get("id") == plan_id
            ),
            None,
        )
        fact = (
            f"计划「{_short(plan.expected, 160)}」已经完成。"
            f"全部 {len(plan.tasks)} 个 task 成功，plan checks 通过。"
        )
        if completed:
            fact += f" 完成记录：{completed.get('result') or '无额外结果'}。"
        await self._quick_game_line(
            friend_id,
            fact=fact,
            instruction=(
                "用符合人格的一两句自然的话告诉 TA：这件事已经做完了。"
                "不要重新检查、不要重新规划、不要说还在做，也不要调用工具。"
            ),
        )

    async def _plan_completion_evidence(self, plan: Any) -> str:
        """给完成验收的紧凑事实：计划本身 + 可核对的落块坐标。"""
        lines = [
            f"plan：{plan.id}｜status={plan.status}",
            f"condition：{_short(plan.condition, 180) or '（空）'}",
            f"expected：{_short(plan.expected, 220) or '（空）'}",
            "tasks：",
        ]
        place_checks: list[dict[str, Any]] = []
        for index, task in enumerate(plan.tasks, start=1):
            args = _short(
                json.dumps(task.args, ensure_ascii=False, sort_keys=True),
                220,
            )
            result = _short(
                json.dumps(task.result, ensure_ascii=False, sort_keys=True)
                if isinstance(task.result, (dict, list))
                else task.result,
                160,
            )
            lines.append(
                f"{index}. [{task.status}] {task.action} {args}"
                + (f" → {result}" if result else "")
            )
            if task.action == "place":
                args_map = task.args if isinstance(task.args, Mapping) else {}
                try:
                    place_checks.append(
                        {
                            "x": int(float(args_map["x"])),
                            "y": int(float(args_map["y"])),
                            "z": int(float(args_map["z"])),
                            "block": str(args_map.get("block") or ""),
                        }
                    )
                except (KeyError, TypeError, ValueError):
                    pass
        if place_checks:
            try:
                self._internal_task_depth += 1
                try:
                    blocks = await self._inspect_positions(
                        [
                            {"x": item["x"], "y": item["y"], "z": item["z"]}
                            for item in place_checks[:512]
                        ]
                    )
                finally:
                    self._internal_task_depth = max(
                        0, self._internal_task_depth - 1
                    )
                observed = {
                    (
                        int(float(item["x"])),
                        int(float(item["y"])),
                        int(float(item["z"])),
                    ): str(item.get("name") or "")
                    for item in blocks
                    if all(axis in item for axis in ("x", "y", "z"))
                }
                matched = sum(
                    1
                    for item in place_checks
                    if observed.get((item["x"], item["y"], item["z"]))
                    == item["block"]
                )
                lines.append(
                    f"落块证据：{matched}/{len(place_checks)} 个 place 目标"
                    "当前确实是计划要求的方块。"
                )
                if matched < len(place_checks):
                    missing = [
                        item
                        for item in place_checks
                        if observed.get((item["x"], item["y"], item["z"]))
                        != item["block"]
                    ][:8]
                    lines.append(
                        "未对上：" + json.dumps(missing, ensure_ascii=False)
                    )
            except Exception as exc:  # noqa: BLE001
                lines.append(f"落块证据读取失败：{exc}")
        return "\n".join(lines)

    async def _audit_plan_completion(
        self, friend_id: str, plan_id: str
    ) -> None:
        """所有 task 跑完但无 checks 时，做一次短验收；完成才允许收口。"""
        plan = self._plans.get(plan_id)
        if plan is None:
            self._plan_completion_audits.discard(plan_id)
            return
        active_task = asyncio.current_task()
        parser_note = ""
        parsed: dict[str, Any] | None = None
        try:
            evidence = await self._plan_completion_evidence(plan)
            async with self._chat_lock:
                self._active_turn_task = active_task
                self._chat_busy = True
                try:
                    with session_scope() as db:
                        friend = db.get(Friend, friend_id)
                        if friend is None:
                            return
                        provider = self._provider_factory(db, friend)
                        history = message_service.recent_for_context(
                            db, friend_id, limit=6
                        )
                        action_names: list[str] = []
                        caps = self._service.session.capabilities if self._service else None
                        if caps is not None:
                            action_names = sorted(caps.action_names())
                        messages: list[dict[str, Any]] = [
                            {
                                "role": "system",
                                "content": (
                                    f"你是「{friend.name}」。\n"
                                    f"{friend.personality or ''}\n"
                                    f"{friend.custom_instructions or ''}\n\n"
                                    "# 计划完成验收\n"
                                    "计划的所有 task 已经执行完，但“task 跑完”不等于"
                                    "“expected 达到”。你要根据 expected、task 和真实世界证据"
                                    "判断是否真的完成。只输出一个 JSON 对象，不要 markdown：\n"
                                    '{"complete":true|false,"reason":"简短原因",'
                                    '"missing":"还缺什么","next_tasks":[{"action":"...",'
                                    '"args":{...}}],"say":"给 TA 的一两句自然话"}\n'
                                    "如果 expected 是房子/墙/屋顶/结构，而 task 只覆盖地板、"
                                    "一面墙或一块方块，必须判 complete=false，并给出缺失任务。\n"
                                    f"可用 action：{', '.join(action_names)}。"
                                    "complete=true 时 say 必须是用她口吻告诉 TA 真正做完了。"
                                ),
                            }
                        ]
                        for message in history[-6:]:
                            role = str(getattr(message, "role", "") or "")
                            content = str(getattr(message, "content", "") or "").strip()
                            if role in ("user", "assistant") and content:
                                messages.append(
                                    {"role": role, "content": content[:400]}
                                )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"{evidence}\n\n"
                                    "请只做完成验收，不要重新规划整个目标；"
                                    "只有真的达到 expected 才 complete=true。"
                                ),
                            }
                        )
                        if db.in_transaction():
                            db.commit()
                    chunks: list[str] = []
                    async for event in provider.chat_stream(
                        messages,
                        max_tokens=700,
                        temperature=0.2,
                        usage_purpose="game",
                    ):
                        if event.get("type") == "content":
                            chunks.append(str(event.get("delta") or ""))
                        elif event.get("type") == "done":
                            break
                    raw = "".join(chunks).strip()
                    start = raw.find("{")
                    end = raw.rfind("}")
                    if start >= 0 and end > start:
                        try:
                            parsed = json.loads(raw[start : end + 1])
                        except json.JSONDecodeError:
                            parser_note = "验收 JSON 解析失败"
                    else:
                        parser_note = "验收没有返回 JSON"
                finally:
                    if self._active_turn_task is active_task:
                        self._active_turn_task = None
                    self._chat_busy = False
                    self._last_turn_at = time.monotonic()
        except asyncio.CancelledError:
            self._plan_completion_audits.discard(plan_id)
            raise
        except Exception as exc:  # noqa: BLE001
            parser_note = f"完成验收失败：{exc}"
        finally:
            self._plan_completion_audits.discard(plan_id)

        plan = self._plans.get(plan_id)
        if plan is None:
            return
        if parsed is None:
            if plan.status == "executing":
                self._plans.pause(plan_id)
            self._spawn(
                self._review_plan(
                    friend_id,
                    plan_id,
                    reason="completion_incomplete",
                    error=parser_note or "完成验收没有结论",
                )
            )
            return
        complete = bool(parsed.get("complete"))
        reason = _short(parsed.get("reason"), 200)
        missing = _short(parsed.get("missing"), 200)
        say = _clean_game_text(str(parsed.get("say") or ""))
        if plan.status != "executing":
            return
        if complete:
            if not isinstance(plan.result, dict):
                plan.result = {}
            plan.result = {
                **plan.result,
                "verified": True,
                "tasks_done": len(plan.tasks),
                "audit": {"reason": reason, "verified_by": "llm_completion_audit"},
            }
            self._plans.complete(plan_id, result=plan.result)
            self._remember_completed_plan(plan)
            if say:
                await self._persist_game_message(
                    friend_id, "assistant", f"[游戏] {say}"
                )
                await self.command("say", {"text": say[:200]}, source="agent")
            else:
                self._spawn(
                    self._announce_plan_completion(friend_id, plan_id)
                )
            return

        next_tasks = parsed.get("next_tasks")
        if isinstance(next_tasks, list) and next_tasks:
            try:
                self.plan_action(
                    "add_tasks",
                    plan_id=plan_id,
                    tasks=next_tasks,
                    before=len(plan.tasks) + 1,
                )
                logger.info(
                    "plan {} 完成验收不通过，补 {} 个 task 后继续：{}",
                    plan_id,
                    len(next_tasks),
                    reason or missing,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("完成验收补 task 失败：{}", exc)
        self._plans.pause(plan_id)
        self._spawn(
            self._review_plan(
                friend_id,
                plan_id,
                reason="completion_incomplete",
                error=missing or reason or "task 跑完但 expected 未达到",
            )
        )

    async def _initiative_turn(
        self, friend_id: str, event, *, critical: bool  # noqa: ANN001
    ) -> None:
        """事件触发的主动思考：她可以提醒、战斗、逃跑、建议，或不说话。"""
        if not self.active or self._chat_busy or self._pending_user_chat:
            return
        async with self._chat_lock:
            self._chat_busy = True
            previous_origin = self._turn_origin
            self._turn_origin = "auto"
            resume_plan_id: str | None = None
            try:
                if critical and self._plans.active():
                    resume_plan_id = await self._interrupt_plan_for_danger()
                happened = self._describe_event(event)
                plan_text = self._plan_snapshot_text()
                mode_text = MODE_LABELS.get(self._mode, self._mode)
                hint = (
                    f"刚刚发生：{happened}。当前模式：{mode_text}。"
                    f"当前 plan：{plan_text}。"
                    "这不是反射，不需要机械动作；你自己判断要不要说话或动手。"
                    "如果危险，可以用 game_do 攻击/escape/eat，或先躲开；"
                    "如果 TA 在旁边，也可以提醒、吐槽、提建议。"
                    "如果正在做长期 plan 且这件事不值得停手，就继续你的计划。"
                    "不需要说话时不要硬说。"
                )
                await self._run_game_turn(
                    friend_id,
                    {"text": hint},
                    persist_user=False,
                    hint=f"[游戏里] {hint}",
                    quiet=True,
                    deep_thinking_override=False,
                    max_tool_rounds=2,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("事件唤醒思考失败（已吞掉）：{}", exc)
            finally:
                self._turn_origin = previous_origin
                self._chat_busy = False
                self._last_turn_at = time.monotonic()
                if resume_plan_id:
                    plan = self._plans.get(resume_plan_id)
                    if plan is not None and plan.status == "paused":
                        self._plans.resume(resume_plan_id)
                        self._spawn(
                            self._execute_next_plan_task(friend_id)
                        )
                self._next_idle_think_at = (
                    time.monotonic() + AUTONOMY_BUFFER_SECONDS
                )
                if self._attention_dirty:
                    self._attention_dirty = False
                    if not self._plans.active():
                        self._next_idle_think_at = time.monotonic() + 1.0
                self._drain_pending_chat()

    async def _respond_to_game_chat(
        self,
        friend_id: str,
        payload: Mapping[str, Any],
        *,
        persist_user: bool = True,
        hint: str | None = None,
        quiet: bool = False,
    ) -> None:
        """游戏里有人说话（或主动冒泡）→ 跑一轮**带工具的**回复，并确保在游戏里说出来。"""
        sender = str(payload.get("from") or "").strip()
        text = str(payload.get("text") or "").strip()
        if hint is None and not text:
            return
        interrupted_plan_id: str | None = None
        if persist_user and sender:
            self._last_player_speaker = sender
        if persist_user:
            # 用户消息优先：先抢占正在跑的认知/计划，不把 TA 的话塞在队尾等。
            pending_texts: list[str] = []
            remaining: list[dict[str, Any]] = []
            for item in self._pending_user_chat:
                if item.get("friend_id") == friend_id:
                    pending_text = str(
                        (item.get("payload") or {}).get("text") or ""
                    ).strip()
                    if pending_text:
                        pending_texts.append(pending_text)
                else:
                    remaining.append(item)
            self._pending_user_chat = remaining
            if pending_texts:
                text = "\n".join([*pending_texts, text]).strip()
                payload = {**dict(payload), "text": text}

            if (
                self._chat_busy
                and self._turn_origin == "user"
                and not _looks_like_stop_request(text)
            ):
                # 上一条也是 TA 的话：只合并排队，不取消上一轮用户消息。
                self._pending_user_chat.append(
                    {
                        "friend_id": friend_id,
                        "payload": dict(payload),
                        "hint": hint,
                    }
                )
                return
            if (
                self._chat_busy
                and self._turn_origin == "user"
                and _looks_like_stop_request(text)
            ):
                active_text = (self._active_user_message_text or "").strip()
                if active_text and active_text not in text:
                    text = f"{active_text}\n{text}"
                    payload = {**dict(payload), "text": text}

            interrupted_plan_id = await self._preempt_for_user(text)
            self._attention_dirty = False

            if _looks_like_stop_request(text):
                if text:
                    await self._persist_game_message(
                        friend_id, "user", f"[游戏] {sender}：{text}"
                    )
                await self._quick_game_line(
                    friend_id,
                    fact=(
                        f"TA 在游戏里说：「{text}」。"
                        "你已经立刻停下当前动作，并把正在进行的计划暂停了。"
                    ),
                    instruction=(
                        "用符合人格的一两句自然回应 TA，确认你听到了、已经停下；"
                        "不要继续动作，不要重新规划，不要假装还在建。"
                    ),
                )
                self._drain_pending_chat()
                return

            if self._chat_busy:
                # 连续发来的几句话合并成一轮，别一句一轮地排队（否则会"蹦出几条"）。
                if (
                    self._pending_user_chat
                    and self._pending_user_chat[-1].get("friend_id") == friend_id
                ):
                    last = self._pending_user_chat[-1]
                    last_text = str((last.get("payload") or {}).get("text") or "")
                    last["payload"]["text"] = (
                        f"{last_text}\n{text}" if last_text else text
                    )
                else:
                    self._pending_user_chat.append(
                        {
                            "friend_id": friend_id,
                            "payload": dict(payload),
                            "hint": hint,
                        }
                    )
                return
        elif self._chat_busy:
            # 主动搭话 / 死亡反应不跟续跑抢
            return

        async with self._chat_lock:
            self._chat_busy = True
            previous_origin = self._turn_origin
            self._turn_origin = "user" if persist_user else "auto"
            try:
                plan_was_interrupted = bool(self._plans.active())
                extra_user_note = (
                    (
                        "TA 刚开口，你主动把计划暂停了。先回应 TA；"
                        "如果只是补充信息、事情还要继续，你可以用 game_plan(resume) 继续；"
                        "如果 TA 让你停，就保持 paused。"
                    )
                    if plan_was_interrupted
                    else None
                )
                await self._run_game_turn(
                    friend_id,
                    payload,
                    persist_user=persist_user,
                    hint=hint,
                    quiet=quiet,
                    # 游戏聊天优先响应：不在聊天回合里跑长思考；失败分析轮才开思考。
                    deep_thinking_override=False,
                    extra_user_note=extra_user_note,
                )
            finally:
                self._turn_origin = previous_origin
                self._chat_busy = False
                self._last_turn_at = time.monotonic()
                if persist_user:
                    self._resume_after_user_interrupt(
                        friend_id, interrupted_plan_id
                    )
                if (
                    self._mode in ("follow", "chat")
                    and self._plans.executing() is None
                ):
                    # 聊天回合结束后尽快回到跟随/聊天常态；paused plan 不挡路。
                    self._schedule_mode_restore(0.2)
                self._next_idle_think_at = (
                    time.monotonic() + AUTONOMY_BUFFER_SECONDS
                )
                # D286：她问 plan 状态却只回"我去查查"就断了 → 补一轮把事实说出来。
                pending = self._plan_answer_pending
                self._plan_answer_pending = None
                if pending == friend_id:
                    self._spawn(
                        self._proactive_game_line(
                            friend_id,
                            "TA 刚问你 plan 的事。事实："
                            f"{self._plan_snapshot_text()}。"
                            "现在就用 game_say 直接回答 TA：在不在、什么状态、做到哪；"
                            "不要说“我去查查”，别让 TA 等。",
                        )
                    )
                nudge = self._plan_nudge_pending
                self._plan_nudge_pending = None
                if nudge == friend_id:
                    self._spawn(
                        self._proactive_game_line(
                            friend_id,
                            "TA 刚交给你一件需要动手的事，但你只回了文字，没有创建 plan，"
                            "也没有执行任何世界动作。现在必须用 game_plan 创建或更新计划，"
                            "至少列出第一步并开始；如果这件事不该做，就明确说明原因，"
                            "不要只聊天。",
                        )
                    )
                self._drain_pending_chat()

    async def _run_game_turn(
        self,
        friend_id: str,
        payload: Mapping[str, Any],
        *,
        persist_user: bool,
        hint: str | None,
        quiet: bool = False,
        deep_thinking_override: bool | None = None,
        max_tool_rounds: int | None = None,
        extra_user_note: str | None = None,
    ) -> bool:
        """跑一轮带工具的回复；返回这一轮有没有真的调用 `game_do`。"""
        sender = str(payload.get("from") or "").strip()
        text = str(payload.get("text") or "").strip()
        if hint is None and not text:
            return False
        active_task = asyncio.current_task()
        self._active_turn_task = active_task
        if persist_user:
            self._active_user_message_text = text
        previous_plan_user_request = self._plan_user_request_active
        self._plan_user_request_active = persist_user

        if not quiet:
            # 聊天/主动说话期间允许站着；20 秒内看门狗不会插巡逻
            self._chat_until = time.monotonic() + CHAT_GRACE_SECONDS
            await self._cancel_patrol()

        started = time.monotonic()
        acted = False
        plan_touched = False
        said_in_game = False
        said_text = ""
        said_texts: list[dict[str, Any]] = []
        reply = ""
        mood: str | None = None
        moods: list[str] = []
        try:
            from backend.agent import loop  # noqa: PLC0415 —— 懒导入，避免循环

            with session_scope() as db:
                friend = db.get(Friend, friend_id)
                if friend is None:
                    return False
                provider = self._provider_factory(db, friend)
                # 游戏轮次走轻量上下文：不跑向量召回 / 纸条 / 日程 / 看图，
                # 续跑轮也不带长期记忆，先把"反应慢"压下来（D266）。
                history = message_service.recent_for_context(
                    db, friend_id, limit=6 if quiet else 8
                )
                memories = [] if quiet else memory_service.list_alive(db, friend_id)
                # 用户发起的回合跟随全局设置；静默续跑强制关闭，优先反应速度。
                if deep_thinking_override is not None:
                    deep_thinking = deep_thinking_override
                else:
                    deep_thinking = (
                        False
                        if quiet
                        else bool(settings_service.get(db).deep_thinking)
                    )
                # 建完上下文先结束事务：LLM 首字可能要十几秒，别让读事务一直占着 SQLite（D197）
                if db.in_transaction():
                    db.commit()

                chunks: list[str] = []
                said_in_game = False
                said_text = ""
                # 只有"用户真的开口"才算问 plan；主动搭话/复盘不算（避免自我递归）。
                plan_query = bool(
                    not quiet
                    and hint is None
                    and any(
                        word in text
                        for word in ("plan", "计划", "任务", "进度", "做到哪")
                    )
                )
                plan_snapshot = self._plan_snapshot_text() if plan_query else ""
                user_input = hint or (
                    f"[游戏里] {sender} 说：{text}"
                    "（先立刻用 game_say 回一句，再决定要不要动手；"
                    "不要把动作全做完才说话）"
                    + (
                        "（TA 在问 plan/计划。现在的事实："
                        f"{plan_snapshot}。"
                        "直接根据事实用 game_say 回答 TA：在不在、什么状态、做到哪；"
                        "不要只说“我去查查”，不要让 TA 等。"
                        "需要改计划时才用 game_plan。）"
                        if plan_query
                        else ""
                    )
                )
                if extra_user_note:
                    user_input += f"\n（系统事实：{extra_user_note}）"
                async for event in loop.stream_reply(
                    friend=friend,
                    user_input=user_input,
                    provider=provider,
                    session=db,
                    history=history,
                    memories=memories,
                    deep_thinking=deep_thinking,
                    game_turn=True,
                    max_game_says=2 if persist_user else 1,
                    usage_purpose="game",
                    # 用户回合最多 2 轮工具：回应 → 查/做 → 收尾；
                    # 不再为了一次聊天串很长的工具链。
                    max_tool_rounds=(
                        max_tool_rounds
                        if max_tool_rounds is not None
                        else 2
                    ),
                ):
                    # 每帧结束一次事务，别让 SQLite 的写锁被这一轮一直握着（D197）
                    if db.in_transaction():
                        db.commit()
                    if event.get("type") == "mood":
                        found = str(event.get("mood") or "").strip()
                        if found:
                            moods.append(found)
                            mood = found
                    elif event.get("type") == "content":
                        chunks.append(str(event.get("delta") or ""))
                    elif (
                        event.get("type") == "tool_result"
                        and event.get("name") == "game_say"
                    ):
                        said_in_game = True
                        raw_say = (
                            ((event.get("args") or {}).get("text"))
                            or ((event.get("result") or {}).get("said"))
                            or ""
                        )
                        say_parser = MoodTagParser()
                        said_text = _clean_game_text(
                            say_parser.feed(str(raw_say)) + say_parser.flush()
                        )
                        if say_parser.mood:
                            mood = say_parser.mood
                            moods.extend(say_parser.moods)
                        if said_text:
                            said_texts.append(
                                {
                                    "text": said_text,
                                    "mood": mood,
                                    "moods": list(moods),
                                }
                            )
                    elif (
                        event.get("type") == "tool_result"
                        and event.get("name") == "game_plan"
                        and event.get("ok")
                    ):
                        plan_touched = True
                    elif (
                        event.get("type") == "tool_result"
                        and event.get("name")
                        in ("game_do", "game_build", "game_skill")
                        and event.get("ok")
                    ):
                        acted = True

                reply = _clean_game_text("".join(chunks))

            # 统一进聊天记录：游戏里的话也算一条（用户拍板：不要"双线程"）
            if persist_user and text:
                await self._persist_game_message(
                    friend_id, "user", f"[游戏] {sender}：{text}"
                )
            # 只有她**明确 game_say** 的话才算"说出口"。
            # 续跑轮（quiet）的正文是思考过程，不进聊天记录、不兜底发到游戏。
            for said in said_texts:
                await self._persist_game_message(
                    friend_id,
                    "assistant",
                    f"[游戏] {said['text']}",
                    mood=said.get("mood"),
                    moods=said.get("moods") or [],
                )

            # D318：正文不是"已发生的行动"。只有这一轮真的做了动作（game_do /
            # game_build / game_skill）时，才允许把未显式 game_say 的正文兜底发出去；
            # 零动作回合的正文只当思考，不发进游戏，避免"没动却说在建造"。
            if not quiet and acted and reply and not said_in_game:
                await self._persist_game_message(
                    friend_id,
                    "assistant",
                    f"[游戏] {reply}",
                    mood=mood,
                    moods=moods,
                )
                await self.command("say", {"text": reply[:200]}, source="agent")
            # 问 plan 却只回"我去查查/等等" → 标记补答（由外层 finally 触发）。
            if plan_query and _looks_like_stall(said_text or reply):
                self._plan_answer_pending = friend_id
            if (
                persist_user
                and text
                and not plan_touched
                and not acted
                and not self._has_running_task()
                and _looks_like_action_request(text)
            ):
                self._plan_nudge_pending = friend_id
            logger.info(
                "游戏内聊天已处理：{} 说「{}」→ {}（{:.1f}s）",
                sender,
                text[:30],
                "她说了一句" if (reply or said_in_game) else "没回",
                time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 —— 游戏内回复失败不能拖垮会话
            logger.warning("游戏内聊天自动回复失败（已吞掉）：{}", exc)
        finally:
            if self._active_turn_task is active_task:
                self._active_turn_task = None
            if persist_user and self._active_user_message_text == text:
                self._active_user_message_text = None
            self._plan_user_request_active = previous_plan_user_request
        self._last_turn_said = bool(
            (said_in_game and said_text)
            or (not quiet and acted and reply and not said_in_game)
        )
        return acted

    async def _quick_game_line(
        self,
        friend_id: str,
        *,
        fact: str,
        instruction: str,
    ) -> None:
        """单次纯说话调用：不提供工具、不做计划复盘，只把事实说给 TA。"""
        active_task = asyncio.current_task()
        async with self._chat_lock:
            self._active_turn_task = active_task
            self._chat_busy = True
            try:
                with session_scope() as db:
                    friend = db.get(Friend, friend_id)
                    if friend is None:
                        return
                    provider = self._provider_factory(db, friend)
                    history = message_service.recent_for_context(
                        db, friend_id, limit=6
                    )
                    messages: list[dict[str, Any]] = [
                        {
                            "role": "system",
                            "content": (
                                f"你是「{friend.name}」。\n"
                                f"{friend.personality or ''}\n"
                                f"{friend.custom_instructions or ''}\n\n"
                                "# 现在的任务\n"
                                "你正在和 TA 一起玩游戏。只需要用符合人格的一两句自然的话"
                                "在游戏里回应；不要复述系统说明，不要调用工具，"
                                "不要解释你是 AI。"
                            ),
                        }
                    ]
                    for message in history[-6:]:
                        role = str(getattr(message, "role", "") or "")
                        content = str(getattr(message, "content", "") or "").strip()
                        if role in ("user", "assistant") and content:
                            messages.append(
                                {"role": role, "content": content[:500]}
                            )
                    messages.append(
                        {
                            "role": "user",
                            "content": f"{fact}\n\n{instruction}",
                        }
                    )
                    requested_temperature = getattr(friend, "temperature", None)
                    if db.in_transaction():
                        db.commit()

                chunks: list[str] = []
                kwargs: dict[str, Any] = {
                    "max_tokens": 220,
                    "usage_purpose": "game",
                }
                if isinstance(requested_temperature, (int, float)):
                    kwargs["temperature"] = max(
                        0.0, min(1.5, float(requested_temperature))
                    )
                async for event in provider.chat_stream(messages, **kwargs):
                    if event.get("type") == "content":
                        chunks.append(str(event.get("delta") or ""))
                    elif event.get("type") == "done":
                        break
                parser = MoodTagParser()
                text = _clean_game_text(
                    parser.feed("".join(chunks)) + parser.flush()
                )
                if not text:
                    logger.warning("快速游戏发言为空，跳过本轮")
                    return
                await self._persist_game_message(
                    friend_id,
                    "assistant",
                    f"[游戏] {text}",
                    mood=parser.mood,
                    moods=parser.moods,
                )
                await self.command("say", {"text": text[:200]}, source="agent")
            finally:
                if self._active_turn_task is active_task:
                    self._active_turn_task = None
                self._chat_busy = False
                self._last_turn_at = time.monotonic()

    async def _proactive_game_line(self, friend_id: str, hint: str) -> None:
        """主动在游戏里说一句（心跳 / 死亡反应）。"""
        await self._respond_to_game_chat(
            friend_id,
            {"text": hint},
            persist_user=False,
            hint=f"[游戏里] {hint}（用 game_say 说；别复读，别解释系统）",
        )

    # ---------- 计划驱动续跑 ----------

    def _cancel_continuation(self) -> None:
        """取消待续跑（用户插话 / 新指令 / 停会话）。"""
        self._continuation_generation += 1
        task = self._continuation_task
        self._continuation_task = None
        if task is not None and not task.done():
            task.cancel()

    def _drain_pending_chat(self) -> None:
        if self._pending_user_chat:
            pending = self._pending_user_chat.pop(0)
            self._spawn(
                self._respond_to_game_chat(
                    pending["friend_id"],
                    pending["payload"],
                    persist_user=True,
                    hint=pending.get("hint"),
                )
            )

    def _schedule_continuation(
        self, *, delay: float | None = None, retry: bool = False
    ) -> None:
        """完成一步之后，短延迟继续推进；**不是周期节拍**。"""
        if not self._auto_continue or not self.active or self._mode != "free":
            return
        self._continuation_generation += 1
        generation = self._continuation_generation
        if self._continuation_task is not None and not self._continuation_task.done():
            self._continuation_task.cancel()
        if not retry:
            self._continuation_retry = 0
        wait = self._continuation_delay if delay is None else max(0.0, delay)
        self._continuation_task = asyncio.create_task(
            self._continue_after(generation, wait, retry=retry),
            name="game-continue",
        )

    async def _continue_after(
        self, generation: int, wait: float, *, retry: bool
    ) -> None:
        try:
            await asyncio.sleep(wait)
            # 她正在说话 / 用户刚插话 → 让那一轮先跑；最多等 60 秒
            for _ in range(120):
                if generation != self._continuation_generation or not self.active:
                    return
                if self._pending_user_chat:
                    return
                if not self._chat_busy:
                    break
                await asyncio.sleep(0.5)
            else:
                return
            await self._continue_plan(generation, retry=retry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("游戏续跑失败（已吞掉）：{}", exc)

    async def _continue_plan(self, generation: int, *, retry: bool) -> None:
        if (
            generation != self._continuation_generation
            or not self.active
            or self._mode != "free"
        ):
            return
        # D315：计划执行权唯一。有 executing plan 时，续跑只能把控制权交回
        # 计划执行器，不能让快脑另起一个 goto/mine 把计划饿死。
        if self._plans.executing() is not None:
            await self._execute_next_plan_task(self.friend_id or "")
            return
        # 有 paused plan 时它正在等复盘/修改；普通续跑不得趁机开快脑。
        if self._plans.active():
            return
        task = self._last_task or {}
        task_name = str(task.get("action") or "一步动作")
        result = task.get("result") or {}
        if isinstance(result, dict) and result.get("error"):
            task_name += f"（失败：{_short(result.get('error'), 80)}）"
        elif isinstance(result, dict) and result.get("failed"):
            task_name += f"（{result.get('failed')} 失败）"
        elif isinstance(result, dict) and self._remaining(result) > 0:
            done, target = self._task_count(result)
            if done is not None and target is not None:
                task_name += f"（只完成 {done}/{target}，还差 {self._remaining(result)}）"
            else:
                task_name += f"（还差 {self._remaining(result)} 个）"
        elif isinstance(result, dict) and result.get("note"):
            task_name += f"（{_short(result.get('note'), 80)}）"
        failed_last = isinstance(result, dict) and bool(
            result.get("error")
            or result.get("failed")
            or self._remaining(result) > 0
        )
        if failed_last and not retry:
            hint = (
                f"你刚做的这一步出问题了：{task_name}。先分析原因，再决定下一步。"
                "**不要原样重试**：格式错误 → 改格式；条件不满足 → 补条件/换目标；"
                "目标不可达 → 换地方；策略不对 → 改计划。"
                "只有你能说清“哪一点变了”才重试。"
                "这一轮安静做事；如果决定放弃，用 game_plan(done=true) 或 game_say 说明。"
            )
        else:
            hint = (
                CONTINUE_RETRY_HINT
                if retry
                else CONTINUE_HINT.format(task=task_name)
            )
        if self._pending_user_chat:
            return
        async with self._chat_lock:
            self._chat_busy = True
            previous_origin = self._turn_origin
            self._turn_origin = "auto"
            try:
                acted = await self._run_game_turn(
                    self.friend_id or "",
                    {"text": hint},
                    persist_user=False,
                    hint=f"[游戏里] {hint}",
                    quiet=True,
                )
            finally:
                self._turn_origin = previous_origin
                self._chat_busy = False
                self._last_turn_at = time.monotonic()
        self._drain_pending_chat()
        if generation != self._continuation_generation:
            return
        if acted:
            self._continuation_retry = 0
            return
        # 只补一次"你还没动手"，不进入无限催动；有没有计划都重试
        if self._continuation_retry < 1:
            self._continuation_retry += 1
            self._schedule_continuation(delay=3.0, retry=True)

    async def _persist_game_message(
        self,
        friend_id: str,
        role: str,
        content: str,
        *,
        mood: str | None = None,
        moods: list[str] | None = None,
    ) -> None:
        """把游戏内的一句话说进聊天记录（App 里能看到，不再"双线程"）。"""
        from backend.api.ws import manager as ws_manager  # noqa: PLC0415

        try:
            with session_scope() as db:
                friend = db.get(Friend, friend_id)
                if friend is None:
                    return
                message = message_service.create(
                    db,
                    friend_id,
                    role=role,
                    content=content,
                    mode_at_send=friend.current_mode,
                    is_proactive=False,
                    stream_status=message_service.COMPLETED,
                    mood=mood,
                    moods=moods,
                )
                if role == "assistant":
                    friend.unread_count = (friend.unread_count or 0) + 1
                db.flush()
                payload = message_service.to_dict(message)
                friend_name = friend.name
            await ws_manager.broadcast(
                {
                    "type": "new_message",
                    "friend_id": friend_id,
                    "friend_name": friend_name,
                    "message": payload,
                }
            )
        except Exception as exc:  # noqa: BLE001 —— 落库失败不影响游戏里的动作
            logger.warning("游戏内消息落库失败（已吞掉）：{}", exc)

    async def _comment(self, friend_id: str, hint: str) -> None:
        """生成并投递一句主动消息。**LLM 调用期间不占数据库会话**（D197 的教训）。"""
        try:
            with session_scope() as db:
                friend = db.get(Friend, friend_id)
                if friend is None:
                    return
                provider = _provider_for(db, friend)
                messages = build_messages(
                    friend,
                    history=message_service.recent_for_context(db, friend_id, limit=20),
                    user_input=hint,
                    memories=memory_service.list_alive(db, friend_id),
                    mode_override="normal",
                    schedule_note=schedule_service.upcoming_for_prompt(db, friend_id),
                )
            chunks: list[str] = []
            async for chunk in provider.chat_stream(messages, temperature=0.8, max_tokens=300):
                if chunk.get("type") == "content":
                    chunks.append(chunk.get("delta") or "")
                elif chunk.get("type") == "done":
                    break
            content = "".join(chunks).strip()
            if not content or proactive_service.is_silent(content):
                return
            with session_scope() as db:
                friend = db.get(Friend, friend_id)
                if friend is not None:
                    await proactive_service.deliver(db, friend, content)
        except Exception as exc:  # noqa: BLE001 —— 游戏冒泡失败绝不影响会话
            logger.warning("游戏主动消息失败（已吞掉）：{}", exc)

    async def _game_chat_heartbeat_loop(self) -> None:
        """每 5 秒看一眼：只负责**偶尔主动搭句话**，不驱动任何动作。

        动作推进完全由 `task_done → 续跑` 负责（D262）。
        """
        try:
            self._last_turn_at = time.monotonic()
            while self.active:
                await asyncio.sleep(5)
                if not self.active:
                    return
                if self._chat_busy or self._pending_user_chat:
                    continue
                continuation = self._continuation_task
                if continuation is not None and not continuation.done():
                    continue
                friend_id = self.friend_id
                if not friend_id:
                    continue
                now = time.monotonic()
                interval = self._game_chat_interval()
                if interval > 0 and now - self._last_turn_at >= interval:
                    await self._proactive_game_line(friend_id, TALK_HINT)
                    self._last_turn_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("游戏主动说话心跳异常（已吞掉）：{}", exc)

    def _has_running_task(self, *, include_patrol: bool = False) -> bool:
        session = self._service.session if self._service else None
        if session is None:
            return False
        for task in (session.tasks or {}).values():
            if str(task.get("status") or "") != "running":
                continue
            if not include_patrol and str(task.get("action") or "") in ("patrol", "say"):
                continue
            return True
        return False

    async def _watchdog_loop(self) -> None:
        """0.5 秒看门狗：自由模式没有真实任务 → 巡逻 + 自主循环；
        跟随/聊天模式只负责恢复常态（D276）。"""
        try:
            while self.active:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                if not self.active:
                    continue
                if self._chat_busy or self._pending_user_chat:
                    continue
                if self._has_running_task():
                    continue
                continuation = self._continuation_task
                if continuation is not None and not continuation.done():
                    continue
                if time.monotonic() < self._chat_until:
                    continue
                # D340：只有 executing plan 占身体；paused plan 不能冻结跟随/自由。
                if self._plans.executing() is not None:
                    await self._execute_next_plan_task(self.friend_id or "")
                    continue
                paused_plans = [
                    plan
                    for plan in self._plans.active()
                    if plan.status == "paused"
                ]
                if paused_plans and self._mode == "free":
                    # 给自由模式一次处理暂停计划的机会，但不要按固定秒数反复复盘。
                    if self._next_idle_think_at > time.monotonic() + 2.0:
                        self._next_idle_think_at = time.monotonic() + 2.0
                if self._mode in ("follow", "chat"):
                    if time.monotonic() < self._mode_paused_until:
                        continue
                    if (
                        not self._mode_applied
                        and time.monotonic() - self._last_mode_apply_at >= 2.0
                    ):
                        try:
                            await self._apply_mode()
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("看门狗恢复游戏模式失败：{}", exc)
                if self._mode == "free" and self._patrol_supported:
                    await self._ensure_patrol()
                self._spawn_autonomy()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("游戏待机看门狗异常（已吞掉）：{}", exc)

    def _spawn_autonomy(self) -> None:
        """空闲思考：任何模式都适用；只在她真的没事可做时唤醒。"""
        if (
            not self._autonomy_enabled
            or not self.active
            or self._mode != "free"
            or self._plans.executing() is not None
            or self._mode_switching
        ):
            return
        if self._chat_busy or self._pending_user_chat:
            return
        if self._autonomy_task is not None and not self._autonomy_task.done():
            return
        if time.monotonic() < self._next_idle_think_at:
            return
        friend_id = self.friend_id
        if not friend_id:
            return
        self._autonomy_task = asyncio.create_task(
            self._autonomy_turn(friend_id), name="game-autonomy"
        )

    async def _ensure_patrol(self) -> None:
        if not self.active or not self._patrol_supported or self._mode != "free":
            return
        if self._placeholder_task_id or self._has_running_task():
            return
        if time.monotonic() < self._chat_until:
            return
        try:
            result = await self.command(
                "patrol", {"seconds": PATROL_SECONDS}, source="auto"
            )
            self._placeholder_task_id = str(result.get("task_id") or "") or None
            logger.debug("待机巡逻下发：{}s", PATROL_SECONDS)
        except Exception as exc:  # noqa: BLE001 —— 适配器不支持就别再试
            self._patrol_supported = False
            logger.warning("待机巡逻不可用（已关闭）：{}", exc)

    async def _cancel_patrol(self) -> None:
        task_id = self._placeholder_task_id
        self._placeholder_task_id = None
        service = self._service
        if not task_id or service is None:
            return
        try:
            await service.cancel(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("取消待机巡逻失败（忽略）：{}", exc)

    def _cancel_autonomy(self) -> None:
        task = self._autonomy_task
        self._autonomy_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _cancel_holding_tasks(self) -> None:
        """真正要做事时，先把 rest / wait 停掉。"""
        service = self._service
        session = service.session if service else None
        if service is None or session is None:
            return
        for task_id, task in list((session.tasks or {}).items()):
            if str(task.get("status") or "") != "running":
                continue
            if str(task.get("action") or "") not in ("rest", "wait"):
                continue
            try:
                await service.cancel(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("取消 rest/wait 失败（忽略）：{}", exc)

    async def _autonomy_turn(self, friend_id: str) -> None:
        """自由模式的空闲思考；跟随/聊天模式由常态模式负责。"""
        if (
            self._mode != "free"
            or self._chat_busy
            or self._plans.executing() is not None
        ):
            return
        logger.info("空闲思考触发：看看现在该做什么")
        hint = SELF_START_HINT
        if any(plan.status == "paused" for plan in self._plans.active()):
            hint += (
                " 你有一个 paused 的 plan。先看它：能继续/修补就 resume，"
                "决定不做就明确 cancel；不要无视它去开一个完全无关的新计划。"
            )
        progress = False
        async with self._chat_lock:
            self._chat_busy = True
            previous_origin = self._turn_origin
            self._turn_origin = "auto"
            failed = False
            try:
                acted = await self._run_game_turn(
                    friend_id,
                    {"text": hint},
                    persist_user=False,
                    hint=f"[游戏里] {hint}",
                    quiet=True,
                )
                # 定出 plan / 发出动作 / 主动说话都算有进展，重置空闲退避。
                progress = bool(
                    acted
                    or self._plans.executing() is not None
                    or self._has_running_task()
                    or self._last_turn_said
                )
            except Exception as exc:  # noqa: BLE001 —— 自主回合失败不能变成未处理任务
                failed = True
                logger.warning("空闲思考失败（稍后再试）：{}", exc)
            finally:
                self._turn_origin = previous_origin
                self._chat_busy = False
                now = time.monotonic()
                if progress and not failed:
                    self._autonomy_no_progress = 0
                    self._last_autonomy_at = now
                    self._next_idle_think_at = now + AUTONOMY_BUFFER_SECONDS
                else:
                    # 没有 plan / 没有动作 = 空转。指数退避，避免每 2 秒烧一次慢脑；
                    # 用户说话、切到自由模式或真的推进了都会重置。
                    self._autonomy_no_progress += 1
                    delay = min(
                        AUTONOMY_BACKOFF_MAX_SECONDS,
                        AUTONOMY_BACKOFF_BASE_SECONDS
                        * (2 ** (self._autonomy_no_progress - 1)),
                    )
                    self._last_autonomy_at = now
                    self._next_idle_think_at = now + delay
                    logger.info(
                        "空闲思考没有进展，{:.0f} 秒后再试（连续 {} 次空转）",
                        delay,
                        self._autonomy_no_progress,
                    )
                if self._attention_dirty:
                    self._attention_dirty = False
                    if not self._plans.active():
                        self._next_idle_think_at = now + 1.0
                self._last_turn_at = time.monotonic()
                self._drain_pending_chat()

    # ---------- plan / task（D285） ----------

    async def _auto_repair_plan_task(
        self,
        plan_id: str,
        index: int,
        task: Any,
        error_text: str,
    ) -> bool:
        """确定性失败先本地修复，不叫 LLM 复盘；修不好才交回复盘。"""
        key = (plan_id, index)
        attempts = int(self._plan_task_auto_repairs.get(key, 0))
        if attempts >= 2:
            return False
        text = str(error_text or "")
        action = str(getattr(task, "action", "") or "")
        args = getattr(task, "args", {}) or {}

        async def retry_later(reason: str) -> bool:
            self._plan_task_auto_repairs[key] = attempts + 1
            self._plans.mark_task(
                plan_id,
                index,
                status="pending",
                result={"auto_repair": reason, "attempt": attempts + 1},
            )
            self._spawn(self._execute_next_plan_task(self.friend_id or ""))
            logger.info(
                "plan {} task {} 自动修复后重试：{}（第 {}/2 次）",
                plan_id,
                index,
                reason,
                attempts + 1,
            )
            return True

        if action == "place" and (
            "occupied" in text or "已经有方块" in text
        ):
            try:
                coords = {
                    "x": int(float(args.get("x"))),
                    "y": int(float(args.get("y"))),
                    "z": int(float(args.get("z"))),
                }
            except (TypeError, ValueError):
                return False
            try:
                self._internal_task_depth += 1
                try:
                    blocks = await self._inspect_positions([coords])
                finally:
                    self._internal_task_depth = max(
                        0, self._internal_task_depth - 1
                    )
                observed = next(
                    (
                        str(item.get("name") or "")
                        for item in blocks
                        if all(axis in item for axis in ("x", "y", "z"))
                        and int(float(item["x"])) == coords["x"]
                        and int(float(item["y"])) == coords["y"]
                        and int(float(item["z"])) == coords["z"]
                    ),
                    "",
                )
                if observed and _BLUEPRINT_PROTECTED_BLOCK.search(observed):
                    logger.info(
                        "目标格是受保护方块 {}，不自动拆：{}",
                        observed,
                        coords,
                    )
                    return False
                result = await self.command("dig_at", coords, source="plan")
                task_id = str(result.get("task_id") or "")
                if not task_id:
                    return False
                done = await self._wait_task_result(task_id, 12.0)
                if not done.get("ok", True):
                    return False
            except Exception as exc:  # noqa: BLE001 —— 修不了就交回复盘
                logger.debug("自动清理目标格失败：{}", exc)
                return False
            return await retry_later("cleared_occupied_target")

        if action in ("place", "sequence", "goto", "bring", "escape") and any(
            word in text
            for word in (
                "走到放置点超时",
                "让开目标格失败",
                "goal was changed",
                "block is still water",
                "block is still air",
            )
        ):
            return await retry_later("transient_placement_or_move")
        return False

    async def _execute_next_plan_task(self, friend_id: str) -> bool:
        if not self.active or not friend_id:
            return False
        if self._chat_busy or self._pending_user_chat or self._has_running_task():
            return False
        plan = self._plans.executing()
        if plan is None or plan.status != "executing":
            return False
        if plan.id in self._plan_completion_audits:
            return False
        nxt = self._plans.next_pending(plan.id)
        if nxt is None:
            self._plans.pause(plan.id)
            self._spawn(
                self._review_plan(friend_id, plan.id, reason="all_tasks_done")
            )
            return True
        index, task = nxt
        if task.action in ("say", "stop", "mode", "patrol"):
            self._plans.mark_task(
                plan.id,
                index,
                status="failed",
                result={"error": f"plan task 不能是 {task.action}"},
            )
            self._plans.pause(plan.id)
            self._spawn(
                self._review_plan(
                    friend_id,
                    plan.id,
                    reason="task_failed",
                    task_index=index,
                    error=f"plan task 不能是 {task.action}",
                )
            )
            return True
        if task.action == "prepare_site":
            self._plans.mark_task(plan.id, index, status="executing")
            try:
                result = await self._prepare_site(
                    anchor=task.args.get("anchor") or {},
                    size=task.args.get("size") or {},
                    fill_block=str(task.args.get("fill_block") or "dirt"),
                )
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            ok = bool(result.get("ok"))
            self._plans.mark_task(
                plan.id,
                index,
                status="success" if ok else "failed",
                result=result,
            )
            if not ok:
                self._plans.pause(plan.id)
                self._spawn(
                    self._review_plan(
                        friend_id,
                        plan.id,
                        reason="task_failed",
                        task_index=index,
                        error=str(result.get("error") or "整地失败"),
                    )
                )
                return True
            if self._plans.next_pending(plan.id) is not None:
                self._spawn(self._execute_next_plan_task(friend_id))
            return True
        if task.action == "blueprint":
            self._plans.mark_task(plan.id, index, status="executing")
            try:
                result = await self.run_blueprint(
                    task.args.get("blueprint") or {},
                    task.args.get("anchor") or {},
                )
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
            ok = bool(result.get("ok"))
            self._plans.mark_task(
                plan.id,
                index,
                status="success" if ok else "failed",
                result=result,
            )
            if not ok:
                self._plans.pause(plan.id)
                self._spawn(
                    self._review_plan(
                        friend_id,
                        plan.id,
                        reason="postcondition_failed",
                        task_index=index,
                        error=str(result.get("error") or "蓝图校验失败"),
                    )
                )
                return True
            if self._plans.next_pending(plan.id) is None:
                plan.result = {"verified": True, "blueprint": result}
                if plan.checks:
                    try:
                        with session_scope() as db:
                            learned = skill_store.learn_from_plan(
                                db,
                                friend_id=friend_id,
                                plan=plan,
                                game_kind=self.game_kind or "minecraft",
                            )
                        if learned is not None:
                            plan.result["skill_id"] = learned["id"]
                            plan.result["skill_name"] = learned["name"]
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("蓝图 plan 学习技能失败：{}", exc)
                self._plans.complete(plan.id, result=plan.result)
                self._remember_completed_plan(plan)
                self._spawn(
                    self._announce_plan_completion(friend_id, plan.id)
                )
            else:
                self._spawn(self._execute_next_plan_task(friend_id))
            return True
        self._plans.mark_task(plan.id, index, status="executing")
        try:
            result = await self.command(
                task.action,
                task.args,
                # D321：带上 plan 身份，让 command() 知道这是计划执行器，
                # 而不是又一个可以随便取消当前 task 的外部回合。
                source="plan",
            )
        except Exception as exc:  # noqa: BLE001 —— 失败交给 DeepSeek 重新评估
            if await self._auto_repair_plan_task(
                plan.id, index, task, str(exc)
            ):
                return True
            self._plans.mark_task(
                plan.id,
                index,
                status="failed",
                result={"error": str(exc)},
            )
            self._plans.pause(plan.id)
            self._spawn(
                self._review_plan(
                    friend_id,
                    plan.id,
                    reason="task_failed",
                    task_index=index,
                    error=str(exc),
                )
            )
            return False
        adapter_id = str(result.get("task_id") or "")
        if not adapter_id:
            self._plans.mark_task(
                plan.id,
                index,
                status="failed",
                result={"error": "适配器没有返回 task_id"},
            )
            self._plans.pause(plan.id)
            self._spawn(
                self._review_plan(
                    friend_id,
                    plan.id,
                    reason="task_failed",
                    task_index=index,
                    error="适配器没有返回 task_id",
                )
            )
            return False
        self._plan_task_links[adapter_id] = (plan.id, index)
        return True

    def _plan_snapshot_text(self) -> str:
        """给"TA 问 plan 状态"用的紧凑事实（D286）。"""
        plans = self._plans.active()
        if not plans:
            if self._recent_completed_plans:
                recent = self._recent_completed_plans[-1]
                return (
                    "现在没有进行中的计划；最近完成："
                    f"{recent.get('expected') or recent.get('id')}"
                    f"（{recent.get('completed_at')}，"
                    f"{recent.get('tasks_done')} 个 task）"
                )
            return "现在没有进行中的计划（没有 executing / paused 的 plan）"
        parts: list[str] = []
        for plan in plans:
            total = len(plan.tasks)
            done = sum(1 for task in plan.tasks if task.status == "success")
            current = next(
                (
                    index
                    for index, task in enumerate(plan.tasks, start=1)
                    if task.status in ("executing", "pending")
                ),
                None,
            )
            item = (
                f"{plan.id}（{plan.status}）"
                f"目标：{_short(plan.expected, 60)}；task {done}/{total} 完成"
            )
            if current is not None:
                item += f"，下一步是第 {current} 个"
            parts.append(item)
        return "；".join(parts)

    def _compact_plan_text(self, plan: Any, focus_index: int | None = None) -> str:
        """复盘只带当前计划摘要，不回灌整段 plan JSON。"""
        lines = [
            f"{plan.id}｜status={plan.status}",
            f"condition：{_short(plan.condition, 180) or '（空）'}",
            f"expected：{_short(plan.expected, 180) or '（空）'}",
            "tasks：",
        ]
        for index, task in enumerate(plan.tasks, start=1):
            args = _short(
                json.dumps(task.args, ensure_ascii=False, sort_keys=True),
                220,
            )
            marker = "  <-- 卡在这里" if index == focus_index else ""
            lines.append(
                f"{index}. [{task.status}] {task.action} {args}{marker}"
            )
        return "\n".join(lines)

    async def _review_plan(
        self,
        friend_id: str,
        plan_id: str,
        *,
        reason: str,
        task_index: int | None = None,
        error: str | None = None,
    ) -> None:
        """plan 暂停 → DeepSeek 看 condition/expected/失败原因，改 task 或结束。"""
        plan = self._plans.get(plan_id)
        if plan is None or plan_id in self._plan_review_running:
            return
        before_updated_at = plan.updated_at
        before_tasks = [
            (task.action, dict(task.args), task.status) for task in plan.tasks
        ]
        if reason in ("task_failed", "postcondition_failed"):
            try:
                with session_scope() as db:
                    skill_store.record_failure(
                        db,
                        friend_id=friend_id,
                        plan_id=plan.id,
                        reason=reason,
                        detail=error or "task 失败",
                    )
            except Exception as exc:  # noqa: BLE001 —— 笔记失败不能拖垮复盘
                logger.warning("写技能失败笔记失败：{}", exc)
        if reason in ("task_failed", "postcondition_failed"):
            count = self._plan_failure_counts.get(plan_id, 0) + 1
            self._plan_failure_counts[plan_id] = count
            if count >= MAX_SAME_FAILURES:
                self._plans.cancel(
                    plan_id,
                    result=f"连续失败 {count} 次，无法完成：{error or 'task 失败'}",
                )
                logger.warning(
                    "plan {} 连续失败 {} 次，已取消并回到默认模式",
                    plan_id,
                    count,
                )
                self._spawn(
                    self._proactive_game_line(
                        friend_id,
                        "这个计划连续失败几次，已经停下来了；"
                        "用一句自然的话告诉 TA 你做不下去，不要假装完成。",
                    )
                )
                return
        self._plan_review_running.add(plan_id)
        reason_text = {
            "all_tasks_done": "预定的 task 全部完成了",
            "task_failed": "有一个 task 失败了",
            "postcondition_failed": "有 task 或 plan 的后置条件没有通过",
            "completion_incomplete": "task 都执行完了，但完成验收判断 expected 还没达到",
            "review_stalled": "上一次复盘没有推进计划",
        }.get(reason, reason)
        failed_task = ""
        failed_action = ""
        failed_args: Mapping[str, Any] = {}
        if task_index is not None and 1 <= task_index <= len(plan.tasks):
            task = plan.tasks[task_index - 1]
            failed_action = task.action
            failed_args = task.args
            failed_task = (
                f"\n失败的是第 {task_index} 个 task：{task.action} {task.args}"
                f"（{task.status}）"
            )
        failure_kind = preflight.classify_failure(error)
        recovery = preflight.recovery_hint(failure_kind, failed_action, failed_args)
        hint = (
            f"plan {plan.id} 现在暂停了：{reason_text}。"
            f"{failed_task}"
            + (f"\n错误：{error}" if error else "")
            + (
                f"\n失败类型：{failure_kind}；恢复建议：{recovery}"
                if reason in ("task_failed", "postcondition_failed")
                else ""
            )
            + "\n\n当前 plan 摘要：\n"
            + self._compact_plan_text(plan, focus_index=task_index)
            + "\n\n先判断 condition（前置条件）是否满足：\n"
            "- 不满足 → 用 game_plan(action=\"add_tasks\", plan_id=..., "
            "tasks=[补条件的 task], before=...) 把补条件的 task 插到前面；\n"
            "- 条件满足但 task 失败 → 改 task / 换做法，不要原样重试；\n"
            "- 所有 task 都完成 → 对照 expected 和 result，判断是否 complete；\n"
            "- 如果 expected 还没达到，就 add_tasks/update 后再 resume；\n"
            "- 决定放弃时 cancel，并在 result 里写原因。\n"
            "**只能在当前 plan 上改，不要 create 新 plan，也不要换地方从零重搭。**\n"
            "**只允许调用一次 game_plan；不要 game_inspect / game_do / game_say。**\n"
            "只有 task 会失败，plan 不会失败；plan 的状态只有 "
            "executing / paused / cancelled / completed。\n"
            "这一轮安静做事，不要汇报进度。"
        )
        logger.info("plan 局部复盘：{} / {} / {}", plan.id, reason, error or "")
        try:
            async with self._chat_lock:
                self._chat_busy = True
                previous_origin = self._turn_origin
                self._turn_origin = (
                    plan.origin if plan.origin in ("user", "auto") else "auto"
                )
                try:
                    await self._run_game_turn(
                        friend_id,
                        {"text": hint},
                        persist_user=False,
                        hint=f"[游戏里] {hint}",
                        quiet=True,
                        deep_thinking_override=False,
                        max_tool_rounds=1,
                    )
                finally:
                    self._turn_origin = previous_origin
                    self._chat_busy = False
                    self._last_turn_at = time.monotonic()
        finally:
            self._plan_review_running.discard(plan_id)
            if plan.status == "paused":
                changed = plan.updated_at != before_updated_at or [
                    (task.action, dict(task.args), task.status)
                    for task in plan.tasks
                ] != before_tasks
                if (
                    changed
                    and not self._plan_stop_hold
                    and self._plans.executing() is None
                ):
                    # 复盘已经改出了新做法：接着执行，不再要求模型额外调一次 resume。
                    self._plans.resume(plan_id)
                    self._spawn(self._execute_next_plan_task(friend_id))
                    logger.info(
                        "plan {} 局部复盘已改计划，自动恢复执行", plan_id
                    )
                else:
                    logger.info(
                        "plan {} 局部复盘未推进，保持 paused，不再自动重试",
                        plan_id,
                    )
            else:
                self._plan_review_attempts.pop(plan_id, None)
                self._plan_review_retry_at.pop(plan_id, None)
        self._drain_pending_chat()

    # ---------- 本地快脑（D279） ----------

    def _fast_provider(self):  # noqa: ANN202
        if time.monotonic() < self._fast_brain_disabled_until:
            return None
        with session_scope() as db:
            purposes_json = settings_service.get(db).purposes
        if (
            self._fast_provider_cache is not None
            and self._fast_provider_signature == purposes_json
        ):
            return self._fast_provider_cache
        try:
            provider = create_provider_for_purpose(purposes_json, "game_fast")
        except LLMConfigError:
            return None
        except Exception as exc:  # noqa: BLE001 —— 本地没配好不该拖垮游戏
            logger.warning("游戏快脑配置读取失败（回退完整回合）：{}", exc)
            return None
        self._fast_provider_cache = provider
        self._fast_provider_signature = purposes_json
        return provider

    async def _fast_state_text(self) -> str:
        try:
            snapshot = await self.snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.debug("快脑取快照失败：{}", exc)
            snapshot = {}
        player = snapshot.get("player") or {}
        blocks = snapshot.get("nearby_blocks") or []
        lines = [
            f"模式：{self._mode}",
            (
                f"位置：({player.get('x', '?')}, {player.get('y', '?')}, {player.get('z', '?')})"
                f"｜血量：{player.get('health', '?')}｜饥饿：{player.get('food', '?')}"
            ),
        ]
        lines.extend(_inventory_lines(snapshot))
        lines.append(
            "附近方块："
            + (
                "、".join(
                    f"{item.get('name')}×{item.get('count')}" for item in blocks[:10]
                )
                or "无"
            )
        )
        lines.extend(_perception_lines(snapshot, self._last_player_speaker))
        recent = self._recent_perception_text(limit=3)
        if recent:
            lines.append("刚刚：" + recent)
        dropped_items = snapshot.get("dropped_items") or []
        if dropped_items:
            lines.append(
                "附近掉落物："
                + "、".join(
                    f"{item.get('name') or 'item'}×{item.get('count') or 1}"
                    f"（{item.get('distance')} 格）"
                    for item in dropped_items[:5]
                    if isinstance(item, Mapping)
                )
            )
        terrain = snapshot.get("terrain") or {}
        if terrain.get("flat"):
            lines.append("地形：脚下这一片比较平")
        local_grid = _local_grid_text(snapshot)
        if local_grid:
            lines.append("身边 5×5 格子：" + local_grid)
        flat_sites = snapshot.get("flat_sites") or []
        if flat_sites:
            site = flat_sites[0]
            lines.append(
                f"附近平地：({site.get('x')}, {site.get('y')}, {site.get('z')})"
                f"，距离约 {site.get('distance')} 格"
            )
        running = [
            task
            for task in (snapshot.get("tasks") or [])
            if task.get("status") == "running"
        ]
        if running:
            lines.append(
                "正在做："
                + "、".join(
                    f"{task.get('action') or task.get('task_id')}"
                    + (
                        f"({task['progress'].get('current')}/{task['progress'].get('target')})"
                        if isinstance(task.get("progress"), dict)
                        else ""
                    )
                    for task in running[:3]
                )
            )
        if self._failure_history:
            last = self._failure_history[-1]
            kind = str(last.get("kind") or preflight.classify_failure(last.get("error")))
            suggestion = str(
                last.get("suggestion")
                or preflight.recovery_hint(
                    kind,
                    str(last.get("action") or ""),
                    last.get("args") if isinstance(last.get("args"), Mapping) else {},
                )
            )
            lines.append(
                f"上次失败：{last.get('action') or '?'} {last.get('args') or {}}"
                f"（类型：{kind}）；建议：{suggestion}"
            )
        return "\n".join(lines)

    def _fast_plan(self) -> tuple[str, list[str]]:
        """给快脑看的当前计划。

        v5.1 的结构化 plan 不再经过旧的 `_plan` 字符串；如果只读 `_plan`，
        有 `game_plan(action=create)` 时快脑会误以为“没有计划”，每次自主回合
        都升级 DeepSeek。这里优先读 executing plan 的 expected + 未完成 task。
        """
        plan = self._plans.executing()
        if plan is not None:
            expected = str(plan.expected or "").strip()
            condition = str(plan.condition or "").strip()
            plan_text = expected or condition
            if expected and condition and condition != expected:
                plan_text = f"{expected}（前置条件：{condition}）"
            steps: list[str] = []
            for index, task in enumerate(plan.tasks, start=1):
                if task.status == "success":
                    continue
                args = json.dumps(task.args, ensure_ascii=False, sort_keys=True)
                steps.append(f"{index}. {task.action} {args}")
            if plan_text:
                return plan_text, steps
        return self._plan, list(self._plan_steps)

    async def _run_fast_decision(
        self, friend_id: str, *, user_task: bool = False
    ) -> bool:
        """本地快脑选一个动作；接不住就返回 False，让调用方升级给聊天模型。"""
        if not self.active:
            return False
        provider = self._fast_provider()
        if provider is None:
            return False
        plan_text, plan_steps = self._fast_plan()
        if self._mode == "free" and not plan_text:
            # 定计划是"复杂思考"，交给聊天模型。
            return False
        with session_scope() as db:
            friend = db.get(Friend, friend_id)
            if friend is None:
                return False
            friend_name = friend.name
            personality = friend.personality or ""
        state = await self._fast_state_text()
        try:
            decision, _raw, usage = await fast_brain.decide(
                provider,
                friend_name=friend_name,
                personality=personality,
                state=state,
                plan=plan_text,
                plan_steps=plan_steps,
                last_task=self._last_task,
            )
        except Exception as exc:  # noqa: BLE001 —— 本地模型 404 / 超时都回退
            self._fast_brain_disabled_until = (
                time.monotonic() + FAST_BRAIN_FAILURE_COOLDOWN
            )
            logger.warning(
                "游戏快脑不可用（{:.0f} 秒内回退聊天模型）：{}",
                FAST_BRAIN_FAILURE_COOLDOWN,
                exc,
            )
            return False
        self._last_brain_decision = {
            "lane": "local" if decision is not None else "local->deepseek",
            "model": provider.config.model,
            "action": (decision or {}).get("action"),
            "latency_ms": usage.get("latency_ms"),
            "prompt_tokens": usage.get("prompt_tokens") or usage.get("input_tokens"),
            "completion_tokens": usage.get("completion_tokens")
            or usage.get("output_tokens"),
        }
        if decision is None:
            logger.info(
                "游戏快脑升级给聊天模型（{}ms）",
                usage.get("latency_ms"),
            )
            return False
        action = str(decision.get("action") or "")
        args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
        try:
            await self.command(
                action,
                args,
                source="user" if user_task else "auto",
            )
            say = str(decision.get("say") or "").strip()
            if say:
                await self.command(
                    "say",
                    {"text": say},
                    source="user" if user_task else "auto",
                )
        except Exception as exc:  # noqa: BLE001 —— 本地选错就升级
            logger.warning("游戏快脑执行失败（升级）：{}", exc)
            return False
        logger.info(
            "游戏快脑执行：{} {}（{}ms，model={}）",
            action,
            args,
            usage.get("latency_ms"),
            provider.config.model,
        )
        return True

    def _game_chat_interval(self) -> int:
        try:
            with session_scope() as db:
                base = int(
                    getattr(settings_service.get(db), "game_chat_interval_seconds", 0) or 0
                )
        except Exception:
            return 0
        if base > 0 and self._mode == "chat":
            # 聊天模式更主动一点：间隔减半，但至少 20 秒，避免刷屏。
            return max(20, base // 2)
        return base

    def plan_action(
        self,
        action: str,
        *,
        plan_id: str | None = None,
        condition: Any = None,
        expected: Any = None,
        result: Any = None,
        checks: Any = None,
        tasks: Any = None,
        before: Any = None,
    ) -> dict[str, Any]:
        """plan / task 工具的统一入口（D285）。"""
        if not self.active:
            raise RuntimeError("现在没有进行中的游戏会话")
        action = str(action or "").strip().lower()
        if isinstance(tasks, str):
            try:
                tasks = json.loads(tasks)
            except json.JSONDecodeError as exc:
                raise ValueError("tasks 不是合法 JSON 数组") from exc
        if tasks is not None and not isinstance(tasks, list):
            raise ValueError("tasks 必须是数组")
        if tasks:
            normalized_tasks: list[Any] = []
            for task in tasks:
                if not isinstance(task, Mapping):
                    normalized_tasks.append(task)
                    continue
                if str(task.get("action") or "").strip() != "blueprint":
                    normalized_tasks.append(task)
                    continue
                task_args = task.get("args")
                task_args = dict(task_args) if isinstance(task_args, Mapping) else {}
                if not task_args.get("blueprint"):
                    blueprint_name = str(
                        task_args.get("blueprint_name")
                        or task_args.get("name")
                        or ""
                    ).strip()
                    preview = self._blueprint_previews.get(blueprint_name)
                    if preview is None and self._last_blueprint_preview:
                        requested_anchor = task_args.get("anchor")
                        preview_anchor = self._last_blueprint_preview.get("anchor")
                        same_anchor = False
                        try:
                            same_anchor = (
                                isinstance(requested_anchor, Mapping)
                                and isinstance(preview_anchor, Mapping)
                                and all(
                                    int(float(requested_anchor.get(axis)))
                                    == int(float(preview_anchor.get(axis)))
                                    for axis in ("x", "y", "z")
                                )
                            )
                        except (TypeError, ValueError):
                            same_anchor = False
                        if not requested_anchor or same_anchor:
                            preview = self._last_blueprint_preview
                    if preview is not None:
                        task_args["blueprint"] = preview.get("blueprint")
                        if not task_args.get("anchor"):
                            task_args["anchor"] = preview.get("anchor")
                if not task_args.get("blueprint"):
                    raise ValueError(
                        "[格式错误] blueprint task 需要 args.blueprint"
                        "（game_build 返回的完整设计稿）；也可以先调用 game_build，"
                        "再用 blueprint_name 引用最近的设计稿。"
                    )
                normalized_tasks.append({**task, "args": task_args})
            tasks = normalized_tasks
            caps = self._service.session.capabilities if self._service and self._service.session else None
            allowed = set(caps.action_names()) if caps is not None else set()
            allowed.add("blueprint")
            allowed.add("prepare_site")
            for index, task in enumerate(tasks, start=1):
                if not isinstance(task, Mapping):
                    continue
                action_name = str(task.get("action") or "").strip()
                if action_name and action_name not in allowed:
                    raise ValueError(
                        f"task {index} 的动作 {action_name!r} 不存在；"
                        f"可用动作：{', '.join(sorted(allowed))}"
                    )
        if (
            action in ("update", "add_tasks")
            and tasks
            and self._has_running_task()
        ):
            raise RuntimeError(
                "[条件不满足] 当前 task 还在跑，等它结束再改 tasks"
            )
        if action == "create" and self._plan_review_running:
            raise ValueError(
                "复盘阶段只能在现有 plan 上 update / add_tasks / resume，"
                "不要新建 plan，也不要 cancel 后重建"
            )
        if (
            action == "resume"
            and plan_id
            and self._plan_stop_hold
            and self._plan_stop_hold == plan_id
        ):
            raise ValueError(
                "[条件不满足] TA 刚明确让你停下；这个 plan 保持暂停，"
                "等 TA 说继续后再恢复。"
            )
        before_index = None
        if before is not None and str(before).strip():
            before_index = int(before)

        if action == "create":
            self._plan_stop_hold = None
            plan = self._plans.create(
                condition=condition or "",
                expected=expected or "",
                result=result or "",
                checks=checks if isinstance(checks, list) else None,
                tasks=tasks or [],
                origin=self._turn_origin if self._turn_origin in ("user", "auto") else "auto",
            )
        elif action == "update":
            if not plan_id:
                raise ValueError("update 需要 plan_id")
            plan = self._plans.update(
                plan_id,
                condition=condition,
                expected=expected,
                result=result,
                checks=checks if isinstance(checks, list) else None,
                tasks=tasks,
            )
        elif action == "add_tasks":
            if not plan_id:
                raise ValueError("add_tasks 需要 plan_id")
            plan = self._plans.add_tasks(
                plan_id,
                tasks or [],
                before=before_index,
            )
        elif action == "pause":
            plan = self._plans.pause(plan_id or "")
        elif action == "resume":
            plan = self._plans.resume(plan_id or "")
        elif action == "cancel":
            plan = self._plans.cancel(plan_id or "", result=result)
        elif action == "complete":
            plan = self._plans.complete(plan_id or "", result=result)
        elif action == "get":
            plan = self._plans.get(plan_id or "")
            if plan is None:
                raise ValueError(f"没有这个 plan：{plan_id}")
        elif action == "list":
            return {
                "plans": [item.view() for item in self._plans.list()],
                "executing": (
                    self._plans.executing().id if self._plans.executing() else None
                ),
            }
        else:
            raise ValueError(
                "action 只能是 create / update / add_tasks / pause / resume / "
                "cancel / complete / get / list"
            )
        if action in ("complete", "cancel"):
            self._plan_failure_counts.pop(plan.id, None)
            if self._plan_stop_hold == plan.id:
                self._plan_stop_hold = None
        if action in ("resume", "update", "add_tasks"):
            self._plan_review_attempts.pop(plan.id, None)
            self._plan_review_retry_at.pop(plan.id, None)
            # 计划被改过 → 同一位置的 task 已经是新做法，自动修复次数重新算。
            for key in [
                item
                for item in self._plan_task_auto_repairs
                if item[0] == plan.id
            ]:
                self._plan_task_auto_repairs.pop(key, None)
        elif action in ("pause", "complete", "cancel"):
            # 用户主动暂停 / 结束，不属于“等复盘”，看门狗不要来插一脚。
            self._plan_review_attempts.pop(plan.id, None)
            self._plan_review_retry_at.pop(plan.id, None)
        logger.info("plan 操作：{} → {}（{}）", action, plan.id, plan.status)
        if action in ("create", "resume", "update", "add_tasks"):
            self._spawn(self._execute_next_plan_task(self.friend_id or ""))
        return {
            "plan": plan.view(),
            "plans": [item.view() for item in self._plans.list()],
            "executing": (
                self._plans.executing().id if self._plans.executing() else None
            ),
        }

    def set_plan(
        self,
        friend_id: str,
        goal: str,
        steps: list[str] | None = None,
        *,
        done: bool = False,
    ) -> dict[str, Any]:
        """设置 / 完成当前计划。空 goal 或 done=true = 清空。"""
        if not self.active_for(friend_id):
            raise RuntimeError("现在没有进行中的游戏会话")
        if not done and self._mode != "free" and not self._plan_user_request_active:
            raise ValueError(
                "现在不是自由模式：没人要求时不要自己定计划；"
                "TA 明确交办多步任务时可以直接建计划"
            )
        goal = str(goal or "").strip()[:500]
        if done or not goal:
            self._plan = ""
            self._plan_steps = []
            self._continuation_retry = 0
            return {"goal": "", "steps": [], "done": True}
        self._plan = goal
        self._plan_steps = [
            str(step).strip()[:300]
            for step in (steps or [])
            if str(step).strip()
        ][:8]
        return {"goal": self._plan, "steps": list(self._plan_steps), "done": False}

    # ---------- 摘要 ----------

    async def _summarize(self, session_id: str) -> None:
        # ① 短事务读日志（LLM 期间不占数据库）
        with session_scope() as db:
            row = game_store.get_session(db, session_id)
            if row is None or row.summary_state == "done":
                return
            events = game_store.recent_events(db, session_id, limit=500)
            transcript = format_events(events)
            friend = db.get(Friend, row.friend_id)
            if friend is None or not transcript.strip():
                row.summary_state = "failed"
                return
            try:
                provider = _provider_for(db, friend)
            except Exception as exc:  # noqa: BLE001 —— 没配模型时也要把日志留下
                logger.warning("游戏摘要：没有可用模型（{}）", exc)
                provider = None
            friend_id = row.friend_id
            game_kind = row.game_kind

        # ② LLM（不占数据库会话）
        try:
            summary, memories = await self._summarizer(provider, transcript)
        except Exception as exc:  # noqa: BLE001
            logger.warning("游戏摘要生成失败：{}", exc)
            with session_scope() as db:
                row = game_store.get_session(db, session_id)
                if row is not None:
                    row.summary_state = "failed"
            return

        # ③ 落库
        with session_scope() as db:
            row = game_store.get_session(db, session_id)
            if row is None:
                return
            row.journal = transcript[-4000:]
            if not summary:
                row.summary_state = "failed"
                return
            memory, _ = memory_service.save(
                db,
                Actor.AGENT,
                friend_id,
                content=f"[游戏·{game_kind}] {summary}",
                tag="normal",
                layer="long_term",
                context=f"game:{game_kind}",
                importance=0.65,
            )
            for item in memories:
                memory_service.save(
                    db,
                    Actor.AGENT,
                    friend_id,
                    content=f"[游戏·{game_kind}] {item['content']}",
                    tag="normal",
                    layer="long_term",
                    context=f"game:{game_kind}",
                    importance=float(item.get("importance", 0.6)),
                )
            row.summary = summary
            row.summary_memory_id = memory.id
            row.summary_state = "done"
        logger.info("游戏摘要已生成：{}（{} 条记忆）", session_id[:12], len(memories))

    async def recover_orphans(self) -> int:
        """进程重启后：把没正常结束的会话标 crashed，并补摘要。"""
        with session_scope() as db:
            crashed = game_store.mark_unfinished_crashed(db)
            pending = [row.id for row in game_store.pending_summaries(db)]
        for session_id in pending:
            self._spawn(self._summarize(session_id))
        if crashed or pending:
            logger.warning(
                "游戏恢复：{} 个会话标 crashed，{} 个补摘要", crashed, len(pending)
            )
        return crashed

    # ---------- 工具 ----------

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)


_manager: GameManager | None = None


def get_manager() -> GameManager:
    global _manager
    if _manager is None:
        _manager = GameManager()
    return _manager
