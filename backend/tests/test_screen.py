"""T2.1-5 看屏（D100）+ v2.2（D103）：手动看屏 = 带截图的回合；
看屏现在是**主动消息的第一步**（T2.2-8 的测试在 test_proactive.py 里）。"""

from __future__ import annotations

import io
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import backend.config as config
import backend.db as db
from backend.agent.context import build_system_prompt
from backend.api.deps import get_provider_factory
from backend.app import create_app
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.services import screen_service, vision_service
from backend.services.errors import ServiceError


def _png(width: int = 400, height: int = 300) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 90, 180)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeProvider:
    def __init__(self, text: str = "看到了屏幕。") -> None:
        self._text = text
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
        yield {"type": "content", "delta": self._text}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _clean_screen_state():
    screen_service.reset_state()
    yield
    screen_service.reset_state()


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_file = tmp_path / "screen.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "IMAGE_DIR", tmp_path / "images")
    app = create_app()
    with TestClient(app) as client:
        client.patch(
            "/api/settings",
            json={
                "purposes": {
                    "chat": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "fake-model",
                        "api_key": "sk-fake",
                    },
                    "vision": {"follow_chat": True},
                }
            },
        )
        yield app, client, tmp_path


def _friend(client) -> str:
    return client.post("/api/friends", json={"name": "阿月", "personality": "话不多"}).json()["id"]


# ---------- focus / 最近看到的 ----------


def test_focus_has_a_ttl(monkeypatch):
    screen_service.set_focus("f1")
    assert screen_service.get_focus()["friend_id"] == "f1"
    # 假装心跳已经过了 3 分钟 → 当"没人"
    screen_service._focus["at"] -= screen_service.FOCUS_TTL_SECONDS + 1
    assert screen_service.get_focus()["friend_id"] is None
    assert screen_service.get_focus()["fresh"] is False


def test_latest_observation_expires():
    screen_service.set_latest("f1", "他在写代码")
    seen = screen_service.latest("f1")
    assert seen and "写代码" in seen["text"] and "分钟前" in seen["note"]
    screen_service._latest["f1"]["at"] -= screen_service.OBSERVATION_TTL_SECONDS + 1
    assert screen_service.latest("f1") is None


def test_capture_downscales_and_returns_png(monkeypatch):
    big = Image.new("RGB", (3000, 1500), (10, 10, 10))

    class _Grab:
        @staticmethod
        def grab(**_kwargs):
            return big

    import sys
    import types

    fake_module = types.ModuleType("PIL.ImageGrab")
    fake_module.grab = _Grab.grab
    monkeypatch.setitem(sys.modules, "PIL.ImageGrab", fake_module)
    png = screen_service.capture()
    image = Image.open(io.BytesIO(png))
    assert max(image.size) <= screen_service.CAPTURE_MAX_SIDE


async def test_observe_needs_a_vision_model(app_client):
    app, client, _tmp = app_client
    # 视觉关掉（不跟随聊天 + 没单独配）→ 人话错误
    client.patch("/api/settings", json={"purposes": {"vision": {"follow_chat": False}}})
    with db.session_scope() as session:
        with pytest.raises(ServiceError) as info:
            await screen_service.observe(_png(), session=session)
    assert "看屏要一个能看图的模型" in info.value.message


async def test_observe_returns_one_line(app_client, monkeypatch):
    _app, _client, _tmp = app_client
    provider = FakeProvider("他在看代码")
    monkeypatch.setattr(screen_service, "create_provider_for_purpose", lambda *_a, **_k: provider)
    with db.session_scope() as session:
        text = await screen_service.observe(_png(), session=session)
    assert text == "他在看代码"
    assert provider.snapshots[0][0]["content"][1]["type"] == "image_url"


# ---------- 手动看屏 = 带截图的回合 ----------


