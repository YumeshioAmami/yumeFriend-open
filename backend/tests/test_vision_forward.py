"""D135：给「聊天模型看不了图」的场景补上独立视觉模型的**转述**。

盯这几件事（都是踩过 / 容易踩的）：

1. 没配 / 视觉就是跟随聊天 → **一次网络都不发**（老行为不变：如实说"看不到"）
2. 独立配了 → 转述文字**真的进了 chat 的 messages**（当轮 + 历史占位）
3. 同一张图只问一次（按文件缓存）
4. 视觉模型连不上 / 报错 → 退回"看不到"，**绝不把聊天带崩**
5. 历史里的图**只读缓存、绝不联网**（不然每轮都要多等一次视觉模型）
"""

from __future__ import annotations

import io
import json
import types

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.config as config
import backend.db as db
from backend.agent import loop as loop_module
from backend.api.deps import get_provider_factory
from backend.app import create_app
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.services import settings_service, vision_service

TRANSCRIPT = "一只红猫坐在窗台上，背后是蓝窗帘。"


def _png(width: int = 32, height: int = 32) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, text: str) -> None:
        self._text = text
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._text}}]}


class _FakeVision:
    """假装是 `httpx.AsyncClient`：记下请求，回一句固定转述（或者按需炸）。"""

    def __init__(self, text: str = TRANSCRIPT, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.requests: list[dict] = []

    def client(self, *_args, **_kwargs) -> "_FakeVision":
        return self

    async def __aenter__(self) -> "_FakeVision":
        return self

    async def __aexit__(self, *_exc_info) -> bool:
        return False

    async def post(self, url, headers=None, json=None):  # noqa: A002 —— 照着 httpx 的签名
        self.requests.append({"url": url, "headers": headers or {}, "body": json})
        if self.error is not None:
            raise self.error
        return _Resp(self.text)


class FakeProvider:
    """假聊天模型：只把收到的 messages 记下来。"""

    def __init__(self) -> None:
        self.snapshots: list[list[dict]] = []
        self.config = ProviderConfig(
            name="fake",
            base_url="http://127.0.0.1:9/v1",
            api_key="k",
            model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, messages, tools=None, **kwargs):
        self.snapshots.append([dict(m) for m in messages])
        yield {"type": "content", "delta": "看到了。"}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _clean_vision_cache():
    """进程内缓存是模块级的 → 每个用例前后都清干净（否则互相串味）。"""
    vision_service._cache.clear()
    yield
    vision_service._cache.clear()


@pytest.fixture()
def session(tmp_path, monkeypatch):
    db_file = tmp_path / "vision.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    return db.session_scope


def _independent_vision(session) -> None:
    """把「视觉」配成一个**独立**模型（不跟随聊天）——这就是 D135 生效的条件。"""
    with session() as s:
        settings_service.merge_purposes(
            s,
            {
                "vision": {
                    "follow_chat": False,
                    "base_url": "https://vision.example/v1",
                    "model": "vl-1",
                    "api_key": "sk-vision",
                }
            },
        )


def _patch_vision(monkeypatch, fake: _FakeVision) -> _FakeVision:
    """只换掉 vision_service 自己引用的 httpx，不污染全局（别的模块也在用 httpx）。"""
    monkeypatch.setattr(
        vision_service,
        "httpx",
        types.SimpleNamespace(AsyncClient=fake.client, HTTPError=httpx.HTTPError),
    )
    return fake


def _write_image(tmp_path, rel: str = "images/a.png"):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png())
    return path, rel


# ---------- vision_service 本身 ----------


async def test_vision_following_chat_is_not_independent(session):
    """默认（跟随聊天）→ 不算独立配置，永远不联网。"""
    with session() as s:
        assert vision_service.configured(s) is False
        assert await vision_service.describe(s, "whatever.png") is None


async def test_independent_vision_without_key_is_not_configured(session):
    with session() as s:
        settings_service.merge_purposes(
            s,
            {
                "vision": {
                    "follow_chat": False,
                    "base_url": "https://vision.example/v1",
                    "model": "vl-1",
                    "api_key": "",
                }
            },
        )
        assert vision_service.configured(s) is False
        assert await vision_service.describe(s, "whatever.png") is None


