"""同步方向：both / pull / push 必须严格按请求执行（D307）。"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import sync as sync_api
from backend.services import sync_lan


PEER = {"ip": "192.168.1.9", "port": 4567}
REMOTE_BUNDLE = {
    "device_id": "remote-1",
    "device_name": "平板",
    "device_kind": "tablet",
}


@contextmanager
def _session():
    yield object()


def test_pull_only_downloads_and_never_pushes(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(sync_lan, "session_scope", _session)
    monkeypatch.setattr(sync_lan.sync_service, "remember_device", lambda *_args: None)
    monkeypatch.setattr(
        sync_lan.sync_service,
        "merge_bundle",
        lambda _session, _bundle: {
            "imported": {"messages": 2},
            "attachments": [{"sha256": "aaa", "path": "images/a.png"}],
        },
    )
    monkeypatch.setattr(
        sync_lan,
        "_http_json",
        lambda url, code, payload=None, timeout=20: (
            calls.append(("http", url, payload)) or REMOTE_BUNDLE
        ),
    )
    monkeypatch.setattr(
        sync_lan,
        "_download_blob",
        lambda base, code, item: calls.append(("download", item["path"])),
    )
    monkeypatch.setattr(
        sync_lan.sync_service,
        "export_bundle",
        lambda _session: (_ for _ in ()).throw(AssertionError("pull 不该导出/推送本机包")),
    )
    monkeypatch.setattr(
        sync_lan,
        "_upload_blob",
        lambda *_args: (_ for _ in ()).throw(AssertionError("pull 不该上传附件")),
    )

    result = sync_lan.sync_with_peer(PEER, "123456", "pull")

    assert result["direction"] == "pull"
    assert result["pulled"] == {"messages": 2}
    assert result["pushed"] == {}
    assert calls == [
        ("http", "http://192.168.1.9:4567/bundle", None),
        ("download", "images/a.png"),
    ]


def test_push_only_uploads_and_never_merges(monkeypatch):
    calls: list[tuple] = []
    remembered: list[dict] = []
    monkeypatch.setattr(sync_lan, "session_scope", _session)
    monkeypatch.setattr(
        sync_lan.sync_service,
        "remember_device",
        lambda _session, peer: remembered.append(peer),
    )
    monkeypatch.setattr(
        sync_lan.sync_service,
        "merge_bundle",
        lambda *_args: (_ for _ in ()).throw(AssertionError("push 不该合并对方业务包")),
    )
    monkeypatch.setattr(
        sync_lan.sync_service,
        "export_bundle",
        lambda _session: {"device_id": "local-1"},
    )

    def http(url, code, payload=None, timeout=20):
        calls.append(("http", url, payload))
        if url.endswith("/ping"):
            return REMOTE_BUNDLE
        return {
            "imported": {"memories": 3},
            "attachments": [{"sha256": "bbb", "path": "portraits/开心.png"}],
        }

    monkeypatch.setattr(sync_lan, "_http_json", http)
    monkeypatch.setattr(
        sync_lan,
        "_download_blob",
        lambda *_args: (_ for _ in ()).throw(AssertionError("push 不该下载附件")),
    )
    monkeypatch.setattr(
        sync_lan,
        "_upload_blob",
        lambda base, code, item: calls.append(("upload", item["path"])),
    )

    result = sync_lan.sync_with_peer(PEER, "123456", "push")

    assert result["direction"] == "push"
    assert result["pulled"] == {}
    assert result["pushed"] == {"memories": 3}
    assert remembered == [
        {"device_id": "remote-1", "name": "平板", "kind": "tablet"},
    ]
    assert calls == [
        ("http", "http://192.168.1.9:4567/ping", None),
        ("http", "http://192.168.1.9:4567/bundle", {"device_id": "local-1"}),
        ("upload", "portraits/开心.png"),
    ]


def test_both_runs_pull_then_push(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(sync_lan, "session_scope", _session)
    monkeypatch.setattr(sync_lan.sync_service, "remember_device", lambda *_args: None)
    monkeypatch.setattr(
        sync_lan.sync_service,
        "merge_bundle",
        lambda *_args: {
            "imported": {"messages": 1},
            "attachments": [{"sha256": "ccc", "path": "images/c.png"}],
        },
    )
    monkeypatch.setattr(
        sync_lan.sync_service,
        "export_bundle",
        lambda _session: {"device_id": "local-1"},
    )

    def http(url, code, payload=None, timeout=20):
        calls.append(("http", url, payload))
        if url.endswith("/bundle") and payload is None:
            return REMOTE_BUNDLE
        if url.endswith("/ping"):
            return REMOTE_BUNDLE
        return {
            "imported": {"memories": 4},
            "attachments": [{"sha256": "ddd", "path": "avatars/a.png"}],
        }

    monkeypatch.setattr(sync_lan, "_http_json", http)
    monkeypatch.setattr(
        sync_lan,
        "_download_blob",
        lambda base, code, item: calls.append(("download", item["path"])),
    )
    monkeypatch.setattr(
        sync_lan,
        "_upload_blob",
        lambda base, code, item: calls.append(("upload", item["path"])),
    )

    result = sync_lan.sync_with_peer(PEER, "123456", "both")

    assert result["direction"] == "both"
    assert result["pulled"] == {"messages": 1}
    assert result["pushed"] == {"memories": 4}
    assert calls == [
        ("http", "http://192.168.1.9:4567/bundle", None),
        ("download", "images/c.png"),
        ("http", "http://192.168.1.9:4567/ping", None),
        ("http", "http://192.168.1.9:4567/bundle", {"device_id": "local-1"}),
        ("upload", "avatars/a.png"),
    ]


def test_rejects_unknown_direction(monkeypatch):
    monkeypatch.setattr(
        sync_lan,
        "_http_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不该发请求")),
    )
    with pytest.raises(ValueError, match="同步方向"):
        sync_lan.sync_with_peer(PEER, "123456", "sideways")


def test_api_forwards_direction(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        sync_api.sync_lan,
        "sync_with_peer",
        lambda peer, code, direction: (
            seen.append((peer, code, direction)) or {"ok": True, "direction": direction}
        ),
    )
    app = FastAPI()
    app.include_router(sync_api.router)

    response = TestClient(app).post(
        "/api/sync/run",
        json={"peer": PEER, "code": "123456", "direction": "push"},
    )

    assert response.status_code == 200
    assert response.json()["direction"] == "push"
    assert seen == [(PEER, "123456", "push")]
