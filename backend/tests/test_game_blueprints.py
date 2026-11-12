"""v5.1-P5 / D289：蓝图、缺料拆解、自由创作。"""

from __future__ import annotations

import asyncio

import pytest

import backend.config as config
import backend.db as db
import backend.game.manager as game_manager_module
from backend.app import create_app
from backend.game import blueprints, preflight
from backend.game.manager import GameManager
from backend.services import blueprint_store, friend_service, game_store
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


def _blueprint(block: str = "stone", count: int = 2) -> dict:
    return {
        "name": "test_platform",
        "description": "测试平台",
        "size": {"x": count, "y": 1, "z": 1},
        "palette": {"floor": block},
        "structure": "沿 x 方向铺一排",
        "layers": [
            {
                "y": 0,
                "blocks": [
                    {"x": x, "y": 0, "z": 0, "block": block}
                    for x in range(count)
                ],
            },
        ],
    }


def test_blueprint_validation_materials_and_gap():
    bp = blueprints.Blueprint.from_dict(_blueprint("cobblestone", 2))
    assert bp.requirements() == {"cobblestone": 2}
    gap = blueprints.material_gap(
        bp.requirements(), {"inventory": [{"item": "cobblestone", "count": 1}]}
    )
    assert gap == {"cobblestone": 1}
    tasks = blueprints.procurement_tasks(gap)
    assert tasks[0]["action"] == "mine"
    assert tasks[0]["args"] == {"block": "stone", "count": 1}
    checks = bp.block_checks({"x": 10, "y": 64, "z": -3})
    assert checks[0]["type"] == "block_at"
    assert checks[0]["x"] == 10
    assert checks[0]["z"] == -3


def test_simple_hut_uses_requested_material_and_has_door_and_roof():
    bp = blueprints.simple_hut_blueprint("dirt", width=5, depth=5, height=2)
    assert bp.requirements() == {"dirt": 78}
    positions = {
        (block.x, block.y, block.z): block.block
        for _y, blocks in bp.layers
        for block in blocks
    }
    assert all(block == "dirt" for block in positions.values())
    assert (2, 1, 0) not in positions  # 正面中间的两格高门洞
    assert (2, 2, 0) not in positions
    assert positions[(2, 3, 0)] == "dirt"  # 平屋顶
    assert positions[(0, 1, 2)] == "dirt"  # 侧墙
    assert (0, 2, 2) not in positions  # 侧窗


def test_compile_design_from_parts_and_holes_is_free_form():
    """D326：预设只是参考；parts 可以自由拼出任意体素结构。"""
    bp = blueprints.compile_design(
        {
            "name": "tower",
            "description": "3×3 小塔，中间开门",
            "size": {"x": 3, "y": 4, "z": 3},
            "palette": {"wall": "cobblestone"},
            "parts": [
                {
                    "x": 0,
                    "y": 0,
                    "z": 0,
                    "width": 3,
                    "height": 1,
                    "depth": 3,
                    "block": "wall",
                },
                {
                    "x": 0,
                    "y": 1,
                    "z": 0,
                    "width": 1,
                    "height": 2,
                    "depth": 3,
                    "block": "wall",
                },
                {
                    "x": 2,
                    "y": 1,
                    "z": 0,
                    "width": 1,
                    "height": 2,
                    "depth": 3,
                    "block": "wall",
                },
                {
                    "x": 0,
                    "y": 1,
                    "z": 0,
                    "width": 3,
                    "height": 2,
                    "depth": 1,
                    "block": "wall",
                },
                {
                    "x": 0,
                    "y": 1,
                    "z": 2,
                    "width": 3,
                    "height": 2,
                    "depth": 1,
                    "block": "wall",
                },
            ],
            "holes": [
                {
                    "x": 1,
                    "y": 1,
                    "z": 0,
                    "width": 1,
                    "height": 2,
                    "depth": 1,
                }
            ],
        }
    )
    positions = {
        (block.x, block.y, block.z): block.block
        for _y, blocks in bp.layers
        for block in blocks
    }
    assert bp.requirements() == {"cobblestone": 23}
    assert positions[(0, 0, 0)] == "cobblestone"
    assert (1, 1, 0) not in positions


