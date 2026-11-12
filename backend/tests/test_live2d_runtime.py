from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.app import create_app
from backend.services import live2d_runtime_service


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.headers = {
            "content-type": "text/javascript",
            "content-length": str(len(payload)),
        }
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, _chunk_size: int):
        midpoint = len(self._payload) // 2
        yield self._payload[:midpoint]
        yield self._payload[midpoint:]


class _FakeClient:
    payload = b"/* Live2D Cubism Core */\n" + (b"x" * 40000)

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, *args, **kwargs):
        del args, kwargs
        return _FakeResponse(self.payload)


def test_runtime_download_stream_writes_installed_core(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIVE2D_DIR", tmp_path / "live2d")
    monkeypatch.setattr(live2d_runtime_service.httpx, "AsyncClient", _FakeClient)

    async def collect():
        return [event async for event in live2d_runtime_service.stream_download_runtime()]

    events = asyncio.run(collect())

    assert events[0]["type"] == "progress"
    assert events[-1]["type"] == "done"
    target = live2d_runtime_service.runtime_file()
    assert target.is_file()
    assert target.read_bytes() == _FakeClient.payload


def test_runtime_status_and_consent_api(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "LIVE2D_DIR", tmp_path / "live2d")
    monkeypatch.setattr(live2d_runtime_service, "_bundled_runtime_files", lambda: ())

    app = create_app()
    with TestClient(app) as client:
        status = client.get("/api/live2d/runtime/status")
        assert status.status_code == 200
        assert status.json()["available"] is False

        rejected = client.post("/api/live2d/runtime/download", json={})
        assert rejected.status_code == 400
        assert "许可协议" in rejected.text

        core = client.get("/api/live2d/runtime/core.js")
        assert core.status_code == 404

        target = live2d_runtime_service.runtime_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"/* Live2D Cubism Core */\n" + b"x" * 40000)
        installed = client.get("/api/live2d/runtime/status").json()
        assert installed["available"] is True
        assert installed["installed"] is True
        served = client.get("/api/live2d/runtime/core.js")
        assert served.status_code == 200
        assert served.content.startswith(b"/* Live2D Cubism Core */")


def test_runtime_download_endpoint_streams_ndjson(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "LIVE2D_DIR", tmp_path / "live2d")

    async def fake_download():
        yield {"type": "progress", "received": 1024, "total": 2048}
        yield {"type": "done", "ok": True, "size": 2048, "sha256": "abc"}

    monkeypatch.setattr(
        live2d_runtime_service,
        "stream_download_runtime",
        fake_download,
    )
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/live2d/runtime/download",
            json={"accepted_license": True},
        )
        assert response.status_code == 200
        assert '"type": "progress"' in response.text
        assert '"type": "done"' in response.text