def test_manual_look_attaches_the_screenshot(app_client, monkeypatch):
    app, client, tmp_path = app_client
    provider = FakeProvider("你在看代码呀。")
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    monkeypatch.setattr(screen_service, "capture", lambda: _png())
    friend_id = _friend(client)

    response = client.post(
        f"/api/friends/{friend_id}/messages", json={"content": "", "screen": True}
    )
    assert response.status_code == 200, response.text
    last = provider.snapshots[0][-1]
    assert isinstance(last["content"], list) and last["content"][1]["type"] == "image_url"
    assert "看看我现在在干嘛" in last["content"][0]["text"], "空内容要有个默认说法"
    assert list((tmp_path / "images").glob("*.png")), "截图要存下来（以后还能 look_image）"

    listed = client.get(f"/api/friends/{friend_id}/messages").json()["messages"]
    assert listed[0]["image_url"], "这条消息要带上截图"


def test_manual_look_without_vision_is_a_hint(app_client, monkeypatch):
    _app, client, _tmp = app_client
    client.patch("/api/settings", json={"purposes": {"vision": {"follow_chat": False}}})
    friend_id = _friend(client)
    response = client.post(
        f"/api/friends/{friend_id}/messages", json={"content": "看屏幕", "screen": True}
    )
    assert response.status_code == 400
    assert "看屏要一个能看图的模型" in response.json()["error"]["message"]


class _FakeVisionClient:
    """假装 `httpx.AsyncClient`：给"独立视觉模型"那条路一个固定转述（D135）。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def __call__(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc_info) -> bool:
        return False

    async def post(self, url, headers=None, json=None):  # noqa: A002
        self.calls += 1
        text = self.text

        class _Resp:
            status_code = 200

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {"choices": [{"message": {"content": text}}]}

        return _Resp()


def test_manual_look_with_a_separate_vision_model(app_client, monkeypatch):
    """D135 补充：单独配了视觉模型（不跟随聊天）→ 看屏也照样能用（转述进上下文）。"""
    app, client, _tmp = app_client
    vision_service._cache.clear()
    fake = _FakeVisionClient("他在写代码，右边开着编辑器。")
    monkeypatch.setattr(vision_service.httpx, "AsyncClient", fake)
    monkeypatch.setattr(screen_service, "capture", lambda: _png())
    client.patch(
        "/api/settings",
        json={
            "purposes": {
                "vision": {
                    "follow_chat": False,
                    "base_url": "https://vision.example/v1",
                    "model": "vl-1",
                    "api_key": "sk-v",
                }
            }
        },
    )
    provider = FakeProvider("你在看代码呀。")
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    friend_id = _friend(client)

    response = client.post(
        f"/api/friends/{friend_id}/messages", json={"content": "看屏幕", "screen": True}
    )

    assert response.status_code == 200, response.text
    last = provider.snapshots[0][-1]
    assert isinstance(last["content"], str), "看不了图的 chat 模型不该收到截图 parts"
    assert "帮你转述了这张图" in last["content"]
    assert "他在写代码，右边开着编辑器。" in last["content"]
    assert fake.calls == 1


# ---------- 进上下文 / 设置 ----------


def test_screen_note_goes_into_the_system_prompt():
    class _F:
        name = "阿月"
        personality = "话不多"
        custom_instructions = ""
        current_mode = "normal"
        current_role = None
        avatar_description = None

    prompt = build_system_prompt(_F(), [], screen_note="（3 分钟前看到的）\n他在写代码")
    assert "# 你刚看到 TA 的屏幕" in prompt and "在写代码" in prompt
    # 没有就不出现（别留空段）
    assert "# 你刚看到 TA 的屏幕" not in build_system_prompt(_F(), [], screen_note=None)


def test_screen_watch_settings_roundtrip(app_client):
    _app, client, _tmp = app_client
    body = client.get("/api/settings").json()
    assert body["screen_watch_enabled"] is False
    # D103：看屏不再有独立的随机区间（频率跟主动消息共用一个数字）
    assert "screen_watch_min_minutes" not in body and "screen_watch_max_minutes" not in body
    patched = client.patch("/api/settings", json={"screen_watch_enabled": True}).json()
    assert patched["screen_watch_enabled"] is True


def test_focus_endpoints(app_client):
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    client.post("/api/focus", json={"friend_id": friend_id})
    body = client.get("/api/focus").json()
    assert body["friend_id"] == friend_id and body["fresh"] is True
    # D103：focus 只报"现在开着谁"，不再带看屏间隔
    assert body["enabled"] is False and "interval" not in body
