"""D285：plan/task 和游戏 manager 的接线。"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import backend.config as config
import backend.db as db
from backend.app import create_app
from backend.agent import tools as tools_module
from backend.game import preflight
from backend.game.manager import GameManager, _looks_like_stall
import backend.game.manager as game_manager_module
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.services import friend_service, game_store
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


@pytest.mark.asyncio
async def test_plan_task_without_checks_waits_for_completion_audit(
    temp_db, monkeypatch
):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    audits: list[tuple] = []
    reviews: list[tuple] = []

    async def fake_review(_friend_id, plan_id, *, reason, task_index=None, error=None):
        reviews.append((plan_id, reason, task_index, error))

    monkeypatch.setattr(manager, "_review_plan", fake_review)

    async def fake_audit(friend_id, plan_id):
        audits.append((friend_id, plan_id))

    monkeypatch.setattr(manager, "_audit_plan_completion", fake_audit)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        result = manager.plan_action(
            "create",
            condition="有木头",
            expected="搭好一面墙",
            tasks=[{"action": "mine", "args": {"block": "oak_log", "count": 1}}],
        )
        plan_id = result["plan"]["id"]
        for _ in range(100):
            plan = manager._plans.get(plan_id)
            if audits:
                break
            await asyncio.sleep(0.02)
        assert audits and audits[0][1] == plan_id
        assert not reviews
        assert manager._plans.get(plan_id).status == "executing"
        assert manager._plans.get(plan_id).tasks[0].status == "success"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_follow_mode_idle_does_not_start_autonomy(temp_db, monkeypatch):
    """D340：跟随模式空闲交给跟随；自主找事只属于 free 模式。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=True,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    calls: list[str] = []

    async def fake_run(*_args, **_kwargs):
        calls.append("run")
        return True

    monkeypatch.setattr(manager, "_run_game_turn", fake_run)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._mode = "follow"
        manager._next_idle_think_at = 0.0
        manager._spawn_autonomy()
        await asyncio.sleep(0.05)
        assert calls == []
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_free_mode_autonomy_sees_paused_plan(temp_db, monkeypatch):
    """D340：paused plan 不冻结自由模式，自主回合先看到待处理计划。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    captured: list[str] = []

    async def fake_run(_friend_id, _payload, *, hint=None, **_kwargs):
        captured.append(str(hint))
        return False

    monkeypatch.setattr(manager, "_run_game_turn", fake_run)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有木头",
            expected="搭墙",
            tasks=[{"action": "mine", "args": {"block": "oak_log", "count": 1}}],
        )
        manager._plans.pause(plan.id)
        manager._mode = "free"
        await manager._autonomy_turn(friend_id)
        assert captured
        assert "paused 的 plan" in captured[0]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_plan_rejects_unknown_action_at_creation(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        with pytest.raises(ValueError, match="不存在"):
            manager.plan_action(
                "create",
                condition="有空地",
                expected="走到空地",
                tasks=[{"action": "walk_to_open_area", "args": {}}],
            )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_plan_review_cannot_create_a_new_plan(temp_db, monkeypatch):
    """D334：复盘阶段只能修改原 plan，不能 cancel 后重建。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._plan_review_running.add("plan-1")
        with pytest.raises(ValueError, match="复盘阶段"):
            manager.plan_action(
                "create",
                condition="有木头",
                expected="搭墙",
                tasks=[{"action": "mine", "args": {"block": "oak_log", "count": 1}}],
            )
    finally:
        await manager.stop()


class FakeAuditProvider:
    def __init__(self, payload: dict):
        self.payload = payload
        self.config = ProviderConfig(
            name="fake-audit",
            base_url="http://x",
            api_key="k",
            model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, _messages, **_kwargs):
        yield {
            "type": "content",
            "delta": json.dumps(self.payload, ensure_ascii=False),
        }
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}


