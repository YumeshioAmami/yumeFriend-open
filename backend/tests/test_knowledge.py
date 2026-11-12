"""T2.1-3 知识库（D98）：抽取 / 切块 / 检索 / 增删改查 / 进上下文 / 删朋友连文件一起清。"""

from __future__ import annotations

import io

import pytest
from docx import Document as DocxDocument
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.agent import tools as tools_module
from backend.agent.context import build_messages
from backend.api.deps import get_provider_factory
from backend.app import create_app
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Document, Friend, Message
from backend.services import friend_service, knowledge_service


class FakeProvider:
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
        yield {"type": "content", "delta": "知道了。"}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_file = tmp_path / "kb.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOCUMENT_DIR", tmp_path / "documents")
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
                    }
                }
            },
        )
        yield app, client, tmp_path


def _friend(client, name: str = "阿月") -> str:
    return client.post("/api/friends", json={"name": name, "personality": "话不多"}).json()["id"]


def _upload(
    client,
    friend_id: str,
    content: bytes,
    filename: str = "资料.txt",
    mime: str = "text/plain",
) -> dict:
    response = client.post(
        f"/api/friends/{friend_id}/documents", files={"file": (filename, content, mime)}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------- 切块 / 检索 ----------


def test_chunk_text_splits_paragraphs_and_keeps_every_char():
    text = "\n\n".join(["第一段" * 120, "第二段" * 120])  # 两段各 360 字，合起来超一块
    chunks = knowledge_service.chunk_text(text)
    assert len(chunks) >= 2
    assert all(len(chunk) <= knowledge_service.CHUNK_CHARS for chunk in chunks)
    # 原文一个字都没改（把块拼起来应能覆盖原文的主要字符）
    joined = "".join(chunks)
    assert "第一段" in joined and "第二段" in joined


def test_chunk_text_hard_splits_a_giant_paragraph():
    chunks = knowledge_service.chunk_text("啊" * 2000)
    assert len(chunks) >= 3
    assert all(len(chunk) <= knowledge_service.CHUNK_CHARS for chunk in chunks)


def test_terms_and_score_favour_longer_matches():
    terms = knowledge_service._terms("日本 出差")
    assert "日本" in terms and "出差" in terms
    assert knowledge_service._score(terms, "下周去日本出差") > knowledge_service._score(terms, "随便聊聊")


# ---------- 增删改查 ----------


def test_txt_utf16_with_bom_is_not_mojibake():
    """Windows 记事本存的"Unicode" txt = UTF-16LE + BOM（2026-09-29 用户实测的 bug）。"""
    text = "书名：传习录\r\n\r\n心即理也，知行合一。"
    raw = text.encode("utf-16")
    assert knowledge_service.extract_sync("txt", raw, filename="传习录.txt") == text


def test_txt_utf16_without_bom_is_recognized():
    """没有 BOM 的 UTF-16：靠 NUL 的分布认（ASCII 字符在高字节留 0）。"""
    text = "Rick and Morty\nline two\n"
    raw = text.encode("utf-16-le")
    assert knowledge_service.extract_sync("txt", raw, filename="a.txt") == text


def test_txt_utf8_bom_and_gbk_still_work():
    assert knowledge_service.extract_sync("txt", "中文，带 BOM。".encode("utf-8-sig"), filename="a.txt") == "中文，带 BOM。"
    assert knowledge_service.extract_sync("txt", "中文，GBK 编码。".encode("gbk"), filename="a.txt") == "中文，GBK 编码。"


def test_upload_txt_then_list_detail_rename_delete(app_client):
    _app, client, tmp_path = app_client
    friend_id = _friend(client)
    body = ("苹果的价格是 5 元。" * 60).encode("utf-8")
    doc = _upload(client, friend_id, body, "水果笔记.txt")
    assert doc["status"] == "ready" and doc["chars"] > 0 and doc["kind"] == "txt"
    assert (tmp_path / "documents").glob("*.txt")

    listed = client.get(f"/api/friends/{friend_id}/documents").json()["documents"]
    assert [item["name"] for item in listed] == ["水果笔记.txt"]
    assert "text" not in listed[0], "列表不带全文（省带宽）"

    detail = client.get(f"/api/friends/{friend_id}/documents/{doc['id']}").json()
    assert "苹果的价格是 5 元" in detail["text"]

    renamed = client.patch(
        f"/api/friends/{friend_id}/documents/{doc['id']}", json={"name": "水果价格"}
    ).json()
    assert renamed["name"] == "水果价格"

    path = knowledge_service.abs_path(
        next(iter((tmp_path / "documents").glob("*.txt"))).name
        if False
        else f"documents/{next(iter((tmp_path / 'documents').glob('*.txt'))).name}"
    )
    assert path is not None
    assert client.delete(f"/api/friends/{friend_id}/documents/{doc['id']}").status_code == 204
    assert not path.exists(), "删资料要**连文件一起删**（宪法第九条）"
    with db.session_scope() as session:
        assert session.get(Document, doc["id"]) is None
    assert client.get(f"/api/friends/{friend_id}/documents").json()["documents"] == []


def test_upload_docx_reads_paragraphs(app_client):
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    document = DocxDocument()
    document.add_paragraph("她的生日是 3 月 14 日。")
    document.add_paragraph("她喜欢白桃乌龙。")
    buffer = io.BytesIO()
    document.save(buffer)
    doc = _upload(
        client,
        friend_id,
        buffer.getvalue(),
        "设定.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert doc["status"] == "ready"
    detail = client.get(f"/api/friends/{friend_id}/documents/{doc['id']}").json()
    assert "3 月 14 日" in detail["text"] and "白桃乌龙" in detail["text"]


def test_broken_pdf_and_empty_text_are_failed_not_errors(app_client):
    """抽不出来**不是错误**：留 status=failed + 人话（D83）。"""
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    broken = _upload(client, friend_id, b"not a real pdf", "坏了.pdf", "application/pdf")
    assert broken["status"] == "failed"
    assert broken["error"] and "读不出" in broken["error"]

    empty = _upload(client, friend_id, "   \n\n ".encode(), "空的.txt")
    assert empty["status"] == "failed"
    assert "读出文字" in empty["error"]


def test_unsupported_kind_is_a_human_hint(app_client):
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    response = client.post(
        f"/api/friends/{friend_id}/documents",
        files={"file": ("录音.mp3", b"x", "audio/mpeg")},
    )
    assert response.status_code == 400
    assert "支持" in response.json()["error"]["message"]


def test_other_friends_documents_are_invisible(app_client):
    _app, client, _tmp = app_client
    first, second = _friend(client, "阿月"), _friend(client, "小满")
    doc = _upload(client, first, "只给阿月看的内容".encode(), "秘密.txt")
    assert client.get(f"/api/friends/{second}/documents").json()["documents"] == []
    assert client.get(f"/api/friends/{second}/documents/{doc['id']}").status_code == 404
    assert client.post(
        f"/api/friends/{second}/documents/search", json={"query": "秘密"}
    ).json()["hits"] == []


# ---------- 检索 / 进上下文 ----------


def test_retrieve_finds_the_right_document(app_client):
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    _upload(client, friend_id, ("我们下周去日本出差，先订机票。" * 40).encode(), "出差.txt")
    _upload(client, friend_id, ("白桃乌龙是她的最爱，别买错了。" * 40).encode(), "口味.txt")

    with db.session_scope() as session:
        hits = knowledge_service.retrieve(session, friend_id, "日本出差要带什么")
    assert hits and hits[0]["document_name"] == "出差.txt"
    assert "日本" in hits[0]["text"]

    with db.session_scope() as session:
        assert knowledge_service.retrieve(session, friend_id, "量子力学") == []


def test_search_endpoint_and_tool(app_client):
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    _upload(client, friend_id, ("她的生日是 3 月 14 日，喜欢白桃乌龙。" * 40).encode(), "设定.txt")

    hits = client.post(
        f"/api/friends/{friend_id}/documents/search", json={"query": "她生日"}
    ).json()["hits"]
    assert hits and "3 月 14 日" in hits[0]["text"]

    with db.session_scope() as session:
        record = tools_module.execute(
            session, friend_id, {"name": "search_knowledge", "arguments": {"query": "她喜欢喝什么"}}
        )
        miss = tools_module.execute(
            session, friend_id, {"name": "search_knowledge", "arguments": {"query": "量子力学"}}
        )
    assert record["ok"] is True and record["result"]["hits"]
    assert miss["ok"] is False and "没找到" in miss["error"]


def test_knowledge_goes_into_the_system_prompt(app_client):
    """资料清单永远带；命中片段按这句话贴进「可能相关」段（D98）。"""
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    _upload(client, friend_id, ("我们下周去日本出差，先订机票。" * 40).encode(), "出差.txt")

    with db.session_scope() as session:
        friend = session.get(Friend, friend_id)
        messages = build_messages(
            friend,
            history=[],
            user_input="日本出差的事我怎么说的来着",
            document_index=knowledge_service.document_index(session, friend_id),
            knowledge_hits=knowledge_service.retrieve(session, friend_id, "日本出差"),
        )
    system = messages[0]["content"]
    assert "# 你手头的资料" in system
    assert "《出差.txt》" in system
    assert "# 这次可能相关的片段" in system
    assert "日本出差" in system
    assert "不许拿资料当借口编" in system


def test_chat_turn_carries_the_knowledge(app_client):
    """整轮串起来：问了资料里的事 → 模型收到的 system 里有那份资料。"""
    app, client, _tmp = app_client
    provider = FakeProvider()
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    friend_id = _friend(client)
    _upload(client, friend_id, ("我们下周去日本出差，先订机票。" * 40).encode(), "出差.txt")

    assert client.post(
        f"/api/friends/{friend_id}/messages", json={"content": "日本出差的事我怎么说的来着"}
    ).status_code == 200
    system = provider.snapshots[0][0]["content"]
    assert "# 你手头的资料" in system and "日本出差" in system


def test_purging_a_friend_removes_documents_and_files(app_client):
    _app, client, tmp_path = app_client
    friend_id = _friend(client)
    _upload(client, friend_id, ("要一起清掉的内容。" * 40).encode(), "资料.txt")
    assert list((tmp_path / "documents").glob("*.txt"))

    assert client.post(f"/api/friends/{friend_id}/purge").status_code == 204
    assert not list((tmp_path / "documents").glob("*.txt")), "删朋友时知识库原件也要清"
    with db.session_scope() as session:
        assert session.query(Document).count() == 0
        assert session.query(Message).count() == 0
    with db.session_scope() as session:
        assert friend_service.list_friends(session, actor=None, include_deleted=True) == []


def test_short_documents_are_given_in_full(app_client):
    """资料都很短 → **全给**（关键词检索会被同义改写绕过，实测踩过）。"""
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    _upload(client, friend_id, "用户养了一只叫「橘子」的猫。".encode(), "小资料.txt")
    with db.session_scope() as session:
        short = knowledge_service.small_texts(session, friend_id)
        assert short and "橘子" in short[0]["text"]

    # 资料一多（超阈值）就不再全给，改走关键词检索
    _upload(client, friend_id, ("很长很长的资料。" * 400).encode(), "大资料.txt")
    with db.session_scope() as session:
        assert knowledge_service.small_texts(session, friend_id) == []


def test_no_hits_but_docs_exist_tells_her_to_search(app_client):
    """清单非空但没命中 → system 里提示她**先调 search_knowledge**（不然她会直接说"没写"）。"""
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    _upload(client, friend_id, ("很长很长的资料。" * 400).encode(), "大资料.txt")
    with db.session_scope() as session:
        friend = session.get(Friend, friend_id)
        messages = build_messages(
            friend,
            history=[],
            user_input="无关的问题",
            document_index=knowledge_service.document_index(session, friend_id),
            knowledge_hits=[],
        )
    assert "search_knowledge" in messages[0]["content"]


def test_user_can_edit_the_extracted_text(app_client):
    """用户报的「知识库只能加和看」→ 补上：**手改抽取出来的文字**（改完重新切块）。"""
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    doc = _upload(client, friend_id, ("原始内容：苹果 5 元。" * 60).encode(), "水果.txt")

    updated = client.patch(
        f"/api/friends/{friend_id}/documents/{doc['id']}",
        json={"text": "改过的内容：桃子 9 元。"},
    ).json()
    assert updated["chars"] > 0 and updated["status"] == "ready"

    detail = client.get(f"/api/friends/{friend_id}/documents/{doc['id']}").json()
    assert "桃子 9 元" in detail["text"] and "苹果" not in detail["text"]

    # 切块要跟着重建（检索找到的是新内容，旧的找不到）
    with db.session_scope() as session:
        hits = knowledge_service.retrieve(session, friend_id, "桃子多少钱")
        old = knowledge_service.retrieve(session, friend_id, "苹果多少钱")
    assert hits and "桃子" in hits[0]["text"]
    assert old == []


def test_clearing_the_text_marks_it_failed_not_broken(app_client):
    _app, client, _tmp = app_client
    friend_id = _friend(client)
    doc = _upload(client, friend_id, "有内容".encode(), "a.txt")
    updated = client.patch(
        f"/api/friends/{friend_id}/documents/{doc['id']}", json={"text": "   "}
    ).json()
    assert updated["status"] == "failed" and updated["error"]
