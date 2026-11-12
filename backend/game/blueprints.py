"""蓝图建造与自由创作（v5.1-P5 / D289）。

蓝图是可验证的：逐层放块，再用真实方块查询做 `block_at` 校验。
自由创作不判失败：只记录设计、前后 diff 和用户评分。
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

PlaceFn = Callable[[dict[str, Any], float], Awaitable[dict[str, Any]]]
InspectFn = Callable[[list[dict[str, int]]], Awaitable[list[dict[str, Any]]]]
ClearFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _number(value: Any, name: str, *, minimum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return number


def _block_name(value: Any, palette: Mapping[str, str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("方块名不能为空")
    return palette.get(raw, raw)


@dataclass
class BlueprintBlock:
    x: int
    y: int
    z: int
    block: str

    def absolute(self, anchor: Mapping[str, int]) -> dict[str, Any]:
        return {
            "x": int(anchor["x"]) + self.x,
            "y": int(anchor["y"]) + self.y,
            "z": int(anchor["z"]) + self.z,
            "block": self.block,
        }


@dataclass
class Blueprint:
    name: str
    description: str
    size: dict[str, int]
    palette: dict[str, str]
    layers: list[tuple[int, list[BlueprintBlock]]]
    structure: str = ""
    screenshot: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> "Blueprint":
        if not isinstance(raw, Mapping):
            raise ValueError("蓝图必须是 JSON 对象")
        name = str(raw.get("name") or "").strip()
        description = str(raw.get("description") or raw.get("brief") or "").strip()
        if not name or not description:
            raise ValueError("蓝图需要 name 和 description（设计意图）")
        size_raw = raw.get("size")
        if not isinstance(size_raw, Mapping):
            raise ValueError("蓝图需要 size: {x,y,z}")
        size = {
            axis: _number(size_raw.get(axis), f"size.{axis}", minimum=1)
            for axis in ("x", "y", "z")
        }
        palette_raw = raw.get("palette")
        palette = (
            {str(key): str(value) for key, value in palette_raw.items()}
            if isinstance(palette_raw, Mapping)
            else {}
        )
        layers_raw = raw.get("layers")
        if not isinstance(layers_raw, list) or not layers_raw:
            raise ValueError("蓝图需要 layers（至少一层）")
        layers: list[tuple[int, list[BlueprintBlock]]] = []
        seen: set[tuple[int, int, int]] = set()
        for layer_index, layer_raw in enumerate(layers_raw):
            if not isinstance(layer_raw, Mapping):
                raise ValueError("each layer 必须是对象")
            y = _number(layer_raw.get("y", layer_index), "layer.y", minimum=0)
            blocks_raw = layer_raw.get("blocks")
            if not isinstance(blocks_raw, list):
                raise ValueError("layer.blocks 必须是数组")
            blocks: list[BlueprintBlock] = []
            for item in blocks_raw:
                if not isinstance(item, Mapping):
                    raise ValueError("block 必须是对象")
                block = BlueprintBlock(
                    x=_number(item.get("x"), "block.x", minimum=0),
                    y=_number(item.get("y", y), "block.y", minimum=0),
                    z=_number(item.get("z"), "block.z", minimum=0),
                    block=_block_name(item.get("block"), palette),
                )
                if (
                    block.x >= size["x"]
                    or block.y >= size["y"]
                    or block.z >= size["z"]
                ):
                    raise ValueError(
                        f"方块 ({block.x},{block.y},{block.z}) 超出蓝图尺寸 {size}"
                    )
                key = (block.x, block.y, block.z)
                if key in seen:
                    raise ValueError(f"方块坐标重复：{key}")
                seen.add(key)
                blocks.append(block)
            if not blocks:
                raise ValueError(f"第 {layer_index + 1} 层没有方块")
            layers.append((y, blocks))
        return cls(
            name=name,
            description=description,
            size=size,
            palette=palette,
            layers=layers,
            structure=str(raw.get("structure") or raw.get("sketch") or ""),
            screenshot=str(raw["screenshot"]) if raw.get("screenshot") else None,
        )

    def requirements(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _y, blocks in self.layers:
            for block in blocks:
                if block.block == "air":
                    continue
                counts[block.block] = counts.get(block.block, 0) + 1
        return counts

    def block_checks(self, anchor: Mapping[str, int]) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for _y, blocks in self.layers:
            for block in blocks:
                if block.block == "air":
                    continue
                pos = block.absolute(anchor)
                checks.append(
                    {
                        "type": "block_at",
                        "x": pos["x"],
                        "y": pos["y"],
                        "z": pos["z"],
                        "block": pos["block"],
                    }
                )
        return checks

    def view(self, anchor: Mapping[str, int] | None = None) -> dict[str, Any]:
        data = {
            "name": self.name,
            "description": self.description,
            "size": dict(self.size),
            "palette": dict(self.palette),
            "structure": self.structure,
            "screenshot": self.screenshot,
            "layers": [
                {
                    "y": y,
                    "blocks": [
                        {"x": block.x, "y": block.y, "z": block.z, "block": block.block}
                        for block in blocks
                    ],
                }
                for y, blocks in self.layers
            ],
            "requirements": self.requirements(),
        }
        if anchor is not None:
            data["anchor"] = dict(anchor)
            data["checks"] = self.block_checks(anchor)
        return data


def material_gap(
    requirements: Mapping[str, int], snapshot: Mapping[str, Any]
) -> dict[str, int]:
    inventory: dict[str, int] = {}
    for item in snapshot.get("inventory") or []:
        if isinstance(item, Mapping):
            name = str(item.get("item") or "")
            try:
                inventory[name] = inventory.get(name, 0) + int(item.get("count") or 0)
            except (TypeError, ValueError):
                pass
    held = snapshot.get("held")
    if isinstance(held, Mapping):
        name = str(held.get("item") or "")
        try:
            inventory[name] = inventory.get(name, 0) + int(held.get("count") or 0)
        except (TypeError, ValueError):
            pass
    return {
        name: count - inventory.get(name, 0)
        for name, count in requirements.items()
        if count > inventory.get(name, 0)
    }


def simple_hut_blueprint(
    material: str,
    *,
    width: int = 5,
    depth: int = 5,
    height: int = 2,
) -> Blueprint:
    """生成一栋确定性的空心小屋蓝图。

    这是给 `game_build(action="hut")` 用的“身体能力”：地板、墙、门洞、窗和
    平屋顶都由代码排好，LLM 只决定材料、尺寸和大致位置。
    """
    material = _block_name(material, {})
    width = _number(width, "width", minimum=3)
    depth = _number(depth, "depth", minimum=3)
    height = _number(height, "height", minimum=2)
    if width > 7 or depth > 7 or height > 4:
        raise ValueError("小屋尺寸太大：width / depth 最多 7，height 最多 4")

    door_x = width // 2
    layers: list[dict[str, Any]] = []
    layers.append(
        {
            "y": 0,
            "blocks": [
                {"x": x, "y": 0, "z": z, "block": material}
                for x in range(width)
                for z in range(depth)
            ],
        }
    )
    for y in range(1, height + 1):
        blocks: list[dict[str, Any]] = []
        for x in range(width):
            for z in range(depth):
                perimeter = x in (0, width - 1) or z in (0, depth - 1)
                if not perimeter:
                    continue
                if z == 0 and x == door_x and y <= 2:
                    continue
                # 两侧各留一格窗洞，避免整栋像个实心土块。
                if y == 2 and z == depth // 2 and x in (0, width - 1):
                    continue
                blocks.append({"x": x, "y": y, "z": z, "block": material})
        layers.append({"y": y, "blocks": blocks})
    roof_y = height + 1
    layers.append(
        {
            "y": roof_y,
            "blocks": [
                {"x": x, "y": roof_y, "z": z, "block": material}
                for x in range(width)
                for z in range(depth)
            ],
        }
    )
    safe_name = "".join(
        char if char.isalnum() or char in ("_", "-") else "_"
        for char in material
    ).strip("_") or "material"
    return Blueprint.from_dict(
        {
            "name": f"{safe_name}_hut",
            "description": (
                f"{width}×{height + 2}×{depth} 的 {material} 小屋："
                "地板、两格高墙、中间门洞、侧窗和平屋顶"
            ),
            "size": {"x": width, "y": height + 2, "z": depth},
            "palette": {"material": material},
            "structure": "先铺地板，再围墙留门洞和侧窗，最后盖平屋顶",
            "layers": layers,
        }
    )


def _box_ranges(item: Mapping[str, Any]) -> tuple[range, range, range]:
    """parts / holes 用：from+to 或 x/y/z+width/height/depth。"""
    start = item.get("from")
    end = item.get("to")
    if isinstance(start, Mapping) and isinstance(end, Mapping):
        x1, y1, z1 = (
            _number(start.get(axis), f"from.{axis}") for axis in ("x", "y", "z")
        )
        x2, y2, z2 = (
            _number(end.get(axis), f"to.{axis}") for axis in ("x", "y", "z")
        )
        return (
            range(min(x1, x2), max(x1, x2) + 1),
            range(min(y1, y2), max(y1, y2) + 1),
            range(min(z1, z2), max(z1, z2) + 1),
        )
    x = _number(item.get("x"), "x", minimum=0)
    y = _number(item.get("y"), "y", minimum=0)
    z = _number(item.get("z"), "z", minimum=0)
    width = _number(item.get("width", 1), "width", minimum=1)
    height = _number(item.get("height", 1), "height", minimum=1)
    depth = _number(item.get("depth", 1), "depth", minimum=1)
    return (
        range(x, x + width),
        range(y, y + height),
        range(z, z + depth),
    )


def compile_design(raw: Any) -> Blueprint:
    """把 LLM 的自由设计编译成可验证蓝图。

    支持两种写法：
      1. 直接给 layers（完整体素蓝图）；
      2. 给 size + palette + parts（盒子/墙/洞），由代码展开成体素。

    parts 只表达“她想建成什么”，不限制必须是预设小屋。
    """
    if not isinstance(raw, Mapping):
        raise ValueError("设计必须是 JSON 对象")
    if raw.get("layers") and not raw.get("parts"):
        return Blueprint.from_dict(raw)
    name = str(raw.get("name") or "").strip()
    description = str(raw.get("description") or raw.get("brief") or "").strip()
    if not name or not description:
        raise ValueError("设计需要 name 和 description（建的是什么、为什么）")
    size_raw = raw.get("size")
    if not isinstance(size_raw, Mapping):
        raise ValueError("设计需要 size: {x,y,z}")
    size = {
        axis: _number(size_raw.get(axis), f"size.{axis}", minimum=1)
        for axis in ("x", "y", "z")
    }
    palette_raw = raw.get("palette")
    palette = (
        {str(key): str(value) for key, value in palette_raw.items()}
        if isinstance(palette_raw, Mapping)
        else {}
    )
    parts = raw.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("设计需要 parts（盒子/墙等结构）或完整 layers")

    cells: dict[tuple[int, int, int], str] = {}
    for index, part in enumerate(parts, start=1):
        if not isinstance(part, Mapping):
            raise ValueError(f"part {index} 必须是对象")
        xs, ys, zs = _box_ranges(part)
        block = str(part.get("block") or "").strip()
        if not block:
            raise ValueError(f"part {index} 需要 block")
        is_hole = str(part.get("type") or "").lower() in ("hole", "air")
        for x in xs:
            for y in ys:
                for z in zs:
                    if x >= size["x"] or y >= size["y"] or z >= size["z"]:
                        raise ValueError(f"part {index} 超出 size {size}")
                    key = (x, y, z)
                    if is_hole or block == "air":
                        cells.pop(key, None)
                    else:
                        cells[key] = block

    holes = raw.get("holes")
    if isinstance(holes, list):
        for index, hole in enumerate(holes, start=1):
            if not isinstance(hole, Mapping):
                raise ValueError(f"hole {index} 必须是对象")
            xs, ys, zs = _box_ranges(hole)
            for x in xs:
                for y in ys:
                    for z in zs:
                        cells.pop((x, y, z), None)

    if not cells:
        raise ValueError("设计里没有任何实心方块")
    by_y: dict[int, list[dict[str, Any]]] = {}
    for (x, y, z), block in sorted(cells.items()):
        by_y.setdefault(y, []).append(
            {"x": x, "y": y, "z": z, "block": block}
        )
    layers = [
        {"y": y, "blocks": by_y[y]}
        for y in sorted(by_y)
    ]
    return Blueprint.from_dict(
        {
            "name": name,
            "description": description,
            "size": size,
            "palette": palette,
            "structure": str(raw.get("structure") or "按 parts 编译的自由设计"),
            "layers": layers,
        }
    )


def procurement_tasks(gaps: Mapping[str, int]) -> list[dict[str, Any]]:
    """把材料缺口翻译成现有 plan 能执行的 mine / craft task。"""
    tasks: list[dict[str, Any]] = []
    for item, missing in gaps.items():
        if item == "oak_planks":
            logs = math.ceil(missing / 4)
            tasks.append(
                {
                    "action": "mine",
                    "args": {"block": "oak_log", "count": logs},
                    "checks": [
                        {
                            "type": "inventory_has",
                            "item": "oak_log",
                            "count": f">={logs}",
                        }
                    ],
                }
            )
            tasks.append(
                {
                    "action": "craft",
                    "args": {"item": "oak_planks", "count": missing},
                    "checks": [
                        {
                            "type": "inventory_has",
                            "item": "oak_planks",
                            "count": f">={missing}",
                        }
                    ],
                }
            )
            continue
        mine_alias = {
            "cobblestone": "stone",
            "stone": "stone",
            "dirt": "dirt",
            "sand": "sand",
            "gravel": "gravel",
            "oak_log": "oak_log",
        }.get(item)
        if mine_alias:
            tasks.append(
                {
                    "action": "mine",
                    "args": {"block": mine_alias, "count": missing},
                    "checks": [
                        {
                            "type": "inventory_has",
                            "item": item,
                            "count": f">={missing}",
                        }
                    ],
                }
            )
        else:
            tasks.append(
                {
                    "action": "craft",
                    "args": {"item": item, "count": missing},
                    "checks": [
                        {
                            "type": "inventory_has",
                            "item": item,
                            "count": f">={missing}",
                        }
                    ],
                }
            )
    return tasks


def validate_creative_design(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("自由创作需要设计对象")
    brief = str(raw.get("brief") or raw.get("description") or "").strip()
    design = raw.get("design") if isinstance(raw.get("design"), Mapping) else raw
    has_design = any(
        design.get(key)
        for key in ("sketch", "palette", "structure", "blueprint")
    )
    if not brief or not has_design:
        raise ValueError(
            "创作前必须先设计：至少给 brief + sketch / palette / structure / blueprint 之一"
        )
    return {"brief": brief, "design": dict(design)}


def cube_positions(anchor: Mapping[str, int], size: Mapping[str, int]) -> list[dict[str, int]]:
    """给自由创作做前后 diff 的体积采样；限制最多 4096 个坐标。"""
    count = int(size["x"]) * int(size["y"]) * int(size["z"])
    if count > 4096:
        raise ValueError("自由创作体积太大（最多 4096 格）；缩小一点再记录")
    return [
        {
            "x": int(anchor["x"]) + x,
            "y": int(anchor["y"]) + y,
            "z": int(anchor["z"]) + z,
        }
        for x in range(int(size["x"]))
        for y in range(int(size["y"]))
        for z in range(int(size["z"]))
    ]


def block_diff(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> dict[str, Any]:
    before_map = {
        (int(item["x"]), int(item["y"]), int(item["z"])): str(item.get("name") or "air")
        for item in before
        if isinstance(item, Mapping)
    }
    after_map = {
        (int(item["x"]), int(item["y"]), int(item["z"])): str(item.get("name") or "air")
        for item in after
        if isinstance(item, Mapping)
    }
    changed: list[dict[str, Any]] = []
    for pos in sorted(set(before_map) | set(after_map)):
        old = before_map.get(pos, "air")
        new = after_map.get(pos, "air")
        if old != new:
            changed.append(
                {"x": pos[0], "y": pos[1], "z": pos[2], "before": old, "after": new}
            )
    return {"changed": changed, "changed_count": len(changed)}


class BlueprintRunner:
    """逐层放块并做真实方块校验；确定性执行，不调 LLM。"""

    def __init__(
        self,
        place: PlaceFn,
        inspect: InspectFn,
        *,
        clear: ClearFn | None = None,
        retry_delay: float = 0.15,
    ) -> None:
        self._place = place
        self._inspect = inspect
        self._clear = clear
        self._retry_delay = max(0.0, float(retry_delay))

    async def run(
        self,
        blueprint: Blueprint,
        anchor: Mapping[str, int],
        *,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        placed: set[tuple[int, int, int]] = set()
        cleared = 0
        all_targets: dict[tuple[int, int, int], dict[str, Any]] = {}
        for _y, blocks in blueprint.layers:
            for block in blocks:
                if block.block == "air":
                    continue
                target = block.absolute(anchor)
                all_targets[(target["x"], target["y"], target["z"])] = target
        air_targets: dict[tuple[int, int, int], dict[str, Any]] = {}
        for x in range(int(blueprint.size["x"])):
            for y in range(int(blueprint.size["y"])):
                for z in range(int(blueprint.size["z"])):
                    key = (
                        int(anchor["x"]) + x,
                        int(anchor["y"]) + y,
                        int(anchor["z"]) + z,
                    )
                    if key not in all_targets:
                        air_targets[key] = {
                            "x": key[0],
                            "y": key[1],
                            "z": key[2],
                            "block": "air",
                        }
        if len(all_targets) + len(air_targets) > 4096:
            raise ValueError("蓝图体积太大（最多 4096 格）；缩小一点")
        all_positions = [
            {"x": item["x"], "y": item["y"], "z": item["z"]}
            for item in [*all_targets.values(), *air_targets.values()]
        ]

        async def inspect_map() -> dict[tuple[int, int, int], str]:
            found = await self._inspect(all_positions)
            return {
                (int(item["x"]), int(item["y"]), int(item["z"])): str(
                    item.get("name") or "air"
                )
                for item in found
                if isinstance(item, Mapping)
            }

        async def place_target(target: dict[str, Any]) -> tuple[bool, str]:
            errors: list[str] = []
            for _attempt in range(3):
                result = await self._place(target, 25.0)
                if not result.get("error") and not result.get("failed"):
                    key = (target["x"], target["y"], target["z"])
                    placed.add(key)
                    return True, ""
                errors.append(str(result.get("error") or "放块失败"))
                await asyncio.sleep(self._retry_delay)
            return False, errors[-1] if errors else "放块失败"

        async def clear_target(target: dict[str, Any]) -> tuple[bool, str]:
            if self._clear is None:
                return False, "没有清理能力"
            result = await self._clear(target)
            if result.get("error") or result.get("failed"):
                return False, str(result.get("error") or "清理失败")
            if result.get("cleared") is False:
                return False, "清理后目标格仍然不是空气"
            return True, ""

        failures: list[dict[str, Any]] = []
        for layer_number, (_y, blocks) in enumerate(blueprint.layers, start=1):
            targets = [
                block.absolute(anchor)
                for block in blocks
                if block.block != "air"
            ]
            before_map = await inspect_map()
            for target in targets:
                key = (target["x"], target["y"], target["z"])
                if before_map.get(key) == target["block"]:
                    continue
                if before_map.get(key) not in (None, "air", "unknown"):
                    # D322：同一锚点原位修复。冲突格如果是可清理方块，先清掉；
                    # 挖不动 / 容器类则由 manager 拒绝，最终如实报 conflict。
                    if self._clear is None:
                        continue
                    ok, _reason = await clear_target(
                        {**target, "observed": before_map.get(key)}
                    )
                    if not ok:
                        continue
                    cleared += 1
                await place_target(target)
            if on_progress:
                current = await inspect_map()
                checked = sum(
                    1
                    for key, target in all_targets.items()
                    if key in current and current[key] == target["block"]
                )
                on_progress(
                    {
                        "layer": layer_number,
                        "layers": len(blueprint.layers),
                        "placed": len(placed),
                        "checked": checked,
                        "failures": len(all_targets) - checked,
                    }
                )

        # 先在同一锚点把“空气缺格”补上；已经有别的方块的格子单独记为 conflict，
        # 不假装成功，也不建议模型换地方重开整座建筑。
        for _repair_round in range(2):
            current = await inspect_map()
            retry_targets = [
                target
                for key, target in all_targets.items()
                if current.get(key) != target["block"]
                and (
                    current.get(key) == "air"
                    or (self._clear is not None and current.get(key) != "unknown")
                )
            ]
            air_blocked = [
                target
                for key, target in air_targets.items()
                if current.get(key) not in (None, "air", "unknown")
                and self._clear is not None
            ]
            if not retry_targets and not air_blocked:
                break
            for target in retry_targets:
                key = (target["x"], target["y"], target["z"])
                if current.get(key) not in (None, "air", "unknown"):
                    ok, _reason = await clear_target(
                        {**target, "observed": current.get(key)}
                    )
                    if not ok:
                        continue
                    cleared += 1
                await place_target(target)
                await asyncio.sleep(self._retry_delay)
            for target in air_blocked:
                key = (target["x"], target["y"], target["z"])
                ok, _reason = await clear_target(
                    {**target, "observed": current.get(key)}
                )
                if ok:
                    cleared += 1
                await asyncio.sleep(self._retry_delay)

        final_map = await inspect_map()
        missing: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        air_conflicts: list[dict[str, Any]] = []
        for key, target in all_targets.items():
            observed = final_map.get(key, "unknown")
            if observed == target["block"]:
                continue
            item = {
                "x": target["x"],
                "y": target["y"],
                "z": target["z"],
                "block": target["block"],
                "observed": observed,
            }
            if observed in ("air", "unknown"):
                item["error"] = "block_at 校验失败：目标格仍是空气"
                missing.append(item)
            else:
                item["error"] = f"block_at 冲突：目标格是 {observed}"
                conflicts.append(item)
            failures.append(item)
        for key, target in air_targets.items():
            observed = final_map.get(key, "unknown")
            if observed in ("air", "unknown"):
                continue
            item = {
                "x": target["x"],
                "y": target["y"],
                "z": target["z"],
                "block": "air",
                "observed": observed,
                "error": f"预期空气格被 {observed} 挡住",
            }
            air_conflicts.append(item)
            failures.append(item)

        return {
            "ok": not failures,
            "name": blueprint.name,
            "anchor": dict(anchor),
            "placed": len(placed),
            "cleared": cleared,
            "checked": len(all_targets),
            "layers": len(blueprint.layers),
            "failures": failures[:20],
            "missing": missing[:20],
            "conflicts": conflicts[:20],
            "air_conflicts": air_conflicts[:20],
            "error": (
                "蓝图校验失败："
                + "；".join(
                    f"({item['x']},{item['y']},{item['z']}) {item.get('error')}"
                    for item in failures[:5]
                )
                if failures
                else ""
            ),
        }