@pytest.mark.asyncio
async def test_completion_audit_incomplete_adds_tasks_and_resumes(
    temp_db, monkeypatch
):
    """D339：task 跑完但 expected 未达成时补 task，不标记完成。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    audit_payload = {
        "complete": False,
        "reason": "只铺了地基，墙和屋顶还没建",
        "missing": "四面墙和屋顶",
        "next_tasks": [
            {
                "action": "place",
                "args": {"block": "oak_planks", "x": 1, "y": 64, "z": 1},
            }
        ],
        "say": "还差墙和屋顶，我接着补。",
    }
    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
        provider_factory=lambda _db, _friend: FakeAuditProvider(audit_payload),
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    executed: list[str] = []

    async def fake_evidence(_plan):
        return "expected：小屋；tasks：只有一块地基"

    async def fake_execute(friend_id):
        executed.append(friend_id)
        return True

    monkeypatch.setattr(manager, "_plan_completion_evidence", fake_evidence)
    monkeypatch.setattr(manager, "_execute_next_plan_task", fake_execute)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有木板",
            expected="建一间能住的小屋",
            tasks=[{"action": "mine", "args": {"block": "oak_log", "count": 1}}],
        )
        manager._plans.mark_task(plan.id, 1, status="success")
        await manager._audit_plan_completion(friend_id, plan.id)

        assert manager._plans.get(plan.id).status == "executing"
        assert len(manager._plans.get(plan.id).tasks) == 2
        assert manager._plans.get(plan.id).tasks[1].action == "place"
        await asyncio.sleep(0.02)
        assert executed == [friend_id]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_completion_audit_complete_marks_plan_done_once(
    temp_db, monkeypatch
):
    """D339：验收确认 expected 达到后才 completed 并自然通知。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
        provider_factory=lambda _db, _friend: FakeAuditProvider(
            {
                "complete": True,
                "reason": "任务和实际方块都符合 expected",
                "missing": "",
                "next_tasks": [],
                "say": "小屋真的建好啦。",
            }
        ),
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)

    async def fake_evidence(_plan):
        return "expected：小屋；落块证据：全部匹配"

    monkeypatch.setattr(manager, "_plan_completion_evidence", fake_evidence)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有木板",
            expected="建一间能住的小屋",
            tasks=[{"action": "mine", "args": {"block": "oak_log", "count": 1}}],
        )
        manager._plans.mark_task(plan.id, 1, status="success")
        await manager._audit_plan_completion(friend_id, plan.id)

        assert manager._plans.get(plan.id).status == "completed"
        assert manager._plans.get(plan.id).result["verified"] is True
        assert manager._recent_completed_plans[-1]["id"] == plan.id
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_occupied_place_is_auto_repaired_without_llm_review(temp_db, monkeypatch):
    """D335：目标格被普通方块占住时，先 dig_at 再重试，不叫 LLM 复盘。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    calls: list[tuple[str, dict, str]] = []

    async def fake_command(action, args, *, source="user"):
        calls.append((action, dict(args), source))
        if action == "dig_at":
            return {"accepted": True, "task_id": "dig-1"}
        raise AssertionError(f"不应直接执行 {action}")

    async def fake_wait(_task_id, _timeout):
        return {"ok": True, "result": {"cleared": True}}

    async def fake_inspect(positions):
        return [
            {**positions[0], "name": "dirt"},
        ]

    def fake_spawn(coro):
        coro.close()

    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有石头",
            expected="铺一格",
            tasks=[
                {
                    "action": "place",
                    "args": {"block": "stone", "x": 1, "y": 64, "z": 1},
                }
            ],
        )
        manager._plans.mark_task(plan.id, 1, status="executing")
        monkeypatch.setattr(manager, "command", fake_command)
        monkeypatch.setattr(manager, "_wait_task_result", fake_wait)
        monkeypatch.setattr(manager, "_inspect_positions", fake_inspect)
        monkeypatch.setattr(manager, "_spawn", fake_spawn)
        repaired = await manager._auto_repair_plan_task(
            plan.id,
            1,
            plan.tasks[0],
            "[预检:occupied] 目标位置 (1,64,1) 已经有方块了",
        )
        assert repaired is True
        assert plan.tasks[0].status == "pending"
        assert calls and calls[0][0] == "dig_at"
        assert calls[0][2] == "plan"
        assert manager._plan_task_auto_repairs[(plan.id, 1)] == 1
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_transient_place_failure_retries_without_llm_and_has_cap(
    temp_db, monkeypatch
):
    """D335：短暂施工失败本地重试两次；超过上限才交回复盘。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)

    def fake_spawn(coro):
        coro.close()

    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有石头",
            expected="补一面墙",
            tasks=[
                {
                    "action": "place",
                    "args": {"block": "stone", "x": 1, "y": 64, "z": 1},
                }
            ],
        )
        manager._plans.mark_task(plan.id, 1, status="executing")
        monkeypatch.setattr(manager, "_spawn", fake_spawn)
        error = "目标位置还有水：block is still water"
        assert await manager._auto_repair_plan_task(
            plan.id, 1, plan.tasks[0], error
        )
        manager._plans.mark_task(plan.id, 1, status="executing")
        assert await manager._auto_repair_plan_task(
            plan.id, 1, plan.tasks[0], error
        )
        manager._plans.mark_task(plan.id, 1, status="executing")
        assert not await manager._auto_repair_plan_task(
            plan.id, 1, plan.tasks[0], error
        )
        assert manager._plan_task_auto_repairs[(plan.id, 1)] == 2
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_failed_plan_task_pauses_and_reviews(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    reviews: list[tuple] = []

    async def fake_review(_friend_id, plan_id, *, reason, task_index=None, error=None):
        reviews.append((plan_id, reason, task_index, error))

    monkeypatch.setattr(manager, "_review_plan", fake_review)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有木头",
            expected="搭好一面墙",
            tasks=[{"action": "mine", "args": {"block": "oak_log", "count": 1}}],
        )
        plan_id = plan.id
        game = manager._service.session
        game.tasks["task-fail"] = {
            "action": "mine",
            "args": {},
            "origin": "user",
            "status": "running",
        }
        with db.session_scope() as session:
            game_store.add_task(
                session,
                session_id=manager.session_id,
                adapter_task_id="task-fail",
                action="mine",
                args={},
            )
        manager._plan_task_links["task-fail"] = (plan_id, 1)
        await manager._on_event(
            game,
            SimpleNamespace(
                type="task_done",
                payload={
                    "task_id": "task-fail",
                    "ok": False,
                    "result": {"error": "附近没找到 oak_log"},
                },
                event_id="ev-plan-fail",
                ts="",
            ),
        )
        await asyncio.sleep(0.02)
        assert reviews and reviews[0][1] == "task_failed"
        assert manager._plans.get(plan_id).status == "paused"
        assert manager._plans.get(plan_id).tasks[0].status == "failed"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_plan_tool_creates_plan_and_prompt_shows_it(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "plan-create",
                    "name": "game_plan",
                    "arguments": {
                        "action": "create",
                        "condition": "有 8 块木头",
                        "expected": "搭好一面墙",
                        "tasks": [
                            {
                                "action": "mine",
                                "args": {"block": "oak_log", "count": 8},
                            }
                        ],
                    },
                },
            )
        assert record["ok"] is True
        assert record["result"]["plan"]["condition"] == "有 8 块木头"
        assert record["result"]["plan"]["expected"] == "搭好一面墙"
        note = await manager.note_for_prompt(friend_id)
        assert "有 8 块木头" in note
        assert "搭好一面墙" in note
        assert "第一版不做自动建房" not in note
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_mode_switch_does_not_stop_executing_plan(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有木头",
            expected="搭墙",
            tasks=[{"action": "mine", "args": {"block": "oak_log", "count": 1}}],
            origin="auto",
        )
        await manager.set_mode("follow", force=True)
        assert manager._mode == "follow"
        assert manager._mode_applied is False  # 先不下发跟随，等 plan 结束
        assert manager._plans.get(plan.id).status == "executing"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_executing_plan_blocks_mode_but_paused_plan_does_not(
    temp_db, monkeypatch
):
    """D340：executing plan 优先；paused plan 不阻止跟随模式恢复。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    cancelled: list[str] = []

    def fake_cancel_continuation():
        cancelled.append("continuation")

    async def fake_apply_mode(*, player=None):  # noqa: ARG001
        cancelled.append("mode")
        return {}

    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有木头",
            expected="搭墙",
            tasks=[{"action": "mine", "args": {"block": "oak_log", "count": 1}}],
            origin="user",
        )
        monkeypatch.setattr(manager, "_cancel_continuation", fake_cancel_continuation)
        monkeypatch.setattr(manager, "_apply_mode", fake_apply_mode)

        await manager.set_mode("follow", force=True)
        assert manager._mode == "follow"
        assert manager._mode_applied is False
        assert cancelled == []

        manager._plans.pause(plan.id)
        await manager._restore_mode_after(0)
        assert "mode" in cancelled
        assert manager._plans.get(plan.id).status == "paused"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_repeated_plan_failures_cancel_the_plan(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    calls: list[str] = []

    async def fake_run(*_args, **_kwargs):
        calls.append("run")
        return True

    async def fake_line(*_args, **_kwargs):
        calls.append("line")

    monkeypatch.setattr(manager, "_run_game_turn", fake_run)
    monkeypatch.setattr(manager, "_proactive_game_line", fake_line)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有木头",
            expected="搭墙",
            tasks=[{"action": "mine", "args": {"block": "oak_log", "count": 1}}],
            origin="user",
        )
        for _ in range(3):
            await manager._review_plan(
                friend_id,
                plan.id,
                reason="task_failed",
                task_index=1,
                error="附近没找到 oak_log",
            )
        assert manager._plans.get(plan.id).status == "cancelled"
        assert "连续失败" in (manager._plans.get(plan.id).result or "")
    finally:
        await manager.stop()


def test_plan_snapshot_reports_status_and_progress():
    manager = GameManager(autonomy_enabled=False, auto_continue=False)
    plan = manager._plans.create(
        condition="有木头",
        expected="搭好一面墙",
        tasks=[
            {"action": "mine", "args": {"block": "oak_log", "count": 4}},
            {"action": "place", "args": {"block": "oak_planks", "x": 0, "y": 64, "z": 0}},
        ],
        origin="user",
    )
    manager._plans.mark_task(plan.id, 1, status="success", result={"mined": 4})
    text = manager._plan_snapshot_text()
    assert plan.id in text
    assert "executing" in text
    assert "1/2" in text
    assert "第 2 个" in text


@pytest.mark.asyncio
async def test_continuation_with_executing_plan_hands_off_to_plan_executor(
    temp_db, monkeypatch
):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._mode = "free"
        plan = manager._plans.create(
            condition="有泥土",
            expected="用泥土建房子",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 10}}],
            origin="user",
        )
        called: list[str] = []

        async def fake_next(friend_id_arg: str):
            called.append(friend_id_arg)
            return True

        async def fail_fast(*_args, **_kwargs):
            raise AssertionError("有 executing plan 时不应让快脑抢走")

        monkeypatch.setattr(manager, "_execute_next_plan_task", fake_next)
        monkeypatch.setattr(manager, "_run_fast_decision", fail_fast)
        await manager._continue_plan(manager._continuation_generation, retry=False)
        assert called == [friend_id]
        assert manager._plans.get(plan.id).status == "executing"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_active_plan_blocks_plain_chat_turn_from_cancelling_task(
    temp_db, monkeypatch
):
    """D321：普通聊天里的 game_do 不能把正在跑的 plan task 抢走。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)

    async def allow_preflight(*_args, **_kwargs):
        return preflight.PreflightResult(True)

    monkeypatch.setattr(manager, "_preflight_action", allow_preflight)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有泥土",
            expected="建房子",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 8}}],
            origin="user",
        )
        game = manager._service.session
        game.tasks["task-plan"] = {
            "action": "mine",
            "args": {"block": "dirt", "count": 8},
            "origin": "user",
            "status": "running",
        }
        manager._turn_origin = "user"
        with pytest.raises(RuntimeError, match="当前 plan 正在推进"):
            await manager.command(
                "goto", {"x": 1, "y": 64, "z": 1}, source="agent"
            )
        assert game.tasks["task-plan"]["status"] == "running"
        assert manager._plans.get(plan.id).status == "executing"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_stalled_plan_stays_paused_without_auto_retry(temp_db, monkeypatch):
    """D336：复盘没有推进时保持 paused，不再 8 秒后自动连环复盘。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    async def fake_run(*_args, **_kwargs):
        return False

    monkeypatch.setattr(manager, "_run_game_turn", fake_run)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有泥土",
            expected="建房子",
            tasks=[
                {"action": "mine", "args": {"block": "dirt", "count": 8}},
                {"action": "place", "args": {"block": "dirt", "x": 1, "y": 64, "z": 1}},
            ],
            origin="user",
        )
        manager._plans.mark_task(
            plan.id, 1, status="failed", result={"error": "附近没找到 dirt"}
        )
        manager._plans.pause(plan.id)
        await manager._review_plan(
            friend_id,
            plan.id,
            reason="task_failed",
            task_index=1,
            error="附近没找到 dirt",
        )
        await manager._review_plan(
            friend_id, plan.id, reason="review_stalled", task_index=1
        )
        await manager._review_plan(
            friend_id, plan.id, reason="review_stalled", task_index=1
        )
        assert manager._plans.get(plan.id).status == "paused"
        await asyncio.sleep(0.02)
        assert manager._plan_review_running == set()
        assert plan.id not in manager._plan_review_retry_at
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_user_speech_preempts_plan_and_keeps_completion_fact(
    temp_db, monkeypatch
):
    """D336：TA 开口先暂停 plan/取消当前动作；完成后的事实继续可注入。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有泥土",
            expected="铺一小块地",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 1}}],
        )
        manager._plans.mark_task(plan.id, 1, status="executing")
        manager._service.session.tasks["preempt-task"] = {
            "action": "mine",
            "args": {},
            "origin": "auto",
            "status": "running",
        }
        manager._plan_task_links["preempt-task"] = (plan.id, 1)

        async def long_turn():
            await asyncio.sleep(60)

        active_turn = asyncio.create_task(long_turn())
        manager._active_turn_task = active_turn
        manager._chat_busy = True
        manager._turn_origin = "auto"
        await manager._preempt_for_user("等一下，先看看这边")

        assert active_turn.cancelled()
        assert manager._plans.get(plan.id).status == "paused"
        assert manager._plans.get(plan.id).tasks[0].status == "pending"
        assert "preempt-task" not in manager._plan_task_links

        manager._plans.complete(plan.id, result={"verified": True})
        manager._remember_completed_plan(manager._plans.get(plan.id))
        note = await manager.note_for_prompt(friend_id)
        assert "最近真正完成的计划" in note
        assert "铺一小块地" in note
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_explicit_stop_blocks_resume_and_world_actions_until_continue(
    temp_db, monkeypatch
):
    """D336：TA 说停就确定性暂停并锁世界动作；说继续才解锁。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有泥土",
            expected="铺一小块地",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 1}}],
        )
        manager._plans.pause(plan.id)
        await manager._preempt_for_user("好了，先不建了")
        assert manager.world_actions_blocked()
        with pytest.raises(ValueError, match="保持暂停"):
            manager.plan_action("resume", plan_id=plan.id)
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "blocked_after_stop",
                    "name": "game_do",
                    "arguments": {"action": "mine", "args": {"block": "dirt"}},
                },
            )
        assert record["ok"] is False
        assert "锁定" in record["error"]

        await manager._preempt_for_user("继续建吧")
        assert not manager.world_actions_blocked()
        manager.plan_action("resume", plan_id=plan.id)
        assert manager._plans.get(plan.id).status == "executing"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_plan_completion_uses_one_short_announcement(temp_db, monkeypatch):
    """D336：完成通知只带完成事实和说话指令，不进入完整复盘。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    captured: list[tuple[str, str]] = []

    async def fake_quick(_friend_id, *, fact, instruction):
        captured.append((fact, instruction))

    monkeypatch.setattr(manager, "_quick_game_line", fake_quick)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有泥土",
            expected="铺一小块地",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 1}}],
        )
        manager._plans.mark_task(plan.id, 1, status="success")
        plan.result = {"verified": True, "tasks_done": 1}
        manager._plans.complete(plan.id, result=plan.result)
        manager._remember_completed_plan(plan)

        await manager._announce_plan_completion(friend_id, plan.id)
        assert captured
        fact, instruction = captured[0]
        assert "已经完成" in fact
        assert "不要重新规划" in instruction
        assert "game_plan" not in fact
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_successful_plan_review_auto_resumes_execution(temp_db, monkeypatch):
    """D336：复盘改出新 task 后自动恢复，不再要求模型额外调一次 resume。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    executed: list[str] = []

    async def fake_run(*_args, **_kwargs):
        manager._plans.update(
            plan.id,
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 2}}],
        )
        return False

    async def fake_execute(friend_id):
        executed.append(friend_id)
        return True

    monkeypatch.setattr(manager, "_run_game_turn", fake_run)
    monkeypatch.setattr(manager, "_execute_next_plan_task", fake_execute)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有泥土",
            expected="挖两块泥土",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 1}}],
        )
        manager._plans.mark_task(plan.id, 1, status="failed")
        manager._plans.pause(plan.id)
        await manager._review_plan(
            friend_id,
            plan.id,
            reason="task_failed",
            task_index=1,
            error="附近没找到 dirt",
        )
        assert manager._plans.get(plan.id).status == "executing"
        await asyncio.sleep(0.02)
        assert executed == [friend_id]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_user_interrupt_auto_resume_helper_only_resumes_after_chat(
    temp_db, monkeypatch
):
    """D336：TA 只是插话时，处理完自动接回 plan；被叫停则不恢复。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    executed: list[str] = []

    async def fake_execute(friend_id):
        executed.append(friend_id)
        return True

    monkeypatch.setattr(manager, "_execute_next_plan_task", fake_execute)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有泥土",
            expected="铺地",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 1}}],
        )
        manager._plans.pause(plan.id)
        manager._resume_after_user_interrupt(friend_id, plan.id)
        assert manager._plans.get(plan.id).status == "executing"

        manager._plans.pause(plan.id)
        manager._plan_stop_hold = plan.id
        manager._resume_after_user_interrupt(friend_id, plan.id)
        assert manager._plans.get(plan.id).status == "paused"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_incident_supervisor_has_single_flight_lock(temp_db, monkeypatch):
    """D338：同一事故只允许一个 LLM 决策在跑，更新事件不会叠加请求。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[dict] = []

    async def fake_incident_turn(_friend_id, payload):
        calls.append(dict(payload))
        started.set()
        await release.wait()

    monkeypatch.setattr(manager, "_incident_turn", fake_incident_turn)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        payload = {
            "incident_id": "incident-1",
            "incident_kind": "water",
            "phase": "started",
            "position": {"x": 0, "y": 62, "z": 0},
            "oxygen": 80,
            "health": 20,
            "in_water": True,
        }
        await manager._handle_incident_event(
            friend_id, SimpleNamespace(type="incident", payload=payload)
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        await manager._handle_incident_event(
            friend_id,
            SimpleNamespace(
                type="incident",
                payload={**payload, "phase": "update", "oxygen": 78},
            ),
        )
        assert len(calls) == 1
        release.set()
        await asyncio.wait_for(manager._incident_task, timeout=2)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_incident_cleared_resumes_interrupted_plan(temp_db, monkeypatch):
    """D338：事故解除后把事故期间暂停的计划接回来。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    executed: list[str] = []

    async def fake_execute(friend_id):
        executed.append(friend_id)
        return True

    monkeypatch.setattr(manager, "_execute_next_plan_task", fake_execute)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有泥土",
            expected="铺地",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 1}}],
        )
        manager._plans.pause(plan.id)
        manager._incident_plan_id = plan.id
        manager._active_incident = {"incident_id": "incident-1"}
        await manager._handle_incident_event(
            friend_id,
            SimpleNamespace(
                type="incident_cleared",
                payload={
                    "incident_id": "incident-1",
                    "incident_kind": "water",
                },
            ),
        )
        assert manager._plans.get(plan.id).status == "executing"
        await asyncio.sleep(0.02)
        assert executed == [friend_id]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_incident_action_bypasses_plan_gate_only_while_incident_active(
    temp_db, monkeypatch
):
    """D338：事故处理允许单次自保动作，但只限事故响应期间。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    calls: list[tuple[str, dict]] = []

    async def fake_command(action, args, *, source="user"):
        calls.append((action, dict(args)))
        return {"accepted": True, "task_id": "incident-task"}

    monkeypatch.setattr(manager, "command", fake_command)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._incident_response_active = True
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "incident_escape",
                    "name": "game_do",
                    "arguments": {"action": "escape", "args": {}},
                },
            )
        assert record["ok"] is True
        assert calls and calls[0][0] == "escape"

        manager._incident_response_active = False
        with db.session_scope() as session:
            blocked = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "non_incident_escape",
                    "name": "game_do",
                    "arguments": {"action": "escape", "args": {}},
                },
            )
        assert blocked["ok"] is False
        assert "世界动作必须放在 plan 里" in blocked["error"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_incident_llm_attempts_are_bounded_with_escape_fallback(
    temp_db, monkeypatch
):
    """D338：同一事故最多问两次（测试配置），随后只做一次 escape 保底。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    monkeypatch.setattr(game_manager_module, "INCIDENT_LLM_COOLDOWN", 0.0)
    monkeypatch.setattr(game_manager_module, "INCIDENT_MAX_ATTEMPTS", 2)
    turns: list[dict] = []
    fallbacks: list[dict] = []

    async def fake_turn(_friend_id, payload):
        turns.append(dict(payload))

    async def fake_fallback(_friend_id, payload):
        fallbacks.append(dict(payload))

    monkeypatch.setattr(manager, "_incident_turn", fake_turn)
    monkeypatch.setattr(manager, "_incident_fallback", fake_fallback)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        payload = {
            "incident_id": "incident-1",
            "incident_kind": "water",
            "phase": "started",
            "position": {"x": 0, "y": 62, "z": 0},
            "oxygen": 80,
            "health": 20,
            "in_water": True,
        }
        for _ in range(3):
            await manager._handle_incident_event(
                friend_id,
                SimpleNamespace(type="incident", payload=payload),
            )
            task = manager._incident_task
            if task is not None:
                await asyncio.wait_for(task, timeout=2)
        await asyncio.sleep(0.02)
        assert len(turns) == 2
        assert len(fallbacks) == 1
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_user_requested_plan_is_allowed_in_follow_mode(temp_db, monkeypatch):
    """D324：跟随/聊天模式只限制“自己找活”，不阻止 TA 明确交办的多步任务。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._mode = "follow"
        manager._plan_user_request_active = True
        result = manager.set_plan(friend_id, "给 TA 搭个房子", ["找平地", "搭小屋"])
        assert result["goal"] == "给 TA 搭个房子"
        assert result["done"] is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_danger_event_wakes_thinking_and_resumes_plan(temp_db, monkeypatch):
    """D327：挨打/近身敌对会唤醒思考；短暂停 plan task 后从原计划继续。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    resumed: list[str] = []
    hints: list[str] = []

    async def fake_run(_friend_id, _payload, *, hint=None, **_kwargs):
        hints.append(str(hint))
        return True

    async def fake_execute(friend_id):
        resumed.append(friend_id)
        return True

    async def no_cancel():
        return None

    monkeypatch.setattr(manager, "_run_game_turn", fake_run)
    monkeypatch.setattr(manager, "_execute_next_plan_task", fake_execute)
    monkeypatch.setattr(manager, "_cancel_real_tasks", no_cancel)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有泥土",
            expected="建房子",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 8}}],
            origin="user",
        )
        manager._plans.mark_task(plan.id, 1, status="executing")
        game = manager._service.session
        game.tasks["task-plan"] = {
            "action": "mine",
            "args": {},
            "origin": "user",
            "status": "running",
        }
        manager._plan_task_links["task-plan"] = (plan.id, 1)
        await manager._initiative_turn(
            friend_id,
            SimpleNamespace(
                type="damage",
                payload={
                    "amount": 3,
                    "health": 12,
                    "source": {"name": "zombie", "distance": 2.1},
                },
            ),
            critical=True,
        )
        assert hints and "刚刚发生" in hints[0]
        assert "zombie" in hints[0]
        assert manager._plans.get(plan.id).status == "executing"
        assert manager._plans.get(plan.id).tasks[0].status == "pending"
        await asyncio.sleep(0.02)
        assert resumed == [friend_id]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_thinking_lock_does_not_queue_repeated_damage(
    temp_db, monkeypatch
):
    """D329：思考期间连续受击只更新 dirty 标记，不排队第二轮。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    calls: list[str] = []

    async def fake_turn(*_args, **_kwargs):
        calls.append("turn")

    monkeypatch.setattr(manager, "_initiative_turn", fake_turn)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._chat_busy = True
        for _ in range(3):
            manager._maybe_initiative(
                friend_id,
                SimpleNamespace(
                    type="damage", payload={"amount": 2, "health": 10}
                ),
            )
        assert manager._attention_dirty is True
        assert calls == []
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_unlinked_task_done_hands_control_back_to_executing_plan(
    temp_db, monkeypatch
):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._mode = "free"
        plan = manager._plans.create(
            condition="有泥土",
            expected="用泥土建房子",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 10}}],
            origin="user",
        )
        game = manager._service.session
        game.tasks["task-background"] = {
            "action": "goto",
            "args": {"x": 1, "y": 64, "z": 1},
            "origin": "auto",
            "status": "running",
        }
        called: list[str] = []

        async def fake_next(friend_id_arg: str):
            called.append(friend_id_arg)
            return True

        monkeypatch.setattr(manager, "_execute_next_plan_task", fake_next)
        await manager._on_event(
            game,
            SimpleNamespace(
                type="task_done",
                payload={
                    "task_id": "task-background",
                    "ok": True,
                    "result": {"arrived": True},
                },
                event_id="ev-background-done",
                ts="",
            ),
        )
        await asyncio.sleep(0.02)
        assert called == [friend_id]
        assert manager._plans.get(plan.id).status == "executing"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_status_exposes_structured_current_plan(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有泥土",
            expected="用泥土建房子",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 10}}],
            origin="user",
        )
        status = manager.status()
        assert status["current_plan"]["id"] == plan.id
        assert status["plans"][0]["id"] == plan.id
        assert "dirt" in status["plan_steps"][0]
    finally:
        await manager.stop()


def test_stall_answer_detection():
    assert _looks_like_stall("")
    assert _looks_like_stall("我去查查")
    assert _looks_like_stall("稍等，我看看")
    assert not _looks_like_stall(
        "有一个计划 plan-1，正在执行，已经挖了 8 块木头，下一步是第 3 个任务"
    )
    assert not _looks_like_stall("现在没有进行中的计划")
    assert not _looks_like_stall("还在执行，我去查查背包里有没有木头")


@pytest.mark.asyncio
async def test_game_prompt_puts_players_hostiles_and_recent_events_up_front(
    temp_db, monkeypatch
):
    """D322：身边的 TA / 敌对生物 / 刚刚发生的事要出现在每次游戏提示词里。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})

        async def fake_snapshot():
            return {
                "state": "running",
                "player": {"name": "Xiyin_", "x": 0, "y": 64, "z": 0, "health": 18, "food": 20},
                "inventory": [{"item": "dirt", "count": 32}],
                "inventory_tools": [
                    {
                        "name": "wooden_pickaxe",
                        "count": 1,
                        "durability": 37,
                        "max_durability": 59,
                    }
                ],
                "inventory_ammo": [{"name": "arrow", "count": 12}],
                "inventory_boats": [{"name": "oak_boat", "count": 1}],
                "equipment": {
                    "held": {"name": "dirt", "count": 32},
                    "offhand": None,
                    "armor": [],
                },
                "entity_sense": {
                    "players": [
                        {
                            "name": "YumeshioAmami",
                            "type": "player",
                            "distance": 3.2,
                            "direction": "东南",
                            "vertical": "同高",
                        }
                    ],
                    "hostiles": [
                        {
                            "name": "zombie",
                            "type": "mob",
                            "distance": 6.4,
                            "direction": "北",
                            "vertical": "同高",
                            "hostile": True,
                        }
                    ],
                    "passive_mobs": [],
                },
                "hazards": {"health": 18, "food": 20},
                "nearby_blocks": [],
                "flat_sites": [],
            }

        monkeypatch.setattr(manager._service, "snapshot", fake_snapshot)
        manager._last_player_speaker = "YumeshioAmami"
        manager._recent_events.append(
            SimpleNamespace(
                type="hostile_near",
                payload={
                    "name": "zombie",
                    "distance": 6.4,
                    "direction": "北",
                    "vertical": "同高",
                    "hostile": True,
                },
            )
        )
        note = await manager.note_for_prompt(friend_id)
        assert "身边的玩家：TA" in note
        assert "YumeshioAmami" in note
        assert "附近的敌对生物：zombie" in note
        assert "刚刚发生：" in note
        assert "敌对生物靠近" in note
        assert "背包（全部）：dirt×32" in note
        assert "工具：wooden_pickaxe（耐久 37/59）" in note
        assert "弹药：arrow×12" in note
        assert "船：oak_boat×1" in note
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_inventory_detail_exposes_full_backpack_facts(
    temp_db, monkeypatch
):
    """D328：完整背包查询必须能看到工具耐久、弹药、船和槽位。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})

        async def fake_snapshot():
            return {
                "inventory": [
                    {"item": "dirt", "count": 32},
                    {"item": "arrow", "count": 12},
                ],
                "inventory_details": {
                    "items": [
                        {"slot": 36, "name": "dirt", "count": 32},
                        {"slot": 40, "name": "arrow", "count": 12},
                    ]
                },
                "inventory_empty_slots": 20,
                "inventory_full": False,
                "inventory_tools": [
                    {
                        "name": "wooden_pickaxe",
                        "count": 1,
                        "durability": 37,
                        "max_durability": 59,
                    }
                ],
                "inventory_ammo": [{"name": "arrow", "count": 12}],
                "inventory_boats": [{"name": "oak_boat", "count": 1}],
                "inventory_foods": [{"name": "bread", "count": 3}],
                "equipment": {"held": {"name": "dirt", "count": 32}},
                "held": {"item": "dirt", "count": 32},
            }

        monkeypatch.setattr(manager, "snapshot", fake_snapshot)
        detail = await manager.inventory_detail()
        assert detail["inventory"][0]["item"] == "arrow"
        assert detail["tools"][0]["durability"] == 37
        assert detail["ammo"][0]["count"] == 12
        assert detail["boats"][0]["name"] == "oak_boat"
        assert detail["details"]["items"][0]["slot"] == 36
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_idle_think_runs_in_free_mode_without_fast_brain(
    temp_db, monkeypatch
):
    """D340：自由模式空闲会自己找事；不再让快脑独立决策。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    calls: list[str] = []

    async def fake_run(_friend_id, _payload, *, hint=None, **_kwargs):
        calls.append(str(hint))
        manager._last_turn_said = True
        return False

    async def fail_fast(*_args, **_kwargs):
        raise AssertionError("快脑不应参与空闲决策")

    monkeypatch.setattr(manager, "_run_game_turn", fake_run)
    monkeypatch.setattr(manager, "_run_fast_decision", fail_fast)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._mode = "free"
        await manager._autonomy_turn(friend_id)
        assert calls and "看看周围" in calls[0]
        assert manager._autonomy_no_progress == 0
        assert manager._next_idle_think_at > time.monotonic()
    finally:
        await manager.stop()