@pytest.mark.asyncio
async def test_blueprint_runner_places_and_verifies_layers():
    state: dict[tuple[int, int, int], str] = {}

    async def place(target, _timeout):
        state[(target["x"], target["y"], target["z"])] = target["block"]
        return {"ok": True, "placed": 1}

    async def inspect(positions):
        return [
            {
                "x": item["x"],
                "y": item["y"],
                "z": item["z"],
                "name": state.get((item["x"], item["y"], item["z"]), "air"),
            }
            for item in positions
        ]

    bp = blueprints.Blueprint.from_dict(_blueprint("stone", 2))
    runner = blueprints.BlueprintRunner(place, inspect, retry_delay=0)
    result = await runner.run(bp, {"x": 0, "y": 64, "z": 0})
    assert result["ok"] is True
    assert result["placed"] == 2
    assert result["checked"] == 2
    assert state[(0, 64, 0)] == "stone"
    assert state[(1, 64, 0)] == "stone"


@pytest.mark.asyncio
async def test_blueprint_runner_repairs_transient_missing_block():
    state: dict[tuple[int, int, int], str] = {}
    calls = {"count": 0}

    async def place(target, _timeout):
        key = (target["x"], target["y"], target["z"])
        if key == (1, 64, 0) and calls["count"] == 0:
            calls["count"] += 1
            return {"ok": False, "error": "Server refused: still air"}
        state[key] = target["block"]
        return {"ok": True, "placed": 1}

    async def inspect(positions):
        return [
            {
                "x": item["x"],
                "y": item["y"],
                "z": item["z"],
                "name": state.get((item["x"], item["y"], item["z"]), "air"),
            }
            for item in positions
        ]

    bp = blueprints.Blueprint.from_dict(_blueprint("stone", 2))
    runner = blueprints.BlueprintRunner(place, inspect, retry_delay=0)
    result = await runner.run(bp, {"x": 0, "y": 64, "z": 0})
    assert result["ok"] is True
    assert result["missing"] == []
    assert result["conflicts"] == []
    assert state[(1, 64, 0)] == "stone"


@pytest.mark.asyncio
async def test_blueprint_runner_reports_conflicts_without_claiming_success():
    state: dict[tuple[int, int, int], str] = {
        (0, 64, 0): "stone",
        (1, 64, 0): "dirt",
    }

    async def place(_target, _timeout):
        raise AssertionError("冲突格不应该被反复尝试")

    async def inspect(positions):
        return [
            {
                "x": item["x"],
                "y": item["y"],
                "z": item["z"],
                "name": state.get((item["x"], item["y"], item["z"]), "air"),
            }
            for item in positions
        ]

    bp = blueprints.Blueprint.from_dict(_blueprint("stone", 2))
    runner = blueprints.BlueprintRunner(place, inspect, retry_delay=0)
    result = await runner.run(bp, {"x": 0, "y": 64, "z": 0})
    assert result["ok"] is False
    assert result["conflicts"][0]["observed"] == "dirt"
    assert result["conflicts"][0]["x"] == 1


