"""T2.0-7 搜网：解析 / 缓存 / 失败要人话 / 来源落到消息上（D92）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.agent import tools as tools_module
from backend.api.deps import get_provider_factory
from backend.app import create_app
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.services import web_search_service
from backend.services.errors import ServiceError

FAKE_BING = """<html><body>
<li class="b_algo"><h2><a href="https://example.com/a">第一条 &amp; 标题</a></h2>
  <p>第一段摘要。</p></li>
<li class="b_algo"><h2><a href="https://example.com/b">第二条</a></h2>
  <div class="b_caption"><p>第二段摘要。</p></div></li>
</body></html>"""

PAGE = "<html><head><style>p{color:red}</style><script>var x=1</script></head><body><p>正文内容。</p></body></html>"


class _Resp:
    def __init__(self, text: str = "", status: int = 200, ctype: str = "text/html") -> None:
        self.text = text
        self.status_code = status
        self.headers = {"content-type": ctype}


class FakeProvider:
    """假模型：第一轮调 web_search，第二轮把结果说出来。"""

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
        if len(self.snapshots) == 1:
            yield {
                "type": "content",
                "delta": '<tool>{"name":"web_search","arguments":{"query":"OpenAI 最新"}}</tool>',
            }
        else:
            yield {"type": "content", "delta": "我查到了，是 [1] 说的那样。"}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_file = tmp_path / "search.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    web_search_service.clear_cache()
    app = create_app()
    with TestClient(app) as client:
        yield app, client


def _fake_get(counter: list[str]):
    def _inner(url, *, proxy="", params=None, timeout=0.0):  # noqa: ANN001
        counter.append(url)
        if url == web_search_service.SEARCH_URL:
            return _Resp(FAKE_BING)
        return _Resp(PAGE)

    return _inner


# ---------- 解析 ----------


def test_parse_bing_pulls_out_title_url_snippet():
    results = web_search_service.parse_bing(FAKE_BING)
    assert [item["url"] for item in results] == ["https://example.com/a", "https://example.com/b"]
    assert results[0]["title"] == "第一条 & 标题"  # 实体反转义
    assert "第一段摘要" in results[0]["snippet"]
    assert "第二段摘要" in results[1]["snippet"]


def test_parse_bing_respects_the_limit():
    assert len(web_search_service.parse_bing(FAKE_BING, limit=1)) == 1


def test_strip_html_drops_script_and_style():
    text = web_search_service.strip_html(PAGE)
    assert "正文内容" in text
    assert "var x" not in text and "color:red" not in text


# ---------- 搜索 / 缓存 ----------


def test_search_numbers_results_and_keeps_excerpts(monkeypatch):
    counter: list[str] = []
    monkeypatch.setattr(web_search_service, "_get", _fake_get(counter))

    payload = web_search_service.search("openai")
    assert [item["no"] for item in payload["results"]] == [1, 2]
    assert payload["cached"] is False
    assert payload["excerpts"] and "正文内容" in payload["excerpts"][0]["text"]
    assert counter[0] == web_search_service.SEARCH_URL


def test_same_question_is_not_searched_twice(monkeypatch):
    """同一问题短时间内不重复请求（其实就是缓存在挡）。"""
    counter: list[str] = []
    monkeypatch.setattr(web_search_service, "_get", _fake_get(counter))

    web_search_service.search("OpenAI 最新")
    calls_after_first = len(counter)
    assert calls_after_first >= 1

    again = web_search_service.search("openai 最新")  # 大小写不同也是同一问题
    assert again["cached"] is True
    assert len(counter) == calls_after_first, "第二次不该再打网络"


def test_empty_and_unreachable_are_human_errors(monkeypatch):
    monkeypatch.setattr(web_search_service, "_get", lambda *a, **k: _Resp("<html></html>"))
    with pytest.raises(ServiceError) as info:
        web_search_service.search("什么都没有")
    assert info.value.code == "SEARCH_EMPTY"
    assert "查不到" in info.value.message

    def _boom(*_a, **_k):
        raise ServiceError("SEARCH_UNREACHABLE", "连不上搜索（可能是没网，或者梯子断了）", 503)

    monkeypatch.setattr(web_search_service, "_get", _boom)
    with pytest.raises(ServiceError) as info2:
        web_search_service.search("随便")
    assert info2.value.code == "SEARCH_UNREACHABLE"


# ---------- 工具层 ----------


def test_tool_gives_the_model_a_readable_error_when_offline(app_client, monkeypatch):
    """查不到时**不许编**：工具返回 ok=false + 人话，模型会照实说。"""
    _app, _client = app_client

    def _boom(*_a, **_k):
        raise ServiceError("SEARCH_UNREACHABLE", "连不上搜索（可能是没网）—— 查不到就如实说", 503)

    monkeypatch.setattr(web_search_service, "search", _boom)
    with db.session_scope() as session:
        record = tools_module.execute(
            session, "f1", {"name": "web_search", "arguments": {"query": "今天的新闻"}}
        )
    assert record["ok"] is False
    assert "查不到就如实说" in record["error"]


def test_tool_needs_a_query(app_client):
    _app, _client = app_client
    with db.session_scope() as session:
        record = tools_module.execute(session, "f1", {"name": "web_search", "arguments": {}})
    assert record["ok"] is False
    assert "query" in record["error"]


def test_sources_land_on_the_message(app_client, monkeypatch):
    """整轮串起来：她调 web_search → 回答 → 消息体里带 sources（前端渲染小链接）。"""
    app, client = app_client
    monkeypatch.setattr(web_search_service, "_get", _fake_get([]))
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
    provider = FakeProvider()
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)

    friend_id = client.post("/api/friends", json={"name": "阿月", "personality": "话不多"}).json()["id"]
    response = client.post(f"/api/friends/{friend_id}/messages", json={"content": "OpenAI 最新模型是啥？"})
    assert response.status_code == 200, response.text

    # 模型拿到的回灌里要有编号和摘要（不然它没法引用）
    follow_up = "".join(str(m.get("content")) for m in provider.snapshots[1])
    assert "example.com/a" in follow_up
    assert '"no": 1' in follow_up

    listed = client.get(f"/api/friends/{friend_id}/messages").json()["messages"]
    assistant = [m for m in listed if m["role"] == "assistant"][-1]
    assert assistant["sources"] == [
        {"no": 1, "title": "第一条 & 标题", "url": "https://example.com/a"},
        {"no": 2, "title": "第二条", "url": "https://example.com/b"},
    ]
    # 用户那条不该有来源
    assert listed[0]["sources"] is None
