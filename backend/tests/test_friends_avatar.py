"""头像（v2，T2.0-3）：上传 / 读取 / 删除 / 删除朋友时一起清。

盯这几件容易漏的事：
· 上传会**统一处理**（4000×3000 的图进来，出去必须是 256×256 PNG）
· 不是图片 → 4xx（不能 500）
· **物理删朋友时头像文件要跟着没**（宪法第九条"删除要彻底"）
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.config as config
import backend.db as db
from backend.app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "avatar.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "AVATAR_DIR", tmp_path / "avatars")
    app = create_app()
    with TestClient(app) as c:
        yield c


def _png(width: int = 4000, height: int = 3000, color=(200, 120, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _friend(client, name="阿月") -> str:
    return client.post("/api/friends", json={"name": name, "personality": "话不多"}).json()["id"]


def test_new_friend_has_no_avatar_and_no_url(client):
    fid = _friend(client)
    body = client.get(f"/api/friends/{fid}").json()
    assert body["avatar_url"] is None  # 没头像 → 前端用首字母色块


def test_upload_resizes_to_256_png_and_sets_url(client):
    fid = _friend(client)
    result = client.post(f"/api/friends/{fid}/avatar", files={"file": ("big.png", _png(), "image/png")})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["avatar_url"], "上传后应该给 URL"
    assert body["avatar_url"].startswith(f"/api/friends/{fid}/avatar?v=")

    got = client.get(f"/api/friends/{fid}/avatar")
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(got.content)) as im:
        assert im.size == (256, 256), f"应该被压到 256×256，实际 {im.size}"


def test_not_an_image_is_rejected_not_500(client):
    fid = _friend(client)
    result = client.post(
        f"/api/friends/{fid}/avatar", files={"file": ("x.png", b"this is not an image", "image/png")}
    )
    assert 400 <= result.status_code < 500, result.text
    assert result.json()["error"]["code"] == "AVATAR_NOT_IMAGE"


def test_delete_avatar_clears_url_and_file(client):
    fid = _friend(client)
    client.post(f"/api/friends/{fid}/avatar", files={"file": ("a.png", _png(300, 300), "image/png")})
    path = config.AVATAR_DIR / f"{fid}.png"
    assert path.exists()

    body = client.delete(f"/api/friends/{fid}/avatar").json()
    assert body["avatar_url"] is None
    assert not path.exists(), "删头像要把文件也删掉"
    assert client.get(f"/api/friends/{fid}/avatar").status_code == 404


def test_purging_friend_removes_avatar_file(client):
    """物理删除朋友 → 头像文件不能留下（删除要彻底）。"""
    fid = _friend(client)
    client.post(f"/api/friends/{fid}/avatar", files={"file": ("a.png", _png(300, 300), "image/png")})
    path = config.AVATAR_DIR / f"{fid}.png"
    assert path.exists()

    # 物理删除是 POST /purge（DELETE /{id} 只是软删，那时**不该**动头像文件）
    assert client.post(f"/api/friends/{fid}/purge").status_code == 204
    assert not path.exists(), "物理删朋友时头像文件也要没"


def test_soft_delete_keeps_avatar_file(client):
    """软删（30 天内可恢复）**不能**把头像删掉——恢复了还得有脸。"""
    fid = _friend(client)
    client.post(f"/api/friends/{fid}/avatar", files={"file": ("a.png", _png(300, 300), "image/png")})
    path = config.AVATAR_DIR / f"{fid}.png"

    assert client.delete(f"/api/friends/{fid}").status_code == 204
    assert path.exists(), "软删阶段文件要留着"

    assert client.post(f"/api/friends/{fid}/restore").status_code in (200, 204)
    assert path.exists()


def test_upload_triggers_description_task(client, monkeypatch):
    """上传头像要顺手把"看图写描述"的后台任务点着（D84）。"""
    from backend.services import avatar_description

    calls: list[str] = []
    monkeypatch.setattr(avatar_description, "spawn_describe", calls.append)

    fid = _friend(client)
    client.post(f"/api/friends/{fid}/avatar", files={"file": ("a.png", _png(300, 300), "image/png")})

    assert calls == [fid]


def test_describe_endpoint_says_human_words_when_no_avatar(client):
    """没头像 → 200 + 人话提示（**不是错误**，D83 的口径）。"""
    fid = _friend(client)
    body = client.post(f"/api/friends/{fid}/avatar/describe").json()
    assert body["ok"] is False
    assert "头像" in body["hint"]


def test_avatar_is_committed_before_the_describe_task_starts(client, monkeypatch):
    """T2.0-9 抓到的 bug：后台任务用自己的新会话读朋友行，
    所以**上传必须先提交**，否则它读到 avatar_path 还是 NULL → 静默跳过（没日志、没描述）。"""
    from backend.models import Friend
    from backend.services import avatar_description

    seen: dict = {}

    def fake_spawn(friend_id: str) -> None:
        with db.session_scope() as session:  # 独立的新会话 = 后台任务看到的那个世界
            seen["avatar_path"] = session.get(Friend, friend_id).avatar_path

    monkeypatch.setattr(avatar_description, "spawn_describe", fake_spawn)
    fid = _friend(client)
    response = client.post(
        f"/api/friends/{fid}/avatar", files={"file": ("a.png", _png(300, 300), "image/png")}
    )
    assert response.status_code == 200
    assert seen.get("avatar_path"), "起后台任务时 avatar_path 必须已经提交"


def test_avatar_model_upload_get_delete_and_purge(client, tmp_path):
    """每个朋友可以上传自己的 VRM；删除/物理删朋友都要清干净。"""
    fid = _friend(client)
    payload = b"glTF" + b"\x00" * 32
    result = client.post(
        f"/api/friends/{fid}/avatar-model",
        files={"file": ("me.vrm", payload, "model/gltf-binary")},
    )
    assert result.status_code == 200, result.text
    model_url = result.json()["model_url"]
    assert model_url.startswith(f"/api/friends/{fid}/avatar-model?v=")
    state = client.get(f"/api/friends/{fid}/space").json()["state"]
    # 版本串只负责破缓存，上传响应和状态读取可能用不同精度生成；
    # 这里断言两者指向同一个模型端点，且都带版本号。
    state_model = state["appearance"]["model"]
    assert state_model.split("?v=")[0] == model_url.split("?v=")[0]
    assert "?v=" in state_model

    got = client.get(f"/api/friends/{fid}/avatar-model")
    assert got.status_code == 200
    assert got.content == payload

    deleted = client.delete(f"/api/friends/{fid}/avatar-model")
    assert deleted.status_code == 200
    assert deleted.json()["state"]["appearance"]["model"] is None
    assert client.get(f"/api/friends/{fid}/avatar-model").status_code == 404

    client.post(
        f"/api/friends/{fid}/avatar-model",
        files={"file": ("me.vrm", payload, "model/gltf-binary")},
    )
    path = config.AVATAR_MODEL_DIR / f"{fid}.vrm"
    assert path.exists()
    assert client.post(f"/api/friends/{fid}/purge").status_code == 204
    assert not path.exists(), "物理删朋友时自定义 3D 模型也要删掉"


def test_avatar_model_rejects_non_vrm(client):
    fid = _friend(client)
    result = client.post(
        f"/api/friends/{fid}/avatar-model",
        files={"file": ("me.glb", b"glTF", "model/gltf-binary")},
    )
    assert 400 <= result.status_code < 500


def test_space_appearance_auto_uses_uploaded_vrm_when_state_model_is_empty(tmp_path, monkeypatch):
    """文件在 `user_data/avatar_models` 里时，状态里 model 为空也要自动捡起来。"""
    from backend.services import space_service

    monkeypatch.setattr(config, "AVATAR_MODEL_DIR", tmp_path)
    (tmp_path / "friend-1.vrm").write_bytes(b"glTF")
    appearance = space_service._normalize_appearance({"outfit": {}}, friend_id="friend-1")
    assert appearance["model"].startswith("/api/friends/friend-1/avatar-model?v=")


def test_space_zone_labels_do_not_include_explicit_anatomy():
    from backend.services import space_service

    removed = ("nip" "ple", "va" "gina", "gr" "oin")
    assert all(zone not in space_service.ZONE_LABELS for zone in removed)
