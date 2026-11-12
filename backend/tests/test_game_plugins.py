"""v5.1-P7 / D288：模组插件桥。"""

from __future__ import annotations

import pytest

import backend.config as config
import backend.db as db
import backend.game.manager as game_manager_module
from backend.agent import tools as tools_module
from backend.app import create_app
from backend.game.manager import GameManager
from backend.services import friend_service
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
    with db.session_scope() as session:
        friend = friend_service.create(session, Actor.USER, name="小敏")
        friend_id = friend.id
    await manager.start(friend_id=friend_id, game_kind="mock", config={})
    return manager, friend_id


@pytest.mark.asyncio
async def test_plugin_list_and_call_through_manager(temp_db, monkeypatch):
    manager, _friend_id = await _manager(temp_db, monkeypatch)
    try:
        listed = await manager.plugin_action("list")
        assert listed["plugins"][0]["id"] == "inventory_bridge"
        called = await manager.plugin_action(
            "call",
            {
                "plugin": "inventory_bridge",
                "action": "inventory_summary",
                "args": {},
            },
        )
        assert called["ok"] is True
        assert called["plugin"] == "inventory_bridge"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_plugin_tool_is_exposed_and_calls(temp_db, monkeypatch):
    manager, friend_id = await _manager(temp_db, monkeypatch)
    try:
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "plugin-call",
                    "name": "game_plugin",
                    "arguments": {
                        "action": "call",
                        "plugin": "inventory_bridge",
                        "action_name": "use_held_item",
                        "args": {},
                    },
                },
            )
        assert record["ok"] is True, record
        assert record["result"]["ok"] is True
        assert "game_plugin" in "\n".join(
            tools_module.prompt_lines("normal", game_active=True)
        )
    finally:
        await manager.stop()
