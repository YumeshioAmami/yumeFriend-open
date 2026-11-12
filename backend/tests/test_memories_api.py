"""T3.5 验收：记忆 API（api.md §3）。"""

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "mem_api.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    app = create_app()
    with TestClient(app) as c:
        yield c


def _friend(client, name="阿月") -> str:
    return client.post("/api/friends", json={"name": name}).json()["id"]


def _add(client, fid, content, **over) -> tuple[int, dict]:
    body = {"friend_id": fid, "content": content, **over}
    r = client.post("/api/memories", json=body)
    return r.status_code, r.json()


# ---------- 新增 ----------


def test_create_returns_201_and_shows_up_in_list(client):
    fid = _friend(client)
    status, body = _add(client, fid, "用户生日是 5 月 20 日", layer="pinned", tag="notebook")
    assert status == 201
    assert body["merged"] is False

    listing = client.get(f"/api/friends/{fid}/memories").json()
    assert listing["total"] == 1
    item = listing["memories"][0]
    assert item["content"] == "用户生日是 5 月 20 日"
    assert item["layer"] == "pinned"
    assert item["tag"] == "notebook"
    assert item["refreshed_at"] and item["created_at"]
    assert item["id"] == body["id"]


def test_second_identical_create_merges_with_200(client):
    """api.md §3.2：命中写入去重 → 200 + merged:true + 返回已有那条的 id。"""
    fid = _friend(client)
    _, first = _add(client, fid, "用户不吃香菜")
    status, second = _add(client, fid, "用户不吃香菜")
    assert status == 200
    assert second["merged"] is True
    assert second["id"] == first["id"]
    assert client.get(f"/api/friends/{fid}/memories").json()["total"] == 1


def test_create_defaults_to_pinned_notebook(client):
    fid = _friend(client)
    _, body = _add(client, fid, "随手写的")
    item = client.get(f"/api/friends/{fid}/memories").json()["memories"][0]
    assert item["layer"] == "pinned"
    assert item["tag"] == "notebook"


def test_create_validates_layer_and_friend(client):
    fid = _friend(client)
    assert client.post(
        "/api/memories", json={"friend_id": fid, "content": "x", "layer": "超级重要"}
    ).status_code == 422
    assert client.post(
        "/api/memories", json={"friend_id": "nope", "content": "x"}
    ).status_code == 404
    assert client.post(
        "/api/memories", json={"friend_id": fid, "content": "   "}
    ).status_code == 422


# ---------- 列表筛选 ----------


def test_list_filters_by_layer_tag_and_search(client):
    fid = _friend(client)
    _add(client, fid, "用户的猫叫团子", layer="pinned")
    _add(client, fid, "用户不吃香菜", layer="long_term")
    _add(client, fid, "想吃火锅", layer="long_term", tag="rp")

    assert client.get(f"/api/friends/{fid}/memories").json()["total"] == 3
    long_only = client.get(f"/api/friends/{fid}/memories?layer=long_term").json()
    assert long_only["total"] == 2
    rp_only = client.get(f"/api/friends/{fid}/memories?tag=rp").json()
    assert rp_only["total"] == 1 and rp_only["memories"][0]["content"] == "想吃火锅"
    found = client.get(f"/api/friends/{fid}/memories?search=香菜").json()
    assert found["total"] == 1 and found["memories"][0]["content"] == "用户不吃香菜"


def test_list_pagination(client):
    fid = _friend(client)
    for i in range(5):
        _add(client, fid, f"第 {i} 条记忆")
    page = client.get(f"/api/friends/{fid}/memories?limit=2&offset=0").json()
    assert len(page["memories"]) == 2 and page["total"] == 5


def test_list_returns_layer_counts(client):
    fid = _friend(client)
    _add(client, fid, "a", layer="pinned")
    _add(client, fid, "b", layer="pinned")
    _add(client, fid, "c", layer="long_term")
    counts = client.get(f"/api/friends/{fid}/memories").json()["layer_counts"]
    assert counts == {"pinned": 2, "permanent": 0, "long_term": 1, "short_term": 0}


