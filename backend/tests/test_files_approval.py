"""T2.1-4 文件操作 + 工具审批（D99）。
口径：**读随便读；写（增删改）要用户点头；设置里可免授权**。"""

from __future__ import annotations

import asyncio
import pathlib
import time

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.agent import tools as tools_module
from backend.api.deps import get_provider_factory
from backend.app import create_app
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.services import approval_service, file_service, settings_service


class FakeSocket:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.payloads.append(payload)


class FakeProvider:
    """假模型：第一轮调一个工具，第二轮收尾。"""

    def __init__(self, tool_block: str) -> None:
        self._tool_block = tool_block
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
            yield {"type": "content", "delta": self._tool_block}
        else:
            yield {"type": "content", "delta": "好，我处理了。"}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}

    async def health_check(self) -> bool:
        return True


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_file = tmp_path / "files.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
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
        yield app, client


@pytest.fixture()
def socket():
    from backend.api.ws import manager

    fake = FakeSocket()
    manager._connections.add(fake)
    yield fake
    manager._connections.discard(fake)


def _friend(client) -> str:
    return client.post("/api/friends", json={"name": "阿月", "personality": "话不多"}).json()["id"]


# ---------- 文件服务 ----------


def test_read_write_list_roundtrip(tmp_path):
    target = tmp_path / "笔记" / "待办.txt"
    result = file_service.write_file(str(target), "第一行\n第二行")
    assert result["created"] is True and target.read_text(encoding="utf-8") == "第一行\n第二行"
    again = file_service.write_file(str(target), "覆盖了")
    assert again["created"] is False and target.read_text(encoding="utf-8") == "覆盖了"

    read = file_service.read_file(str(target))
    assert read["text"] == "覆盖了"
    listing = file_service.list_dir(str(tmp_path))
    assert [item["name"] for item in listing["entries"]] == ["笔记"]
    assert listing["entries"][0]["kind"] == "dir"


def test_read_file_refuses_binary_and_relative(tmp_path):
    binary = tmp_path / "x.bin"
    binary.write_bytes(b"\x00\x01\x02\xff\xfe")
    with pytest.raises(ValueError) as info:
        file_service.read_file(str(binary))
    assert "二进制" in str(info.value)

    with pytest.raises(ValueError) as info2:
        file_service.read_file("notes/a.txt")
    assert "绝对路径" in str(info2.value)


def test_read_file_truncates_big_files(tmp_path):
    path = tmp_path / "big.txt"
    path.write_text("啊" * 9000, encoding="utf-8")
    result = file_service.read_file(str(path), max_chars=1000)
    assert result["truncated"] is True and len(result["text"]) == 1000


def test_read_file_reads_utf16_text_not_binary(tmp_path):
    """Windows 记事本存的"Unicode" txt = UTF-16LE + BOM：**不能当二进制拒读**（2026-09-29）。"""
    path = tmp_path / "unicode.txt"
    path.write_bytes("第一行\n第二行".encode("utf-16"))
    result = file_service.read_file(str(path))
    assert "第一行" in result["text"] and "第二行" in result["text"]


