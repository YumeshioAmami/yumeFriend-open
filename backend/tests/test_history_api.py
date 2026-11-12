"""T2.5 验收：历史分页（api.md §2.2）。"""

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
    app = create_app()
    with TestClient(app) as c:
        yield c


def _friend(client) -> str:
    return client.post("/api/friends", json={"name": "小敏"}).json()["id"]


def _seed(fid: str, count: int) -> list[str]:
    """造 count 条消息，时间递增，返回 id（老的在前）。"""
    ids = []
    with db.session_scope() as s:
        for i in range(count):
            mid = str(uuid.uuid4())
            ids.append(mid)
            s.add(
                Message(
                    id=mid,
                    friend_id=fid,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"第 {i} 条",
                    stream_status="completed",
                    created_at=f"2026-09-18T10:{i:02d}:00+00:00",
                )
            )
    return ids


def test_empty_history(client):
    body = client.get(f"/api/friends/{_friend(client)}/messages").json()
    assert body == {"messages": [], "has_more": False}


def test_returns_oldest_first_and_has_more(client):
    fid = _friend(client)
    _seed(fid, 5)
    body = client.get(f"/api/friends/{fid}/messages?limit=2").json()
    assert [m["content"] for m in body["messages"]] == ["第 3 条", "第 4 条"]  # 正序
    assert body["has_more"] is True
    assert set(body["messages"][0]) >= {
        "id",
        "role",
        "content",
        "mode_at_send",
        "is_proactive",
        "stream_status",
        "tools_used",
        "created_at",
    }


def test_paging_backwards_with_before(client):
    fid = _friend(client)
    _seed(fid, 5)

    page1 = client.get(f"/api/friends/{fid}/messages?limit=2").json()
    page2 = client.get(
        f"/api/friends/{fid}/messages",
        params={"limit": 2, "before": page1["messages"][0]["created_at"]},
    ).json()
    page3 = client.get(
        f"/api/friends/{fid}/messages",
        params={"limit": 2, "before": page2["messages"][0]["created_at"]},
    ).json()

    assert [m["content"] for m in page2["messages"]] == ["第 1 条", "第 2 条"]
    assert page2["has_more"] is True
    assert [m["content"] for m in page3["messages"]] == ["第 0 条"]
    assert page3["has_more"] is False


def test_limit_is_bounded(client):
    fid = _friend(client)
    assert client.get(f"/api/friends/{fid}/messages?limit=0").status_code == 422
    assert client.get(f"/api/friends/{fid}/messages?limit=999").status_code == 422


def test_other_friends_messages_are_isolated(client):
    """朋友之间消息严格隔离（宪法附录 B）。"""
    a = _friend(client)
    b = client.post("/api/friends", json={"name": "老王"}).json()["id"]
    _seed(a, 3)
    assert client.get(f"/api/friends/{b}/messages").json()["messages"] == []
    assert len(client.get(f"/api/friends/{a}/messages").json()["messages"]) == 3


def test_history_of_unknown_friend(client):
    r = client.get("/api/friends/nope/messages")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "FRIEND_NOT_FOUND"