def test_list_sort_options(client):
    """几百条记忆时，排序是刚需：最近提起 / 重要度 / 写入时间。"""
    fid = _friend(client)
    _, low = _add(client, fid, "不太重要的", importance=0.2)
    _, high = _add(client, fid, "很重要的", importance=0.9)

    by_importance = client.get(f"/api/friends/{fid}/memories?sort=importance").json()
    assert [m["id"] for m in by_importance["memories"]] == [high["id"], low["id"]]

    by_created = client.get(f"/api/friends/{fid}/memories?sort=created").json()
    assert [m["id"] for m in by_created["memories"]] == [high["id"], low["id"]]  # 新的在前

    assert client.get(f"/api/friends/{fid}/memories?sort=胡说").status_code == 422


def test_search_and_layer_filter_combine(client):
    fid = _friend(client)
    _add(client, fid, "用户在学 Rust", layer="long_term")
    _add(client, fid, "用户的猫叫团子", layer="pinned")
    _add(client, fid, "用户在学吉他", layer="long_term")
    body = client.get(f"/api/friends/{fid}/memories?layer=long_term&search=学").json()
    assert body["total"] == 2
    assert all("学" in m["content"] for m in body["memories"])


def test_memories_are_isolated_between_friends(client):
    a = _friend(client, "阿月")
    b = _friend(client, "老王")
    _add(client, a, "阿月的记忆")
    assert client.get(f"/api/friends/{b}/memories").json()["total"] == 0
    assert client.get(f"/api/friends/{a}/memories").json()["total"] == 1


def test_list_unknown_friend_404(client):
    r = client.get("/api/friends/nope/memories")
    assert r.status_code == 404 and r.json()["error"]["code"] == "FRIEND_NOT_FOUND"


# ---------- 改 / 删 ----------


def test_patch_content_and_importance(client):
    fid = _friend(client)
    _, body = _add(client, fid, "用户喜欢咖啡", importance=0.4)
    r = client.patch(f"/api/memories/{body['id']}", json={"content": "用户喜欢喝茶", "importance": 0.8})
    assert r.status_code == 200
    assert r.json()["content"] == "用户喜欢喝茶"
    assert r.json()["importance"] == 0.8


def test_patch_can_change_layer_but_not_tag(client):
    """D230：`layer` 可以改（升 / 降级）；`tag` 仍不可改（多传的 tag 直接被忽略）。"""
    fid = _friend(client)
    _, body = _add(client, fid, "x", layer="long_term", tag="normal")
    r = client.patch(f"/api/memories/{body['id']}", json={"layer": "pinned", "tag": "rp"})
    assert r.status_code == 200
    assert r.json()["layer"] == "pinned" and r.json()["tag"] == "normal"


def test_memory_events_endpoint(client):
    """D230：记忆改动留痕能查（用户 / 朋友 / 系统各带 actor）。"""
    fid = _friend(client)
    _, body = _add(client, fid, "x", layer="long_term")
    client.patch(f"/api/memories/{body['id']}", json={"layer": "pinned"})
    data = client.get(f"/api/friends/{fid}/memory-events").json()
    actions = [event["action"] for event in data["events"]]
    assert "create" in actions and "update" in actions


def test_delete_is_soft_and_hides_from_list(client):
    fid = _friend(client)
    _, body = _add(client, fid, "要删掉的")
    assert client.delete(f"/api/memories/{body['id']}").status_code == 204
    assert client.get(f"/api/friends/{fid}/memories").json()["total"] == 0
    assert client.delete(f"/api/memories/{body['id']}").status_code == 404
    assert client.patch(f"/api/memories/{body['id']}", json={"content": "改不动"}).status_code == 404
