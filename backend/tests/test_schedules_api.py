"""日程 API 验收（api.md【v4.0】/ D243、D244）。"""

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "schedule_api.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    app = create_app()
    with TestClient(app) as c:
        yield c


def _friend(client, name="汐音") -> str:
    return client.post("/api/friends", json={"name": name}).json()["id"]


def _add(client, fid, title="买武忠祥", date="2026-09-24", **over):
    body = {"friend_id": fid, "title": title, "date": date, **over}
    r = client.post("/api/schedules", json=body)
    return r.status_code, r.json()


def test_create_then_list(client):
    fid = _friend(client)
    status, body = _add(client, fid, time="15:30", owner="friend")
    assert status == 201
    assert body["title"] == "买武忠祥" and body["date"] == "2026-09-24"
    assert body["time"] == "15:30" and body["state"] == "pending"
    assert body["owner"] == "friend" and body["source"] == "ui"

    listing = client.get(f"/api/schedules?friend_id={fid}").json()["schedules"]
    assert [s["id"] for s in listing] == [body["id"]]


def test_create_for_unknown_friend_404(client):
    r = client.post(
        "/api/schedules", json={"friend_id": "nope", "title": "x", "date": "2026-09-24"}
    )
    assert r.status_code == 404


def test_bad_date_is_422(client):
    fid = _friend(client)
    status, body = _add(client, fid, date="下周三")
    assert status == 422 and body["error"]["code"] == "INVALID_INPUT"


def test_patch_can_reschedule_and_settle(client):
    fid = _friend(client)
    _, created = _add(client, fid, time="15:00")

    moved = client.patch(
        f"/api/schedules/{created['id']}", json={"date": "2026-09-25", "time": "10:00"}
    ).json()
    assert moved["date"] == "2026-09-25" and moved["time"] == "10:00"

    settled = client.patch(
        f"/api/schedules/{created['id']}", json={"state": "settled", "settled_reason": "done"}
    ).json()
    assert settled["state"] == "settled" and settled["settled_reason"] == "done"


def test_patch_refuses_to_write_active(client):
    """active / archived 归扫描器 —— 界面硬写要被挡下（422）。"""
    fid = _friend(client)
    _, created = _add(client, fid)
    r = client.patch(f"/api/schedules/{created['id']}", json={"state": "active"})
    assert r.status_code == 422


def test_patch_bad_settled_reason_422(client):
    fid = _friend(client)
    _, created = _add(client, fid)
    r = client.patch(
        f"/api/schedules/{created['id']}", json={"state": "settled", "settled_reason": "也许吧"}
    )
    assert r.status_code == 422


def test_delete_soft_deletes(client):
    fid = _friend(client)
    _, created = _add(client, fid)
    assert client.delete(f"/api/schedules/{created['id']}").status_code == 204
    assert client.get(f"/api/schedules?friend_id={fid}").json()["schedules"] == []
    # include_archived 也看不到了（软删的读不出来）
    assert client.get(f"/api/schedules?friend_id={fid}&include_archived=true").json()[
        "schedules"
    ] == []


def test_schedule_events_are_recorded(client):
    """留痕接口（她 / 用户动过这条的痕迹）。"""
    fid = _friend(client)
    _, created = _add(client, fid)
    client.patch(f"/api/schedules/{created['id']}", json={"date": "2026-09-26"})

    events = client.get(f"/api/friends/{fid}/schedule-events").json()["events"]
    actions = [e["action"] for e in events]
    assert "create" in actions and "reschedule" in actions
    assert all(e["actor"] == "user" for e in events)


def test_list_filters_by_owner(client):
    fid = _friend(client)
    _add(client, fid, title="我的事", owner="user")
    _add(client, fid, title="她的事", owner="friend")
    hers = client.get(f"/api/schedules?friend_id={fid}&owner=friend").json()["schedules"]
    assert [s["title"] for s in hers] == ["她的事"]