@pytest.mark.asyncio
async def test_blueprint_runner_clears_safe_conflict_at_same_anchor():
    """D322：蓝图冲突格先原位清掉再放，不换地方从零重开。"""
    state: dict[tuple[int, int, int], str] = {
        (0, 64, 0): "stone",
        (1, 64, 0): "grass_block",
    }
    cleared: list[tuple[int, int, int]] = []

    async def clear(target):
        assert target["observed"] == "grass_block"
        key = (target["x"], target["y"], target["z"])
        cleared.append(key)
        state[key] = "air"
        return {"ok": True, "cleared": True}

    async def place(target, _timeout):
        state[(target["x"], target["y"], target["z"])] = target["block"]
        return {"ok": True, "placed": 1}

    async def inspect(positions):
        return [
            {
                "x": item["x"],
                "y": item["y"],
                "z": item["z"],
                "name": state.get((item["x"], item["y"], item["z"]), "air"),
            }
            for item in positions
        ]

    bp = blueprints.Blueprint.from_dict(_blueprint("stone", 2))
    runner = blueprints.BlueprintRunner(
        place, inspect, clear=clear, retry_delay=0
    )
    result = await runner.run(bp, {"x": 0, "y": 64, "z": 0})
    assert result["ok"] is True
    assert result["cleared"] == 1
    assert cleared == [(1, 64, 0)]
    assert state[(1, 64, 0)] == "stone"


