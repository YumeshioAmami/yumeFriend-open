"""D294：动作下发前的确定性预检 + 失败分类。

这里只读快照、背包和配方事实，不调 LLM、不猜世界状态。
目标是拦住“明知会失败”的动作，并把失败翻译成模型能直接照做的恢复建议。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

PREFLIGHT_ACTIONS = (
    "mine",
    "place",
    "craft",
    "equip",
    "eat",
    "drop",
    "bring",
    "smelt",
    "goto",
    "attack",
    "sequence",
)

FAILURE_KINDS = (
    "format",
    "missing_tool",
    "missing_item",
    "missing_food",
    "missing_ingredient",
    "no_support",
    "occupied",
    "unreachable",
    "goal_changed",
    "timeout",
    "unknown",
)

_AIR_BLOCKS = {"air", "cave_air", "void_air"}
_PICKAXE_WORDS = ("stone", "cobblestone", "deepslate", "_ore")
_SUPPORT_OFFSETS = (
    (0, -1, 0),
    (0, 1, 0),
    (1, 0, 0),
    (-1, 0, 0),
    (0, 0, 1),
    (0, 0, -1),
)


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    kind: str = ""
    message: str = ""
    suggestion: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "message": self.message,
            "suggestion": self.suggestion,
            "details": dict(self.details),
        }

    def error_text(self) -> str:
        text = self.message or "预检未通过"
        if self.suggestion:
            text += f"；建议：{self.suggestion}"
        return text


def _ok() -> PreflightResult:
    return PreflightResult(True)


def _fail(
    kind: str,
    message: str,
    suggestion: str = "",
    **details: Any,
) -> PreflightResult:
    return PreflightResult(
        False,
        kind=kind,
        message=message,
        suggestion=suggestion,
        details=details,
    )


def _item_name(args: Mapping[str, Any]) -> str:
    return str(args.get("item") or args.get("block") or args.get("target") or "").strip()


def _coord(args: Mapping[str, Any], key: str) -> float | None:
    try:
        return float(args.get(key))
    except (TypeError, ValueError):
        return None


def _inventory_count(snapshot: Mapping[str, Any], item: str) -> int:
    total = 0
    if not item:
        return 0
    for entry in snapshot.get("inventory") or []:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("item") or "") == item:
            try:
                total += int(entry.get("count") or 0)
            except (TypeError, ValueError):
                pass
    held = snapshot.get("held")
    if isinstance(held, Mapping) and str(held.get("item") or "") == item:
        try:
            total += int(held.get("count") or 0)
        except (TypeError, ValueError):
            pass
    return total


def _has_pickaxe(snapshot: Mapping[str, Any]) -> bool:
    if _inventory_count(snapshot, "pickaxe") > 0:
        return True
    for entry in snapshot.get("inventory") or []:
        if isinstance(entry, Mapping) and str(entry.get("item") or "").endswith("_pickaxe"):
            return True
    held = snapshot.get("held")
    return bool(
        isinstance(held, Mapping)
        and str(held.get("item") or "").endswith("_pickaxe")
    )


def _has_food(snapshot: Mapping[str, Any]) -> bool:
    return any(
        isinstance(entry, Mapping) and bool(entry.get("food"))
        for entry in snapshot.get("inventory") or []
    )


def _block_name(entry: Mapping[str, Any] | None) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    name = entry.get("name")
    return str(name) if name is not None else None


def _block_loaded_and_solid(entry: Mapping[str, Any] | None) -> bool | None:
    name = _block_name(entry)
    if name is None:
        return None
    return name not in _AIR_BLOCKS


def check_recipe(
    recipe: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any],
    item: str,
    count: int = 1,
) -> PreflightResult:
    """用 minecraft-data 的配方事实检查材料；没有配方数据就放行。"""
    if not recipe or not recipe.get("ok") or not recipe.get("recipes"):
        return _ok()
    best_missing: list[dict[str, Any]] = []
    for candidate in recipe["recipes"]:
        if not isinstance(candidate, Mapping):
            continue
        output = (
            candidate.get("output")
            if isinstance(candidate.get("output"), Mapping)
            else {}
        )
        output_count = max(1, int(output.get("count") or 1))
        batches = max(1, math.ceil(max(1, count) / output_count))
        missing: list[dict[str, Any]] = []
        for ingredient in candidate.get("ingredients") or []:
            if not isinstance(ingredient, Mapping):
                continue
            name = str(ingredient.get("item") or "").strip()
            need = max(0, int(ingredient.get("count") or 0)) * batches
            if not name or need <= 0:
                continue
            have = _inventory_count(snapshot, name)
            if have < need:
                missing.append({"item": name, "need": need, "have": have})
        if not missing:
            return _ok()
        if not best_missing or len(missing) < len(best_missing):
            best_missing = missing
    if not best_missing:
        return _ok()
    text = "、".join(f"{item['item']} {item['have']}/{item['need']}" for item in best_missing)
    return _fail(
        "missing_ingredient",
        f"合成 {item} 的材料不够：{text}",
        "先补齐缺少的材料；需要工作台就先做并放下工作台。",
        item=item,
        missing=best_missing,
    )


def check_place_support(
    blocks: list[Mapping[str, Any]],
    target: Mapping[str, Any],
) -> PreflightResult:
    """用 inspect_blocks 的结果检查目标格是否被占 / 有没有可依托面。"""
    tx = _coord(target, "x")
    ty = _coord(target, "y")
    tz = _coord(target, "z")
    if tx is None or ty is None or tz is None:
        return _ok()
    by_pos: dict[tuple[float, float, float], Mapping[str, Any]] = {}
    for entry in blocks:
        if not isinstance(entry, Mapping):
            continue
        x, y, z = _coord(entry, "x"), _coord(entry, "y"), _coord(entry, "z")
        if None not in (x, y, z):
            by_pos[(x, y, z)] = entry
    target_block = by_pos.get((tx, ty, tz))
    occupied = _block_loaded_and_solid(target_block)
    if occupied is True:
        return _fail(
            "occupied",
            f"目标位置 ({tx:g},{ty:g},{tz:g}) 已经有方块了",
            "换一个相邻空格，不要原样重试。",
            target=dict(target),
        )
    supports = 0
    loaded_neighbors = 0
    for dx, dy, dz in _SUPPORT_OFFSETS:
        entry = by_pos.get((tx + dx, ty + dy, tz + dz))
        solid = _block_loaded_and_solid(entry)
        if solid is None:
            continue
        loaded_neighbors += 1
        if solid:
            supports += 1
    if supports == 0 and loaded_neighbors > 0:
        return _fail(
            "no_support",
            f"目标位置 ({tx:g},{ty:g},{tz:g}) 六个方向都没有可依托的方块",
            "先放一块支撑方块，或者换到贴着实心方块的侧面；不要原样重试。",
            target=dict(target),
        )
    return _ok()


def check_action(
    action: str,
    args: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    *,
    recipe: Mapping[str, Any] | None = None,
) -> PreflightResult:
    """检查一个动作的明显前置条件；未知动作直接放行。"""
    action = str(action or "").strip()
    params = args if isinstance(args, Mapping) else {}
    snap = snapshot if isinstance(snapshot, Mapping) else {}

    if action not in PREFLIGHT_ACTIONS:
        return _ok()

    if action == "sequence":
        steps = params.get("steps")
        if not isinstance(steps, list) or not steps:
            return _fail("format", "sequence 需要 args.steps 数组", "按 game_do 的示例重写 steps。")
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, Mapping):
                return _fail("format", f"sequence 第 {index} 步不是对象", "每步都要有 action/args。")
            nested = check_action(
                str(step.get("action") or ""),
                step.get("args") if isinstance(step.get("args"), Mapping) else {},
                snap,
            )
            if not nested.ok:
                return _fail(
                    nested.kind,
                    f"sequence 第 {index} 步预检失败：{nested.message}",
                    nested.suggestion,
                    **nested.details,
                )
        return _ok()

    if action == "goto":
        if any(_coord(params, key) is None for key in ("x", "y", "z")):
            return _fail(
                "format",
                "goto 需要 x / y / z 数字",
                '用 {"action":"goto","args":{"x":0,"y":64,"z":0}}。',
            )
        return _ok()

    if action == "attack":
        if not (params.get("mob") or params.get("target") or params.get("name")):
            return _fail("format", "attack 需要目标名称", "先看附近实体，再指定一个具体目标。")
        return _ok()

    if action == "mine":
        block = _item_name(params)
        if not block:
            return _fail("format", "mine 需要 block", "例如 {\"block\":\"oak_log\",\"count\":4}。")
        if any(word in block for word in _PICKAXE_WORDS) and not _has_pickaxe(snap):
            return _fail(
                "missing_tool",
                f"没有镐子，挖 {block} 不会掉落",
                "先 craft 一把木镐/石镐，或换不需要镐子的目标。",
                block=block,
            )
        return _ok()

    if action == "place":
        block = _item_name(params)
        if not block:
            return _fail("format", "place 需要 block", "例如 {\"block\":\"oak_planks\",\"x\":0,\"y\":64,\"z\":0}。")
        if any(_coord(params, key) is None for key in ("x", "y", "z")):
            return _fail("format", "place 需要 x / y / z 数字", "给出具体方块坐标。")
        if _inventory_count(snap, block) <= 0:
            return _fail(
                "missing_item",
                f"背包里没有 {block}",
                f"先采集/合成 {block}，或换一种已有方块。",
                item=block,
            )
        return _ok()

    if action == "craft":
        item = _item_name(params)
        if not item:
            return _fail("format", "craft 需要 item", "例如 {\"item\":\"oak_planks\",\"count\":4}。")
        try:
            count = max(1, int(params.get("count") or 1))
        except (TypeError, ValueError):
            count = 1
        return check_recipe(recipe, snap, item, count)

    if action == "equip":
        item = _item_name(params)
        if not item:
            return _fail("format", "equip 需要 item", "例如 {\"item\":\"stone_pickaxe\"}。")
        if _inventory_count(snap, item) <= 0:
            return _fail("missing_item", f"背包里没有 {item}", "先合成/找到它，或换一件已有的工具。", item=item)
        return _ok()

    if action == "eat":
        item = _item_name(params)
        if item and _inventory_count(snap, item) <= 0:
            return _fail("missing_item", f"背包里没有 {item}", "换一种现有食物，或先去找食物。", item=item)
        if not item and not _has_food(snap):
            return _fail("missing_food", "背包里没有能吃的食物", "先找食物，不要空着肚子继续硬做。")
        return _ok()

    if action in ("drop", "bring"):
        item = _item_name(params)
        if not item:
            return _fail("format", f"{action} 需要 item", "指定具体物品名。")
        try:
            count = max(1, int(params.get("count") or 1))
        except (TypeError, ValueError):
            count = 1
        if _inventory_count(snap, item) < count:
            return _fail(
                "missing_item",
                f"背包里 {item} 不够（要 {count} 个）",
                "先确认背包，数量不够就换目标或先采集。",
                item=item,
                need=count,
            )
        return _ok()

    if action == "smelt":
        item = _item_name(params)
        if not item:
            return _fail("format", "smelt 需要 item", "例如 {\"item\":\"iron_ore\",\"count\":1}。")
        if _inventory_count(snap, item) <= 0:
            return _fail("missing_item", f"背包里没有 {item}", "先挖/收集原料，或换目标。", item=item)
        return _ok()

    return _ok()


def classify_failure(text: Any) -> str:
    """把适配器/复盘错误文本归类；只根据明确词，不猜。"""
    value = str(text or "").lower()
    if not value:
        return "unknown"
    if any(
        word in value
        for word in (
            "格式错误",
            "需要 steps",
            "需要 block",
            "需要 x",
            "需要 item",
            "需要 positions",
        )
    ):
        return "format"
    if any(word in value for word in ("没有镐子", "需要木镐", "需要石镐", "pickaxe")):
        return "missing_tool"
    if any(word in value for word in ("没有吃的", "没有食物", "没有能吃的")):
        return "missing_food"
    if any(
        word in value
        for word in (
            "材料不够",
            "missing ingredient",
            "需要工作台",
            "需要 crafting_table",
            "配方",
        )
    ):
        return "missing_ingredient"
    if any(word in value for word in ("四面悬空", "没有可依托", "still air", "没有支撑")):
        return "no_support"
    if any(word in value for word in ("已经有方块", "occupied", "位置被占")):
        return "occupied"
    if any(word in value for word in ("goal was changed", "目标被改变", "goal changed")):
        return "goal_changed"
    if any(
        word in value
        for word in (
            "没找到",
            "不可达",
            "太远或被挡住",
            "unreachable",
            "寻路失败",
            "没有路径",
        )
    ):
        return "unreachable"
    if any(word in value for word in ("超时", "timeout", "timed out")):
        return "timeout"
    if any(word in value for word in ("背包里没有", "not enough", "数量不够")):
        return "missing_item"
    return "unknown"


def recovery_hint(
    kind: str,
    action: str = "",
    args: Mapping[str, Any] | None = None,
) -> str:
    params = args if isinstance(args, Mapping) else {}
    target = _item_name(params)
    if kind == "format":
        return "按工具示例重写成正确参数；不要再发同样的坏格式。"
    if kind == "missing_tool":
        return "先 craft 木镐/石镐（木镐=木板+木棍），或者换不需要镐子的目标。"
    if kind == "missing_food":
        return "先找食物或吃背包里的东西；不要继续硬做。"
    if kind == "missing_ingredient":
        return f"先补齐 {target or '缺少的材料'}，需要工作台就先做并放下工作台。"
    if kind == "missing_item":
        return f"先采集/合成 {target or '缺少的物品'}，或者换一个已有物品。"
    if kind == "no_support":
        return "先放一块支撑方块，或者换到贴着实心方块的侧面；不要原样重试。"
    if kind == "occupied":
        return "换一个相邻空格坐标；不要往已经有方块的格子放。"
    if kind == "goal_changed":
        return "重新看当前目标和玩家位置，丢掉旧目标后再行动。"
    if kind == "unreachable":
        return "换最近的目标/换地方；不要沿同一条失败路径原样重试。"
    if kind == "timeout":
        return "把动作拆小、换目标或先休息；不要连续重复同一个超时动作。"
    return "先看当前状态，换动作或换目标；不要原样重试。"
