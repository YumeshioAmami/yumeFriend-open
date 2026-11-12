"""v5.1-P6 / D291 / D288：反射配置与 minecraft-data 知识查询。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.config as config
import backend.db as db
import backend.game.manager as game_manager_module
from backend.app import create_app
from backend.game import knowledge, reflexes
from backend.game.manager import GameManager
from backend.services import friend_service, settings_service
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


def test_reflex_config_normalizes_priority_and_order():
    config_data = reflexes.normalize_config(
        {
            "modes": {
                "self_preservation": {"enabled": True, "priority": 999},
                "unstuck": {"enabled": True, "priority": 1},
            }
        }
    )
    assert config_data["modes"]["self_preservation"]["priority"] == 999
    assert "unstuck" not in config_data["modes"]
    names = [name for name, _ in reflexes.active_modes(config_data)]
    assert names[0] == "self_preservation"


def test_decision_reflexes_are_removed():
    assert set(reflexes.REFLEX_MODES) == {"self_preservation"}
    names = [name for name, _ in reflexes.active_modes(reflexes.default_config())]
    assert "item_collecting" not in names
    assert "self_defense" not in names
    assert "auto_eat" not in names
    assert "torch_placing" not in names


@pytest.mark.asyncio
async def test_tool_knowledge_combines_block_requirement_with_inventory(monkeypatch):
    manager = GameManager(autonomy_enabled=False, auto_continue=False)

    async def fake_snapshot():
        return {"inventory": [{"item": "wooden_pickaxe", "count": 1}]}

    monkeypatch.setattr(manager, "snapshot", fake_snapshot)
    result = await manager._augment_tool_knowledge(
        {"tools": ["wooden_pickaxe", "stone_pickaxe"]},
        "tool_tier_for",
    )
    assert result["can_harvest_with_inventory"] is True
    assert result["your_matching_tools"][0]["item"] == "wooden_pickaxe"


@pytest.mark.asyncio
async def test_reflex_config_persists_and_live_applies(temp_db, monkeypatch):
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
        friend = friend_service.create(session, Actor.USER, name="小敏")
        friend_id = friend.id
    await manager.start(friend_id=friend_id, game_kind="mock", config={})
    try:
        updated = await manager.set_reflex_config(
            {
                "modes": {
                    "self_preservation": {
                        "enabled": True,
                        "priority": 123,
                        "interrupts": False,
                        "cooldown": 4,
                    }
                }
            }
        )
        assert updated["modes"]["self_preservation"]["priority"] == 123
        with db.session_scope() as session:
            stored = settings_service.game_reflexes_from(
                settings_service.get(session)
            )
        assert stored["modes"]["self_preservation"]["cooldown"] == 4.0
        assert (
            manager.status()["reflexes"]["modes"]["self_preservation"]["priority"]
            == 123
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_knowledge_query_uses_adapter_and_cache(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    knowledge.clear()
    with db.session_scope() as session:
        friend = friend_service.create(session, Actor.USER, name="小敏")
        friend_id = friend.id
    await manager.start(friend_id=friend_id, game_kind="mock", config={})
    try:
        first = await manager.recipe_for("oak_planks")
        second = await manager.recipe_for("oak_planks")
        assert first["ok"] is True
        assert first["cached"] is False
        assert second["cached"] is True
        tier = await manager.tool_tier_for("stone")
        assert tier["ok"] is True
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_reflex_event_is_kept_for_data_page(temp_db, monkeypatch):
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
        friend = friend_service.create(session, Actor.USER, name="小敏")
        friend_id = friend.id
    await manager.start(friend_id=friend_id, game_kind="mock", config={})
    try:
        await manager._on_event(
            manager._service.session,
            SimpleNamespace(
                type="reflex",
                payload={
                    "mode": "self_preservation",
                    "action": "swim_to_shore",
                    "detail": "水里朝岸边游",
                },
                event_id="reflex-1",
                ts="",
            ),
        )
        events = manager.status()["recent_events"]
        assert any(event["type"] == "reflex" for event in events)
    finally:
        await manager.stop()
