"""T1.4 验收：朋友 CRUD（api.md §1）。

每个测试用独立的临时数据库 + 真实跑一次 Alembic 迁移。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.app import create_app
from backend.models import Message


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    app = create_app()  # 会跑 upgrade head 到临时库
    with TestClient(app) as c:
        yield c


def _create(client, name="小敏") -> dict:
    r = client.post(
        "/api/friends",
        json={"name": name, "personality": "温柔、耐心", "custom_instructions": "爱用叠词"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------- v4.3（D249）：每个朋友自己的"温度" ----------


def test_temperature_defaults_to_null(client):
    """不传就是 NULL —— 跟随默认（老行为一个字不变）。"""
    created = _create(client)
    assert created["temperature"] is None


def test_temperature_can_be_set_on_create_and_read_back(client):
    r = client.post(
        "/api/friends",
        json={"name": "Rick", "personality": "疯", "temperature": 1.1},
    )
    assert r.status_code == 201, r.text
    assert r.json()["temperature"] == 1.1
    assert client.get(f"/api/friends/{r.json()['id']}").json()["temperature"] == 1.1


def test_temperature_update_patch_and_reset(client):
    created = _create(client)
    fid = created["id"]

    # 设一个
    r = client.patch(f"/api/friends/{fid}", json={"temperature": 1.25})
    assert r.status_code == 200, r.text
    assert r.json()["temperature"] == 1.25

    # **不传这个字段** = 这次不改（别的字段照改）
    r = client.patch(f"/api/friends/{fid}", json={"name": "小敏2"})
    assert r.status_code == 200, r.text
    assert r.json()["temperature"] == 1.25
    assert r.json()["name"] == "小敏2"

    # **显式传 null** = 恢复默认
    r = client.patch(f"/api/friends/{fid}", json={"temperature": None})
    assert r.status_code == 200, r.text
    assert r.json()["temperature"] is None


def test_temperature_out_of_range_is_rejected(client):
    """超出 0.0~1.5 直接 422，别让它悄悄落库。"""
    created = _create(client)
    assert client.patch(f"/api/friends/{created['id']}", json={"temperature": 3.0}).status_code == 422
    assert client.patch(f"/api/friends/{created['id']}", json={"temperature": -0.5}).status_code == 422


def test_temperature_for_falls_back_to_global_default():
    """没设 → 0.7（全局默认）；设了 → 用她的（D249）。"""
    from backend.agent.context import TEMPERATURE_DEFAULT, temperature_for
    from backend.models import Friend

    assert temperature_for(Friend(id="x", name="n", temperature=None)) == TEMPERATURE_DEFAULT
    assert temperature_for(Friend(id="x", name="n", temperature=1.1)) == 1.1


def test_clamp_temperature_bounds():
    """夹进 [0.0, 1.5]；坏值当"跟随默认"。"""
    from backend.agent.context import (
        TEMPERATURE_MAX,
        TEMPERATURE_MIN,
        clamp_temperature,
    )

    assert clamp_temperature(None) is None
    assert clamp_temperature("1.0") == 1.0
    assert clamp_temperature(9.0) == TEMPERATURE_MAX
    assert clamp_temperature(-3) == TEMPERATURE_MIN
    assert clamp_temperature("nope") is None


def test_create_then_list(client):
    created = _create(client)
    assert created["name"] == "小敏"
    assert created["presence"] is True
    assert created["current_mode"] == "normal"
    assert created["unread_count"] == 0

    friends = client.get("/api/friends").json()["friends"]
    assert len(friends) == 1
    assert friends[0]["id"] == created["id"]
    # 列表项必须带前端要的三个派生字段（api.md §1.4 / D30）
    assert "last_message_preview" in friends[0]
    assert "last_message_at" in friends[0]
    assert friends[0]["has_unfinished"] is False


def test_detail_contains_personality(client):
    created = _create(client)
    detail = client.get(f"/api/friends/{created['id']}").json()
    assert detail["personality"] == "温柔、耐心"
    assert detail["custom_instructions"] == "爱用叠词"


def test_update_name(client):
    created = _create(client)
    r = client.patch(f"/api/friends/{created['id']}", json={"name": "小敏敏"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "小敏敏"
    assert client.get(f"/api/friends/{created['id']}").json()["name"] == "小敏敏"


def test_presence_toggle(client):
    created = _create(client)
    fid = created["id"]
    assert client.patch(f"/api/friends/{fid}/presence", json={"presence": False}).json()["presence"] is False
    assert client.patch(f"/api/friends/{fid}/presence", json={"presence": True}).json()["presence"] is True


def test_mode_switch_rules(client):
    """api.md §1.2：切到非 roleplay 清 role；role=null 清角色留在 roleplay。"""
    fid = _create(client)["id"]

    r = client.patch(f"/api/friends/{fid}/mode", json={"mode": "roleplay", "role": "女朋友"})
    assert r.json() == {"friend_id": fid, "current_mode": "roleplay", "current_role": "女朋友"}

    r = client.patch(f"/api/friends/{fid}/mode", json={"mode": "roleplay", "role": None})
    assert r.json()["current_role"] is None
    assert r.json()["current_mode"] == "roleplay"

    r = client.patch(f"/api/friends/{fid}/mode", json={"mode": "roleplay", "role": "学姐"})
    assert r.json()["current_role"] == "学姐"

    r = client.patch(f"/api/friends/{fid}/mode", json={"mode": "normal"})
    assert r.json()["current_mode"] == "normal"
    assert r.json()["current_role"] is None, "切到非 roleplay 必须清空角色"

    assert client.patch(f"/api/friends/{fid}/mode", json={"mode": "bad"}).status_code == 422


def test_soft_delete_restore_and_visibility(client):
    fid = _create(client)["id"]
    assert client.delete(f"/api/friends/{fid}").status_code == 204

    assert client.get("/api/friends").json()["friends"] == []
    assert len(client.get("/api/friends?include_deleted=true").json()["friends"]) == 1
    assert len(client.get("/api/friends/recoverable").json()["recoverable"]) == 1
    # 已删除的朋友不能再看详情
    assert client.get(f"/api/friends/{fid}").status_code == 404

    assert client.post(f"/api/friends/{fid}/restore").status_code == 200
    assert len(client.get("/api/friends").json()["friends"]) == 1


def test_purge_cascades_children(client):
    """彻底删除必须不留 orphan 数据（宪法§9）。"""
    fid = _create(client)["id"]
    with db.session_scope() as s:
        s.add(
            Message(
                id=str(uuid.uuid4()),
                friend_id=fid,
                role="user",
                content="你好",
                created_at="2026-09-18T00:00:00+00:00",
            )
        )
    with db.session_scope() as s:
        assert s.query(Message).count() == 1

    assert client.post(f"/api/friends/{fid}/purge").status_code == 204
    with db.session_scope() as s:
        assert s.query(Message).count() == 0, "物理删除后 messages 有孤儿数据"


def test_mark_read(client):
    fid = _create(client)["id"]
    with db.session_scope() as s:
        from backend.models import Friend

        s.get(Friend, fid).unread_count = 3
    assert client.get(f"/api/friends/{fid}").json()["unread_count"] == 3
    assert client.post(f"/api/friends/{fid}/read").status_code == 204
    assert client.get(f"/api/friends/{fid}").json()["unread_count"] == 0


def test_not_found(client):
    r = client.get("/api/friends/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "FRIEND_NOT_FOUND"


def test_visual_mode_model_and_portrait_pack_fields(client):
    created = _create(client)
    fid = created["id"]
    assert created["visual_mode"] == "live2d"
    assert created["live2d_model_id"] is None
    assert created["portrait_pack_id"] is None

    r = client.patch(
        f"/api/friends/{fid}",
        json={
            "visual_mode": "portrait",
            "live2d_model_id": "model-1",
            "portrait_pack_id": "pack-1",
        },
    )
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["visual_mode"] == "portrait"
    assert detail["live2d_model_id"] == "model-1"
    assert detail["portrait_pack_id"] == "pack-1"
