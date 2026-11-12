from __future__ import annotations

import hashlib
import io
import json
import socket
import threading
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from backend import config
import backend.db as db
from backend.app import create_app
from backend.services import minecraft_probe, minecraft_version_service


def _pack_bytes(version: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "minecraft-data/package.json",
            json.dumps({"name": "minecraft-data", "version": "test"}),
        )
        archive.writestr("minecraft-data/index.js", "")
        archive.writestr(
            "minecraft-data/minecraft-data/data/dataPaths.json",
            json.dumps({"pc": {version: {}}}),
        )
    return buffer.getvalue()


def _manifest(tmp_path: Path, versions: list[tuple[str, int]]) -> Path:
    entries = []
    for version, protocol in versions:
        payload = _pack_bytes(version)
        path = tmp_path / f"minecraft-data-{version}.zip"
        path.write_bytes(payload)
        entries.append(
            {
                "version": version,
                "protocol": protocol,
                "url": path.as_uri(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "tier": "stable",
            }
        )
    manifest = tmp_path / "versions.json"
    manifest.write_text(
        json.dumps({"schema": 1, "versions": entries}),
        encoding="utf-8",
    )
    return manifest


def test_download_and_install_version_pack(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path, [("1.20.1", 763)])
    monkeypatch.setattr(config, "MINECRAFT_DIR", tmp_path / "minecraft")
    monkeypatch.setattr(
        config, "MINECRAFT_VERSION_DIR", tmp_path / "minecraft" / "versions"
    )
    monkeypatch.setattr(config, "MINECRAFT_VERSION_MANIFEST_URL", str(manifest))

    path = minecraft_version_service.download_and_install("1.20.1")

    assert (path / "package.json").is_file()
    assert minecraft_version_service.pack_installed("1.20.1")
    assert minecraft_version_service.status(force=True)["installed_count"] == 1


def test_resolve_newer_server_uses_highest_pack_as_experimental(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path, [("1.20.1", 763), ("26.1", 775)])
    monkeypatch.setattr(config, "MINECRAFT_DIR", tmp_path / "minecraft")
    monkeypatch.setattr(
        config, "MINECRAFT_VERSION_DIR", tmp_path / "minecraft" / "versions"
    )
    monkeypatch.setattr(config, "MINECRAFT_VERSION_MANIFEST_URL", str(manifest))
    monkeypatch.setattr(
        minecraft_version_service,
        "ping_server",
        lambda host, port: {
            "ok": True,
            "server_version": "26.3",
            "protocol": 777,
            "latency_ms": 1,
        },
    )
    minecraft_version_service._catalog_cache = None

    resolution = minecraft_version_service.resolve_for_server(
        "127.0.0.1", 25565, requested_version="auto"
    )

    assert resolution.pack_version == "26.1"
    assert resolution.mode == "experimental"
    assert "26.3" in (resolution.warning or "")


def test_resolve_same_protocol_uses_matching_pack(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path, [("1.21.9", 773), ("26.1", 775)])
    monkeypatch.setattr(config, "MINECRAFT_DIR", tmp_path / "minecraft")
    monkeypatch.setattr(
        config, "MINECRAFT_VERSION_DIR", tmp_path / "minecraft" / "versions"
    )
    monkeypatch.setattr(config, "MINECRAFT_VERSION_MANIFEST_URL", str(manifest))
    monkeypatch.setattr(
        minecraft_version_service,
        "ping_server",
        lambda host, port: {
            "ok": True,
            "server_version": "1.21.10",
            "protocol": 773,
            "latency_ms": 1,
        },
    )
    minecraft_version_service._catalog_cache = None

    resolution = minecraft_version_service.resolve_for_server(
        "127.0.0.1", 25565, requested_version="auto"
    )

    assert resolution.pack_version == "1.21.9"
    assert resolution.mode == "same_protocol"


def test_probe_parses_status_response():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = int(server.getsockname()[1])
    status = {
        "version": {"name": "Paper 1.20.1", "protocol": 763},
        "players": {"online": 1, "max": 20},
    }

    def serve():
        conn, _ = server.accept()
        try:
            conn.recv(4096)
            payload = json.dumps(status).encode("utf-8")
            packet = b"\x00" + minecraft_probe._encode_varint(len(payload)) + payload
            conn.sendall(minecraft_probe._encode_varint(len(packet)) + packet)
        finally:
            conn.close()
            server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    result = minecraft_probe.ping_server("127.0.0.1", port)
    thread.join(timeout=2)

    assert result["server_version"] == "Paper 1.20.1"
    assert result["protocol"] == 763


def test_minecraft_versions_api(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path, [("1.20.1", 763)])
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "MINECRAFT_DIR", tmp_path / "minecraft")
    monkeypatch.setattr(
        config, "MINECRAFT_VERSION_DIR", tmp_path / "minecraft" / "versions"
    )
    monkeypatch.setattr(config, "MINECRAFT_VERSION_MANIFEST_URL", str(manifest))
    minecraft_version_service._catalog_cache = None

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/game/minecraft/versions")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["manifest_ok"] is True
        assert payload["versions"][0]["version"] == "1.20.1"
