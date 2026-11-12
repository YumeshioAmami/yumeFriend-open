"""D294：确定性动作预检 + 失败分类。"""

import pytest

import backend.config as config
import backend.db as db
from backend.app import create_app
from backend.game import preflight
from backend.game.manager import GameManager
from backend.services import friend_service
from backend.services.actor import Actor


def _snapshot(**items: int) -> dict:
    return {
        "inventory": [{"item": name, "count": count} for name, count in items.items()],
        "held": None,
    }


def test_mine_preflight_requires_pickaxe() -> None:
    missing = preflight.check_action(
        "mine", {"block": "stone", "count": 4}, _snapshot()
    )
    assert missing.ok is False
    assert missing.kind == "missing_tool"
    assert "镐" in missing.suggestion

    ready = preflight.check_action(
        "mine",
        {"block": "stone", "count": 4},
        _snapshot(stone_pickaxe=1),
    )
    assert ready.ok is True


def test_place_preflight_checks_inventory_and_support() -> None:
    missing = preflight.check_action(
        "place",
        {"block": "oak_planks", "x": 0, "y": 64, "z": 0},
        _snapshot(),
    )
    assert missing.kind == "missing_item"

    air = [
        {"x": 0, "y": 64, "z": 0, "name": "air"},
        {"x": 0, "y": 63, "z": 0, "name": "air"},
        {"x": 0, "y": 65, "z": 0, "name": "air"},
        {"x": 1, "y": 64, "z": 0, "name": "air"},
        {"x": -1, "y": 64, "z": 0, "name": "air"},
        {"x": 0, "y": 64, "z": 1, "name": "air"},
        {"x": 0, "y": 64, "z": -1, "name": "air"},
    ]
    floating = preflight.check_place_support(
        air, {"x": 0, "y": 64, "z": 0}
    )
    assert floating.kind == "no_support"

    with_support = [dict(item) for item in air]
    with_support[1]["name"] = "stone"
    assert preflight.check_place_support(
        with_support, {"x": 0, "y": 64, "z": 0}
    ).ok

    occupied = [dict(item) for item in air]
    occupied[0]["name"] = "dirt"
    assert preflight.check_place_support(
        occupied, {"x": 0, "y": 64, "z": 0}
    ).kind == "occupied"


def test_craft_preflight_checks_recipe_ingredients() -> None:
    recipe = {
        "ok": True,
        "recipes": [
            {
                "output": {"item": "oak_planks", "count": 4},
                "ingredients": [{"item": "oak_log", "count": 1}],
            }
        ],
    }
    missing = preflight.check_action(
        "craft",
        {"item": "oak_planks", "count": 8},
        _snapshot(),
        recipe=recipe,
    )
    assert missing.kind == "missing_ingredient"
    assert missing.details["missing"][0]["item"] == "oak_log"

    ready = preflight.check_action(
        "craft",
        {"item": "oak_planks", "count": 8},
        _snapshot(oak_log=2),
        recipe=recipe,
    )
    assert ready.ok is True


def test_sequence_preflight_checks_nested_action() -> None:
    result = preflight.check_action(
        "sequence",
        {"steps": [{"action": "mine", "args": {"block": "stone", "count": 1}}]},
        _snapshot(),
    )
    assert result.kind == "missing_tool"
    assert "第 1 步" in result.message


def test_failure_classification_and_recovery_hint() -> None:
    assert preflight.classify_failure("这个位置四面悬空，没有可依托的方块") == "no_support"
    assert preflight.classify_failure("The goal was changed before it could be completed") == "goal_changed"
    assert preflight.classify_failure("附近 64 格内没找到 oak_log") == "unreachable"
    assert "支撑" in preflight.recovery_hint("no_support")
    assert "craft" in preflight.recovery_hint("missing_tool")


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
async def test_command_blocks_preflight_failure_before_adapter(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    with db.session_scope() as session:
        friend = friend_service.create(session, Actor.USER, name="汐音")
        friend_id = friend.id
    await manager.start(friend_id=friend_id, game_kind="mock", config={})
    try:
        async def fake_preflight(*_args, **_kwargs):
            return preflight.PreflightResult(
                False,
                kind="missing_item",
                message="背包里没有 oak_planks",
                suggestion="先合成 oak_planks",
            )

        manager._preflight_action = fake_preflight  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="预检:missing_item"):
            await manager.command(
                "place",
                {"block": "oak_planks", "x": 1, "y": 64, "z": 1},
            )
        assert not (manager._service.session.tasks or {})
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_fast_state_includes_failure_hint(monkeypatch):
    manager = GameManager(
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )

    async def fake_snapshot():
        return _snapshot(stone=12)

    monkeypatch.setattr(manager, "snapshot", fake_snapshot)
    manager._failure_history = [
        {
            "action": "mine",
            "args": {"block": "stone", "count": 4},
            "error": "失败：没有镐子",
            "kind": "missing_tool",
            "suggestion": "先 craft 一把木镐",
        }
    ]
    text = await manager._fast_state_text()
    assert "上次失败" in text
    assert "missing_tool" in text
    assert "木镐" in text
