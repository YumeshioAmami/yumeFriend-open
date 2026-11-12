"""D136：慢工具（搜网 / 知识库 / 文件 IO）扔线程里跑，**别堵事件循环**。

堵事件循环的后果很具体：一轮搜网 2~6 秒，这期间后端的 SSE 推不出去、看屏不走、
语音不出、别的请求也全部排队。
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest

import backend.config as config
import backend.db as db
from backend.agent import tools
from backend.agent.tools import ToolSpec
from backend.models import Friend


@pytest.fixture()
def friend_id(tmp_path, monkeypatch):
    db_file = tmp_path / "tool_async.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    from backend.db import Base, get_engine

    Base.metadata.create_all(get_engine())
    now = datetime.now(timezone.utc).isoformat()
    with db.session_scope() as s:
        s.add(Friend(id="f1", name="阿月", created_at=now, updated_at=now))
    return "f1"


def _call(name: str, **arguments) -> dict:
    return {"id": f"tool_{name}", "name": name, "arguments": arguments}


# ---------- manifest：哪些工具是"慢"的 ----------


def test_slow_tools_are_marked_blocking():
    for name in (
        "web_search",
        "search_knowledge",
        "list_dir",
        "read_file",
        "write_file",
        "move_path",
        "delete_path",
    ):
        assert tools._BY_NAME[name].blocking is True, f"{name} 该标 blocking"

    # 画图走 async_handler（D117）→ 别再重复标
    assert tools._BY_NAME["draw_image"].blocking is False
    # 纯 DB 的记忆 / 提醒工具都是几十毫秒的事 → 不值得开线程
    for name in ("save_memory", "recall_memory", "add_schedule", "look_image"):
        assert tools._BY_NAME[name].blocking is False, f"{name} 不该标 blocking"
    assert not [tool.name for tool in tools.TOOLS if tool.blocking and tool.async_handler]


# ---------- 真的换线程了吗 ----------


async def test_blocking_tool_runs_in_a_worker_thread(monkeypatch, friend_id):
    seen: dict = {}

    def handler(session, friend_id, args):  # noqa: ANN001, ARG001
        seen["thread"] = threading.current_thread()
        return {"hi": 1}

    monkeypatch.setitem(
        tools._BY_NAME, "fake_slow", ToolSpec("fake_slow", "假慢工具", handler, blocking=True)
    )
    record = await tools.execute_async(None, friend_id, _call("fake_slow"))

    assert record["ok"] is True
    assert record["result"] == {"hi": 1}
    assert seen["thread"] is not threading.main_thread()


async def test_fast_tool_stays_on_the_loop_thread(monkeypatch, friend_id):
    seen: dict = {}

    def handler(session, friend_id, args):  # noqa: ANN001, ARG001
        seen["thread"] = threading.current_thread()
        return {}

    monkeypatch.setitem(
        tools._BY_NAME, "fake_fast", ToolSpec("fake_fast", "假快工具", handler)
    )
    await tools.execute_async(None, friend_id, _call("fake_fast"))

    assert seen["thread"] is threading.main_thread(), "快工具不该白白开线程"


async def test_blocking_tool_does_not_stall_the_event_loop(monkeypatch, friend_id):
    def handler(session, friend_id, args):  # noqa: ANN001, ARG001
        time.sleep(0.4)
        return {"slept": True}

    monkeypatch.setitem(
        tools._BY_NAME, "fake_sleeper", ToolSpec("fake_sleeper", "睡 0.4 秒", handler, blocking=True)
    )

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)  # 先让心跳跑起来
    record = await tools.execute_async(None, friend_id, _call("fake_sleeper"))
    task.cancel()

    assert record["ok"] is True
    # 0.4 秒 / 每 0.01 秒一跳 ≈ 40 跳；被堵住的话只有 0~1 跳
    assert ticks >= 10, f"事件循环被堵住了（心跳只跳了 {ticks} 次）"


async def test_blocking_tool_error_is_still_normalized(monkeypatch, friend_id):
    """扔进线程也一样：工具炸了只能是 error 结果，不能带崩整轮对话。"""

    def handler(session, friend_id, args):  # noqa: ANN001, ARG001
        raise ValueError("参数不对")

    monkeypatch.setitem(
        tools._BY_NAME, "fake_boom", ToolSpec("fake_boom", "假炸工具", handler, blocking=True)
    )
    record = await tools.execute_async(None, friend_id, _call("fake_boom"))

    assert record["ok"] is False
    assert "参数不对" in record["error"]


async def test_real_session_can_be_used_from_a_worker_thread(friend_id):
    """前置条件：SQLite 会话能跨线程用（SQLAlchemy 对文件库默认 check_same_thread=False）。"""
    with db.session_scope() as session:
        record = await asyncio.to_thread(
            tools.execute, session, friend_id, _call("recall_memory", query="不存在的东西")
        )

    assert record["ok"] is True
    assert record["result"]["count"] == 0


async def test_look_screen_hands_back_the_vision_one_liner(friend_id, monkeypatch):
    """look_screen（D190）= 本地截一张 + 视觉模型写成一句话 → **回给模型的是那句话**（不是图）。"""
    from backend.services import screen_service

    monkeypatch.setattr(screen_service, "capture", lambda: b"\x89PNG-fake")

    async def fake_observe(png, *, session=None):
        assert png == b"\x89PNG-fake"
        return "他正在写代码"

    monkeypatch.setattr(screen_service, "observe", fake_observe)

    record = await tools.execute_async(None, friend_id, _call("look_screen"))

    assert record["ok"] is True
    assert record["result"]["observation"] == "他正在写代码"
