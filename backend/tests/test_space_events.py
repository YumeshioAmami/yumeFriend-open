"""v7.21 / D374：小世界状态、事件、自由模式、聊天注入与删除级联。"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

import backend.config as config
import backend.db as db
from backend.agent import context as ctx
from backend.app import create_app
from backend.features import events, flags
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import (
    Friend,
    FriendSpace,
    FriendSpaceRuntime,
    Memory,
    Message,
    SpaceEvent,
    SpaceObjectKnowledge,
)
from backend.services import (
    character_state_service,
    message_service,
    settings_service,
    space_service,
    space_tools,
    space_world_service,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "space.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setenv("YF_SERVER_ONLY", "1")
    flags.invalidate()
    events.clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    flags.invalidate()
    events.clear()


def _create(client, name: str = "汐音") -> str:
    response = client.post(
        "/api/friends",
        json={"name": name, "personality": "安静、会观察", "custom_instructions": "短句"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_default_state_is_free_and_per_friend(client):
    first = _create(client, "汐音")
    second = _create(client, "阿月")

    state = client.get(f"/api/friends/{first}/space").json()["state"]
    assert state["mode"] == "free"


    assert state["layout"]["mug"] == space_service.DEFAULT_LAYOUT["mug"]
    assert state["prop_states"]["mug"] == "full"
    assert state["world"] == {"environment": "room", "objects": []}

    patched = client.patch(
        f"/api/friends/{first}/space",
        json={
            "mode": "together",
            "layout": {"mug": [0.2, 0.11, -0.4]},
            "prop_states": {"mug": "empty"},
            "world": {
                "environment": "white",
                "objects": [
                    {
                        "id": "table-1",
                        "asset": "table",
                        "position": [0.4, 0, -0.2],
                        "rotation": 0.1,
                        "scale": 1.0,
                    },
                ],
            },
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["state"]["mode"] == "together"
    assert patched.json()["state"]["layout"]["mug"] == [0.2, 0.11, -0.4]
    assert patched.json()["state"]["prop_states"]["mug"] == "empty"
    assert patched.json()["state"]["world"]["environment"] == "white"
    assert patched.json()["state"]["world"]["objects"][0]["asset"] == "table"

    other = client.get(f"/api/friends/{second}/space").json()["state"]
    assert other["mode"] == "free"
    assert other["layout"]["mug"] == space_service.DEFAULT_LAYOUT["mug"]
    assert other["world"] == {"environment": "room", "objects": []}


def test_outfit_is_always_clothed(client):
    fid = _create(client)
    state = client.get(f"/api/friends/{fid}/space").json()["state"]
    assert ("r" "18") not in state["appearance"]
    assert state["appearance"]["outfit"]["top"] == "wear"
    assert state["appearance"]["outfit"]["bottom"] == "wear"

    response = client.patch(
        f"/api/friends/{fid}/space",
        json={
            "appearance": {
                "outfit": {"top": "none", "bottom": "none"},
            }
        },
    )
    assert response.status_code == 200, response.text
    state = response.json()["state"]
    assert ("r" "18") not in state["appearance"]
    assert state["appearance"]["outfit"]["top"] == "wear"
    assert state["appearance"]["outfit"]["bottom"] == "wear"


def test_concurrent_first_access_and_perceive_are_serialized(client):
    friend_id = _create(client)

    def read_state() -> str:
        with db.session_scope() as session:
            return space_service.get_state(session, friend_id)["mode"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: read_state(), range(12))) == ["free"] * 12

    payload = {
        "viewer": {
            "camera": [0, 1.38, 2.6],
            "look": [0, -0.16, -0.99],
            "hand": [0.28, 1.17, 1.89],
            "hand_active": False,
            "hand_holding": None,
        },
        "objects": [
            {"key": "mug", "label": "杯子", "position": [0.95, 0.12, -0.9], "moving": False}
        ],
        "personality_curiosity": 0.55,
    }

    def tick() -> str:
        with db.session_scope() as session:
            return space_service.perceive(
                session,
                friend_id,
                viewer=payload["viewer"],
                objects=payload["objects"],
                personality_curiosity=payload["personality_curiosity"],
            )["runtime"]["friend_id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: tick(), range(16))) == [friend_id] * 16


def test_world_validation_and_prompt(client):
    friend_id = _create(client)
    bad = client.patch(
        f"/api/friends/{friend_id}/space",
        json={"world": {"environment": "white", "objects": [{"asset": "nope"}]}},
    )
    assert bad.status_code == 422

    ok = client.patch(
        f"/api/friends/{friend_id}/space",
        json={
            "world": {
                "environment": "white",
                "objects": [
                    {"id": "chair-1", "asset": "chair", "position": [0, 0, 0]},
                ],
            }
        },
    )
    assert ok.status_code == 200, ok.text
    with db.session_scope() as session:
        note = space_service.prompt_note(session, friend_id)
    assert "纯白空间" in note
    assert "一把椅子" in note


def test_world_model_is_semantic_not_just_coordinates(client):
    friend_id = _create(client)
    patched = client.patch(
        f"/api/friends/{friend_id}/space",
        json={
            "prop_states": {"book": "open"},
            "world": {
                "environment": "room",
                "objects": [
                    {
                        "id": "table-1",
                        "asset": "table",
                        "position": [2.4, 0, -2.3],
                        "rotation": 0.2,
                        "scale": 1.0,
                    },
                    {
                        "id": "box-on-table",
                        "asset": "box",
                        "position": [2.4, 0.8, -2.3],
                        "rotation": 0.0,
                        "scale": 1.0,
                    },
                ],
            },
        },
    )
    assert patched.status_code == 200, patched.text
    client.post(
        f"/api/friends/{friend_id}/space/events",
        json={"kind": "move", "target": "book", "detail": {"to": [0.2, 0, 0]}},
    )
    body = client.get(f"/api/friends/{friend_id}/space").json()
    model = body["world_model"]
    assert model["room"]["width"] == 10.2
    assert model["zones"]
    entities = {item["key"]: item for item in model["entities"]}
    assert entities["book"]["state"]["state"] == "open"
    assert "翻开" in entities["book"]["affordances"]
    assert entities["table-1"]["label"] == "桌子"
    assert "把东西放上去" in entities["table-1"]["affordances"]
    assert entities["table-1"]["zone"]["label"]
    assert entities["box-on-table"]["support"] == "桌面"
    assert entities["book"]["last_event"]


def test_room_fixtures_are_semantic_and_persisted(client):
    friend_id = _create(client)
    patched = client.patch(
        f"/api/friends/{friend_id}/space",
        json={"prop_states": {"room_light": "off", "curtain": "closed"}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["state"]["prop_states"]["room_light"] == "off"
    assert patched.json()["state"]["prop_states"]["curtain"] == "closed"
    model = client.get(f"/api/friends/{friend_id}/space").json()["world_model"]
    entities = {item["key"]: item for item in model["entities"]}
    assert entities["room_light"]["state"]["state"] == "off"
    assert "打开" in entities["room_light"]["affordances"]
    assert entities["curtain"]["state"]["state"] == "closed"
    assert "拉开" in entities["curtain"]["affordances"]


def test_outfit_slots_persist_and_enter_world_prompt(client):
    friend_id = _create(client)
    patched = client.patch(
        f"/api/friends/{friend_id}/space",
        json={
            "appearance": {
                "outfit": {
                    "top": "wear",
                    "bottom": "wear",
                    "shoes": "wear",
                    "accessory": "none",
                }
            }
        },
    )
    assert patched.status_code == 200, patched.text
    outfit = patched.json()["state"]["appearance"]["outfit"]
    assert outfit["top"] == "wear"
    assert outfit["bottom"] == "wear"
    assert outfit["shoes"] == "wear"
    assert outfit["accessory"] == "none"

    invalid = client.patch(
        f"/api/friends/{friend_id}/space",
        json={"appearance": {"outfit": {"top": "not-a-real-shirt"}}},
    )
    assert invalid.status_code == 200, invalid.text
    assert invalid.json()["state"]["appearance"]["outfit"]["top"] == "wear"

    client.patch(
        f"/api/friends/{friend_id}/space",
        json={
            "appearance": {
                "outfit": {
                    "top": "wear",
                    "bottom": "wear",
                    "shoes": "wear",
                    "accessory": "none",
                }
            }
        },
    )
    with db.session_scope() as session:
        model = space_world_service.build_world_snapshot(session, friend_id)
        prompt = space_world_service.to_prompt(model)
    assert model["self"]["outfit"]["top"] == "wear"
    assert model["self"]["outfit"]["accessory"] == "none"
    assert "上衣：穿着" in prompt
    assert "鞋：穿着" in prompt
    toolset = space_tools.toolset_prompt(model)
    assert 'target="outfit"' in toolset
    assert "wear" in toolset


def test_world_senses_and_self_body_are_part_of_perception(client):
    friend_id = _create(client)
    patched = client.patch(
        f"/api/friends/{friend_id}/space",
        json={
            "prop_states": {
                "room_light": "off",
                "curtain": "closed",
                "window": "open",
                "door": "closed",
            }
        },
    )
    assert patched.status_code == 200, patched.text
    with db.session_scope() as session:
        model = space_world_service.build_world_snapshot(
            session,
            friend_id,
            self_state={
                "position": [0.2, 0, -1.0],
                "hand": [0.3, 1.1, -1.1],
                "holding": "杯子",
                "sitting": True,
                "moving": False,
                "still_seconds": 4.0,
            },
            objects=[
                {
                    "key": "mug",
                    "label": "杯子",
                    "position": [0.95, 0.12, -0.9],
                    "moving": True,
                }
            ],
        )
    senses = model["senses"]
    assert senses["time_phase"]["label"]
    assert senses["light"]["room_light"] == "off"
    assert senses["light"]["curtain"] == "closed"
    assert senses["light"]["window"] == "open"
    assert senses["temperature"]["celsius"] < 25
    assert any("听见" in line for line in senses["summary"])
    assert "杯子" in senses["sound"]["moving"]
    assert model["self"]["holding"] == "杯子"
    assert model["self"]["sitting"] is True


def test_world_senses_change_with_light_and_window_state():
    entities = [
        {"key": "mug", "label": "杯子", "moving": True},
        {"key": "book", "label": "书", "moving": False},
    ]
    closed = space_world_service._build_senses(
        {"room_light": "off", "curtain": "closed", "window": "closed", "door": "closed"},
        entities,
        now=datetime(2026, 11, 1, 23, 30, tzinfo=timezone.utc),
    )
    open_air = space_world_service._build_senses(
        {"room_light": "on", "curtain": "open", "window": "open", "door": "open"},
        [],
        now=datetime(2026, 11, 1, 12, 30, tzinfo=timezone.utc),
    )
    assert closed["light"]["level"] < open_air["light"]["level"]
    assert closed["temperature"]["celsius"] < open_air["temperature"]["celsius"]
    assert "穿堂风" in open_air["air"]["description"]
    assert "杯子" in closed["sound"]["description"]


def test_upload_glb_asset_and_use_in_world(client):
    friend_id = _create(client)
    bad = client.post(
        f"/api/friends/{friend_id}/space/assets",
        files={"file": ("x.txt", b"nope", "text/plain")},
    )
    assert bad.status_code == 422

    uploaded = client.post(
        f"/api/friends/{friend_id}/space/assets",
        files={"file": ("chair.glb", b"glTF" + b"\x00" * 32, "model/gltf-binary")},
    )
    assert uploaded.status_code == 200, uploaded.text
    asset = uploaded.json()["asset"]
    assert asset.startswith("glb:")

    world = client.patch(
        f"/api/friends/{friend_id}/space",
        json={
            "world": {
                "environment": "white",
                "objects": [
                    {"id": "glb-1", "asset": asset, "position": [0, 0, 0]},
                ],
            }
        },
    )
    assert world.status_code == 200, world.text
    fetched = client.get(uploaded.json()["url"])
    assert fetched.status_code == 200
    with db.session_scope() as session:
        note = space_service.prompt_note(session, friend_id)
    assert "一个导入的物件" in note


def test_life_text_maps_to_room_action():
    assert space_service.action_for_life_text("看了一篇长文章")["target"] == "book"
    assert space_service.action_for_life_text("在 B 站刷视频")["target"] == "phone"
    assert space_service.action_for_life_text("听一张专辑")["target"] == "cushion"
    assert space_service.action_for_life_text("随便发呆") is None


def test_set_state_action_is_a_first_class_world_action():
    action = space_service._normalize_action(
        {
            "kind": "set_state",
            "target": "book",
            "state": {"open": True},
            "duration_seconds": 3,
        }
    )
    assert action is not None
    assert action["kind"] == "set_state"
    assert action["target"] == "book"
    assert action["state"] == {"open": True}
    picked = space_service._normalize_action(
        {"kind": "pick_up", "target": "mug", "duration_seconds": 3}
    )
    assert picked is not None and picked["kind"] == "pick_up"
    pushed = space_service._normalize_action(
        {"kind": "push", "target": "box-1", "strength": 1.8, "duration_seconds": 2}
    )
    assert pushed is not None and pushed["strength"] == 1.8
    # 房间里导入/摆放的东西也默认可以碰，不因为不是内置道具就被拒绝。
    used = space_service._normalize_action(
        {"kind": "use", "target": "box-1", "duration_seconds": 3}
    )
    arranged = space_service._normalize_action(
        {"kind": "arrange", "target": "glb-chair", "duration_seconds": 3}
    )
    assert used is not None and used["target"] == "box-1"
    assert arranged is not None and arranged["target"] == "glb-chair"
    dress = space_service._normalize_action(
        {
            "kind": "set_state",
            "target": "outfit",
            "state": {"top": "wear", "bottom": "none"},
            "duration_seconds": 2,
        }
    )
    assert dress is not None and dress["target"] == "outfit"


def test_autonomy_use_can_write_memory(client):
    friend_id = _create(client)
    client.post(
        f"/api/friends/{friend_id}/space/events",
        json={
            "kind": "autonomy",
            "target": "book",
            "detail": {"action": {"kind": "use", "target": "book"}},
        },
    )
    with db.session_scope() as session:
        memory_count = session.scalar(
            select(func.count()).select_from(Memory).where(Memory.friend_id == friend_id)
        )
    assert memory_count == 1


def test_event_log_dedupes_and_prompt_note(client):
    friend_id = _create(client)
    first = client.post(
        f"/api/friends/{friend_id}/space/events",
        json={"kind": "touch", "target": "tail"},
    )
    second = client.post(
        f"/api/friends/{friend_id}/space/events",
        json={"kind": "touch", "target": "tail"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["event"]["id"] == second.json()["event"]["id"]

    client.post(
        f"/api/friends/{friend_id}/space/events",
        json={"kind": "move", "target": "plush", "detail": {"to": "window"}},
    )
    with db.session_scope() as session:
        note = space_service.prompt_note(session, friend_id)
    assert "小世界里刚发生的事" in note
    assert "尾巴" in note
    assert "鲸鱼玩偶" in note

    # 普通触摸不写记忆；搬玩偶会写一条，但同一天不会写第二条
    client.post(
        f"/api/friends/{friend_id}/space/events",
        json={"kind": "move", "target": "mug"},
    )
    with db.session_scope() as session:
        memory_count = session.scalar(
            select(func.count()).select_from(Memory).where(Memory.friend_id == friend_id)
        )
        bridge_events = session.scalar(
            select(func.count())
            .select_from(SpaceEvent)
            .where(SpaceEvent.friend_id == friend_id, SpaceEvent.kind == "memory")
        )
    assert memory_count == 1
    assert bridge_events == 1


def test_prompt_block_is_gated_by_space_feature():
    now = datetime.now(timezone.utc).isoformat()
    friend = Friend(
        id="f-space",
        name="汐音",
        personality="安静",
        custom_instructions="短句",
        created_at=now,
        updated_at=now,
    )
    without_space = ctx.build_system_prompt(
        friend, space_note="SPACE_MARKER", enabled_features={"core"}
    )
    assert "SPACE_MARKER" not in without_space
    with_space = ctx.build_system_prompt(
        friend, space_note="SPACE_MARKER", enabled_features={"core", "space"}
    )
    assert "SPACE_MARKER" in with_space


def test_think_falls_back_to_local_action(client):
    friend_id = _create(client)
    response = client.post(
        f"/api/friends/{friend_id}/space/think",
        json={"text": "陪我一起看窗外"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"]["kind"] in space_service.ACTION_KINDS
    assert body["line"] is None

    with db.session_scope() as session:
        result = asyncio.run(space_service.think(session, friend_id, allow_llm=False))
    assert result["action"]["kind"] in space_service.ACTION_KINDS


def test_think_receives_user_camera_and_hand(client, monkeypatch):
    friend_id = _create(client)
    viewer = {
        "camera": [0.2, 1.3, 2.1],
        "look": [0.1, -0.2, -0.9],
        "hand": [0.4, 1.1, 1.5],
        "hand_active": True,
        "hand_holding": "杯子",
    }
    response = client.post(
        f"/api/friends/{friend_id}/space/think",
        json={"text": "你在看什么？", "viewer": viewer},
    )
    assert response.status_code == 200, response.text

    captured: dict[str, str] = {}

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        captured["user"] = user
        return {
            "line": "我在看你手里的杯子。",
            "action": {"kind": "go_to", "target": "desk", "duration_seconds": 6},
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    with db.session_scope() as session:
        result = asyncio.run(
            space_service.think(session, friend_id, text="你在看什么？", viewer=viewer)
        )

    assert result["source"] == "llm"
    assert "相机在" in captured["user"]
    assert "手在" in captured["user"]
    assert "杯子" in captured["user"]


def test_perception_only_focuses_without_template_questions(client):
    friend_id = _create(client)
    payload = {
        "viewer": {
            "camera": [0.2, 1.3, 2.1],
            "look": [0.1, -0.2, -0.9],
            "hand": [0.45, 0.35, -0.95],
            "hand_active": True,
            "hand_holding": "杯子",
        },
        "objects": [
            {
                "key": "mug",
                "label": "杯子",
                "position": [0.5, 0.12, -1.0],
                "moving": False,
            },
            {
                "key": "notebook",
                "label": "笔记本",
                "position": [-1.3, 0.03, -0.5],
                "moving": False,
            },
        ],
        "personality_curiosity": 1.0,
    }
    for _ in range(6):
        response = client.post(
            f"/api/friends/{friend_id}/space/perceive",
            json=payload,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["line"] is None
        assert body["action"] is None
        assert body["ask_intent"] is None
        assert body["focus"] is not None
        assert body["runtime"]["intent"]["kind"] != "ask"
    assert body["runtime"]["attention"] == "notice"
    assert all(item["ask_pressure"] == 0 for item in body["knowledge"])


def test_explanation_without_pending_question_does_not_write_knowledge(client):
    friend_id = _create(client)
    response = client.post(
        f"/api/friends/{friend_id}/space/think",
        json={"text": "这是喝水用的杯子"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["knowledge"] == []
    assert body["discovery"]["understood"] == 0
    assert body["runtime"]["intent"].get("kind") != "understanding"


def test_perception_uses_motion_current_activity_and_mood(client):
    friend_id = _create(client)
    payload = {
        "viewer": {
            "camera": [0.4, 1.35, 1.8],
            "look": [0, -0.2, -1],
            "hand": [2.5, 1.1, 2.5],
            "hand_active": False,
            "hand_holding": None,
        },
        "objects": [
            {"key": "mug", "label": "杯子", "position": [0.5, 0.12, -1.0], "moving": False},
            {
                "key": "notebook",
                "label": "笔记本",
                "position": [0.5, 0.12, -1.1],
                "moving": True,
            },
        ],
        "personality_curiosity": 1.0,
        "activity_kind": "read",
        "activity_target": "notebook",
        "activity_label": "她在翻一本旧笔记",
        "mood": "好奇",
    }
    response = client.post(
        f"/api/friends/{friend_id}/space/perceive",
        json=payload,
    )
    assert response.status_code == 200, response.text
    rows = {item["object_key"]: item for item in response.json()["knowledge"]}
    assert rows["notebook"]["salience"] > rows["mug"]["salience"]
    assert response.json()["ask_intent"] is None


def test_micro_deliberation_controls_body_and_model_only_speech(client, monkeypatch):
    friend_id = _create(client)
    viewer = {
        "camera": [0.2, 1.3, 2.1],
        "look": [0.1, -0.2, -0.9],
        "hand": [0.45, 0.35, -0.95],
        "hand_active": False,
        "hand_holding": None,
    }
    objects = [
        {"key": "mug", "label": "杯子", "position": [0.5, 0.12, -1.0], "moving": False}
    ]
    moving_payload = {
        "viewer": viewer,
        "self_state": {
            "position": [2.0, 0, 2.0],
            "moving": True,
            "still_seconds": 0.0,
            "target": "mug",
            "at_target": False,
        },
        "objects": objects,
        "paused": False,
        "personality_curiosity": 1.0,
    }
    body: dict = {}
    for _ in range(24):
        body = client.post(
            f"/api/friends/{friend_id}/space/perceive",
            json=moving_payload,
        ).json()
        if body["deliberation_due"] and body["focus"]:
            break
    assert body["deliberation_due"] is True
    assert body["focus"]["object_key"] == "mug"
    assert body["ask_intent"] is None
    assert body["action"] is None

    captured: list[str] = []
    captured_purposes: list[str] = []
    replies = [
        {
            "thought": "先走近一点再看清楚，不要一边走一边说。",
            "space_do": {"kind": "approach", "target": "mug", "duration_seconds": 3},
            "space_say": {"text": "这个杯子是什么？"},
            "next_think_in": 8,
        },
        {
            "thought": "站定以后才看见杯沿有一个小缺口，想问一下。",
            "space_do": {"kind": "idle", "target": None, "duration_seconds": 3},
            "space_say": {"text": "这个杯沿怎么缺了一小块？"},
            "next_think_in": 9,
        },
    ]

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        captured.append(user)
        captured_purposes.append(purpose)
        return replies[len(captured) - 1]

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    first = client.post(
        f"/api/friends/{friend_id}/space/deliberate",
        json=moving_payload,
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    # 她可以边走边问；但问句一出口就停下身体等待回答。
    assert first_body["action"]["kind"] == "wait"
    assert first_body["line"] == "这个杯子是什么？"
    assert first_body["ask_intent"] is None
    assert first_body["runtime"]["intent"]["kind"] == "awaiting_reply"
    assert first_body["awaiting_reply"] is True
    assert "正在走" in captured[0]
    assert captured_purposes[0] == "chat"

    still_payload = {
        **moving_payload,
        "self_state": {
            "position": [0.5, 0, -0.9],
            "moving": False,
            "still_seconds": 2.2,
            "target": "mug",
            "at_target": True,
        },
    }
    second = client.post(
        f"/api/friends/{friend_id}/space/deliberate",
        json=still_payload,
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["source"] == "llm"
    # 刚说过一句又问，属于自主循环的重复开口；反重复闸会压掉台词，
    # 但内部判断仍然更新。
    assert second_body["line"] is None
    assert second_body["runtime"]["intent"]["thought"] == "站定以后才看见杯沿有一个小缺口，想问一下。"
    assert second_body["ask_intent"] is None
    assert second_body["runtime"]["intent"]["kind"] != "awaiting_reply"
    assert "停着" in captured[1]


def test_moving_non_question_speech_is_not_suppressed(client, monkeypatch):
    friend_id = _create(client)

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        return {
            "thought": "窗边的光变了，我想顺口告诉 TA。",
            "space_do": {"kind": "approach", "target": "window", "duration_seconds": 3},
            "space_say": {"text": "窗外好像亮了一点。", "emotion": "calm"},
            "next_think_in": 4,
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    response = client.post(
        f"/api/friends/{friend_id}/space/deliberate",
        json={
            "viewer": {
                "camera": [0.2, 1.3, 2.1],
                "look": [0.1, -0.2, -0.9],
                "hand": [0.45, 0.35, -0.95],
            },
            "self_state": {
                "position": [2.0, 0, 2.0],
                "moving": True,
                "still_seconds": 0.0,
                "target": "window",
                "at_target": False,
            },
            "objects": [],
            "paused": False,
            "personality_curiosity": 0.8,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"]["kind"] == "approach"
    assert body["line"] == "窗外好像亮了一点。"
    assert body["awaiting_reply"] is False
    assert body["runtime"]["intent"]["kind"] != "awaiting_reply"


def test_deliberation_local_fallback_never_invents_speech(client):
    friend_id = _create(client)
    payload = {
        "viewer": {
            "camera": [0.2, 1.3, 2.1],
            "look": [0.1, -0.2, -0.9],
            "hand": [0.45, 0.35, -0.95],
            "hand_active": False,
            "hand_holding": None,
        },
        "self_state": {
            "position": [2.0, 0, 2.0],
            "moving": True,
            "still_seconds": 0.0,
            "target": "mug",
            "at_target": False,
        },
        "objects": [
            {"key": "mug", "label": "杯子", "position": [0.5, 0.12, -1.0], "moving": False}
        ],
        "paused": False,
        "personality_curiosity": 1.0,
    }
    for _ in range(24):
        body = client.post(
            f"/api/friends/{friend_id}/space/perceive",
            json=payload,
        ).json()
        if body["deliberation_due"] and body["focus"]:
            break
    assert body["deliberation_due"] is True

    approach = client.post(
        f"/api/friends/{friend_id}/space/deliberate",
        json=payload,
    ).json()
    assert approach["action"]["kind"] == "approach"
    assert approach["line"] is None
    with db.session_scope() as session:
        runtime = space_service.get_or_create_runtime(session, friend_id)
        runtime.intent = json.dumps(
            {
                "kind": "considering",
                "target": "mug",
                "label": "杯子",
                "since": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        )
        runtime.next_deliberation_at = "2000-01-01T00:00:00+00:00"

    still = {
        **payload,
        "self_state": {
            "position": [0.5, 0, -0.9],
            "moving": False,
            "still_seconds": 2.0,
            "target": "mug",
            "at_target": True,
        },
    }
    arrived = client.post(
        f"/api/friends/{friend_id}/space/perceive",
        json=still,
    ).json()
    assert arrived["deliberation_due"] is True
    assert arrived["focus"]["object_key"] == "mug"
    settled = client.post(
        f"/api/friends/{friend_id}/space/deliberate",
        json=still,
    ).json()
    assert settled["source"] == "local"
    assert settled["ask_intent"] is None
    assert settled["line"] is None
    assert settled["action"]["kind"] in space_service.ACTION_KINDS


def test_deliberation_uses_character_context_goal_plan_and_action_feedback(
    client, monkeypatch
):
    friend_id = _create(client)
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as session:
        state = character_state_service.get_or_create(session, friend_id)
        state.inner_note = "我想把窗边弄得舒服一点"
        session.add(
            Message(
                id="msg-space-deliberation",
                friend_id=friend_id,
                role="user",
                content="窗边看起来不错，你可以自己安排。",
                created_at=now,
                updated_at=now,
            )
        )

    captured: dict[str, str] = {}

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        captured["system"] = system
        captured["user"] = user
        return {
            "thought": "窗边空着，我想先过去，再把靠垫拖过去。",
            "goal": "把窗边变成一个舒服的位置",
            "plan": ["走到窗边", "搬一个靠垫过去", "坐下试试"],
            "action": {"kind": "go_to", "target": "window", "duration_seconds": 4},
            "say": "",
            "ask_target": "",
            "next_think_in": 6,
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    payload = {
        "viewer": {
            "camera": [0.2, 1.3, 2.1],
            "look": [0.1, -0.2, -0.9],
            "hand": [0.45, 0.35, -0.95],
            "hand_active": False,
            "hand_holding": None,
        },
        "self_state": {
            "position": [2.0, 0, 2.0],
            "moving": False,
            "still_seconds": 2.0,
            "target": None,
            "at_target": False,
        },
        "objects": [
            {"key": "mug", "label": "杯子", "position": [0.5, 0.12, -1.0], "moving": False}
        ],
        "paused": False,
        "personality_curiosity": 0.8,
        "activity_label": "她在房间里发呆",
        "mood": "好奇",
    }
    response = client.post(
        f"/api/friends/{friend_id}/space/deliberate",
        json=payload,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"]["kind"] == "go_to"
    action_id = body["action"]["id"]
    assert action_id
    assert body["runtime"]["goal"] == "把窗边变成一个舒服的位置"
    assert body["runtime"]["plan"] == ["走到窗边", "搬一个靠垫过去", "坐下试试"]
    assert body["runtime"]["plan_step"] == 0
    assert body["runtime"]["last_action_result"]["status"] == "issued"

    # 同一颗大脑：小世界深思拿到了聊天用的角色状态、最近聊天和语义世界。
    assert "# 你现在" in captured["user"]
    assert "窗边看起来不错" in captured["user"]
    assert "# 你看得见的东西" in captured["user"]
    assert "杯子" in captured["user"]

    feedback = client.post(
        f"/api/friends/{friend_id}/space/action-result",
        json={"action_id": action_id, "ok": True, "detail": "走到了窗边"},
    )
    assert feedback.status_code == 200, feedback.text
    feedback_body = feedback.json()
    assert feedback_body["runtime"]["plan_step"] == 1
    assert feedback_body["runtime"]["last_action_result"]["status"] == "finished"
    assert feedback_body["runtime"]["last_action_result"]["ok"] is True
    events = client.get(f"/api/friends/{friend_id}/space/events").json()["events"]
    assert any(item["kind"] == "action_result" for item in events)


def test_space_goal_completion_writes_memory_and_resolves_open_loop(client, monkeypatch):
    friend_id = _create(client)

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        return {
            "thought": "我想先走到窗边，把那里当成今天的位置。",
            "goal": "把窗边当成今天待着的位置",
            "plan": ["走到窗边"],
            "action": {"kind": "go_to", "target": "window", "duration_seconds": 4},
            "say": "",
            "ask_target": "",
            "next_think_in": 6,
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    payload = {
        "viewer": {
            "camera": [0.2, 1.3, 2.1],
            "look": [0.1, -0.2, -0.9],
            "hand": [0.45, 0.35, -0.95],
            "hand_active": False,
            "hand_holding": None,
        },
        "self_state": {
            "position": [0, 0, 0],
            "moving": False,
            "still_seconds": 2.0,
            "target": None,
            "at_target": False,
        },
        "objects": [],
        "paused": False,
        "personality_curiosity": 0.7,
        "activity_label": "她在房间里待着",
        "mood": "平静",
    }
    response = client.post(
        f"/api/friends/{friend_id}/space/deliberate",
        json=payload,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    action_id = body["action"]["id"]
    assert body["runtime"]["goal"] == "把窗边当成今天待着的位置"
    with db.session_scope() as session:
        state = character_state_service.state_to_dict(
            character_state_service.get_or_create(session, friend_id)
        )
    assert "窗边" in state["inner_note"]
    assert any(
        "把窗边当成今天待着的位置" in str(item.get("text") if isinstance(item, dict) else item)
        for item in state["open_loops"]
    )

    feedback = client.post(
        f"/api/friends/{friend_id}/space/action-result",
        json={"action_id": action_id, "ok": True, "detail": "走到了窗边"},
    )
    assert feedback.status_code == 200, feedback.text
    assert feedback.json()["runtime"]["plan_step"] == 1
    with db.session_scope() as session:
        state = character_state_service.state_to_dict(
            character_state_service.get_or_create(session, friend_id)
        )
        memory_contents = session.scalars(
            select(Memory.content).where(Memory.friend_id == friend_id)
        ).all()
    assert any(
        "把窗边当成今天待着的位置" in str(item.get("text") if isinstance(item, dict) else item)
        for item in state["open_loops"]
    ) is False
    assert "完成了" in state["inner_note"]
    assert any("把窗边当成今天待着的位置" in content for content in memory_contents)


def test_failed_action_keeps_plan_step_and_schedules_retry(client):
    friend_id = _create(client)
    with db.session_scope() as session:
        runtime = space_service.get_or_create_runtime(session, friend_id)
        runtime.goal = "把书搬到窗边"
        runtime.plan = json.dumps(["走到书边", "拿起书", "搬到窗边"], ensure_ascii=False)
        runtime.plan_step = 1
        runtime.last_action_result = json.dumps(
            {
                "action_id": "act-failed",
                "status": "issued",
                "ok": None,
                "detail": "",
                "at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        )
    response = client.post(
        f"/api/friends/{friend_id}/space/action-result",
        json={"action_id": "act-failed", "ok": False, "detail": "没拿到书"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runtime"]["plan_step"] == 1
    assert body["runtime"]["last_action_result"]["ok"] is False
    assert body["runtime"]["next_deliberation_at"] is not None


def test_successful_action_continues_thinking_immediately(client):
    friend_id = _create(client)
    with db.session_scope() as session:
        runtime = space_service.get_or_create_runtime(session, friend_id)
        runtime.last_action_result = json.dumps(
            {
                "action_id": "act-continue",
                "status": "issued",
                "ok": None,
                "detail": "",
                "at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        )
    response = client.post(
        f"/api/friends/{friend_id}/space/action-result",
        json={"action_id": "act-continue", "ok": True, "detail": "做完了一步"},
    )
    assert response.status_code == 200, response.text
    next_at = response.json()["runtime"]["next_deliberation_at"]
    assert next_at
    delay = (
        datetime.fromisoformat(next_at) - datetime.now(timezone.utc)
    ).total_seconds()
    assert delay <= 1.0


def test_deliberation_single_flight_lock_returns_busy(client):
    friend_id = _create(client)

    async def scenario():
        lock = asyncio.Lock()
        space_service._DELIBERATION_LOCKS[friend_id] = lock
        async with lock:
            with db.session_scope() as session:
                return await space_service.deliberate(
                    session,
                    friend_id,
                    viewer=None,
                    self_state={"position": [0, 0, 0]},
                    objects=[],
                )

    try:
        result = asyncio.run(scenario())
    finally:
        space_service._DELIBERATION_LOCKS.pop(friend_id, None)
    assert result["busy"] is True
    assert result["source"] == "local"
    assert result["action"] is None
    assert result["line"] is None


def test_failure_guides_next_deliberation_and_preference_enters_memory(
    client, monkeypatch
):
    friend_id = _create(client)
    with db.session_scope() as session:
        runtime = space_service.get_or_create_runtime(session, friend_id)
        runtime.goal = "把杯子拿到手边"
        runtime.plan = json.dumps(["走到杯子边", "拿起杯子"], ensure_ascii=False)
        runtime.plan_step = 1
        runtime.last_action_result = json.dumps(
            {
                "action_id": "act-failed-1",
                "status": "issued",
                "ok": None,
                "detail": "",
                "goal": "把杯子拿到手边",
                "reason": "我以为站得够近了",
                "reflection": "",
                "preference": "",
                "failure_streak": 0,
                "at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        )
    failed = client.post(
        f"/api/friends/{friend_id}/space/action-result",
        json={"action_id": "act-failed-1", "ok": False, "detail": "离杯子太远了"},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["runtime"]["last_action_result"]["failure_streak"] == 1

    captured: dict[str, str] = {}

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        captured["system"] = system
        captured["user"] = user
        return {
            "thought": "刚才站得太远。绕到桌子这一侧，手才够得到。",
            "goal": "把杯子拿到手边",
            "plan": ["绕到杯子这一侧，重新拿杯子"],
            "action": {"kind": "pick_up", "target": "mug", "duration_seconds": 4},
            "say": "",
            "ask_target": "",
            "reflection": "失败以后要先换位置，而不是原地再伸手。",
            "preference": "我喜欢把常用的杯子放在靠近自己的桌角。",
            "next_think_in": 5,
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    payload = {
        "viewer": {
            "camera": [0.2, 1.3, 2.1],
            "look": [0.1, -0.2, -0.9],
            "hand": [0.45, 0.35, -0.95],
            "hand_active": False,
            "hand_holding": None,
        },
        "self_state": {
            "position": [0.0, 0, 0.0],
            "moving": False,
            "still_seconds": 2.0,
            "target": None,
            "at_target": False,
        },
        "objects": [
            {"key": "mug", "label": "杯子", "position": [0.95, 0.12, -0.9], "moving": False}
        ],
        "paused": False,
        "personality_curiosity": 0.7,
        "activity_label": "她在桌边发呆",
        "mood": "有点懊恼",
    }
    response = client.post(
        f"/api/friends/{friend_id}/space/deliberate",
        json=payload,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "上一条动作的结果" in captured["user"]
    assert "离杯子太远了" in captured["user"]
    assert "失败 1 次" in captured["user"]
    assert "不需要先问许可" in captured["system"]
    assert body["action"]["kind"] == "pick_up"
    assert body["preference"].startswith("我喜欢")

    succeeded = client.post(
        f"/api/friends/{friend_id}/space/action-result",
        json={
            "action_id": body["action"]["id"],
            "ok": True,
            "detail": "她把杯子拿到了手边",
        },
    )
    assert succeeded.status_code == 200, succeeded.text
    assert succeeded.json()["runtime"]["plan_step"] == 1
    with db.session_scope() as session:
        memories = session.scalars(
            select(Memory.content).where(Memory.friend_id == friend_id)
        ).all()
    assert any("我喜欢把常用的杯子放在靠近自己的桌角" in text for text in memories)
    assert any("失败以后要先换位置" in text for text in memories)


def test_chat_prompt_note_carries_active_space_goal_and_result(client):
    friend_id = _create(client)
    with db.session_scope() as session:
        runtime = space_service.get_or_create_runtime(session, friend_id)
        runtime.goal = "把窗边变成看书的位置"
        runtime.plan = json.dumps(["搬靠垫过去", "坐下试试"], ensure_ascii=False)
        runtime.plan_step = 0
        runtime.last_action_result = json.dumps(
            {
                "action_id": "act-note",
                "status": "finished",
                "ok": False,
                "detail": "靠垫太重，拖不动",
                "at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        )
        note = space_service.prompt_note(session, friend_id)
    assert "把窗边变成看书的位置" in note
    assert "搬靠垫过去" in note
    assert "没做成：靠垫太重，拖不动" in note
    assert "亲手做过" in note


def test_space_toolset_uses_live_entities_and_action_preconditions(client):
    friend_id = _create(client)
    patched = client.patch(
        f"/api/friends/{friend_id}/space",
        json={
            "world": {
                "environment": "room",
                "objects": [
                    {
                        "id": "box-live",
                        "asset": "box",
                        "position": [0.8, 0, -0.4],
                        "rotation": 0,
                        "scale": 1,
                    }
                ],
            }
        },
    )
    assert patched.status_code == 200, patched.text
    model = client.get(f"/api/friends/{friend_id}/space").json()["world_model"]
    prompt = space_tools.toolset_prompt(model)
    assert "space_do" in prompt
    assert "box-live" in prompt
    assert "pick_up(target)" in prompt
    assert "1.8m" in prompt
    assert "打开" in prompt or "把东西放进去" in prompt
    assert space_tools.looks_like_action_request("把杯子拿过来")
    assert space_tools.looks_like_action_request("把箱子推到床边")
    assert not space_tools.looks_like_action_request("你今天心情怎么样")


def test_world_dialogue_returns_tracked_space_do_and_plan(client, monkeypatch):
    friend_id = _create(client)
    called: list[str] = []

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        called.append(purpose)
        return {
            "thought": "TA 让我把杯子拿过来；先走近，再拿。",
            "space_plan": {
                "goal": "把杯子拿过来",
                "steps": ["走到杯子边", "拿起杯子"],
            },
            "space_say": {"text": "好，我去拿。"},
            "space_do": {
                "kind": "approach",
                "target": "mug",
                "duration_seconds": 4,
            },
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    response = client.post(
        f"/api/friends/{friend_id}/space/think",
        json={"text": "把杯子拿过来"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert called == ["chat"]
    assert body["line"] == "好，我去拿。"
    assert body["action"]["kind"] == "approach"
    assert body["action"]["target"] == "mug"
    assert body["action"]["id"]
    assert body["runtime"]["goal"] == "把杯子拿过来"
    assert body["runtime"]["plan"] == ["走到杯子边", "拿起杯子"]
    assert body["runtime"]["last_action_result"]["status"] == "issued"

    feedback = client.post(
        f"/api/friends/{friend_id}/space/action-result",
        json={
            "action_id": body["action"]["id"],
            "ok": True,
            "detail": "她走到了杯子边",
        },
    )
    assert feedback.status_code == 200, feedback.text
    assert feedback.json()["runtime"]["plan_step"] == 1


def test_action_request_repairs_a_say_only_world_reply(client, monkeypatch):
    friend_id = _create(client)
    replies = [
        {"space_say": {"text": "好呀。"}, "space_do": None},
        {
            "thought": "刚才只答应了，没有动手。",
            "space_say": {"text": "我现在过去。"},
            "space_do": {
                "kind": "approach",
                "target": "mug",
                "duration_seconds": 4,
            },
        },
    ]
    captured: list[str] = []

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        captured.append(user)
        return replies[len(captured) - 1]

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    response = client.post(
        f"/api/friends/{friend_id}/space/think",
        json={"text": "把杯子拿过来"},
    )
    assert response.status_code == 200, response.text
    assert len(captured) == 2
    assert "[工具纠正]" in captured[1]
    assert response.json()["action"]["kind"] == "approach"
    assert response.json()["action"]["id"]


def test_space_talk_stays_in_space_event_stream_and_has_own_transcript(client, monkeypatch):
    friend_id = _create(client)
    with db.session_scope() as session:
        message_service.create(
            session,
            friend_id,
            role="user",
            content="我想把窗边弄舒服一点。",
            stream_status="completed",
        )
    captured: dict[str, str] = {}

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        captured["purpose"] = purpose
        captured["user"] = user
        return {
            "line": "记得。你刚才说，想和我一起把窗边弄舒服一点。",
            "action": {"kind": "go_to", "target": "window", "duration_seconds": 4},
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    response = client.post(
        f"/api/friends/{friend_id}/space/think",
        json={"text": "你还记得我刚才跟你说什么吗？"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "llm"
    assert body["space_message"] is True
    assert captured["purpose"] == "chat"
    assert "# 你们最近在普通聊天里说过" in captured["user"]
    assert "我想把窗边弄舒服一点" in captured["user"]

    with db.session_scope() as session:
        messages = list(
            session.scalars(
                select(Message)
                .where(Message.friend_id == friend_id)
                .order_by(Message.created_at.asc())
            ).all()
        )
        state = character_state_service.state_to_dict(
            character_state_service.get_or_create(session, friend_id)
        )
        note = space_service.prompt_note(session, friend_id)
    # 小世界对话不再写入主聊天；主聊天只保留原来的普通消息。
    assert [item.role for item in messages] == ["user"]
    assert messages[0].content == "我想把窗边弄舒服一点。"
    assert "小世界里" in state["inner_note"]
    assert "你们刚在小世界里说过" not in note

    refreshed = client.get(f"/api/friends/{friend_id}/space").json()
    space_chat = refreshed["world_model"]["space_chat"]
    assert [item["role"] for item in space_chat[-2:]] == ["TA", "你"]
    assert "你还记得我刚才跟你说什么吗" in space_chat[-2]["content"]
    assert "窗边弄舒服" in space_chat[-1]["content"]
    assert refreshed["world_model"]["shared_state"]["intent"] == "陪 TA"

    history = client.get(f"/api/friends/{friend_id}/messages?limit=10").json()
    assert [item["role"] for item in history["messages"]] == ["user"]


def test_chat_messages_and_relevant_memories_enter_world_deliberation(client, monkeypatch):
    friend_id = _create(client)
    with db.session_scope() as session:
        message_service.create(
            session,
            friend_id,
            role="user",
            content="我把靠垫放到窗边了，晚上想坐那里。",
            stream_status="completed",
        )

    captured: dict[str, str] = {}

    def fake_recall(session, fid, query, limit=6):
        captured["memory_query"] = query
        return []

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        captured["user"] = user
        return {
            "thought": "TA 把靠垫放到窗边了，我可以过去坐坐。",
            "goal": "去窗边坐一会儿",
            "plan": ["走到窗边", "坐下"],
            "action": {"kind": "go_to", "target": "window", "duration_seconds": 4},
            "say": "",
            "ask_target": "",
            "next_think_in": 6,
        }

    monkeypatch.setattr(space_service.memory_service, "recall", fake_recall)
    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    response = client.post(
        f"/api/friends/{friend_id}/space/deliberate",
        json={
            "viewer": {
                "camera": [0.2, 1.3, 2.1],
                "look": [0.1, -0.2, -0.9],
                "hand": [0.45, 0.35, -0.95],
                "hand_active": False,
                "hand_holding": None,
            },
            "self_state": {
                "position": [0.0, 0, 0.0],
                "moving": False,
                "still_seconds": 2.0,
                "target": None,
                "at_target": False,
            },
            "objects": [
                {
                    "key": "mug",
                    "label": "杯子",
                    "position": [0.95, 0.12, -0.9],
                    "moving": False,
                }
            ],
            "paused": False,
            "personality_curiosity": 0.7,
            "activity_label": "她在房间里发呆",
            "mood": "平静",
        },
    )
    assert response.status_code == 200, response.text
    assert "我把靠垫放到窗边了" in captured["user"]
    assert "我把靠垫放到窗边了" in captured["memory_query"]


def test_touch_reaction_is_model_driven_and_waits_for_reply(client, monkeypatch):
    friend_id = _create(client)

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        return {
            "thought": "TA 突然碰我胸口，我有点没准备好。",
            "space_say": {"text": "你、你突然碰我干什么呀？", "emotion": "shy"},
            "space_do": None,
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    response = client.post(
        f"/api/friends/{friend_id}/space/react",
        json={
            "zone": "chest",
            "viewer": {
                "camera": [0.2, 1.3, 2.1],
                "look": [0.1, -0.2, -0.9],
                "hand": [0.45, 1.2, -0.95],
                "hand_active": True,
                "hand_holding": None,
            },
            "self_state": {
                "position": [0.4, 0, -0.6],
                "moving": False,
                "still_seconds": 2.0,
                "target": None,
                "at_target": False,
            },
            "objects": [],
            "activity_label": "她在房间里发呆",
            "mood": "平静",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "llm"
    assert body["emotion"] == "shy"
    assert body["awaiting_reply"] is True
    assert body["runtime"]["intent"]["kind"] == "awaiting_reply"
    assert body["runtime"]["intent"]["line"] == "你、你突然碰我干什么呀？"

    perceive = client.post(
        f"/api/friends/{friend_id}/space/perceive",
        json={
            "viewer": {
                "camera": [0.2, 1.3, 2.1],
                "look": [0.1, -0.2, -0.9],
                "hand": [0.45, 1.2, -0.95],
                "hand_active": False,
                "hand_holding": None,
            },
            "self_state": {
                "position": [0.4, 0, -0.6],
                "moving": False,
                "still_seconds": 2.0,
                "target": None,
                "at_target": False,
            },
            "objects": [],
            "paused": False,
            "personality_curiosity": 0.5,
        },
    ).json()
    assert perceive["awaiting_reply"] is True
    assert perceive["deliberation_due"] is False

    with db.session_scope() as session:
        row = session.scalars(
            select(SpaceEvent)
            .where(SpaceEvent.friend_id == friend_id, SpaceEvent.kind == "say")
            .order_by(SpaceEvent.created_at.desc())
        ).first()
    assert row is not None
    detail = json.loads(row.detail)
    assert detail["speaker"] == "assistant"
    assert detail["emotion"] == "shy"
    assert detail["text"] == "你、你突然碰我干什么呀？"
    # 小世界的这句话不进主聊天。
    with db.session_scope() as session:
        assert session.scalars(select(Message).where(Message.friend_id == friend_id)).first() is None


def test_perceive_marks_being_grabbed_and_restores_after_release(client):
    friend_id = _create(client)
    base = {
        "camera": [0.2, 1.3, 2.1],
        "look": [0.1, -0.2, -0.9],
        "hand": [0.45, 1.2, -0.95],
        "hand_active": True,
        "hand_holding": "她",
        "hand_contact_zone": "waist",
        "hand_contact_kind": "pull",
        "hand_contact_speed": 0.7,
        "hand_contact_seconds": 2.4,
    }
    held = client.post(
        f"/api/friends/{friend_id}/space/perceive",
        json={
            "viewer": base,
            "self_state": {
                "position": [0.4, 0, -0.6],
                "moving": False,
                "still_seconds": 0.2,
                "target": None,
                "at_target": False,
            },
            "objects": [],
            "paused": False,
            "personality_curiosity": 0.5,
        },
    )
    assert held.status_code == 200, held.text
    held_body = held.json()
    assert held_body["deliberation_due"] is False
    assert held_body["focus"] is None
    assert held_body["runtime"]["attention"] == "notice"
    assert held_body["runtime"]["intent"]["kind"] == "being_touched"
    assert held_body["runtime"]["intent"]["zone"] == "waist"
    assert held_body["runtime"]["intent"]["motion"] == "pull"

    released = client.post(
        f"/api/friends/{friend_id}/space/perceive",
        json={
            "viewer": {
                **base,
                "hand_active": False,
                "hand_holding": None,
                "hand_contact_zone": None,
                "hand_contact_kind": None,
                "hand_contact_seconds": 0,
            },
            "self_state": {
                "position": [0.4, 0, -0.6],
                "moving": False,
                "still_seconds": 0.5,
                "target": None,
                "at_target": False,
            },
            "objects": [],
            "paused": False,
            "personality_curiosity": 0.5,
        },
    )
    assert released.status_code == 200, released.text
    assert released.json()["runtime"]["intent"]["kind"] != "being_touched"


def test_touch_reaction_prompt_describes_grab_and_pull(client, monkeypatch):
    friend_id = _create(client)
    captured: dict[str, str] = {}

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        captured["user"] = user
        return {
            "thought": "TA 抓着我的腰往后拉，我有点站不稳。",
            "space_say": {"text": "喂……别拽，我站不稳。", "emotion": "surprised"},
            "space_do": None,
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    response = client.post(
        f"/api/friends/{friend_id}/space/react",
        json={
            "zone": "waist",
            "interaction": "pull",
            "sustained_seconds": 2.4,
            "viewer": {
                "camera": [0.2, 1.3, 2.1],
                "look": [0.1, -0.2, -0.9],
                "hand": [0.45, 1.2, -0.95],
                "hand_active": True,
                "hand_holding": "她",
                "hand_contact_zone": "waist",
                "hand_contact_kind": "pull",
                "hand_contact_speed": 0.7,
                "hand_contact_seconds": 2.4,
            },
            "self_state": {
                "position": [0.4, 0, -0.6],
                "moving": False,
                "still_seconds": 0.2,
                "target": None,
                "at_target": False,
            },
            "objects": [],
            "activity_label": "她在房间里发呆",
            "mood": "平静",
        },
    )
    assert response.status_code == 200, response.text
    assert "往自己身边拉" in captured["user"]
    assert "持续接触约 2.4 秒" in captured["user"]
    assert "腰" in captured["user"]
    # world_prompt 也让她知道手正抓着她的腰。
    assert "抓着你" in captured["user"]


def test_direct_space_chat_uses_chat_only_prompt_and_recent_context(client, monkeypatch):
    friend_id = _create(client)
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as session:
        session.add(
            Message(
                id="msg-space-direct-context",
                friend_id=friend_id,
                role="user",
                content="我们刚才聊到王阳明的静坐，你答应今晚陪我试十分钟。",
                created_at=now,
                updated_at=now,
            )
        )
        space_service.memory_service.save(
            session,
            space_service.Actor.AGENT,
            friend_id,
            content="梦汐今晚想试十分钟静坐，不要催他，只要在旁边看着。",
            tag="normal",
            layer="long_term",
            context="小世界测试",
            importance=0.7,
        )

    captured: dict[str, str] = {}

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        captured["system"] = system
        captured["user"] = user
        return {
            "thought": "他是在问今晚要不要真的试，我得把刚才答应的十分钟接住。",
            "space_say": {
                "text": "你刚才自己提的十分钟，我还记着。不用坐得像样，先坐下来就行。",
            },
            "space_memory": "",
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    response = client.post(
        f"/api/friends/{friend_id}/space/think",
        json={"text": "那今晚这个静坐，你觉得我能试吗？"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "llm"
    assert "十分钟" in body["line"]
    assert body["action"]["kind"] == "wait"
    assert body["runtime"]["intent"]["thought"]
    assert "小世界工具箱" not in captured["user"]
    assert "space_do" not in captured["system"]
    assert "王阳明的静坐" in captured["user"]
    assert "十分钟静坐" in captured["user"]
    space = client.get(f"/api/friends/{friend_id}/space").json()
    saved = [
        event
        for event in space["events"]
        if event["kind"] == "say" and event["detail"].get("speaker") in {"user", "assistant"}
    ]
    assert any(
        event["detail"].get("speaker") == "user"
        and "静坐" in str(event["detail"].get("text") or "")
        for event in saved
    )
    assert any(
        event["detail"].get("speaker") == "assistant"
        and "十分钟" in str(event["detail"].get("text") or "")
        for event in saved
    )


def test_free_deliberation_suppresses_repeated_spontaneous_speech(client, monkeypatch):
    friend_id = _create(client)
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as session:
        session.add(
            SpaceEvent(
                id="evt-recent-space-say",
                friend_id=friend_id,
                kind="say",
                target=None,
                detail=json.dumps(
                    {
                        "speaker": "assistant",
                        "text": "我坐这儿了。",
                        "emotion": "calm",
                        "proactive": True,
                    },
                    ensure_ascii=False,
                ),
                notable=1,
                created_at=now,
            )
        )

    captured: dict[str, str] = {}

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        captured["user"] = user
        return {
            "thought": "我还坐在这儿，先不催他。",
            "space_do": {"kind": "wait", "target": None, "duration_seconds": 8},
            "space_say": {"text": "我坐这儿了。", "emotion": "calm"},
            "next_think_in": 10,
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    response = client.post(
        f"/api/friends/{friend_id}/space/deliberate",
        json={
            "viewer": {
                "camera": [0.2, 1.3, 2.1],
                "look": [0.1, -0.2, -0.9],
                "hand": [0.45, 1.2, -0.95],
                "hand_active": False,
                "hand_holding": None,
            },
            "self_state": {
                "position": [0.0, 0.0, 0.0],
                "moving": False,
                "still_seconds": 2.0,
                "target": None,
                "at_target": False,
            },
            "objects": [],
            "paused": False,
            "personality_curiosity": 0.5,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["line"] is None
    assert body["runtime"]["intent"]["thought"] == "我还坐在这儿，先不催他。"
    assert "你最近已经说过的话" in captured["user"]
    assert "我坐这儿了" in captured["user"]


def test_space_dialogue_stream_parser_handles_split_tags():
    parser = space_service.SpaceDialogueStreamParser()
    events = []
    for chunk in [
        "<thou",
        "ght>我还记着刚才的十分钟</thought><emo",
        "tion>calm</emotion><speak>你刚才说的那个",
        "办法，我还记着。</speak><memory></memory>",
    ]:
        events.extend(parser.feed(chunk))
    events.extend(parser.finish())
    assert parser.fields["thought"] == "我还记着刚才的十分钟"
    assert parser.fields["emotion"] == "calm"
    assert parser.fields["speak"] == "你刚才说的那个办法，我还记着。"
    assert any(kind == "thought" for kind, _ in events)
    assert any(kind == "speak" for kind, _ in events)


def test_space_chat_stream_endpoint_streams_thought_and_speech(client, monkeypatch):
    friend_id = _create(client)

    class FakeProvider:
        async def chat_stream(self, messages, **kwargs):
            for chunk in [
                "<thought>他是在接着刚才的话题问</thought>",
                "<emotion>calm</emotion><speak>我记得，",
                "你还说过今晚要试十分钟。</speak>",
                "<memory></memory>",
            ]:
                yield {"type": "content", "delta": chunk}

    monkeypatch.setattr(
        space_service, "_provider_for_space", lambda *args, **kwargs: FakeProvider()
    )
    response = client.post(
        f"/api/friends/{friend_id}/space/chat/stream",
        json={"text": "你还记得我们刚才说的事吗？"},
    )
    assert response.status_code == 200, response.text
    payloads = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert any(item.get("type") == "thought" for item in payloads)
    assert any(item.get("type") == "speak" for item in payloads)
    done = next(item for item in payloads if item.get("type") == "done")
    assert done["line"] == "我记得，你还说过今晚要试十分钟。"
    assert done["thought"] == "他是在接着刚才的话题问"
    assert done["action"]["kind"] == "wait"
    assert done["runtime"]["intent"]["thought"] == "他是在接着刚才的话题问"


def test_space_memory_is_written_only_when_model_marks_it(client, monkeypatch):
    friend_id = _create(client)
    replies = [
        {"line": "嗯，我在听。", "space_memory": ""},
        {
            "line": "我记住了，你喜欢窗边那盏灯。",
            "space_memory": "TA 说喜欢窗边那盏灯，我想以后把它留给 TA。",
        },
    ]

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        return replies.pop(0)

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    for text in ("随便聊一句", "你要记住我喜欢窗边那盏灯"):
        response = client.post(
            f"/api/friends/{friend_id}/space/think",
            json={"text": text},
        )
        assert response.status_code == 200, response.text

    with db.session_scope() as session:
        memories = list(
            session.scalars(select(Memory).where(Memory.friend_id == friend_id)).all()
        )
    assert len(memories) == 1
    assert "窗边那盏灯" in memories[0].content


def test_awaiting_reply_expires_and_resumes_autonomous_thinking(client):
    friend_id = _create(client)
    with db.session_scope() as session:
        runtime = space_service.get_or_create_runtime(session, friend_id)
        runtime.intent = json.dumps(
            {
                "kind": "awaiting_reply",
                "line": "你刚才在看什么？",
                "emotion": "curious",
                "since": "2000-01-01T00:00:00+00:00",
            },
            ensure_ascii=False,
        )
        runtime.next_deliberation_at = "2000-01-01T00:00:00+00:00"

    body = client.post(
        f"/api/friends/{friend_id}/space/perceive",
        json={
            "viewer": {
                "camera": [0.2, 1.3, 2.1],
                "look": [0.1, -0.2, -0.9],
                "hand": [0.45, 0.35, -0.95],
                "hand_active": False,
                "hand_holding": None,
            },
            "self_state": {
                "position": [0, 0, 0],
                "moving": False,
                "still_seconds": 2.0,
                "target": None,
                "at_target": False,
            },
            "objects": [],
            "paused": False,
            "personality_curiosity": 0.5,
        },
    ).json()
    assert body["awaiting_reply"] is False
    assert body["deliberation_due"] is True
    assert body["runtime"]["intent"]["kind"] != "awaiting_reply"


def test_prompt_note_does_not_leak_plain_space_chatter_into_main_chat(client):
    friend_id = _create(client)
    client.post(
        f"/api/friends/{friend_id}/space/events",
        json={
            "kind": "say",
            "detail": {
                "text": "你刚才在房间里看到了什么？",
                "reply": "我看到窗边的光落在靠垫上。",
            },
        },
    )
    with db.session_scope() as session:
        runtime = space_service.get_or_create_runtime(session, friend_id)
        runtime.goal = "把窗边整理成看书的位置"
        runtime.plan = json.dumps(["搬靠垫", "坐下"], ensure_ascii=False)
        runtime.plan_step = 0
        runtime.last_action_result = json.dumps(
            {
                "action_id": "act-dialogue",
                "status": "finished",
                "ok": True,
                "detail": "她把靠垫搬到了窗边",
                "at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        )
        note = space_service.prompt_note(session, friend_id)
    # 普通小世界对话留在 space_chat；主聊天 note 只保留目标/动作。
    assert "你刚才在房间里看到了什么" not in note
    assert "窗边的光落在靠垫上" not in note
    assert "把窗边整理成看书的位置" in note
    assert "把靠垫搬到了窗边" in note


def test_space_never_auto_asks_or_falls_back_to_template(client):
    friend_id = _create(client)
    payload = {
        "viewer": {
            "camera": [0.2, 1.3, 2.1],
            "look": [0.1, -0.2, -0.9],
            "hand": [0.45, 0.35, -0.95],
            "hand_active": False,
            "hand_holding": None,
        },
        "objects": [
            {
                "key": "mug",
                "label": "杯子",
                "position": [0.5, 0.12, -1.0],
                "moving": False,
            }
        ],
        "personality_curiosity": 1.0,
    }
    for _ in range(12):
        body = client.post(
            f"/api/friends/{friend_id}/space/perceive", json=payload
        ).json()
        assert body["ask_intent"] is None
        assert body["line"] is None
        assert body["runtime"]["intent"]["kind"] != "ask"

    with db.session_scope() as session:
        space_service.upsert_knowledge(session, friend_id, "mug", "杯子")
        result = asyncio.run(
            space_service.ask_about_object(
                session,
                friend_id,
                object_key="mug",
                viewer=None,
                allow_llm=False,
            )
        )
    assert result["source"] == "local"
    assert result["line"] == ""


def test_space_feature_off_blocks_routes(client):
    friend_id = _create(client)
    assert client.patch("/api/features/space", json={"enabled": False}).status_code == 200
    blocked = client.get(f"/api/friends/{friend_id}/space")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "FEATURE_DISABLED"


def test_delete_friend_cascades_space_rows(client):
    friend_id = _create(client)
    client.post(
        f"/api/friends/{friend_id}/space/events",
        json={"kind": "enter"},
    )
    with db.session_scope() as session:
        space_service.get_or_create_runtime(session, friend_id)
        assert session.get(FriendSpace, friend_id) is not None
        assert session.get(FriendSpaceRuntime, friend_id) is not None
        space_service.upsert_knowledge(
            session, friend_id, "mug", "杯子", status="understood", user_explanation="喝水"
        )
        assert session.scalar(
            select(func.count())
            .select_from(SpaceObjectKnowledge)
            .where(SpaceObjectKnowledge.friend_id == friend_id)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(SpaceEvent).where(SpaceEvent.friend_id == friend_id)
        ) == 1
        session.execute(text("DELETE FROM friends WHERE id = :id"), {"id": friend_id})
        session.commit()
        session.expire_all()
        assert session.get(FriendSpace, friend_id) is None
        assert session.get(FriendSpaceRuntime, friend_id) is None
        assert session.scalar(
            select(func.count())
            .select_from(SpaceObjectKnowledge)
            .where(SpaceObjectKnowledge.friend_id == friend_id)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(SpaceEvent).where(SpaceEvent.friend_id == friend_id)
        ) == 0


def test_surface_registry_distinguishes_floor_seats_and_tables():
    world = {
        "environment": "room",
        "objects": [
            {"id": "chair-1", "asset": "chair", "position": [-1.0, 0, 0.2], "scale": 1.0},
            {"id": "bed-1", "asset": "bed", "position": [1.0, 0, -0.5], "scale": 1.0},
            {"id": "table-1", "asset": "table", "position": [0.0, 0, 1.0], "scale": 1.0},
            {"id": "cushion-1", "asset": "cushion", "position": [0.4, 0, -0.3], "scale": 1.0},
        ],
    }
    surfaces, rugs = space_world_service._surfaces_from_world(world)
    by_key = {surface["key"]: surface for surface in surfaces}

    assert by_key["floor"]["walkable"] is True
    assert by_key["floor"]["sittable"] is True
    assert by_key["floor"]["lieable"] is True
    assert by_key["chair-1"]["sittable"] is True
    assert by_key["chair-1"]["lieable"] is False
    assert by_key["bed-1"]["lieable"] is True
    assert by_key["cushion-1"]["walkable"] is True
    assert by_key["table-1"]["sittable"] is False
    assert rugs == []

    assert (
        space_world_service._support_for([-1.0, 0.52, 0.2], surfaces, rugs)
        == "椅子面"
    )
    assert (
        space_world_service._support_for([1.0, 0.51, -0.5], surfaces, rugs)
        == "床面"
    )
    assert space_world_service._support_for([3.0, 0.0, 3.0], surfaces, rugs) == "地板"


def test_sit_and_lie_actions_accept_floor_or_real_surface_keys():
    lying = space_service._normalize_action({"kind": "lie"})
    assert lying is not None
    assert lying["kind"] == "lie"
    assert lying["target"] == "floor"

    sitting = space_service._normalize_action(
        {"kind": "sit", "target": "chair-1", "duration_seconds": 8}
    )
    assert sitting is not None
    assert sitting["target"] == "chair-1"

    invalid = space_service._normalize_action({"kind": "sleep", "target": "chair-1"})
    assert invalid is None


def test_space_llm_json_honors_deep_thinking_and_reasoning_budget(client, monkeypatch):
    friend_id = _create(client)
    captured: dict[str, object] = {}

    class FakeProvider:
        config = ProviderConfig(
            name="fake-chat",
            base_url="http://fake",
            api_key="",
            model="fake",
            capabilities=ProviderCapabilities(reasoning=True),
        )

        async def chat_stream(self, messages, **kwargs):
            captured.update(kwargs)
            captured["messages"] = messages
            yield {"type": "content", "delta": '{"thought":"我真的在判断"}'}

    monkeypatch.setattr(
        space_service, "_provider_for_space", lambda *_args, **_kwargs: FakeProvider()
    )
    with db.session_scope() as session:
        friend = session.get(Friend, friend_id)
        assert friend is not None
        settings = settings_service.get(session)
        settings.deep_thinking = 1
        session.commit()
        result = asyncio.run(
            space_service._llm_json(
                session,
                friend,
                "system",
                "user",
                purpose="chat",
                max_tokens=200,
            )
        )

    assert result == {"thought": "我真的在判断"}
    assert captured["reasoning_effort"] == "high"
    assert captured["max_tokens"] == 900


def test_builtin_cushion_is_a_real_seat_and_use_gets_corrected_to_sit():
    world = {"environment": "room", "objects": []}
    layout = {"cushion": [-0.95, 0.16, 0.15]}
    surfaces, _ = space_world_service._surfaces_from_world(world, layout)
    by_key = {surface["key"]: surface for surface in surfaces}

    assert by_key["cushion"]["sittable"] is True
    assert by_key["cushion"]["top"] > 0.2

    world_model = {"surfaces": surfaces}
    corrected = space_service._coerce_posture_action(
        {"kind": "use", "target": "cushion", "duration_seconds": 8},
        world_model,
        "我坐好了，靠垫就在背后。",
    )
    assert corrected is not None
    assert corrected["kind"] == "sit"
    assert corrected["target"] == "cushion"

    not_changed = space_service._coerce_posture_action(
        {"kind": "arrange", "target": "cushion", "duration_seconds": 5},
        world_model,
        "我把靠垫拍松一点。",
    )
    assert not_changed is not None
    assert not_changed["kind"] == "arrange"


def test_room_dialogue_allows_multi_sentence_conversation(client, monkeypatch):
    friend_id = _create(client)
    long_line = (
        "你刚才说想让我自己过日子，我听见了，也认真想了。"
        "我不是只会坐在这儿等你问一句答一句，我也会觉得窗边太亮、靠垫太软，"
        "会想挪一挪，想叫你过来一起坐。"
        "你问我小世界第一眼看见什么，我先看见的是你留出来的那半张靠垫。"
        "然后才是书架、灯、还有你刚才碰过的那只杯子，顺序我都记得。"
        "所以你别把这次当成我回你一个问题，我是在跟你把日子接着往下过。"
    )
    assert len(long_line) > 140

    async def fake_llm(
        session, friend, system, user, *, max_tokens=220, purpose="game_fast"
    ):
        return {
            "thought": "他在跟我说话，不是只问一个功能问题；我要把自己的想法也说出来。",
            "space_say": {"text": long_line, "emotion": "calm"},
            "space_do": {"kind": "idle", "target": None, "duration_seconds": 3},
        }

    monkeypatch.setattr(space_service, "_llm_json", fake_llm)
    response = client.post(
        f"/api/friends/{friend_id}/space/think",
        json={"text": "你在这里待着的时候，自己会想些什么？"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["line"] == long_line
    assert body["source"] == "llm"
