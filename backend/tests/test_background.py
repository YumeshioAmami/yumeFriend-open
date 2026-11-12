"""沉浸模式背景图（v2.2 / D107）。

盯三件事：上传能自动裁成 16:9、朋友字典里带 `background_url`、删掉之后回到"没有"。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.config as config
import backend.db as db
from backend.app import create_app


def _png(width: int = 800, height: int = 600) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 80, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "bg.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "BACKGROUND_DIR", tmp_path / "backgrounds")
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client, tmp_path


def test_background_roundtrip(client):
    http, tmp_path = client
    friend_id = http.post("/api/friends", json={"name": "阿月", "personality": ""}).json()["id"]

    before = http.get(f"/api/friends/{friend_id}").json()
    assert before["background_url"] is None
    assert http.get(f"/api/friends/{friend_id}/background").status_code == 404

    uploaded = http.post(
        f"/api/friends/{friend_id}/background",
        files={"file": ("bg.png", _png(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    url = uploaded.json()["background_url"]
    assert url and "/background" in url

    # 存下来的是一张**16:9 的 JPEG**（沉浸模式全屏铺底用）
    served = http.get(f"/api/friends/{friend_id}/background")
    assert served.status_code == 200 and served.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(served.content)) as image:
        assert image.size == (1920, 1080)

    # 删掉 → 回到"没有"，文件也一起清（删除要彻底）
    removed = http.delete(f"/api/friends/{friend_id}/background")
    assert removed.status_code == 200 and removed.json()["background_url"] is None
    assert not (tmp_path / "backgrounds" / f"{friend_id}.jpg").exists()


def test_background_rejects_garbage(client):
    http, _tmp = client
    friend_id = http.post("/api/friends", json={"name": "阿月", "personality": ""}).json()["id"]
    response = http.post(
        f"/api/friends/{friend_id}/background",
        files={"file": ("x.png", b"not-an-image", "image/png")},
    )
    assert response.status_code == 400
    assert "读不出来" in response.json()["error"]["message"]
