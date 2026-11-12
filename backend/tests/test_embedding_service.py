"""v3.46（D231）验收：本地向量检索（关键词 ∪ 向量）。

**不加载真模型**（那要下 92MB）：把 `embed_texts` 换成确定性的假向量，只验机制
（入库 / 回填 / 余弦排序 / 阈值 / 去重 / 降级）。真模型的中文效果是另外手工验的。
"""

import json
from datetime import datetime, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.models import Friend, Memory, MemoryEmbedding
from backend.services import embedding_service, memory_service, settings_service
from backend.services.actor import Actor


@pytest.fixture()
def friend_id(tmp_path, monkeypatch):
    db_file = tmp_path / "embed.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as s:
        s.add(Friend(id="f1", name="汐音", created_at=now, updated_at=now))
        row = settings_service.get(s)
        row.purposes = json.dumps(
            {
                "embedding": {
                    "enabled": True,
                    "provider": "local",
                    "model": "fake-embedding-model",
                    "dim": 3,
                    "min_similarity": 0.45,
                }
            }
        )
        s.commit()
    embedding_service.invalidate()
    return "f1"


def _fake_embed(monkeypatch):
    """按关键词分桶的假向量：'408/王道/买资料' 同桶，'牙齿' 另一桶，其余第三桶。"""

    def fake(texts, *, query=False, config_override=None):  # noqa: ANN001, ARG001
        def vector(text: str) -> list[float]:
            if any(word in text for word in ("408", "王道", "买资料")):
                return [1.0, 0.0, 0.0]
            if "牙齿" in text:
                return [0.0, 1.0, 0.0]
            return [0.0, 0.0, 1.0]

        return [vector(text) for text in texts]

    monkeypatch.setattr(embedding_service, "embed_texts", fake)


def _save(session, content, *, layer="long_term") -> Memory:
    memory, _ = memory_service.save(session, Actor.AGENT, "f1", content=content, layer=layer)
    return memory


def test_backfill_then_vector_search_ranks(friend_id, monkeypatch):
    _fake_embed(monkeypatch)
    with db.session_scope() as s:
        _save(s, "梦汐在学 408，参考书是王道")
        _save(s, "用户明天要去看牙齿")
        _save(s, "今天天气不错")
        assert embedding_service.backfill(s) == 3
        assert embedding_service.backfill(s) == 0  # 幂等：都齐了不再算

    with db.session_scope() as s:
        hits = embedding_service.search(s, friend_id, "我上次问的那个买资料的事")
        assert hits, "语义改写那条应该被向量召回"
        assert "408" in hits[0][0].content
        assert hits[0][1] == pytest.approx(1.0)
        # 无关的（天气桶）过不了阈值
        assert all("天气" not in memory.content for memory, _ in hits)


def test_content_change_triggers_reembed(friend_id, monkeypatch):
    _fake_embed(monkeypatch)
    with db.session_scope() as s:
        memory = _save(s, "用户明天要去看牙齿")
        embedding_service.backfill(s)
        assert embedding_service.pending(s) == []
        memory_service.update(s, Actor.AGENT, memory.id, content="梦汐在学 408")
        assert [m.id for m in embedding_service.pending(s)] == [memory.id]
        assert embedding_service.backfill(s) == 1


def test_vector_search_excludes_short_term_and_play_style(friend_id, monkeypatch):
    _fake_embed(monkeypatch)
    with db.session_scope() as s:
        _save(s, "临时提到 408", layer="short_term")
        memory_service.save_play_style(s, Actor.AGENT, friend_id, role="女友", text="408 演法")
        embedding_service.backfill(s)

    with db.session_scope() as s:
        assert embedding_service.search(s, friend_id, "408") == []
        assert embedding_service.vector_hits(s, friend_id, "408") == []


def test_embedding_unavailable_degrades_to_empty(friend_id, monkeypatch):
    monkeypatch.setattr(embedding_service, "embed_texts", lambda *a, **k: None)
    with db.session_scope() as s:
        _save(s, "梦汐在学 408")
        assert embedding_service.backfill(s) == 0
        assert embedding_service.search(s, friend_id, "408") == []


def test_hybrid_recall_uses_both_paths(friend_id, monkeypatch):
    _fake_embed(monkeypatch)
    with db.session_scope() as s:
        _save(s, "梦汐在学 408，参考书是王道")  # 字面 + 语义都能命中
        _save(s, "用户明天要去看牙齿")
        embedding_service.backfill(s)

    with db.session_scope() as s:
        # 字面命中（关键词那条路）
        literal = embedding_service.hybrid_recall(s, friend_id, "408")
        assert any("408" in m.content for m in literal)
        # 语义命中：字面完全不沾，但向量能捞回来（这就是 D231 的意义）
        semantic = embedding_service.hybrid_recall(s, friend_id, "上次那个买资料的事")
        assert any("408" in m.content for m in semantic)
        # 去重：同一条不会出现两次
        assert len({m.id for m in semantic}) == len(semantic)


