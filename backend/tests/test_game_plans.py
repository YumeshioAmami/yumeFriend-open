"""D285：plan / task 两层（AI 编程式 TODO list）。"""

import pytest

from backend.game.plans import PlanStore


def _task(action: str, **args):
    return {"action": action, "args": args}


def test_only_one_plan_executes_and_resume_pauses_the_old_one():
    store = PlanStore()
    first = store.create(
        condition="有木头",
        expected="搭好墙",
        tasks=[_task("mine", block="oak_log", count=8)],
    )
    second = store.create(
        condition="有石头",
        expected="铺好地基",
        tasks=[_task("mine", block="stone", count=32)],
    )
    assert first.status == "executing"
    assert second.status == "paused"
    assert store.executing().id == first.id

    store.resume(second.id)
    assert store.get(first.id).status == "paused"
    assert store.get(second.id).status == "executing"
    assert store.executing().id == second.id


def test_user_plan_takes_over_the_current_executing_plan():
    store = PlanStore()
    auto = store.create(
        condition="有木头",
        expected="自己搭墙",
        tasks=[_task("mine", block="oak_log", count=4)],
        origin="auto",
    )
    user = store.create(
        condition="有石头",
        expected="给 TA 铺地基",
        tasks=[_task("mine", block="stone", count=8)],
        origin="user",
    )
    assert store.get(auto.id).status == "paused"
    assert store.get(user.id).status == "executing"
    assert store.executing().id == user.id


def test_plan_fields_can_be_updated_anytime():
    store = PlanStore()
    plan = store.create(
        condition="空手",
        expected="先找木头",
        tasks=[_task("mine", block="oak_log", count=4)],
    )
    store.update(
        plan.id,
        condition={"items": {"oak_log": 8}},
        expected={"walls": 4},
        result="进行中",
    )
    updated = store.get(plan.id)
    assert updated.condition == {"items": {"oak_log": 8}}
    assert updated.expected == {"walls": 4}
    assert updated.result == "进行中"


def test_add_tasks_inserts_prerequisites_at_the_front():
    store = PlanStore()
    plan = store.create(
        condition="有木板",
        expected="盖房子",
        tasks=[_task("place", block="oak_planks", x=0, y=64, z=0)],
    )
    store.add_tasks(
        plan.id,
        [
            _task("mine", block="oak_log", count=8),
            _task("craft", item="oak_planks", count=32),
        ],
        before=1,
    )
    actions = [task.action for task in store.get(plan.id).tasks]
    assert actions == ["mine", "craft", "place"]


def test_task_status_and_plan_completion():
    store = PlanStore()
    plan = store.create(
        condition="有木头",
        expected="墙",
        tasks=[_task("mine", block="oak_log", count=4)],
    )
    index, task = store.next_pending(plan.id)
    assert index == 1 and task.status == "pending"
    store.mark_task(plan.id, 1, status="success", result={"mined": 4})
    assert store.next_pending(plan.id) is None
    store.complete(plan.id, result="墙搭好了")
    assert store.get(plan.id).status == "completed"
    assert store.get(plan.id).result == "墙搭好了"
    assert store.executing() is None


def test_plan_can_be_cancelled_with_a_result():
    store = PlanStore()
    plan = store.create(
        condition="有木头",
        expected="墙",
        tasks=[_task("mine", block="oak_log", count=4)],
    )
    store.cancel(plan.id, result="不做了")
    assert store.get(plan.id).status == "cancelled"
    assert store.get(plan.id).result == "不做了"
    assert store.executing() is None


def test_plan_tasks_cannot_be_say_or_mode():
    store = PlanStore()
    with pytest.raises(ValueError):
        store.create(
            condition="x",
            expected="y",
            tasks=[_task("say", text="hi")],
        )


def test_plan_and_task_checks_are_kept_in_views():
    store = PlanStore()
    plan = store.create(
        condition="有木头",
        expected="背包里多 4 块木头",
        checks=[
            {"type": "inventory_has", "item": "oak_log", "count": ">=4"}
        ],
        tasks=[
            {
                "action": "mine",
                "args": {"block": "oak_log", "count": 4},
                "checks": [
                    {"type": "task_result", "path": "mined", "value": ">=4"}
                ],
            }
        ],
    )
    view = plan.view()
    assert view["checks"][0]["type"] == "inventory_has"
    assert view["tasks"][0]["checks"][0]["type"] == "task_result"
