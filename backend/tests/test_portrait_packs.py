from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.config as config
import backend.db as db
from backend.app import create_app


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


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (64, 96), (*color, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _make_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "高兴.png").write_bytes(_png((255, 200, 80)))
    (source / "害羞.png").write_bytes(_png((255, 120, 160)))
    return source


def test_portrait_pack_crud_and_friend_selection(client, tmp_path):
    source = _make_source(tmp_path)
    scanned = client.post("/api/portrait-packs/scan", json={"dir": str(source)})
    assert scanned.status_code == 200, scanned.text
    assert {item["emotion"] for item in scanned.json()["candidates"]} == {"高兴", "害羞"}

    imported = client.post(
        "/api/portrait-packs/import",
        json={"name": "测试包", "dir": str(source)},
    )
    assert imported.status_code == 200, imported.text
    pack = imported.json()["pack"]
    assert pack["emotions"] == 2

    friend = client.post("/api/friends", json={"name": "小敏"}).json()
    fid = friend["id"]
    updated = client.patch(
        f"/api/friends/{fid}",
        json={
            "visual_mode": "portrait",
            "portrait_pack_id": pack["id"],
        },
    )
    assert updated.status_code == 200, updated.text
    detail = updated.json()
    assert detail["visual_mode"] == "portrait"
    assert detail["portrait_pack_id"] == pack["id"]
    assert set(detail["portrait_emotions"]) == {"高兴", "害羞"}

    image = client.get(f"/api/friends/{fid}/portraits/高兴")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/")

    blocked = client.delete(f"/api/portrait-packs/{pack['id']}")
    assert blocked.status_code == 409

    client.patch(
        f"/api/friends/{fid}",
        json={"visual_mode": "live2d"},
    )
    deleted = client.delete(f"/api/portrait-packs/{pack['id']}")
    assert deleted.status_code == 204


def test_legacy_portrait_dir_appears_as_pack(client):
    friend = client.post("/api/friends", json={"name": "阿月"}).json()
    packs = client.get("/api/portrait-packs").json()["packs"]
    legacy = next(
        item for item in packs if item["id"] == f"legacy_{friend['id']}"
    )
    assert legacy["legacy"] is True
    assert legacy["emotions"] >= 1
