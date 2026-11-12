"""结构化后置条件（v5.1-P3 / D288）。

检查只读 **快照 + 任务返回值**，不调 LLM、不猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

CHECK_TYPES = (
    "inventory_has",
    "block_at",
    "block_count",
    "entity_near",
    "position_near",
    "health",
    "food",
    "task_result",
)

_CMP = re.compile(r"^(>=|<=|==|!=|>|<)?\s*(-?\d+(?:\.\d+)?)$")


def _compare(actual: Any, spec: Any) -> tuple[bool, str, Any]:
    """返回 (是否通过, 运算符, 期望值)。默认数字用 >=。"""
    if isinstance(spec, bool) or not isinstance(spec, (int, float, str)):
        return actual == spec, "==", spec
    if isinstance(spec, (int, float)):
        op, expected = ">=", float(spec)
    else:
        match = _CMP.match(str(spec).strip())
        if not match:
            return str(actual) == str(spec), "==", spec
        op = match.group(1) or ">="
        expected = float(match.group(2))
    try:
        left = float(actual)
    except (TypeError, ValueError):
        return False, op, expected
    ok = {
        ">=": left >= expected,
        ">": left > expected,
        "<=": left <= expected,
        "<": left < expected,
        "==": left == expected,
        "!=": left != expected,
    }[op]
    return ok, op, expected


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _block_at(snapshot: Mapping[str, Any], x: float, y: float, z: float) -> dict[str, Any] | None:
    direct = snapshot.get("block_at")
    if isinstance(direct, Mapping):
        return dict(direct)
    blocks = snapshot.get("blocks")
    if not isinstance(blocks, Mapping):
        return None
    for value in blocks.values():
        if not isinstance(value, Mapping):
            continue
        if (
            _number(value.get("x")) == x
            and _number(value.get("y")) == y
            and _number(value.get("z")) == z
        ):
            return dict(value)
    return None


def _task_value(task_result: Any, path: str | None, key: str | None = None) -> Any:
    current = task_result
    dotted = path or key
    if not dotted:
        return current
    for part in str(dotted).split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError, TypeError):
                return None
        else:
            return None
    return current


@dataclass(frozen=True)
class CheckResult:
    type: str
    ok: bool
    expected: Any
    observed: Any
    message: str

    def view(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "ok": self.ok,
            "expected": self.expected,
            "observed": self.observed,
            "message": self.message,
        }


def evaluate_check(
    check: Any,
    snapshot: Mapping[str, Any] | None,
    task_result: Any = None,
) -> CheckResult:
    data = check if isinstance(check, Mapping) else {}
    kind = str(data.get("type") or "").strip()
    snap = snapshot if isinstance(snapshot, Mapping) else {}
    if kind not in CHECK_TYPES:
        return CheckResult(kind or "unknown", False, check, None, f"不认识的后置条件：{kind}")

    if kind == "inventory_has":
        item = str(data.get("item") or "").strip()
        actual = _inventory_count(snap, item)
        ok, op, expected = _compare(actual, data.get("count", 1))
        return CheckResult(
            kind,
            ok,
            {item: f"{op}{expected:g}"},
            actual,
            f"背包 {item or '?'} = {actual}，要求 {op}{expected:g}",
        )

    if kind == "block_at":
        x = _number(data.get("x"))
        y = _number(data.get("y"))
        z = _number(data.get("z"))
        expected_name = str(data.get("block") or "").strip()
        if x is None or y is None or z is None or not expected_name:
            return CheckResult(kind, False, data, None, "block_at 需要 x / y / z / block")
        observed = _block_at(snap, x, y, z)
        observed_name = str((observed or {}).get("name") or "") or None
        ok = observed_name == expected_name
        return CheckResult(
            kind,
            ok,
            {"x": x, "y": y, "z": z, "block": expected_name},
            observed_name,
            f"({x:g},{y:g},{z:g}) 是 {observed_name or '未知'}，要求 {expected_name}",
        )

    if kind == "block_count":
        block = str(data.get("block") or "").strip()
        actual = 0
        for entry in snap.get("nearby_blocks") or []:
            if isinstance(entry, Mapping) and str(entry.get("name") or "") == block:
                try:
                    actual += int(entry.get("count") or 0)
                except (TypeError, ValueError):
                    pass
        ok, op, expected = _compare(actual, data.get("count", 1))
        radius = data.get("radius")
        return CheckResult(
            kind,
            ok,
            {block: f"{op}{expected:g}"},
            actual,
            f"附近 {block or '?'} = {actual}"
            + (f"（半径 {radius}）" if radius is not None else "")
            + f"，要求 {op}{expected:g}",
        )

    if kind == "entity_near":
        name = str(data.get("name") or data.get("entity") or "").strip()
        radius = _number(data.get("radius"), 3.0) or 3.0
        matched = []
        for entry in snap.get("nearby_entities") or []:
            if not isinstance(entry, Mapping):
                continue
            entry_name = str(entry.get("name") or entry.get("type") or "")
            if name and entry_name != name:
                continue
            distance = _number(entry.get("distance"))
            if distance is not None and distance <= radius:
                matched.append(entry)
        actual = len(matched)
        ok, op, expected = _compare(actual, data.get("count", 1))
        return CheckResult(
            kind,
            ok,
            {"name": name, "radius": radius, "count": f"{op}{expected:g}"},
            actual,
            f"{radius:g} 格内 {name or '实体'} = {actual}，要求 {op}{expected:g}",
        )

    if kind == "position_near":
        player = snap.get("player") or {}
        target = data.get("position") if isinstance(data.get("position"), Mapping) else data
        tx = _number(target.get("x"))
        ty = _number(target.get("y"))
        tz = _number(target.get("z"))
        px = _number(player.get("x"))
        py = _number(player.get("y"))
        pz = _number(player.get("z"))
        if None in (tx, ty, tz, px, py, pz):
            return CheckResult(kind, False, data, None, "position_near 需要目标坐标和玩家坐标")
        distance = ((px - tx) ** 2 + (py - ty) ** 2 + (pz - tz) ** 2) ** 0.5
        max_distance = _number(data.get("distance"), 2.0) or 2.0
        ok, op, expected = _compare(distance, f"<={max_distance:g}")
        return CheckResult(
            kind,
            ok,
            {"x": tx, "y": ty, "z": tz, "distance": f"{op}{expected:g}"},
            round(distance, 2),
            f"距目标 {distance:.2f} 格，要求 {op}{expected:g}",
        )

    if kind in ("health", "food"):
        player = snap.get("player") or {}
        actual = _number(player.get(kind))
        ok, op, expected = _compare(actual, data.get("value", data.get(kind, 1)))
        return CheckResult(
            kind,
            ok,
            f"{op}{expected:g}",
            actual,
            f"{kind} = {actual:g}" if actual is not None else f"{kind} 没有快照",
        )

    path = data.get("path") or data.get("key")
    actual = _task_value(task_result, str(path) if path else None)
    ok, op, expected = _compare(actual, data.get("value", True))
    return CheckResult(
        kind,
        ok,
        {str(path or "result"): f"{op}{expected:g}"}
        if isinstance(expected, float)
        else {str(path or "result"): expected},
        actual,
        f"task_result.{path or '<root>'} = {actual!r}",
    )


def evaluate_checks(
    checks: Any,
    snapshot: Mapping[str, Any] | None,
    task_result: Any = None,
) -> list[dict[str, Any]]:
    items = checks if isinstance(checks, list) else []
    return [
        evaluate_check(item, snapshot, task_result).view()
        for item in items
        if item is not None
    ]


def passed(results: list[dict[str, Any]]) -> bool:
    return all(bool(item.get("ok")) for item in results)


def failure_text(results: list[dict[str, Any]]) -> str:
    failed = [item for item in results if not item.get("ok")]
    return "；".join(str(item.get("message") or item.get("type")) for item in failed)
