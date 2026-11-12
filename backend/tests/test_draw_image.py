"""v3.1：手动画图（手机端那个「画图」面板的接口）。

跟工具 `draw_image` 是**同一条生成路径**，区别只是"谁决定画"：
工具是她自己调（图挂在她那条回复上），这个是用户点按钮（直接建一条她的消息）。
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.config as config
import backend.db as db
from backend.app import create_app
from backend.models import Message
from backend.services import imagegen_service


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "draw.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "UPLOAD_PENDING_DIR", tmp_path / "uploads" / "pending")
    app = create_app()
    with TestClient(app) as c:
        yield c


def _fid(client) -> str:
    response = client.post("/api/friends", json={"name": "阿月", "personality": "话不多"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _fake_generate(monkeypatch, *, calls: list[dict] | None = None):
    async def _generate(prompt: str, *, session, ref_png=None):  # noqa: ANN001, ARG001
        if calls is not None:
            calls.append({"prompt": prompt, "ref": ref_png})
        target = config.IMAGE_DIR / "drawn.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return {"rel": "images/drawn.png", "width": 1024, "height": 1024, "bytes": 12, "model": "fake-model"}

    monkeypatch.setattr(imagegen_service, "generate", _generate)


def test_draw_creates_a_message_with_the_image(client, monkeypatch):
    calls: list[dict] = []
    _fake_generate(monkeypatch, calls=calls)
    fid = _fid(client)

    response = client.post(f"/api/friends/{fid}/draw_image", json={"prompt": "1girl, silver hair"})

    assert response.status_code == 200, response.text
    assert response.json()["message"]["content"] == "画好了。"
    assert len(calls) == 1 and calls[0]["prompt"] == "1girl, silver hair"
    with db.session_scope() as session:
        row = session.query(Message).one()
        assert row.role == "assistant"
        assert row.image_path == "images/drawn.png"
        assert row.image_no == 1  # 这是这个朋友的第 1 张图（给 look_image / ref 用）


def test_draw_passes_the_reference_image(client, monkeypatch):
    calls: list[dict] = []
    _fake_generate(monkeypatch, calls=calls)
    fid = _fid(client)
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (30, 90, 180)).save(buffer, format="PNG")
    ref_bytes = buffer.getvalue()
    uploaded = client.post(
        "/api/uploads/image", files={"file": ("ref.png", ref_bytes, "image/png")}
    ).json()

    response = client.post(
        f"/api/friends/{fid}/draw_image",
        json={"prompt": "same style", "ref_image_id": uploaded["id"]},
    )

    assert response.status_code == 200, response.text
    assert calls[0]["ref"] and calls[0]["ref"].startswith(b"\x89PNG")


def test_draw_needs_a_prompt_and_a_real_reference(client, monkeypatch):
    _fake_generate(monkeypatch)
    fid = _fid(client)

    empty = client.post(f"/api/friends/{fid}/draw_image", json={"prompt": "   "})
    assert empty.status_code == 400
    assert "想画什么" in empty.json()["error"]["message"]

    bad_ref = client.post(
        f"/api/friends/{fid}/draw_image",
        json={"prompt": "x", "ref_image_id": "a" * 32},
    )
    assert bad_ref.status_code == 400
    assert "参考图" in bad_ref.json()["error"]["message"]