async def test_describe_hits_the_endpoint_and_caches(session, monkeypatch, tmp_path):
    fake = _patch_vision(monkeypatch, _FakeVision())
    _independent_vision(session)
    path, _rel = _write_image(tmp_path)

    with session() as s:
        assert vision_service.configured(s) is True
        text = await vision_service.describe(s, str(path))
        again = await vision_service.describe(s, str(path))

    assert text == TRANSCRIPT
    assert again == TRANSCRIPT
    assert len(fake.requests) == 1, "同一张图只该问一次（缓存）"
    request = fake.requests[0]
    assert request["url"] == "https://vision.example/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer sk-vision"
    assert request["body"]["model"] == "vl-1"
    parts = request["body"]["messages"][0]["content"]
    assert parts[0]["type"] == "text" and parts[0]["text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert vision_service.cached(str(path)) == TRANSCRIPT


async def test_describe_failure_is_silent(session, monkeypatch, tmp_path):
    """视觉模型连不上 → None（上层退回"看不到"），绝不抛、也不写缓存。"""
    _patch_vision(monkeypatch, _FakeVision(error=httpx.ConnectError("连不上")))
    _independent_vision(session)
    path, _rel = _write_image(tmp_path)

    with session() as s:
        assert await vision_service.describe(s, str(path)) is None

    assert vision_service.cached(str(path)) is None


# ---------- loop 的接线 ----------


async def test_loop_history_notes_come_from_cache_only(session, monkeypatch, tmp_path):
    """历史里的图**只读缓存**：缓存里有就用，没有就算了 —— 一次网络都不发。"""
    fake = _patch_vision(monkeypatch, _FakeVision())
    _independent_vision(session)
    path, rel = _write_image(tmp_path)
    vision_service._cache[(str(path), path.stat().st_mtime)] = TRANSCRIPT

    history = [{"role": "user", "content": "你看这个", "image_path": rel, "image_no": 3}]
    with session() as s:
        notes, user_note = await loop_module._vision_notes(s, history, None, False)

    assert notes == {3: TRANSCRIPT}
    assert user_note is None
    assert fake.requests == [], "历史图不该联网"


async def test_loop_history_without_cache_is_skipped(session, monkeypatch, tmp_path):
    fake = _patch_vision(monkeypatch, _FakeVision())
    _independent_vision(session)
    _path, rel = _write_image(tmp_path)

    history = [{"role": "user", "content": "你看这个", "image_path": rel, "image_no": 4}]
    with session() as s:
        notes, user_note = await loop_module._vision_notes(s, history, None, False)

    assert notes == {}
    assert user_note is None
    assert fake.requests == []


async def test_loop_describes_the_new_image(session, monkeypatch, tmp_path):
    fake = _patch_vision(monkeypatch, _FakeVision())
    _independent_vision(session)
    _path, rel = _write_image(tmp_path)

    with session() as s:
        notes, user_note = await loop_module._vision_notes(s, [], rel, False)

    assert notes == {}
    assert user_note == TRANSCRIPT
    assert len(fake.requests) == 1


async def test_loop_does_nothing_when_images_are_inlined(session, monkeypatch, tmp_path):
    """视觉跟随聊天（图直接 inline）→ 转述这条路一步都不走。"""
    fake = _patch_vision(monkeypatch, _FakeVision())
    _path, rel = _write_image(tmp_path)

    with session() as s:
        assert await loop_module._vision_notes(s, [], rel, True) == ({}, None)

    assert fake.requests == []


# ---------- 端到端：转述真的进了 chat 的 messages ----------


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_file = tmp_path / "vision-e2e.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "UPLOAD_PENDING_DIR", tmp_path / "uploads" / "pending")
    monkeypatch.setattr(config, "TTS_CACHE_DIR", tmp_path / "tts_cache")
    app = create_app()
    with TestClient(app) as client:
        client.patch(
            "/api/settings",
            json={
                "purposes": {
                    # 9 端口永远是"拒绝连接"：定时热身任务不会真出网（踩过 fake.local 的坑）
                    "chat": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "chat-model",
                        "api_key": "sk-chat",
                    },
                    "vision": {
                        "follow_chat": False,
                        "base_url": "https://vision.example/v1",
                        "model": "vl-1",
                        "api_key": "sk-vision",
                    },
                }
            },
        )
        yield app, client


def _make_friend(client) -> str:
    response = client.post("/api/friends", json={"name": "阿月", "personality": "话不多"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client) -> dict:
    response = client.post(
        "/api/uploads/image", files={"file": ("图.png", _png(), "image/png")}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_chat_model_receives_the_transcript(app_client, monkeypatch):
    app, client = app_client
    fake = _patch_vision(monkeypatch, _FakeVision())
    provider = FakeProvider()
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    friend_id = _make_friend(client)
    info = _upload(client)

    client.post(
        f"/api/friends/{friend_id}/messages",
        json={"content": "这张图里有什么？", "image_id": info["id"]},
    )

    # 当轮：图不进上下文，但**转述进**
    last = provider.snapshots[0][-1]
    assert isinstance(last["content"], str), "看不了图的模型不该收到图片 parts"
    assert "帮你转述了这张图" in last["content"]
    assert TRANSCRIPT in last["content"]
    assert len(fake.requests) == 1

    # 下一轮：历史里的图走缓存（不再联网），占位里带上转述
    provider.snapshots.clear()
    client.post(f"/api/friends/{friend_id}/messages", json={"content": "那它在干嘛？"})
    joined = json.dumps(provider.snapshots[0], ensure_ascii=False)
    assert "别人把这张图转述给我了" in joined
    assert TRANSCRIPT in joined
    assert len(fake.requests) == 1, "历史图不该再联网"


def test_vision_failure_still_answers(app_client, monkeypatch):
    """视觉模型挂了 → 她照样回话，只是如实说"看不到"（D83 兜底）。"""
    app, client = app_client
    _patch_vision(monkeypatch, _FakeVision(error=httpx.ConnectError("连不上")))
    provider = FakeProvider()
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    friend_id = _make_friend(client)
    info = _upload(client)

    response = client.post(
        f"/api/friends/{friend_id}/messages",
        json={"content": "这张图里有什么？", "image_id": info["id"]},
    )

    assert response.status_code == 200, response.text
    last = provider.snapshots[0][-1]
    assert "看不到图片内容" in last["content"]
