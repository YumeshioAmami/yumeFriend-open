"""T6.1 验收：日志窗口的数据源（api.md §6 / D31）。"""

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
import backend.logbuffer as logbuffer
from backend.app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "logs.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    logbuffer.clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    logbuffer.clear()


def _put(level: str, message: str, where: str = "test:fn") -> int:
    return logbuffer.add(level=level, message=message, where=where, time_text="2026-09-19T02:00:00+08:00")["seq"]


def test_incremental_read(client):
    """前端每 2 秒拉增量：给 since，只回新的，并带上 next_seq。"""
    first = _put("INFO", "第一条")
    second = _put("INFO", "第二条")

    body = client.get(f"/api/logs?since={first}").json()
    assert [e["seq"] for e in body["items"]] == [second]
    assert body["next_seq"] == second

    assert client.get(f"/api/logs?since={second}").json()["items"] == []


def test_filter_by_level(client):
    _put("INFO", "普通信息")
    _put("WARNING", "警告一条")
    _put("ERROR", "出错了")
    body = client.get("/api/logs?level=ERROR").json()
    assert [e["level"] for e in body["items"]] == ["ERROR"]
    assert client.get("/api/logs?level=胡说").status_code == 422


def test_clear(client):
    _put("INFO", "x")
    assert client.delete("/api/logs").status_code == 204
    assert client.get("/api/logs").json()["items"] == []


def test_ring_buffer_is_bounded():
    """内存不能无限涨：满了就丢最老的（最近 2000 条）。"""
    logbuffer.clear()
    for i in range(logbuffer.RING_SIZE + 50):
        logbuffer.add(level="INFO", message=f"第 {i} 条", where="t:f", time_text="t")
    assert logbuffer.size() == logbuffer.RING_SIZE
    items, _ = logbuffer.snapshot(limit=logbuffer.RING_SIZE)
    assert items[0]["message"] == "第 50 条"  # 最老的 50 条被挤掉了


def test_api_key_is_redacted():
    """D31 第 5 条：API key 永不记 —— 除了调用方自律，这里再加一道正则兜底。"""
    logbuffer.clear()
    fake_key = "sk-" + "nano-" + "0" * 36
    logbuffer.add(
        level="INFO",
        # ⚠️ 这里**只能用假 key** —— 以前写的是真 key（2026-09-23 才发现），
        # 既进了仓库、也进了 git 历史。教训：**任何"看起来像 key"的样例都要现编一个**。
        message=f"用 {fake_key} 调了一下",
        where="t:f",
        time_text="t",
    )
    items, _ = logbuffer.snapshot()
    assert "sk-nano-00000000" not in items[0]["message"]
    assert "sk-***" in items[0]["message"]


def test_loguru_sink_writes_into_the_buffer():
    """真·loguru 消息要能进缓冲（不然窗口里啥都没有）。"""
    from loguru import logger

    logbuffer.clear()
    logger.add(logbuffer.sink, level="DEBUG")
    logger.info("来自 loguru 的一条 {}", "消息")
    items, _ = logbuffer.snapshot()
    assert any("来自 loguru 的一条 消息" in e["message"] for e in items)


def test_diagnostics_text_is_pasteable(client):
    """一键复制诊断信息：版本 / provider / model / 最近错误 / 最近日志（frontend.md §5.3）。"""
    _put("ERROR", "调用 LLM 失败 request_id=abc：Connection error.")
    text = client.get("/api/logs/diagnostics").json()["text"]
    assert "yumeFriend v" in text
    assert "provider=" in text and "model=" in text
    assert "Connection error." in text
    assert "最近日志" in text