def test_move_and_delete_safety(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("x", encoding="utf-8")
    moved = file_service.move_path(str(source), str(tmp_path / "b.txt"))
    assert moved["ok"] and not source.exists()

    # 目标已存在 → 拒绝（别悄悄覆盖）
    other = tmp_path / "c.txt"
    other.write_text("y", encoding="utf-8")
    with pytest.raises(ValueError) as info:
        file_service.move_path(str(other), str(tmp_path / "b.txt"))
    assert "已经存在" in str(info.value)

    # 删文件可以；删**非空目录**不行（永不递归）
    assert file_service.delete_path(str(tmp_path / "b.txt"))["deleted"] is True
    with pytest.raises(ValueError) as info2:
        file_service.delete_path(str(tmp_path))
    assert "不会递归删" in str(info2.value)


def test_delete_empty_dir_is_allowed(tmp_path):
    empty = tmp_path / "空的"
    empty.mkdir()
    assert file_service.delete_path(str(empty))["kind"] == "dir"
    assert not empty.exists()


def test_danger_note_flags_system_and_own_repo(tmp_path):
    assert "系统目录" in (file_service.danger_note(pathlib.Path(r"C:\Windows\System32")) or "")
    own = config.ROOT_DIR / "backend" / "app.py"
    assert file_service.danger_note(own)
    assert file_service.danger_note(tmp_path / "a.txt") is None


# ---------- 审批 ----------


async def test_ask_pushes_event_and_waits_for_the_user(socket):
    task = asyncio.create_task(
        approval_service.ask(friend_id="f1", friend_name="阿月", tool="write_file", summary="想写文件")
    )
    for _ in range(50):
        if socket.payloads:
            break
        await asyncio.sleep(0.02)
    payload = socket.payloads[-1]
    assert payload["type"] == "approval_request" and payload["friend_name"] == "阿月"
    assert approval_service.resolve(payload["id"], allow=True) is True
    assert await task is True


async def test_ask_times_out_as_denied(socket):
    assert (
        await approval_service.ask(
            friend_id="f1", friend_name="阿月", tool="delete_path", summary="想删东西", timeout=0.1
        )
        is False
    )
    assert approval_service.pending_count() == 0


async def test_resolve_unknown_id_is_false():
    assert approval_service.resolve("nope", allow=True) is False


def test_approval_endpoint(app_client, socket):
    _app, client = app_client
    assert client.post("/api/approvals/nope", json={"allow": True}).json() == {
        "ok": False,
        "allow": True,
    }


# ---------- execute_async：写要点头 ----------


async def test_write_needs_approval_and_denial_does_not_write(app_client, socket, tmp_path):
    _app, _client = app_client
    target = tmp_path / "她的笔记.txt"

    async def resolve_soon() -> None:
        for _ in range(100):
            if socket.payloads:
                approval_service.resolve(socket.payloads[-1]["id"], allow=False)
                return
            await asyncio.sleep(0.02)

    resolver = asyncio.create_task(resolve_soon())
    with db.session_scope() as session:
        record = await tools_module.execute_async(
            session,
            "f1",
            {
                "name": "write_file",
                "arguments": {"path": str(target), "content": "偷偷写的"},
            },
            friend_name="阿月",
        )
    await resolver
    assert record["ok"] is False
    assert "用户没有同意" in record["error"]
    assert not target.exists(), "拒绝之后**绝不能**真写"


async def test_write_runs_after_approval(app_client, socket, tmp_path):
    _app, _client = app_client
    target = tmp_path / "允许写的.txt"

    async def approve_soon() -> None:
        for _ in range(100):
            if socket.payloads:
                approval_service.resolve(socket.payloads[-1]["id"], allow=True)
                return
            await asyncio.sleep(0.02)

    approver = asyncio.create_task(approve_soon())
    with db.session_scope() as session:
        record = await tools_module.execute_async(
            session,
            "f1",
            {"name": "write_file", "arguments": {"path": str(target), "content": "写进去了"}},
            friend_name="阿月",
        )
    await approver
    assert record["ok"] is True, record
    assert target.read_text(encoding="utf-8") == "写进去了"
    # 卡片上要能看清"写什么"
    assert "共 4 字" in socket.payloads[-1]["detail"]


async def test_auto_approve_switch_skips_the_prompt(app_client, socket, tmp_path):
    """用户把「文件改动要点头」关掉 → 免授权直接执行（D99 第 3 条）。"""
    _app, client = app_client
    client.patch("/api/settings", json={"file_write_approval": False})
    target = tmp_path / "免授权.txt"
    with db.session_scope() as session:
        assert settings_service.get(session).file_write_approval == 0
        record = await tools_module.execute_async(
            session,
            "f1",
            {"name": "write_file", "arguments": {"path": str(target), "content": "直接写"}},
        )
    assert record["ok"] is True
    assert target.exists()
    assert not [p for p in socket.payloads if p["type"] == "approval_request"], "免授权就不该弹卡片"


async def test_read_tools_never_ask(app_client, socket, tmp_path):
    _app, _client = app_client
    target = tmp_path / "看看.txt"
    target.write_text("里面的内容", encoding="utf-8")
    with db.session_scope() as session:
        record = await tools_module.execute_async(
            session, "f1", {"name": "read_file", "arguments": {"path": str(target)}}
        )
    assert record["ok"] is True and record["result"]["text"] == "里面的内容"
    assert not socket.payloads, "读操作不该弹审批"


# ---------- 整轮：模型调写工具 → 用户点头 ----------


def test_chat_turn_with_a_write_tool(app_client, socket, tmp_path):
    app, client = app_client
    target = tmp_path / "她写的.txt"
    block = (
        '<tool>{"name":"write_file","arguments":{"path":"' + str(target).replace("\\", "\\\\")
        + '","content":"你好"}}</tool>'
    )
    provider = FakeProvider(block)
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)
    friend_id = _friend(client)

    import threading

    def watch_and_allow() -> None:
        for _ in range(200):
            events = [p for p in socket.payloads if p["type"] == "approval_request"]
            if events:
                approval_service.resolve(events[-1]["id"], allow=True)
                return
            time.sleep(0.05)

    threading.Thread(target=watch_and_allow, daemon=True).start()
    response = client.post(f"/api/friends/{friend_id}/messages", json={"content": "帮我写个文件"})
    assert response.status_code == 200
    assert target.exists(), "用户点了允许 → 文件应该真的写出来"
    with db.session_scope() as session:
        from backend.models import Message

        row = (
            session.query(Message)
            .filter(Message.friend_id == friend_id, Message.role == "assistant")
            .order_by(Message.created_at.desc())
            .first()
        )
    assert "write_file" in (row.tools_used or "")
