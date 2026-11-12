"""D347：说话方式档案的查看 / 手改 / 回滚接口。"""

from __future__ import annotations

import pytest

import backend.config as config
import backend.db as db


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.app import create_app

    db_file = tmp_path / "style-api.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    app = create_app()
    with TestClient(app) as c:
        yield c


def _friend(client, name: str = "汐音") -> str:
    return client.post("/api/friends", json={"name": name}).json()["id"]


def test_style_api_crud_and_events(client):
    fid = _friend(client)
    initial = client.get(f"/api/friends/{fid}/style")
    assert initial.status_code == 200
    body = initial.json()
    assert body["version"] == 0
    assert body["labels"]["length"] == "长度"
    assert body["limits"]["length"] == 3

    added = client.post(
        f"/api/friends/{fid}/style/changes",
        json={
            "changes": [
                {"action": "add", "section": "length", "lesson": "日常一两句就停"}
            ],
            "summary": "手动加一条",
        },
    )
    assert added.status_code == 200
    line = added.json()["profile"]["sections"]["length"][0]
    assert line["text"] == "日常一两句就停"

    updated = client.post(
        f"/api/friends/{fid}/style/changes",
        json={
            "changes": [
                {
                    "action": "update",
                    "section": "length",
                    "target_id": line["id"],
                    "lesson": "日常多数一两句，明确要展开再长",
                }
            ]
        },
    )
    assert updated.status_code == 200
    assert updated.json()["profile"]["sections"]["length"][0]["text"] == "日常多数一两句，明确要展开再长"

    events = client.get(f"/api/friends/{fid}/style/events").json()["events"]
    assert len(events) == 2
    assert events[0]["event_type"] == "manual"
    assert events[0]["after_version"] == 2

    rolled = client.post(
        f"/api/friends/{fid}/style/rollback",
        json={"event_id": events[0]["id"]},
    )
    assert rolled.status_code == 200
    assert rolled.json()["profile"]["sections"]["length"][0]["text"] == "日常一两句就停"

    removed = client.post(
        f"/api/friends/{fid}/style/changes",
        json={
            "changes": [
                {
                    "action": "remove",
                    "section": "length",
                    "target_id": line["id"],
                }
            ]
        },
    )
    assert removed.status_code == 200
    assert removed.json()["profile"]["sections"]["length"] == []


def test_style_api_reset_and_unknown_friend(client):
    fid = _friend(client)
    client.post(
        f"/api/friends/{fid}/style/changes",
        json={"changes": [{"action": "add", "section": "tone", "lesson": "语气软一点"}]},
    )
    reset = client.delete(f"/api/friends/{fid}/style")
    assert reset.status_code == 200
    assert reset.json()["ok"] is True
    assert client.get(f"/api/friends/{fid}/style").json()["profile"]["sections"]["tone"] == []

    assert client.get("/api/friends/nope/style").status_code == 404
