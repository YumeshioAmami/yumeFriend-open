"""确定性 SkillRunner（v5.1-P3 / D288）。

它不做"思考"，只执行声明式步骤：
参数替换 → 下发动作 → 等结果 → 读快照 → 过 checks → 下一步。
超时、连续失败、观测无变化都会停下，不把错误无限重试。
"""

from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from backend.game.postconditions import (
    evaluate_checks,
    failure_text,
    passed,
)

CommandFn = Callable[["SkillStep", dict[str, Any], float], Awaitable[dict[str, Any]]]
SnapshotFn = Callable[[], Awaitable[Mapping[str, Any]]]

_TEMPLATE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def _resolve(value: Any, params: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        match = _TEMPLATE.match(value.strip())
        if match:
            return params.get(match.group(1), value)
        return re.sub(
            r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}",
            lambda match: str(params.get(match.group(1), match.group(0))),
            value,
        )
    if isinstance(value, list):
        return [_resolve(item, params) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, params) for key, item in value.items()}
    return value


@dataclass
class SkillStep:
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    checks: list[Any] = field(default_factory=list)
    timeout: float = 25.0
    max_attempts: int = 3

    @classmethod
    def from_dict(cls, item: Mapping[str, Any]) -> "SkillStep":
        action = str(item.get("action") or "").strip()
        if not action:
            raise ValueError("skill step 需要 action")
        args = item.get("args")
        checks = item.get("checks")
        return cls(
            action=action,
            args=dict(args) if isinstance(args, Mapping) else {},
            checks=list(checks) if isinstance(checks, list) else [],
            timeout=max(0.05, float(item.get("timeout") or 25.0)),
            max_attempts=max(1, min(5, int(item.get("max_attempts") or 3))),
        )


@dataclass
class Skill:
    name: str
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    steps: list[SkillStep] = field(default_factory=list)
    postconditions: list[Any] = field(default_factory=list)

    @classmethod
    def from_dict(cls, item: Mapping[str, Any]) -> "Skill":
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("skill 需要 name")
        raw_steps = item.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("skill 至少需要一个 step")
        params = item.get("params")
        postconditions = item.get("postconditions")
        return cls(
            name=name,
            description=str(item.get("description") or ""),
            params=dict(params) if isinstance(params, Mapping) else {},
            postconditions=(
                list(postconditions) if isinstance(postconditions, list) else []
            ),
            steps=[
                SkillStep.from_dict(step)
                for step in raw_steps
                if isinstance(step, Mapping)
            ],
        )


@dataclass
class SkillRunResult:
    ok: bool
    skill: str
    steps: list[dict[str, Any]]
    error: str = ""
    reason: str = ""

    def view(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "skill": self.skill,
            "steps": self.steps,
            "error": self.error,
            "reason": self.reason,
        }


