"""v3.5（D151）：**按朋友选聊天模型**。

盯五件事：

1. 预设（`settings.llm_bases`）按名字取得出来；**找不到 / 没填全给人话错误**
2. `friends.llm_base` 能存能读；空字符串 = 跟随全局
3. 发消息时**把这个朋友选的名字交给 provider 工厂**（没选 = 和以前一模一样）
4. 预设配错 → 明确报错（`LLM_NOT_CONFIGURED` + 人话），**不静默回落**
5. **看不看得见图跟着模型走**：预设声明了 vision 才 inline 图
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.api.deps import get_provider_factory
from backend.app import create_app
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.llm.errors import LLMConfigError
from backend.llm.factory import resolve_base_config
from backend.models.friend import Friend
from backend.models.settings import Settings
from backend.services import settings_service


class FakeProvider:
    """够用的假 provider：吐一句就完事。"""

    def __init__(self):
        self.config = ProviderConfig(
            name="fake",
            base_url="http://fake.local/v1",
            api_key="sk-fake",
            model="fake-model",
            capabilities=ProviderCapabilities(native_tool_calls=False, reasoning=False),
        )

    async def chat_stream(self, messages, tools=None, **kwargs):
        yield {"type": "content", "delta": "在。"}
        yield {"type": "done"}

    async def health_check(self):
        return True


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_file = tmp_path / "friend-model.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    app = create_app()
    with TestClient(app) as client:
        yield app, client


def _make_friend(client) -> str:
    r = client.post("/api/friends", json={"name": "色图生成器", "personality": "听话。"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _set_bases(client, bases: list[dict]) -> None:
    r = client.patch("/api/settings", json={"llm_bases": bases})
    assert r.status_code == 200, r.text


# ---------- 1. 预设解析 ----------


def test_resolve_base_config_finds_it():
    bases = json.dumps(
        [
            {
                "name": "nsfw",
                "base_url": "https://nano-gpt.com/api/v1",
                "model": "qwen/qwen3.8-27b-uncensored",
                "api_key": "sk-x",
                "capabilities": {"vision": True},
            }
        ]
    )
    config_row = resolve_base_config(bases, "nsfw")
    assert config_row.base_url == "https://nano-gpt.com/api/v1"
    assert config_row.model == "qwen/qwen3.8-27b-uncensored"


def test_resolve_base_config_says_what_to_do_when_missing_or_incomplete():
    with pytest.raises(LLMConfigError) as missing:
        resolve_base_config(json.dumps([{"name": "nsfw", "base_url": "x", "model": "y"}]), "typo")
    assert "找不到" in str(missing.value)
    assert "跟随全局" in str(missing.value)

    with pytest.raises(LLMConfigError) as incomplete:
        resolve_base_config(json.dumps([{"name": "nsfw", "base_url": "", "model": ""}]), "nsfw")
    assert "还差一点" in str(incomplete.value)


# ---------- 2/5. 看图能力跟着模型走 ----------


def _row(bases: list[dict], *, follow_chat: bool = True) -> Settings:
    return Settings(
        id=1,
        llm_bases=json.dumps(bases, ensure_ascii=False),
        purposes=json.dumps(
            {
                "chat": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
                "vision": {"follow_chat": follow_chat},
            }
        ),
        updated_at="2026-09-23T00:00:00+00:00",
    )


def test_vision_for_friend_follows_the_chosen_base():
    row = _row([{"name": "blind", "base_url": "x", "model": "y", "capabilities": {"vision": False}}])
    blind = Friend(id="f1", name="a", personality="", custom_instructions="", llm_base="blind")
    assert settings_service.vision_for_friend(row, blind) is False

    row2 = _row([{"name": "seer", "base_url": "x", "model": "y", "capabilities": {"vision": True}}])
    seer = Friend(id="f2", name="b", personality="", custom_instructions="", llm_base="seer")
    assert settings_service.vision_for_friend(row2, seer) is True

    # 没选预设 → 沿用全局「视觉跟随聊天」
    plain = Friend(id="f3", name="c", personality="", custom_instructions="", llm_base=None)
    assert settings_service.vision_for_friend(row2, plain) is True
    assert settings_service.vision_for_friend(_row([], follow_chat=False), plain) is False


# ---------- 3. 发消息时把预设名交给工厂 ----------


async def test_send_message_hands_the_friends_preset_to_the_factory(app_client):
    app, client = app_client
    fid = _make_friend(client)
    _set_bases(
        client,
        [
            {"name": "nsfw", "base_url": "https://nano-gpt.com/api/v1", "model": "qwen/x"},
            {"name": "smart", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
        ],
    )
    assert client.patch(f"/api/friends/{fid}", json={"llm_base": "nsfw"}).status_code == 200

    seen: list[str | None] = []

    def factory(base_name: str | None = None):
        seen.append(base_name)
        return FakeProvider()

    app.dependency_overrides[get_provider_factory] = lambda: factory
    assert client.post(f"/api/friends/{fid}/messages", json={"content": "在吗"}).status_code == 200
    assert seen == ["nsfw"], "发消息必须把朋友选的预设名交给工厂"


async def test_send_message_without_preset_keeps_the_old_call_shape(app_client):
    """没选预设 = 老行为：工厂**不带参数**调用（老测试的 0 参 lambda 也还能用）。"""
    app, client = app_client
    fid = _make_friend(client)
    calls: list[tuple] = []

    def factory():  # 老形状：0 参
        calls.append(())
        return FakeProvider()

    app.dependency_overrides[get_provider_factory] = lambda: factory
    assert client.post(f"/api/friends/{fid}/messages", json={"content": "在吗"}).status_code == 200
    assert calls == [()]


# ---------- 4. 配错要说话 ----------


def test_broken_preset_errors_instead_of_silently_falling_back(app_client):
    _app, client = app_client
    fid = _make_friend(client)
    # 全局聊天配好（DeepSeek），但这个朋友指的是一个**没填全**的预设
    r = client.patch(
        "/api/settings",
        json={
            "purposes": {
                "chat": {
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "api_key": "sk-deepseek",
                }
            },
            "llm_bases": [{"name": "nsfw", "base_url": "", "model": ""}],
        },
    )
    assert r.status_code == 200, r.text
    assert client.patch(f"/api/friends/{fid}", json={"llm_base": "nsfw"}).status_code == 200

    r = client.post(f"/api/friends/{fid}/messages", json={"content": "在吗"})
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "LLM_NOT_CONFIGURED"
    assert "nsfw" in body["message"] and "还差一点" in body["message"]


# ---------- 2. 存与读 ----------


def test_friend_llm_base_round_trips(app_client):
    _app, client = app_client
    fid = _make_friend(client)
    _set_bases(client, [{"name": "nsfw", "base_url": "https://x/v1", "model": "m"}])

    assert client.get(f"/api/friends/{fid}").json()["llm_base"] is None
    saved = client.patch(f"/api/friends/{fid}", json={"llm_base": "nsfw"}).json()
    assert saved["llm_base"] == "nsfw"
    assert client.get(f"/api/friends/{fid}").json()["llm_base"] == "nsfw"


def test_voice_examples_round_trip(app_client):
    """V6.0-2（D298）：声音样本能存能读；空数组 = 清空。"""
    _app, client = app_client
    r = client.post(
        "/api/friends",
        json={
            "name": "汐音",
            "personality": "话少。",
            "voice_examples": ["行，不排了。", "你别把话收得那么快。"],
        },
    )
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    assert r.json()["voice_examples"] == ["行，不排了。", "你别把话收得那么快。"]

    saved = client.patch(
        f"/api/friends/{fid}",
        json={"voice_examples": ["在。", "先这样。"]},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["voice_examples"] == ["在。", "先这样。"]

    cleared = client.patch(f"/api/friends/{fid}", json={"voice_examples": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["voice_examples"] == []
    # 空字符串 = 跟随全局（存 NULL，而不是空串）
    cleared = client.patch(f"/api/friends/{fid}", json={"llm_base": ""}).json()
    assert cleared["llm_base"] is None
