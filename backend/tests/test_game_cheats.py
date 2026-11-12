"""v5.1-P2 / D290：作弊 profile、工具门控、审计。"""

from __future__ import annotations

import asyncio

import pytest

import backend.config as config
import backend.db as db
import backend.game.manager as game_manager_module
from backend.agent import tools as tools_module
from backend.app import create_app
from backend.game import cheats
from backend.game.manager import GameManager
from backend.services import friend_service, game_store, settings_service
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


def test_default_and_profile_presets():
    default = cheats.default_config()
    assert default["profile"] == "survival"
    assert default["master_enabled"] is False
    assert not any(default["abilities"].values())
    assert not cheats.is_active(default)

    assisted = cheats.normalize_config(
        {
            "profile": "assisted",
            "master_enabled": True,
            "abilities": cheats.PROFILE_PRESETS["assisted"],
        }
    )
    assert cheats.is_active(assisted)
    assert "heal" in cheats.enabled_abilities(assisted)
    assert "command" not in cheats.enabled_abilities(assisted)


def test_cheat_commands_are_concrete():
    assert cheats.build_commands("give", {"item": "diamond", "count": 3}) == [
        "give @s diamond 3"
    ]
    assert cheats.build_commands("teleport", {"x": 1, "y": 64, "z": -2}) == [
        "tp @s 1 64 -2"
    ]
    assert cheats.build_commands("time", {"value": "night"}) == ["time set night"]
    assert cheats.build_commands("instant_mine", {"x": 1, "y": 2, "z": 3}) == [
        "setblock 1 2 3 air destroy"
    ]
    assert cheats.build_commands("heal") == [
        "effect give @s instant_health 1 10 true",
        "effect give @s saturation 1 10 true",
    ]
    assert cheats.build_commands("command", {"command": "/say hi"}) == ["say hi"]


def test_game_cheat_tool_is_gated_by_user_enable():
    assert "game_cheat" not in "\n".join(
        tools_module.prompt_lines("normal", game_active=True)
    )
    assert "game_cheat" in "\n".join(
        tools_module.prompt_lines(
            "normal", game_active=True, cheat_active=True
        )
    )
    off = {
        item["function"]["name"]
        for item in tools_module.native_tools("normal", game_active=True)
    }
    on = {
        item["function"]["name"]
        for item in tools_module.native_tools(
            "normal", game_active=True, cheat_active=True
        )
    }
    assert "game_cheat" not in off
    assert "game_cheat" in on


@pytest.mark.asyncio
async def test_manager_rejects_cheat_until_enabled_and_audits(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "摘要", []

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
    session_id = manager.session_id
    try:
        with pytest.raises(PermissionError):
            await manager.cheat("heal")

        configured = manager.set_cheat_config(
            {
                "profile": "assisted",
                "master_enabled": True,
                "abilities": {"heal": True},
            },
            persist=True,
        )
        assert configured["active"] is True
        assert manager.cheats_active_for(friend_id) is True
        note = await manager.note_for_prompt(friend_id)
        assert "TA 已手动开启作弊" in note
        assert "heal" in note

        with db.session_scope() as session:
            remembered = settings_service.game_cheats_from(
                settings_service.get(session)
            )
        assert remembered["profile"] == "assisted"
        assert remembered["abilities"]["heal"] is True

        result = await manager.cheat("heal")
        assert result["cheat"] == "heal"
        assert result["commands"]

        events = []
        for _ in range(80):
            with db.session_scope() as session:
                events = game_store.recent_events(session, session_id, limit=50)
            if any(event.type == "cheat" for event in events):
                break
            await asyncio.sleep(0.02)
        cheat_events = [event for event in events if event.type == "cheat"]
        assert cheat_events
        assert '"source": "cheat"' in cheat_events[-1].payload

        with db.session_scope() as session:
            blocked = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "cheat_bypass",
                    "name": "game_do",
                    "arguments": {
                        "action": "command",
                        "args": {"commands": ["time set day"]},
                    },
                },
            )
        assert blocked["ok"] is False
        assert "只能由 game_cheat" in blocked["error"]

        manager.set_cheat_config({"master_enabled": False})
        assert manager.cheats_active_for(friend_id) is False
        note_off = await manager.note_for_prompt(friend_id)
        assert "TA 已手动开启作弊" not in note_off
        with pytest.raises(PermissionError):
            await manager.cheat("heal")
    finally:
        await manager.stop()
