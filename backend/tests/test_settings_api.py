"""T2.8 验收：设置读写（api.md §5）。

铁律：**api_key 明文永远不出后端**（api.md §5.1）。
"""

import json

import pytest
from fastapi.testclient import TestClient

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


def _base(name="nanogpt", **over) -> dict:
    data = {
        "name": name,
        "base_url": "https://nano-gpt.com/api/v1",
        "model": "qwen/qwen3.8-27b-uncensored",
        "api_key": "sk-secret-123",
        "proxy": "none",
        "capabilities": {"native_tool_calls": True, "reasoning": True},
    }
    data.update(over)
    return data


def test_my_name_lives_in_the_db_and_defaults_to_wo(client):
    """B9 / D142：沉浸模式里"我"叫什么**存在 DB**（两端一致），默认"我"。

    以前它存在浏览器 localStorage → 电脑和手机各叫各的，改一边另一边不知道。
    """
    assert client.get("/api/settings").json()["my_name"] == "我"

    assert client.patch("/api/settings", json={"my_name": "小北"}).json()["my_name"] == "小北"
    assert client.get("/api/settings").json()["my_name"] == "小北"  # 真的落库了

    # 清空 = 回到默认（不是留一个空名字）
    assert client.patch("/api/settings", json={"my_name": "   "}).json()["my_name"] == "我"

    # 太长会被 schema 拦住（上限 20）
    assert client.patch("/api/settings", json={"my_name": "x" * 21}).status_code == 422


def test_defaults_when_nothing_configured(client):
    body = client.get("/api/settings").json()
    assert body["active_message_frequency"] == 60
    assert body["deep_thinking"] is False
    assert body["show_tool_calls"] is False
    assert body["llm_bases"] == []
    assert body["llm_active"] is None
    assert body["updated_at"]


def test_game_idle_self_start_settings(client):
    """D268：空闲自启动阈值 / 冷却可读写；0 = 关。"""
    body = client.get("/api/settings").json()
    assert body["game_idle_self_start_seconds"] == 180
    assert body["game_idle_self_start_cooldown_seconds"] == 300

    patched = client.patch(
        "/api/settings",
        json={
            "game_idle_self_start_seconds": 60,
            "game_idle_self_start_cooldown_seconds": 600,
        },
    ).json()
    assert patched["game_idle_self_start_seconds"] == 60
    assert patched["game_idle_self_start_cooldown_seconds"] == 600

    off = client.patch(
        "/api/settings",
        json={"game_idle_self_start_seconds": 0},
    ).json()
    assert off["game_idle_self_start_seconds"] == 0


def test_setup_status_tells_frontend_to_guide_user(client):
    assert client.get("/api/settings/setup_status").json() == {
        "needs_setup": True,
        "missing": ["llm_bases", "llm_active"],
    }
    client.patch("/api/settings", json={"llm_bases": [_base()], "llm_active": "nanogpt"})
    assert client.get("/api/settings/setup_status").json() == {
        "needs_setup": False,
        "missing": [],
    }


def test_api_key_never_leaves_the_backend(client):
    client.patch("/api/settings", json={"llm_bases": [_base()], "llm_active": "nanogpt"})
    body = client.get("/api/settings").json()
    dumped = json.dumps(body, ensure_ascii=False)
    assert "sk-secret-123" not in dumped
    assert body["llm_bases"][0]["api_key_set"] is True
    assert "api_key" not in body["llm_bases"][0]
    assert body["llm_active"] == "nanogpt"


def test_patch_without_api_key_keeps_the_old_one(client):
    """前端拿不到明文 key，所以"不回传"必须等于"不改"（api.md §5.2 的实际用法）。"""
    client.patch("/api/settings", json={"llm_bases": [_base()], "llm_active": "nanogpt"})
    client.patch(
        "/api/settings",
        json={"llm_bases": [_base(model="qwen/another-model")]},
    )
    body = client.get("/api/settings").json()
    assert body["llm_bases"][0]["model"] == "qwen/another-model"
    assert body["llm_bases"][0]["api_key_set"] is True, "省略 api_key 时不能把旧 key 弄丢"