class SkillRunner:
    def __init__(
        self,
        command: CommandFn,
        snapshot: SnapshotFn,
        *,
        retry_delay: float = 0.2,
    ) -> None:
        self._command = command
        self._snapshot = snapshot
        self._retry_delay = max(0.0, float(retry_delay))

    async def run(
        self,
        skill: Skill | Mapping[str, Any],
        params: Mapping[str, Any] | None = None,
    ) -> SkillRunResult:
        definition = skill if isinstance(skill, Skill) else Skill.from_dict(skill)
        values = {**definition.params, **dict(params or {})}
        step_views: list[dict[str, Any]] = []
        last_result: Any = None

        for index, step in enumerate(definition.steps, start=1):
            args = _resolve(copy.deepcopy(step.args), values)
            checks = _resolve(copy.deepcopy(step.checks), values)
            last_signature: str | None = None
            same_observations = 0
            last_error = ""
            last_result = None
            reports: list[dict[str, Any]] = []

            for attempt in range(1, step.max_attempts + 1):
                try:
                    result = await asyncio.wait_for(
                        self._command(step, args, step.timeout),
                        timeout=step.timeout,
                    )
                except asyncio.TimeoutError:
                    step_views.append(
                        {
                            "index": index,
                            "action": step.action,
                            "status": "failed",
                            "attempt": attempt,
                            "error": f"超时（{step.timeout:g}s）",
                        }
                    )
                    return SkillRunResult(
                        False,
                        definition.name,
                        step_views,
                        error=f"第 {index} 步 {step.action} 超时",
                        reason="timeout",
                    )
                except Exception as exc:  # noqa: BLE001 —— 失败要结构化返回
                    last_result = {"ok": False, "error": str(exc)}
                else:
                    last_result = result

                if isinstance(last_result, Mapping) and (
                    last_result.get("error") or last_result.get("failed")
                ):
                    last_error = str(
                        last_result.get("error") or last_result.get("failed") or "失败"
                    )
                    await asyncio.sleep(self._retry_delay)
                    continue

                try:
                    snapshot = await self._snapshot()
                except Exception as exc:  # noqa: BLE001
                    last_error = f"快照失败：{exc}"
                    await asyncio.sleep(self._retry_delay)
                    continue
                reports = evaluate_checks(checks, snapshot, last_result)
                if passed(reports):
                    step_views.append(
                        {
                            "index": index,
                            "action": step.action,
                            "status": "success",
                            "attempt": attempt,
                            "result": last_result,
                            "checks": reports,
                        }
                    )
                    break

                last_error = failure_text(reports) or "后置条件未通过"
                signature = repr(
                    [(item.get("type"), item.get("observed"), item.get("ok")) for item in reports]
                )
                same_observations = (
                    same_observations + 1 if signature == last_signature else 1
                )
                last_signature = signature
                if same_observations >= 3:
                    step_views.append(
                        {
                            "index": index,
                            "action": step.action,
                            "status": "failed",
                            "attempt": attempt,
                            "result": last_result,
                            "checks": reports,
                            "error": "连续 3 次观测没有变化",
                        }
                    )
                    return SkillRunResult(
                        False,
                        definition.name,
                        step_views,
                        error=f"第 {index} 步没有净进度：{last_error}",
                        reason="no_progress",
                    )
                await asyncio.sleep(self._retry_delay)
            else:
                step_views.append(
                    {
                        "index": index,
                        "action": step.action,
                        "status": "failed",
                        "attempts": step.max_attempts,
                        "result": last_result,
                        "checks": reports,
                        "error": last_error or "连续失败",
                    }
                )
                return SkillRunResult(
                    False,
                    definition.name,
                    step_views,
                    error=f"第 {index} 步连续失败 {step.max_attempts} 次：{last_error}",
                    reason="same_failure",
                )

        if definition.postconditions:
            try:
                snapshot = await self._snapshot()
                report = evaluate_checks(
                    _resolve(copy.deepcopy(definition.postconditions), values),
                    snapshot,
                    last_result,
                )
            except Exception as exc:  # noqa: BLE001
                report = [
                    {
                        "type": "snapshot",
                        "ok": False,
                        "message": f"技能后置条件取快照失败：{exc}",
                    }
                ]
            step_views.append(
                {
                    "index": len(definition.steps) + 1,
                    "action": "postconditions",
                    "status": "success" if passed(report) else "failed",
                    "checks": report,
                }
            )
            if not passed(report):
                return SkillRunResult(
                    False,
                    definition.name,
                    step_views,
                    error=f"技能后置条件未通过：{failure_text(report)}",
                    reason="postcondition_failed",
                )
        return SkillRunResult(True, definition.name, step_views)


BUILTIN_SKILLS: list[dict[str, Any]] = [
    {
        "name": "gather_oak_logs",
        "description": "砍 N 块橡木并捡起来",
        "params": {"count": 4},
        "versions": {"game_kind": "minecraft"},
        "source": "builtin",
        "steps": [
            {
                "action": "mine",
                "args": {"block": "oak_log", "count": "{{count}}"},
            },
            {"action": "collect"},
        ],
        "postconditions": [
            {
                "type": "inventory_has",
                "item": "oak_log",
                "count": ">={{count}}",
            }
        ],
    },
    {
        "name": "gather_stone",
        "description": "挖 N 块石头并捡起来",
        "params": {"count": 8},
        "versions": {"game_kind": "minecraft"},
        "source": "builtin",
        "steps": [
            {
                "action": "mine",
                "args": {"block": "stone", "count": "{{count}}"},
            },
            {"action": "collect"},
        ],
        "postconditions": [
            {
                "type": "inventory_has",
                "item": "stone",
                "count": ">={{count}}",
            }
        ],
    },
    {
        "name": "craft_oak_planks",
        "description": "把橡木合成为木板",
        "params": {"count": 4},
        "versions": {"game_kind": "minecraft"},
        "source": "builtin",
        "steps": [
            {
                "action": "craft",
                "args": {"item": "oak_planks", "count": "{{count}}"},
            }
        ],
        "postconditions": [
            {
                "type": "inventory_has",
                "item": "oak_planks",
                "count": ">={{count}}",
            }
        ],
    },
]
