"""D285：游戏里的 plan / task 两层（像 AI 编程的 TODO list）。

plan = 目标 + 条件 + 预期结论 + 任务列表；plan 本身不会失败。
task = 一个个可执行的小动作；task 会 success / failed。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

PLAN_STATUSES = ("executing", "paused", "cancelled", "completed")
TASK_STATUSES = ("pending", "executing", "success", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_task(item: Any) -> "PlanTask":
    if isinstance(item, PlanTask):
        return item
    if not isinstance(item, dict):
        raise ValueError("task 必须是对象：{action, args?}")
    action = str(item.get("action") or "").strip()
    if not action:
        raise ValueError("task 需要 action")
    if action in ("say", "stop", "mode", "patrol"):
        raise ValueError(f"plan task 不能是 {action}（它是个动作，不是说话/切模式）")
    args = item.get("args")
    checks = item.get("checks")
    status = str(item.get("status") or "pending")
    if status not in TASK_STATUSES:
        status = "pending"
    return PlanTask(
        action=action,
        args=dict(args) if isinstance(args, dict) else {},
        checks=list(checks) if isinstance(checks, list) else [],
        status=status,
        result=item.get("result"),
    )


@dataclass
class PlanTask:
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    checks: list[Any] = field(default_factory=list)
    status: str = "pending"
    result: Any = None

    def view(self, index: int) -> dict[str, Any]:
        return {
            "index": index,
            "action": self.action,
            "args": dict(self.args),
            "checks": list(self.checks),
            "status": self.status,
            "result": self.result,
        }


@dataclass
class Plan:
    id: str
    condition: Any = ""
    expected: Any = ""
    result: Any = ""
    checks: list[Any] = field(default_factory=list)
    status: str = "paused"
    tasks: list[PlanTask] = field(default_factory=list)
    origin: str = "auto"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "condition": self.condition,
            "expected": self.expected,
            "result": self.result,
            "checks": list(self.checks),
            "status": self.status,
            "tasks": [
                task.view(index)
                for index, task in enumerate(self.tasks, start=1)
            ],
        }


class PlanStore:
    """一局游戏内的 plan 集合；同一时间最多一个 executing。"""

    def __init__(self) -> None:
        self._plans: dict[str, Plan] = {}
        self._executing_id: str | None = None
        self._counter = 0

    def clear(self) -> None:
        self._plans.clear()
        self._executing_id = None
        self._counter = 0

    def get(self, plan_id: str) -> Plan | None:
        return self._plans.get(plan_id)

    def executing(self) -> Plan | None:
        if self._executing_id is None:
            return None
        return self._plans.get(self._executing_id)

    def list(self) -> list[Plan]:
        return list(self._plans.values())

    def active(self) -> list[Plan]:
        return [
            plan
            for plan in self._plans.values()
            if plan.status in ("executing", "paused")
        ]

    def create(
        self,
        *,
        condition: Any,
        expected: Any,
        tasks: list[Any],
        result: Any = "",
        checks: list[Any] | None = None,
        origin: str = "auto",
    ) -> Plan:
        if not str(condition or "").strip():
            raise ValueError("plan 需要 condition（前置条件）")
        if not str(expected or "").strip():
            raise ValueError("plan 需要 expected（预期结论）")
        normalized = [_normalize_task(item) for item in tasks]
        if not normalized:
            raise ValueError("plan 至少需要一个 task")
        self._counter += 1
        plan = Plan(
            id=f"plan-{self._counter}",
            condition=condition,
            expected=expected,
            result=result,
            checks=list(checks) if isinstance(checks, list) else [],
            tasks=normalized,
            origin=origin,
        )
        if origin == "user":
            # 用户新命令优先：旧的 executing plan 暂停，新 plan 立刻执行。
            current = self.executing()
            if current is not None:
                current.status = "paused"
            plan.status = "executing"
            self._executing_id = plan.id
        elif self._executing_id is None:
            plan.status = "executing"
            self._executing_id = plan.id
        else:
            plan.status = "paused"
        self._plans[plan.id] = plan
        return plan

    def update(
        self,
        plan_id: str,
        *,
        condition: Any = None,
        expected: Any = None,
        result: Any = None,
        checks: list[Any] | None = None,
        tasks: list[Any] | None = None,
    ) -> Plan:
        plan = self._require(plan_id)
        if condition is not None:
            plan.condition = condition
        if expected is not None:
            plan.expected = expected
        if result is not None:
            plan.result = result
        if checks is not None:
            plan.checks = list(checks) if isinstance(checks, list) else []
        if tasks is not None:
            plan.tasks = [_normalize_task(item) for item in tasks]
        plan.updated_at = _now()
        return plan

    def add_tasks(
        self,
        plan_id: str,
        tasks: list[Any],
        *,
        before: int | None = None,
    ) -> Plan:
        """插入补条件/补步骤的 task；before 是 1-based 位置，默认插到最前面。"""
        plan = self._require(plan_id)
        normalized = [_normalize_task(item) for item in tasks]
        if not normalized:
            return plan
        index = 0 if before is None else max(0, min(before - 1, len(plan.tasks)))
        plan.tasks[index:index] = normalized
        plan.updated_at = _now()
        return plan

    def pause(self, plan_id: str) -> Plan:
        plan = self._require(plan_id)
        if plan.status == "executing":
            plan.status = "paused"
            if self._executing_id == plan.id:
                self._executing_id = None
        plan.updated_at = _now()
        return plan

    def pause_executing(self) -> Plan | None:
        plan = self.executing()
        if plan is None:
            return None
        return self.pause(plan.id)

    def resume(self, plan_id: str) -> Plan:
        plan = self._require(plan_id)
        if plan.status in ("completed", "cancelled"):
            raise ValueError("已结束的 plan 不能 resume")
        current = self.executing()
        if current is not None and current.id != plan.id:
            current.status = "paused"
        plan.status = "executing"
        self._executing_id = plan.id
        plan.updated_at = _now()
        return plan

    def cancel(self, plan_id: str, *, result: Any = None) -> Plan:
        plan = self._require(plan_id)
        plan.status = "cancelled"
        if result is not None:
            plan.result = result
        if self._executing_id == plan.id:
            self._executing_id = None
        plan.updated_at = _now()
        return plan

    def complete(self, plan_id: str, *, result: Any = None) -> Plan:
        plan = self._require(plan_id)
        plan.status = "completed"
        if result is not None:
            plan.result = result
        if self._executing_id == plan.id:
            self._executing_id = None
        plan.updated_at = _now()
        return plan

    def mark_task(
        self,
        plan_id: str,
        index: int,
        *,
        status: str,
        result: Any = None,
    ) -> PlanTask:
        plan = self._require(plan_id)
        if status not in TASK_STATUSES:
            raise ValueError(f"非法 task 状态：{status}")
        if index < 1 or index > len(plan.tasks):
            raise ValueError(f"plan {plan_id} 没有第 {index} 个 task")
        task = plan.tasks[index - 1]
        task.status = status
        task.result = result
        plan.updated_at = _now()
        return task

    def next_pending(self, plan_id: str) -> tuple[int, PlanTask] | None:
        plan = self._require(plan_id)
        for index, task in enumerate(plan.tasks, start=1):
            if task.status == "pending":
                return index, task
        return None

    def _require(self, plan_id: str) -> Plan:
        plan = self._plans.get((plan_id or "").strip())
        if plan is None:
            raise ValueError(f"没有这个 plan：{plan_id}")
        return plan