def test_explicit_empty_api_key_clears_it(client):
    client.patch("/api/settings", json={"llm_bases": [_base()]})
    client.patch("/api/settings", json={"llm_bases": [_base(api_key="")]})
    assert client.get("/api/settings").json()["llm_bases"][0]["api_key_set"] is False


def test_patch_switches_and_validates_fields(client):
    body = client.patch(
        "/api/settings",
        json={
            "deep_thinking": True,
            "show_tool_calls": True,
            "active_message_frequency": 120,
            "memory_token_budget": 2500,
        },
    ).json()
    assert body["deep_thinking"] is True
    assert body["show_tool_calls"] is True
    assert body["active_message_frequency"] == 120
    assert body["memory_token_budget"] == 2500

    assert client.patch("/api/settings", json={"active_message_frequency": 0}).status_code == 422
    assert client.patch("/api/settings", json={"memory_token_budget": 10}).status_code == 422


def test_deep_thinking_switch_reaches_the_llm_call(client, monkeypatch):
    """设置里的"思考链"开关必须真的影响请求参数（D23）。"""
    from backend.api import deps as api_deps
    from backend.llm.base import ProviderCapabilities, ProviderConfig

    captured = {}

    class FakeProvider:
        config = ProviderConfig(
            name="fake",
            base_url="http://x",
            api_key="k",
            model="m",
            capabilities=ProviderCapabilities(reasoning=True),
        )

        async def chat_stream(self, messages, tools=None, **kwargs):
            captured.update(kwargs)
            yield {"type": "done", "usage": {}, "stop_reason": "stop"}

        async def health_check(self):
            return True

    from backend.app import create_app as _create

    app = _create()
    app.dependency_overrides[api_deps.get_provider_factory] = lambda: (lambda: FakeProvider())
    fid = client.post("/api/friends", json={"name": "阿月"}).json()["id"]

    client.patch("/api/settings", json={"deep_thinking": True})
    with TestClient(app) as c:
        c.post(f"/api/friends/{fid}/messages", json={"content": "hi"})
    assert captured["reasoning_effort"] == "high"


def test_test_provider_returns_not_found_for_unknown_name(client):
    r = client.post("/api/settings/providers/nope/test")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "PROVIDER_NOT_FOUND"


def test_test_provider_reports_failure_instead_of_500(client):
    """连不上时必须回 ok=false + 原因，不能 500（api.md §5.3）。"""
    client.patch(
        "/api/settings",
        json={"llm_bases": [_base(base_url="http://127.0.0.1:9/v1", api_key="k")]},
    )
    body = client.post("/api/settings/providers/nanogpt/test").json()
    assert body["ok"] is False
    assert body["error"]


def test_warmup_settings_roundtrip(client):
    """D87：定时预热的开关 / 间隔能读能写（默认开 + 5 分钟）。"""
    body = client.get("/api/settings").json()
    assert body["warmup_enabled"] is True
    assert body["warmup_interval_minutes"] == 5

    patched = client.patch(
        "/api/settings", json={"warmup_enabled": False, "warmup_interval_minutes": 20}
    ).json()
    assert patched["warmup_enabled"] is False
    assert patched["warmup_interval_minutes"] == 20
    # 重新读一遍（真落库了，不是只回显）
    again = client.get("/api/settings").json()
    assert again["warmup_enabled"] is False
    assert again["warmup_interval_minutes"] == 20


def test_warmup_interval_is_clamped_by_validation(client):
    assert client.patch("/api/settings", json={"warmup_interval_minutes": 0}).status_code == 422
    assert client.patch("/api/settings", json={"warmup_interval_minutes": 999}).status_code == 422
