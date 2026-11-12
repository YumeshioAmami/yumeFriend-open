"""v5.1-P3 / D288：checks 接入 plan 完成判定 + SkillRunner 接线。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import backend.config as config
import backend.db as db
import backend.game.manager as game_manager_module
from backend.agent import tools as tools_module
from backend.app import create_app
from backend.game.manager import GameManager
from backend.services import friend_service, game_store, skill_store
from backend.services.actor import Actor


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    create_app()
    yield db_file


async def _start_manager(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    with db.session_scope() as session:
        friend = friend_service.create(session, Actor.USER, name="汐音")
        friend_id = friend.id
    await manager.start(friend_id=friend_id, game_kind="mock", config={})
    return manager, friend_id


async def _finish_plan_task(manager, plan_id: int, task_id: str, payload: dict):
    game = manager._service.session
    game.tasks[task_id] = {
        "action": "mine",
        "args": {},
        "origin": "user",
        "status": "running",
    }
    with db.session_scope() as session:
        game_store.add_task(
            session,
            session_id=manager.session_id,
            adapter_task_id=task_id,
            action="mine",
            args={},
        )
    manager._plan_task_links[task_id] = (plan_id, 1)
    await manager._on_event(
        game,
        SimpleNamespace(
            type="task_done",
            payload={"task_id": task_id, **payload},
            event_id=f"ev-{task_id}",
            ts="",
        ),
    )


@pytest.mark.asyncio
async def test_adapter_success_but_task_check_fails_and_reviews(
    temp_db, monkeypatch
):
    manager, friend_id = await _start_manager(temp_db, monkeypatch)
    reviews = []

    async def fake_review(_friend_id, plan_id, *, reason, task_index=None, error=None):
        reviews.append((plan_id, reason, task_index, error))

    async def empty_snapshot():
        return {"inventory": []}

    monkeypatch.setattr(manager, "_review_plan", fake_review)
    monkeypatch.setattr(manager, "snapshot", empty_snapshot)
    try:
        plan = manager._plans.create(
            condition="有木头",
            expected="挖到 1 块木头",
            tasks=[
                {
                    "action": "mine",
                    "args": {"block": "oak_log", "count": 1},
                    "checks": [
                        {
                            "type": "inventory_has",
                            "item": "oak_log",
                            "count": ">=1",
                        }
                    ],
                }
            ],
        )
        await _finish_plan_task(
            manager,
            plan.id,
            "task-cheat-success",
            {"ok": True, "result": {"mined": 1}},
        )
        await asyncio.sleep(0.02)
        task = manager._plans.get(plan.id).tasks[0]
        assert task.status == "failed"
        assert task.result["checks"][0]["ok"] is False
        assert reviews and reviews[0][1] == "postcondition_failed"
        assert "后置条件未通过" in reviews[0][3]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_plan_final_check_failure_reviews(temp_db, monkeypatch):
    manager, _friend_id = await _start_manager(temp_db, monkeypatch)
    reviews = []

    async def fake_review(_friend_id, plan_id, *, reason, task_index=None, error=None):
        reviews.append((plan_id, reason, task_index, error))

    async def empty_snapshot():
        return {"inventory": []}

    monkeypatch.setattr(manager, "_review_plan", fake_review)
    monkeypatch.setattr(manager, "snapshot", empty_snapshot)
    try:
        plan = manager._plans.create(
            condition="有木头",
            expected="背包里有木头",
            checks=[
                {"type": "inventory_has", "item": "oak_log", "count": ">=1"}
            ],
            tasks=[{"action": "mine", "args": {"block": "oak_log", "count": 1}}],
        )
        await _finish_plan_task(
            manager,
            plan.id,
            "task-plan-check",
            {"ok": True, "result": {"mined": 1}},
        )
        await asyncio.sleep(0.02)
        assert reviews and reviews[0][1] == "postcondition_failed"
        assert manager._plans.get(plan.id).result["verified"] is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_passing_plan_is_learned_as_friend_skill(temp_db, monkeypatch):
    manager, friend_id = await _start_manager(temp_db, monkeypatch)
    notices = []

    async def fake_notice(_friend_id, plan_id):
        notices.append(plan_id)

    async def wood_snapshot():
        return {
            "inventory": [{"item": "oak_log", "count": 1}],
            "player": {"x": 0, "y": 64, "z": 0},
        }

    monkeypatch.setattr(manager, "_announce_plan_completion", fake_notice)
    monkeypatch.setattr(manager, "snapshot", wood_snapshot)
    try:
        plan = manager._plans.create(
            condition="有木头",
            expected="背包里有木头",
            checks=[
                {"type": "inventory_has", "item": "oak_log", "count": ">=1"}
            ],
            tasks=[{"action": "mine", "args": {"block": "oak_log", "count": 1}}],
        )
        await _finish_plan_task(
            manager,
            plan.id,
            "task-learn",
            {"ok": True, "result": {"mined": 1}},
        )
        await asyncio.sleep(0.02)
        learned = manager._plans.get(plan.id).result.get("skill_id")
        assert learned
        with db.session_scope() as session:
            row = skill_store.get_skill_by_id(session, learned)
        assert row is not None
        assert row["source"] == "learned"
        assert row["friend_id"] == friend_id
        assert notices == [plan.id]
        assert manager._plans.get(plan.id).status == "completed"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_manager_runs_three_step_skill_without_llm(temp_db, monkeypatch):
    manager, _friend_id = await _start_manager(temp_db, monkeypatch)
    called_turns = 0

    async def forbidden_turn(*_args, **_kwargs):
        nonlocal called_turns
        called_turns += 1
        raise AssertionError("SkillRunner 不该调用 LLM")

    monkeypatch.setattr(manager, "_run_game_turn", forbidden_turn)
    try:
        result = await manager.run_skill(
            {
                "name": "goto_mine_collect",
                "steps": [
                    {"action": "goto", "args": {"x": 0, "y": 64, "z": 0}},
                    {"action": "mine", "args": {"block": "oak_log", "count": 1}},
                    {"action": "collect"},
                ],
            }
        )
        assert result["ok"] is True, result
        assert [step["action"] for step in result["steps"]] == [
            "goto",
            "mine",
            "collect",
        ]
        assert called_turns == 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_skill_tool_executes_builtin_and_records_stats(
    temp_db, monkeypatch
):
    manager, friend_id = await _start_manager(temp_db, monkeypatch)
    try:
        manager._plans.create(
            condition="测试技能执行",
            expected="技能记录统计",
            tasks=[{"action": "wait", "args": {"seconds": 1}}],
        )
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "skill-call",
                    "name": "game_skill",
                    "arguments": {
                        "name": "gather_stone",
                        "args": {"count": 1},
                    },
                },
            )
        assert record["ok"] is True, record
        assert record["result"]["ok"] is True
        with db.session_scope() as session:
            skill = skill_store.get_skill(session, friend_id, "gather_stone")
        assert skill is not None
        assert int(skill["stats"].get("runs") or 0) >= 1
        assert int(skill["stats"].get("successes") or 0) >= 1
    finally:
        await manager.stop()