async def _manager(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    async def no_site_fill(*_args, **_kwargs):
        return []

    monkeypatch.setattr(manager, "_site_fill_positions", no_site_fill)
    with db.session_scope() as session:
        friend = friend_service.create(session, Actor.USER, name="小敏")
        friend_id = friend.id
    await manager.start(friend_id=friend_id, game_kind="mock", config={})
    return manager, friend_id


@pytest.mark.asyncio
async def test_plan_blueprint_name_resolves_from_recent_game_build_preview(
    temp_db, monkeypatch
):
    """game_build 预览后，plan 用 blueprint_name 也能补成完整 blueprint。"""
    manager, _friend_id = await _manager(temp_db, monkeypatch)
    blueprint = _blueprint("oak_planks", 2)

    async def fake_execute(_friend_id):
        return True

    monkeypatch.setattr(manager, "_execute_next_plan_task", fake_execute)
    manager._blueprint_previews["test_platform"] = {
        "blueprint": blueprint,
        "anchor": {"x": 1, "y": 64, "z": 2},
    }
    try:
        result = manager.plan_action(
            "create",
            condition="有木板",
            expected="搭平台",
            tasks=[
                {
                    "action": "blueprint",
                    "args": {
                        "blueprint_name": "test_platform",
                        "anchor": {"x": 1, "y": 64, "z": 2},
                    },
                }
            ],
        )
        task = result["plan"]["tasks"][0]
        assert task["args"]["blueprint"] == blueprint
        assert task["args"]["anchor"] == {"x": 1, "y": 64, "z": 2}
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_survival_gap_inserts_procurement_tasks(temp_db, monkeypatch):
    manager, friend_id = await _manager(temp_db, monkeypatch)
    try:
        result = await manager.blueprint_action(
            "build",
            {
                "blueprint": _blueprint("dirt", 2),
                "anchor": {"x": 0, "y": 64, "z": 0},
            },
        )
        plan = result["plan"]
        actions = [task["action"] for task in plan["tasks"]]
        assert actions[0] == "mine"
        assert actions[-1] == "blueprint"
        assert result["gap"] == {"dirt": 2}
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_hut_action_uses_flat_site_and_dirt_procurement(temp_db, monkeypatch):
    manager, friend_id = await _manager(temp_db, monkeypatch)

    async def fake_snapshot():
        return {
            "inventory": [],
            "flat_sites": [
                {"x": 10, "y": 64, "z": -2, "distance": 3.0, "ground": "grass_block"}
            ],
        }

    async def no_exec(*_args, **_kwargs):
        return False

    monkeypatch.setattr(manager, "snapshot", fake_snapshot)
    monkeypatch.setattr(manager, "_execute_next_plan_task", no_exec)
    try:
        result = await manager.blueprint_action(
            "hut",
            {"material": "dirt", "width": 5, "depth": 5},
        )
        plan = result["plan"]
        actions = [task["action"] for task in plan["tasks"]]
        assert result["material"] == "dirt"
        assert result["anchor"]["source"] == "flat_site"
        assert actions[0] == "mine"
        assert plan["tasks"][0]["args"] == {"block": "dirt", "count": 78}
        assert actions[-1] == "blueprint"
        assert {check["block"] for check in plan["checks"]} == {"dirt"}
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_hut_preview_returns_design_without_creating_plan(temp_db, monkeypatch):
    """D333：game_build 只给设计稿，不替她建 plan。"""
    manager, _friend_id = await _manager(temp_db, monkeypatch)

    async def fake_snapshot():
        return {
            "inventory": [],
            "flat_sites": [
                {"x": 10, "y": 64, "z": -2, "distance": 3.0, "ground": "grass_block"}
            ],
            "world": {
                "near": {
                    "rows": ["........."],
                    "origin": {"x": 0, "z": 0},
                    "step": 1,
                    "nearest_water": {"direction": "东", "distance": 3, "x": 3, "y": 63, "z": 0},
                }
            },
        }

    async def no_site_fill(*_args, **_kwargs):
        return []

    monkeypatch.setattr(manager, "snapshot", fake_snapshot)
    monkeypatch.setattr(manager, "_site_fill_positions", no_site_fill)
    try:
        result = await manager.blueprint_action(
            "hut",
            {"material": "dirt", "width": 5, "depth": 5},
            preview_only=True,
        )
        assert result["status"] == "design_preview"
        assert result["plan_tasks"][-1]["action"] == "blueprint"
        assert result["materials_given"] is False
        assert manager._plans.list() == []
        assert result["anchor"]["source"] == "flat_site"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_hut_preview_refuses_to_fallback_to_slope(temp_db, monkeypatch):
    """找不到平地时返回 needs_flat_site，不再偷偷在坡上建房。"""
    manager, _friend_id = await _manager(temp_db, monkeypatch)

    async def fake_snapshot():
        return {
            "inventory": [],
            "flat_sites": [],
            "flat_sites_3": [],
            "world": {
                "near": {
                    "rows": ["^^^^^^^^^"],
                    "origin": {"x": 0, "z": 0},
                    "step": 1,
                }
            },
        }

    monkeypatch.setattr(manager, "snapshot", fake_snapshot)
    try:
        result = await manager.blueprint_action(
            "hut",
            {"material": "dirt", "width": 5, "depth": 5},
            preview_only=True,
        )
        assert result["status"] == "needs_flat_site"
        assert manager._plans.list() == []
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_blueprint_plan_places_blocks_and_passes_checks(temp_db, monkeypatch):
    manager, friend_id = await _manager(temp_db, monkeypatch)
    notices = []

    async def fake_notice(_friend_id, plan_id):
        notices.append(plan_id)

    monkeypatch.setattr(manager, "_announce_plan_completion", fake_notice)
    try:
        result = await manager.blueprint_action(
            "build",
            {
                "blueprint": _blueprint("stone", 2),
                "anchor": {"x": 0, "y": 64, "z": 0},
            },
        )
        plan_id = result["plan"]["id"]
        for _ in range(150):
            plan = manager._plans.get(plan_id)
            if notices:
                break
            await asyncio.sleep(0.02)
        assert notices == [plan_id]
        assert manager._plans.get(plan_id).status == "completed"
        assert manager._plans.get(plan_id).tasks[-1].status == "success"
        assert manager._blueprint_job["state"] == "completed"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_creative_requires_design_diff_and_rating(temp_db, monkeypatch):
    manager, friend_id = await _manager(temp_db, monkeypatch)
    async def allow_preflight(*_args, **_kwargs):
        return preflight.PreflightResult(True)

    monkeypatch.setattr(manager, "_preflight_action", allow_preflight)
    try:
        with pytest.raises(ValueError):
            await manager.blueprint_action(
                "creative_start",
                {
                    "brief": "随便搭点东西",
                    "size": {"x": 1, "y": 1, "z": 1},
                },
            )
        started = await manager.blueprint_action(
            "creative_start",
            {
                "brief": "搭一个小台子",
                "design": {"structure": "一格石台", "palette": {"stone": "stone"}},
                "anchor": {"x": 0, "y": 64, "z": 0},
                "size": {"x": 1, "y": 1, "z": 1},
            },
        )
        record_id = started["record"]["id"]
        placed = await manager.command(
            "place", {"block": "stone", "x": 0, "y": 64, "z": 0}, source="auto"
        )
        await manager._wait_task_result(str(placed["task_id"]), 5)
        finished = await manager.blueprint_action(
            "creative_finish", {"record_id": record_id, "rating": 5}
        )
        assert finished["ok"] is True
        assert finished["record"]["state"] == "finished"
        assert finished["record"]["rating"] == 5
        assert finished["diff"]["changed_count"] >= 1
        with db.session_scope() as session:
            rows = blueprint_store.list_creative(session, friend_id)
        assert rows and rows[0]["state"] == "finished"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_manager_run_blueprint_clears_natural_conflict_in_place(
    temp_db, monkeypatch
):
    """D322：正式执行蓝图时，grass 冲突格也要原位清掉，而不是换锚点重搭。"""

    manager, _friend_id = await _manager(temp_db, monkeypatch)
    state: dict[tuple[int, int, int], str] = {
        (0, 64, 0): "stone",
        (1, 64, 0): "grass_block",
    }
    raw = _blueprint("stone", 2)

    async def fake_command(action, args, *, source="user"):
        if action == "dig_at":
            state[(args["x"], args["y"], args["z"])] = "air"
            return {"task_id": f"dig-{args['x']}"}
        if action == "place":
            state[(args["x"], args["y"], args["z"])] = args["block"]
            return {"task_id": f"place-{args['x']}"}
        raise AssertionError(f"unexpected action {action}")

    async def fake_wait(task_id, _timeout):
        return {"ok": True, "result": {"task_id": task_id, "cleared": True}}

    async def fake_inspect(positions):
        return [
            {
                "x": item["x"],
                "y": item["y"],
                "z": item["z"],
                "name": state.get((item["x"], item["y"], item["z"]), "air"),
            }
            for item in positions
        ]

    monkeypatch.setattr(manager, "command", fake_command)
    monkeypatch.setattr(manager, "_wait_task_result", fake_wait)
    monkeypatch.setattr(manager, "_inspect_positions", fake_inspect)
    try:
        result = await manager.run_blueprint(
            raw, {"x": 0, "y": 64, "z": 0}
        )
        assert result["ok"] is True
        assert result["cleared"] >= 1
        assert state[(1, 64, 0)] == "stone"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_site_fill_positions_raise_low_ground_to_floor(temp_db, monkeypatch):
    """地不平：floor 下面空的列会生成从地面到 floor-1 的垫块坐标。"""

    manager, _friend_id = await _manager(temp_db, monkeypatch)

    async def fake_inspect(positions):
        result = []
        for item in positions:
            x, y, z = item["x"], item["y"], item["z"]
            # 第一列地面在 y=63；第二列地面在 y=59，需要垫 y=60..63。
            ground = 63 if x == 0 else 59
            result.append(
                {"x": x, "y": y, "z": z, "name": "stone" if y == ground else "air"}
            )
        return result

    monkeypatch.setattr(manager, "_inspect_positions", fake_inspect)
    try:
        fill = await GameManager._site_fill_positions(
            manager,
            {"x": 0, "y": 64, "z": 0}, {"x": 2, "y": 1, "z": 1}
        )
        assert [item["y"] for item in fill] == [60, 61, 62, 63]
        assert {item["x"] for item in fill} == {1}
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_uneven_site_inserts_prepare_site_before_blueprint(
    temp_db, monkeypatch
):
    """D326：地不平先垫地基，再按蓝图施工。"""

    manager, _friend_id = await _manager(temp_db, monkeypatch)

    async def fake_snapshot():
        return {"inventory": [{"item": "dirt", "count": 20}]}

    async def fill_positions(*_args, **_kwargs):
        return [{"x": 1, "y": 63, "z": 0}]

    async def no_exec(*_args, **_kwargs):
        return False

    monkeypatch.setattr(manager, "snapshot", fake_snapshot)
    monkeypatch.setattr(manager, "_site_fill_positions", fill_positions)
    monkeypatch.setattr(manager, "_execute_next_plan_task", no_exec)
    try:
        result = await manager.blueprint_action(
            "build",
            {
                "blueprint": _blueprint("dirt", 2),
                "anchor": {"x": 0, "y": 64, "z": 0},
            },
        )
        actions = [task["action"] for task in result["plan"]["tasks"]]
        assert actions == ["prepare_site", "blueprint"]
        assert result["site_fill"] == 1
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_second_hut_request_reuses_active_blueprint_plan(
    temp_db, monkeypatch
):
    """D322：同一蓝图已有 active plan 时，默认继续原 plan，不另起一座。"""

    manager, _friend_id = await _manager(temp_db, monkeypatch)

    async def fake_snapshot():
        return {
            "inventory": [{"item": "dirt", "count": 128}],
            "flat_sites": [
                {"x": 10, "y": 64, "z": -2, "distance": 3.0, "ground": "grass_block"}
            ],
        }

    async def no_exec(*_args, **_kwargs):
        return False

    monkeypatch.setattr(manager, "snapshot", fake_snapshot)
    monkeypatch.setattr(manager, "_execute_next_plan_task", no_exec)
    try:
        first = await manager.blueprint_action(
            "hut", {"material": "dirt", "width": 5, "depth": 5}
        )
        second = await manager.blueprint_action(
            "hut", {"material": "dirt", "width": 5, "depth": 5}
        )
        assert second["status"] == "reused_existing_plan"
        assert second["plan"]["id"] == first["plan"]["id"]
        assert len(manager._plans.list()) == 1
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_hut_falls_back_to_3x3_site_when_5x5_has_no_room(
    temp_db, monkeypatch
):
    """D324：树林里没有 5×5 空地时，降级到 3×3 仍然要立 plan。"""

    manager, _friend_id = await _manager(temp_db, monkeypatch)

    async def fake_snapshot():
        return {
            "inventory": [{"item": "dirt", "count": 128}],
            "flat_sites": [],
            "flat_sites_3": [
                {"x": 12, "y": 64, "z": -3, "distance": 4.0, "ground": "grass_block"}
            ],
        }

    async def no_exec(*_args, **_kwargs):
        return False

    monkeypatch.setattr(manager, "snapshot", fake_snapshot)
    monkeypatch.setattr(manager, "_execute_next_plan_task", no_exec)
    try:
        result = await manager.blueprint_action(
            "hut", {"material": "dirt", "width": 5, "depth": 5}
        )
        assert result["ok"] is True
        assert result["resolved_width"] == 3
        assert result["anchor"]["x"] == 12
        actions = [task["action"] for task in result["plan"]["tasks"]]
        assert actions[-1] == "blueprint"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_hut_always_creates_plan_with_player_offset_fallback(
    temp_db, monkeypatch
):
    """D324：附近完全没有平地时也不能只回一句话；先立一个可复盘的 plan。"""

    manager, _friend_id = await _manager(temp_db, monkeypatch)

    async def fake_snapshot():
        return {
            "inventory": [{"item": "dirt", "count": 128}],
            "flat_sites": [],
            "flat_sites_3": [],
            "player": {"x": 10, "y": 64, "z": -8},
        }

    async def no_exec(*_args, **_kwargs):
        return False

    monkeypatch.setattr(manager, "snapshot", fake_snapshot)
    monkeypatch.setattr(manager, "_execute_next_plan_task", no_exec)
    try:
        result = await manager.blueprint_action(
            "hut", {"material": "dirt", "width": 5, "depth": 5}
        )
        assert result["ok"] is True
        assert result["fallback_site"] is True
        assert result["anchor"]["source"] == "player_offset_fallback"
        assert result["plan"]["status"] == "executing"
    finally:
        await manager.stop()