def test_embedding_cascades_with_memory_hard_delete(friend_id, monkeypatch):
    _fake_embed(monkeypatch)
    with db.session_scope() as s:
        memory = _save(s, "梦汐在学 408")
        embedding_service.backfill(s)
        memory_id = memory.id
    with db.session_scope() as s:
        assert s.query(MemoryEmbedding).count() == 1
        s.delete(s.get(Memory, memory_id))  # 物理删记忆（30 天清理那条路）
    with db.session_scope() as s:
        assert s.query(MemoryEmbedding).count() == 0, "向量没跟着记忆一起删 = orphan"


def test_status_and_rebuild(friend_id, monkeypatch):
    """设置页那张卡的后端：状态 + 一键重建（D231）。"""
    _fake_embed(monkeypatch)
    from backend.api import embedding as embedding_api

    with db.session_scope() as s:
        _save(s, "梦汐在学 408")
    with db.session_scope() as s:
        status = embedding_api._status(s)
        assert status["enabled"] is True  # 默认开
        assert status["indexed"] == 0 and status["total"] == 1
        assert status["model"] == "fake-embedding-model" and status["dim"] == 3

    assert embedding_api._rebuild_all() == 1  # 手动重建
    with db.session_scope() as s:
        assert embedding_api._status(s)["indexed"] == 1
        assert embedding_api._rebuild_all() == 0  # 再点一次：没东西可补


def test_missing_local_model_does_not_auto_download(tmp_path):
    cfg = embedding_service.config_from_dict(
        {
            "enabled": True,
            "provider": "local",
            "model": "jinaai/jina-embeddings-v2-base-zh-quantized",
            "dim": 768,
            "cache_dir": str(tmp_path / "empty-cache"),
            "allow_download": False,
        }
    )
    assert embedding_service.warm(cfg) is False
    assert embedding_service.embed_texts(["不会联网下载"], config_override=cfg) is None


def test_remote_embedding_storage_identity():
    cfg = embedding_service.config_from_dict(
        {
            "enabled": True,
            "provider": "openai",
            "base_url": "https://example.com/v1",
            "model": "text-embedding-3-small",
            "dim": 1536,
        }
    )
    assert cfg.storage_model == "openai:https://example.com/v1:text-embedding-3-small"


def test_remote_embedding_uses_openai_embeddings_shape(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"index": 1, "embedding": [0.2, 0.3]},
                    {"index": 0, "embedding": [0.1, 0.4]},
                ]
            }

    class FakeClient:
        def __init__(self, timeout):  # noqa: ANN001, ARG002
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, headers, json):  # noqa: ANN001
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setattr(embedding_service.httpx, "Client", FakeClient)
    cfg = embedding_service.config_from_dict(
        {
            "enabled": True,
            "provider": "openai",
            "base_url": "https://example.com/v1",
            "model": "text-embedding-3-small",
            "api_key": "sk-test",
            "dim": 2,
        }
    )
    vectors = embedding_service.embed_texts(["a", "b"], config_override=cfg)
    assert vectors == [[0.1, 0.4], [0.2, 0.3]]
    assert captured["url"] == "https://example.com/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "text-embedding-3-small"


def test_gemini_embedding_uses_batch_embed_contents(monkeypatch):
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"embeddings": [{"values": [0.1, 0.2]}, {"values": [0.3, 0.4]}]}

    class FakeClient:
        def __init__(self, timeout):  # noqa: ANN001, ARG002
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, params, json):  # noqa: ANN001
            captured.update(url=url, params=params, json=json)
            return FakeResponse()

    monkeypatch.setattr(embedding_service.httpx, "Client", FakeClient)
    cfg = embedding_service.config_from_dict(
        {
            "enabled": True,
            "provider": "openai",
            "protocol": "gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "model": "gemini-embedding-001",
            "api_key": "gem-key",
            "dim": 2,
        }
    )
    vectors = embedding_service.embed_texts(["a", "b"], config_override=cfg)
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert captured["url"].endswith("/models/gemini-embedding-001:batchEmbedContents")
    assert captured["params"] == {"key": "gem-key"}
    assert captured["json"]["requests"][0]["model"] == "models/gemini-embedding-001"
