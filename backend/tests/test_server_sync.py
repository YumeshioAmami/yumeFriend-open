"""v7.12 / D342：服务器模式增量交换与客户端自动合并。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

import backend.config as config
import backend.db as db
from backend.api import server_sync as server_sync_api
from backend.models import (
    Document,
    DocumentChunk,
    Friend,
    FriendState,
    LifeProject,
    Memory,
    SyncTombstone,
    Message,
    SyncMeta,
)
from backend.services import (
    memory_service,
    message_service,
    server_sync,
    style_profile_service,
    sync_service,
)
from backend.services.actor import Actor


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def sync_db(tmp_path, monkeypatch):
    db_file = tmp_path / "server-sync.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    with db.session_scope() as session:
        sync_service.configure_device(session, name="测试电脑", kind="desktop")
        session.add(
            Friend(
                id="f1",
                name="汐音",
                life_enabled=1,
                created_at=_now(),
                updated_at=_now(),
            )
        )
    return db_file


def test_export_bundle_since_filters_old_rows(sync_db):
    with db.session_scope() as session:
        full = sync_service.export_bundle(session)
        assert {item["id"] for item in full["tables"]["friends"]} == {"f1"}
        empty = sync_service.export_bundle(session, since="2999-01-01T00:00:00+00:00")
        assert empty["tables"]["friends"] == []


def test_server_attachment_upload_download_and_manifest(sync_db, tmp_path, monkeypatch):
    user_data = tmp_path / "user_data"
    (user_data / "avatars").mkdir(parents=True)
    avatar = user_data / "avatars" / "f1.png"
    avatar.write_bytes(b"avatar-bytes")
    manifest_sha = hashlib.sha256(avatar.read_bytes()).hexdigest()
    monkeypatch.setattr(config, "USER_DATA_DIR", user_data)
    monkeypatch.setenv("YF_SERVER_TOKEN", "test-token")
    app = FastAPI()
    app.include_router(server_sync_api.router)
    client = TestClient(app)

    assert client.get("/api/server-sync/attachments").status_code == 403
    manifest = client.get(
        "/api/server-sync/attachments",
        headers={"X-YF-Server-Token": "test-token"},
    )
    assert manifest.status_code == 200, manifest.text
    assert any(
        item["path"] == "avatars/f1.png" and item["sha256"] == manifest_sha
        for item in manifest.json()["attachments"]
    )

    downloaded = client.get(
        f"/api/server-sync/blob/{manifest_sha}",
        headers={"X-YF-Server-Token": "test-token"},
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"avatar-bytes"

    portrait = b"portrait-bytes"
    portrait_sha = hashlib.sha256(portrait).hexdigest()
    uploaded = client.post(
        f"/api/server-sync/blob/{portrait_sha}",
        content=portrait,
        headers={
            "X-YF-Server-Token": "test-token",
            "X-YF-Path": quote("portraits/f1/开心.png", safe=""),
            "Content-Type": "image/png",
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    assert (user_data / "portraits" / "f1" / "开心.png").read_bytes() == portrait


def test_client_sync_attachments_uploads_and_downloads(sync_db, tmp_path, monkeypatch):
    user_data = tmp_path / "user_data"
    (user_data / "avatars").mkdir(parents=True)
    avatar = user_data / "avatars" / "f1.png"
    avatar.write_bytes(b"local-avatar")
    monkeypatch.setattr(config, "USER_DATA_DIR", user_data)
    remote_portrait = b"remote-portrait"
    remote_sha = hashlib.sha256(remote_portrait).hexdigest()
    posted: list[dict] = []

    class FakeResponse:
        def __init__(self, *, payload=None, content=b"", status_code=200):
            self._payload = payload
            self.content = content
            self.status_code = status_code
            self.text = ""

        def json(self):
            return self._payload

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, headers=None):
            if url.endswith("/api/server-sync/attachments"):
                return FakeResponse(
                    payload={
                        "ok": True,
                        "attachments": [
                            {
                                "path": "portraits/f1/开心.png",
                                "sha256": remote_sha,
                                "size": len(remote_portrait),
                                "mime": "image/png",
                            }
                        ],
                    }
                )
            return FakeResponse(content=remote_portrait)

        def post(self, url, content=None, headers=None):
            posted.append({"url": url, "content": content, "headers": headers})
            return FakeResponse(payload={"ok": True})

    monkeypatch.setattr(server_sync.httpx, "Client", lambda *args, **kwargs: FakeClient())
    result = server_sync.sync_attachments(
        "http://sync.test", "test-token", direction="both"
    )
    assert result["downloaded"] == 1
    assert result["uploaded"] == 1
    assert posted and posted[0]["headers"]["X-YF-Path"] == quote("avatars/f1.png", safe="")
    assert (user_data / "portraits" / "f1" / "开心.png").read_bytes() == remote_portrait


def test_message_outbox_reincludes_old_local_message(sync_db):
    with db.session_scope() as session:
        message = message_service.create(
            session,
            "f1",
            role="user",
            content="这条消息比服务器游标旧，但还没确认送达",
        )
        message.created_at = "2026-10-29T17:07:20+00:00"
        message.updated_at = message.created_at
        message_id = message.id

    with db.session_scope() as session:
        bundle = sync_service.export_bundle(
            session, since="2999-01-01T00:00:00+00:00"
        )
        assert message_id in {row["id"] for row in bundle["tables"]["messages"]}
        sync_service.ack_messages(session, [message_id])

    with db.session_scope() as session:
        bundle = sync_service.export_bundle(
            session, since="2999-01-01T00:00:00+00:00"
        )
        assert message_id not in {row["id"] for row in bundle["tables"]["messages"]}


def test_server_relays_old_incoming_message_to_later_cursor(sync_db, monkeypatch):
    monkeypatch.setenv("YF_SERVER_TOKEN", "test-token")
    with db.session_scope() as session:
        message = message_service.create(
            session,
            "f1",
            role="user",
            content="手机补传的旧消息",
        )
        message.created_at = "2026-10-29T17:07:20+00:00"
        message.updated_at = message.created_at
        message_id = message.id
        incoming = sync_service.export_bundle(session)
    # 模拟服务器当前没有这条消息，而客户端游标已经越过消息的旧 updated_at。
    with db.session_scope() as session:
        session.delete(session.get(Message, message_id))

    app = FastAPI()
    app.include_router(server_sync_api.router)
    client = TestClient(app)
    response = client.post(
        "/api/server-sync/exchange",
        headers={"X-YF-Server-Token": "test-token"},
        json={"bundle": incoming, "since": "2026-10-30T00:00:00+00:00"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert message_id in {
        row["id"] for row in payload["bundle"]["tables"]["messages"]
    }


def test_style_profile_and_events_are_in_sync_bundle(sync_db):
    with db.session_scope() as session:
        style_profile_service.apply_changes(
            session,
            "f1",
            [{"action": "add", "section": "length", "lesson": "日常一两句就停"}],
            summary="测试",
        )
        bundle = sync_service.export_bundle(session)
    assert bundle["tables"]["friend_style_profiles"][0]["friend_id"] == "f1"
    assert bundle["tables"]["friend_style_events"][0]["friend_id"] == "f1"


def test_style_profile_is_reincluded_even_when_since_is_later(sync_db):
    with db.session_scope() as session:
        style_profile_service.apply_changes(
            session,
            "f1",
            [{"action": "add", "section": "length", "lesson": "日常先一两句"}],
            summary="旧档案",
        )
        bundle = sync_service.export_bundle(
            session, since="2999-01-01T00:00:00+00:00"
        )
    assert bundle["tables"]["friend_style_profiles"][0]["friend_id"] == "f1"


def test_life_projects_are_reincluded_even_when_since_is_later(sync_db):
    with db.session_scope() as session:
        session.add(
            LifeProject(
                id="p-old",
                friend_id="f1",
                topic="旧项目",
                status="resting",
                source="rss",
                step_count=1,
                reading_excerpt="正文",
                content_depth="full_text",
                last_thought="",
                open_question="",
                evidence="[]",
                last_activity_at="2026-10-20T00:00:00+00:00",
                created_at="2026-10-20T00:00:00+00:00",
                updated_at="2026-10-20T00:00:00+00:00",
            )
        )
        bundle = sync_service.export_bundle(
            session, since="2999-01-01T00:00:00+00:00"
        )
    assert [row["id"] for row in bundle["tables"]["life_projects"]] == ["p-old"]


def test_chat_export_only_contains_chat_scope(sync_db):
    with db.session_scope() as session:
        session.add(
            Message(
                id="m-scope",
                friend_id="f1",
                role="user",
                content="只带聊天",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        bundle = sync_service.export_chat_bundle(session)
    assert set(bundle["tables"]) == {
        "messages",
        "memories",
        "memory_events",
        "friend_states",
    }
    assert bundle["attachments"] == []


def test_memory_delete_creates_tombstone_and_excludes_deleted_from_chat_export(sync_db):
    with db.session_scope() as session:
        memory, _ = memory_service.save(
            session, Actor.AGENT, "f1", content="要被删掉的记忆", layer="long_term"
        )
        memory_id = memory.id
    with db.session_scope() as session:
        memory_service.delete(session, Actor.AGENT, memory_id)
    with db.session_scope() as session:
        tombstones = session.query(SyncTombstone).filter_by(
            entity="memories", entity_id=memory_id
        ).all()
        assert len(tombstones) == 1
        bundle = sync_service.export_chat_bundle(session)
        assert memory_id not in {row["id"] for row in bundle["tables"]["memories"]}
        assert memory_id in {
            row["entity_id"] for row in bundle["tombstones"] if row["entity"] == "memories"
        }


def test_replace_memory_bundle_only_touches_memories(sync_db):
    with db.session_scope() as session:
        session.add(
            Message(
                id="m-keep",
                friend_id="f1",
                role="user",
                content="不要动消息",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        session.add(
            Memory(
                id="mem-old",
                friend_id="f1",
                content="旧记忆",
                tag="normal",
                layer="long_term",
                created_at=_now(),
                refreshed_at=_now(),
                updated_at=_now(),
            )
        )
    bundle = {
        "app": "yumeFriend",
        "schema": 1,
        "device_id": "phone",
        "device_name": "手机",
        "device_kind": "phone",
        "tables": {
            "memories": [
                {
                    "id": "mem-new",
                    "friend_id": "f1",
                    "content": "新记忆",
                    "tag": "normal",
                    "layer": "long_term",
                    "created_at": _now(),
                    "refreshed_at": _now(),
                    "updated_at": _now(),
                }
            ],
            "memory_events": [],
        },
        "tombstones": [],
    }
    with db.session_scope() as session:
        result = sync_service.replace_memory_bundle(session, bundle)
        assert result["ok"]
        assert session.get(Memory, "mem-old") is None
        assert session.get(Memory, "mem-new") is not None
        assert session.get(Message, "m-keep") is not None


def test_replace_chat_bundle_applies_memory_tombstones(sync_db):
    with db.session_scope() as session:
        session.add(
            Memory(
                id="mem-tomb",
                friend_id="f1",
                content="应该被墓碑删掉",
                tag="normal",
                layer="long_term",
                created_at=_now(),
                refreshed_at=_now(),
                updated_at=_now(),
            )
        )
    bundle = {
        "app": "yumeFriend",
        "schema": 1,
        "device_id": "desktop",
        "device_name": "电脑",
        "device_kind": "desktop",
        "tables": {
            "messages": [],
            "memories": [],
            "friend_states": [],
            "memory_events": [],
        },
        "tombstones": [
            {
                "id": "tomb-mem-1",
                "entity": "memories",
                "entity_id": "mem-tomb",
                "deleted_at": _now(),
                "device_id": "desktop",
            }
        ],
        "attachments": [],
    }
    with db.session_scope() as session:
        sync_service.replace_chat_bundle(session, bundle)
        assert session.get(Memory, "mem-tomb") is None


def test_incoming_bundle_cannot_resurrect_tombstoned_memory(sync_db):
    with db.session_scope() as session:
        sync_service.tombstone(session, "memories", "mem-resurrect")
    bundle = {
        "app": "yumeFriend",
        "schema": 1,
        "device_id": "phone",
        "device_name": "手机",
        "device_kind": "phone",
        "tables": {
            "memories": [
                {
                    "id": "mem-resurrect",
                    "friend_id": "f1",
                    "content": "旧设备想复活",
                    "tag": "normal",
                    "layer": "long_term",
                    "created_at": _now(),
                    "refreshed_at": _now(),
                    "updated_at": _now(),
                }
            ],
        },
        "tombstones": [],
    }
    with db.session_scope() as session:
        sync_service.merge_bundle(session, bundle)
        assert session.get(Memory, "mem-resurrect") is None


def test_incoming_bundle_cannot_resurrect_tombstoned_message(sync_db):
    with db.session_scope() as session:
        session.add(
            Message(
                id="m-tomb",
                friend_id="f1",
                role="user",
                content="旧消息",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        sync_service.tombstone(session, "messages", "m-tomb")
        session.delete(session.get(Message, "m-tomb"))
    bundle = {
        "app": "yumeFriend",
        "schema": 1,
        "device_id": "phone",
        "device_name": "手机",
        "device_kind": "phone",
        "tables": {
            "messages": [
                {
                    "id": "m-tomb",
                    "friend_id": "f1",
                    "role": "user",
                    "content": "旧设备想复活",
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            ],
        },
        "tombstones": [],
    }
    with db.session_scope() as session:
        sync_service.merge_bundle(session, bundle)
        assert session.get(Message, "m-tomb") is None


def test_replace_chat_bundle_deletes_missing_rows_and_keeps_friend(sync_db):
    with db.session_scope() as session:
        session.add(
            Message(
                id="m-old",
                friend_id="f1",
                role="user",
                content="旧消息",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        session.add(
            Memory(
                id="mem-old",
                friend_id="f1",
                content="旧记忆",
                tag="normal",
                layer="long_term",
                created_at=_now(),
                refreshed_at=_now(),
                updated_at=_now(),
            )
        )
        session.add(
            FriendState(
                friend_id="f1",
                affect="{}",
                energy=0.5,
                concerns="[]",
                intent="observe",
                inner_note="旧状态",
                relationship_note="",
                open_loops="[]",
                updated_at=_now(),
            )
        )

    bundle = {
        "app": "yumeFriend",
        "schema": 1,
        "device_id": "desktop",
        "device_name": "电脑",
        "device_kind": "desktop",
        "tables": {
            "messages": [
                {
                    "id": "m-new",
                    "friend_id": "f1",
                    "role": "user",
                    "content": "新消息",
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            ],
            "memories": [],
            "friend_states": [
                {
                    "friend_id": "f1",
                    "affect": "{}",
                    "energy": 0.5,
                    "concerns": "[]",
                    "intent": "observe",
                    "inner_note": "新状态",
                    "relationship_note": "",
                    "open_loops": "[]",
                    "updated_at": _now(),
                }
            ],
            "memory_events": [],
        },
        "tombstones": [],
        "attachments": [],
    }
    with db.session_scope() as session:
        result = sync_service.replace_chat_bundle(session, bundle)
        assert result["imported"]["messages"] == 1
        assert session.get(Friend, "f1") is not None
        assert session.get(Message, "m-old") is None
        assert session.get(Message, "m-new") is not None
        assert session.get(Memory, "mem-old") is None
        assert session.get(FriendState, "f1").inner_note == "新状态"


def test_server_full_chat_replace_endpoint(sync_db, monkeypatch):
    monkeypatch.setenv("YF_SERVER_TOKEN", "test-token")
    app = FastAPI()
    app.include_router(server_sync_api.router)
    client = TestClient(app)
    with db.session_scope() as session:
        session.add(
            Message(
                id="old-endpoint-message",
                friend_id="f1",
                role="user",
                content="旧",
                created_at=_now(),
                updated_at=_now(),
            )
        )
    bundle = {
        "app": "yumeFriend",
        "schema": 1,
        "device_id": "desktop",
        "device_name": "电脑",
        "device_kind": "desktop",
        "tables": {
            "messages": [
                {
                    "id": "new-endpoint-message",
                    "friend_id": "f1",
                    "role": "user",
                    "content": "新",
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            ],
            "memories": [],
            "friend_states": [],
            "memory_events": [],
        },
        "tombstones": [],
        "attachments": [],
    }
    response = client.post(
        "/api/server-sync/replace-chat",
        headers={"X-YF-Server-Token": "test-token"},
        json={"bundle": bundle},
    )
    assert response.status_code == 200, response.text
    assert response.json()["imported"]["messages"] == 1
    full = client.get(
        "/api/server-sync/full-chat",
        headers={"X-YF-Server-Token": "test-token"},
    )
    assert full.status_code == 200
    ids = {row["id"] for row in full.json()["bundle"]["tables"]["messages"]}
    assert "new-endpoint-message" in ids
    assert "old-endpoint-message" not in ids
    assert "friends" not in full.json()["bundle"]["tables"]


def test_server_sync_can_pause_and_resume(sync_db):
    paused = server_sync.set_paused(True)
    assert paused["paused"] is True
    resumed = server_sync.set_paused(False)
    assert resumed["paused"] is False


def test_mobile_upgrade_defaults_to_paused_once(sync_db):
    with db.session_scope() as session:
        server_sync.configure(
            session,
            mode="server",
            base_url="https://example.test",
            token="token",
        )
        session.execute(
            delete(SyncMeta).where(SyncMeta.key == server_sync.PAUSED_KEY)
        )
        session.expire_all()
        server_sync.ensure_paused_on_upgrade(session)
        assert server_sync.status(session)["paused"] is True
        session.flush()
    with db.session_scope() as session:
        server_sync.ensure_paused_on_upgrade(session)
        assert server_sync.status(session)["paused"] is True


def test_push_chat_requires_pause_and_sends_chat_scope(sync_db, monkeypatch):
    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "ok": True,
                "backup": "/server/backups/before-replace.db",
                "server_time": "2026-10-30T03:00:00+00:00",
                "imported": {"messages": 1},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

        def post(self, url, headers, json):  # noqa: ANN001
            captured.update(url=url, headers=headers, body=json)
            return FakeResponse()

    monkeypatch.setattr(server_sync.httpx, "Client", FakeClient)
    with db.session_scope() as session:
        server_sync.configure(
            session,
            mode="server",
            base_url="https://example.test",
            token="token",
        )
        session.add(
            Message(
                id="m-push",
                friend_id="f1",
                role="user",
                content="推上去",
                created_at=_now(),
                updated_at=_now(),
            )
        )

    blocked = server_sync.push_chat()
    assert blocked["ok"] is False
    assert "暂停" in blocked["error"]

    server_sync.set_paused(True)
    result = server_sync.push_chat()
    assert result["ok"] is True
    assert result["backup"] == "/server/backups/before-replace.db"
    assert captured["url"].endswith("/api/server-sync/replace-chat")
    assert set(captured["body"]["bundle"]["tables"]) == {
        "messages",
        "memories",
        "memory_events",
        "friend_states",
    }
    assert [row["id"] for row in captured["body"]["bundle"]["tables"]["messages"]] == [
        "m-push"
    ]


def test_pull_chat_replaces_local_and_creates_backup(sync_db, monkeypatch):
    remote_bundle = {
        "app": "yumeFriend",
        "schema": 1,
        "device_id": "server",
        "device_name": "服务器",
        "device_kind": "server",
        "tables": {
            "messages": [
                {
                    "id": "m-pulled",
                    "friend_id": "f1",
                    "role": "user",
                    "content": "从服务器拉下来",
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            ],
            "memories": [],
            "memory_events": [],
            "friend_states": [],
        },
        "tombstones": [],
        "attachments": [],
    }

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "ok": True,
                "bundle": remote_bundle,
                "server_time": "2026-10-30T03:01:00+00:00",
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

        def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return FakeResponse()

    monkeypatch.setattr(server_sync.httpx, "Client", FakeClient)
    with db.session_scope() as session:
        server_sync.configure(
            session,
            mode="server",
            base_url="https://example.test",
            token="token",
        )
        session.add(
            Message(
                id="m-local-old",
                friend_id="f1",
                role="user",
                content="本机旧消息",
                created_at=_now(),
                updated_at=_now(),
            )
        )

    server_sync.set_paused(True)
    result = server_sync.pull_chat()
    assert result["ok"] is True
    assert result["backup"]
    assert Path(result["backup"]).is_file()
    with db.session_scope() as session:
        assert session.get(Message, "m-local-old") is None
        assert session.get(Message, "m-pulled") is not None


def test_server_exchange_roundtrip(sync_db, monkeypatch):
    monkeypatch.setenv("YF_SERVER_TOKEN", "test-token")
    app = FastAPI()
    app.include_router(server_sync_api.router)
    client = TestClient(app)

    with db.session_scope() as session:
        bundle = sync_service.export_bundle(session)

    response = client.post(
        "/api/server-sync/exchange",
        headers={"X-YF-Server-Token": "test-token"},
        json={"bundle": bundle, "since": None},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert {item["id"] for item in payload["bundle"]["tables"]["friends"]} == {"f1"}


def test_server_exchange_rejects_bad_token(sync_db, monkeypatch):
    monkeypatch.setenv("YF_SERVER_TOKEN", "test-token")
    app = FastAPI()
    app.include_router(server_sync_api.router)
    client = TestClient(app)
    response = client.get("/api/server-sync/ping", headers={"X-YF-Server-Token": "wrong"})
    assert response.status_code == 403


def test_client_exchange_merges_remote_bundle(sync_db, monkeypatch):
    remote_friend = {
        "id": "f-remote",
        "name": "远端朋友",
        "personality": "",
        "custom_instructions": "",
        "presence": 1,
        "current_mode": "normal",
        "current_role": None,
        "unread_count": 0,
        "life_enabled": 0,
        "voice_examples": "[]",
        "created_at": _now(),
        "updated_at": _now(),
    }

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "ok": True,
                "bundle": {
                    "app": "yumeFriend",
                    "schema": 1,
                    "device_id": "server",
                    "device_name": "服务器",
                    "device_kind": "server",
                    "tables": {"friends": [remote_friend]},
                    "tombstones": [],
                    "attachments": [],
                    "generated_at": "2026-10-25T10:00:00+00:00",
                },
                "server_time": "2026-10-25T10:00:00+00:00",
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

        def post(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return FakeResponse()

    monkeypatch.setattr(server_sync.httpx, "Client", FakeClient)
    with db.session_scope() as session:
        server_sync.configure(
            session,
            mode="server",
            base_url="https://example.test",
            token="token",
        )
    result = server_sync.exchange_once()
    assert result["ok"] is True
    with db.session_scope() as session:
        assert session.get(Friend, "f-remote") is not None
        assert server_sync.status(session)["cursor"] == "2026-10-25T10:00:00+00:00"


def test_export_skips_empty_streaming_assistant(sync_db):
    with db.session_scope() as session:
        session.add(
            Message(
                id="empty-assistant",
                friend_id="f1",
                role="assistant",
                content="",
                stream_status="streaming",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        session.add(
            Message(
                id="done-assistant",
                friend_id="f1",
                role="assistant",
                content="完整回复",
                stream_status="completed",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        bundle = sync_service.export_bundle(session)
    ids = {row["id"] for row in bundle["tables"]["messages"]}
    assert "empty-assistant" not in ids
    assert "done-assistant" in ids


def test_completed_message_repairs_remote_empty_placeholder(sync_db):
    _use = db.session_scope
    with _use() as session:
        session.add(
            Message(
                id="repair-me",
                friend_id="f1",
                role="assistant",
                content="",
                stream_status="streaming",
                created_at="2026-10-27T09:00:00+00:00",
                updated_at="2026-10-27T09:00:00+00:00",
            )
        )
    with _use() as session:
        message = session.get(Message, "repair-me")
        message.content = "后来补上的正文"
        message.stream_status = "completed"
        message.updated_at = "2026-10-27T10:00:00+00:00"
        bundle = sync_service.export_bundle(
            session, since="2026-10-27T09:30:00+00:00"
        )
    assert [row["id"] for row in bundle["tables"]["messages"]] == ["repair-me"]
    with _use() as session:
        # 模拟对端还停在旧的空 streaming 占位。
        local = session.get(Message, "repair-me")
        local.content = ""
        local.stream_status = "streaming"
        local.updated_at = "2026-10-27T09:00:00+00:00"
    with _use() as session:
        sync_service.merge_bundle(session, bundle)
        repaired = session.get(Message, "repair-me")
        assert repaired is not None
        assert repaired.content == "后来补上的正文"
        assert repaired.stream_status == "completed"


def test_document_chunks_are_incremental(sync_db):
    with db.session_scope() as session:
        session.add(
            Document(
                id="doc-1",
                friend_id="f1",
                name="资料",
                kind="txt",
                bytes=10,
                chars=10,
                text="正文",
                status="ready",
                created_at="2026-10-27T08:00:00+00:00",
                updated_at="2026-10-27T08:00:00+00:00",
            )
        )
        session.add(
            DocumentChunk(
                id="chunk-old",
                document_id="doc-1",
                friend_id="f1",
                seq=0,
                text="旧块",
                created_at="2026-10-27T08:00:00+00:00",
            )
        )
        session.add(
            DocumentChunk(
                id="chunk-new",
                document_id="doc-1",
                friend_id="f1",
                seq=1,
                text="新块",
                created_at="2026-10-27T10:00:00+00:00",
            )
        )
        bundle = sync_service.export_bundle(
            session, since="2026-10-27T09:00:00+00:00"
        )
    assert [row["id"] for row in bundle["tables"]["document_chunks"]] == ["chunk-new"]
