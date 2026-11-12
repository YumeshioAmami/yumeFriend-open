"""P0 端到端：真进程 + 真 HTTP + 真 WebSocket（不碰真游戏、不碰 LLM）。

验收（docs/game_adapter.md §10 P0）：
start → snapshot → command → event → stop 全通。
"""

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
import pytest_asyncio

from backend.game.client import AdapterClient, AdapterError
from backend.game.process import AdapterProcess
from backend.game.service import GameService, GameSession

ROOT = Path(__file__).resolve().parents[2]
MOCK_ADAPTER = [sys.executable, str(ROOT / "adapters" / "mock" / "mock_adapter.py")]


@pytest_asyncio.fixture()
async def adapter():
    async with AdapterProcess(MOCK_ADAPTER, ready_timeout=20.0) as client:
        yield client


async def collect_until(events, predicate, timeout: float = 5.0):
    found = []

    async def runner():
        async for event in events:
            found.append(event)
            if predicate(event):
                return

    await asyncio.wait_for(runner(), timeout)
    return found


@pytest.mark.asyncio
async def test_process_health_and_capabilities(adapter):
    health = await adapter.health()
    assert health["ok"] is True
    assert health["protocol_version"] == 1

    caps = await adapter.capabilities()
    assert caps.game == "mock"
    assert "say" in caps.action_names()


@pytest.mark.asyncio
async def test_full_pipeline_start_snapshot_command_event_stop(adapter):
    started = await adapter.start_session({"world": "p0"})
    assert started["state"] == "running"
    assert started["session_id"].startswith("mock-")

    snapshot = await adapter.snapshot()
    assert snapshot["state"] == "running"
    assert snapshot["player"]["name"] == "mock-friend"
    assert snapshot["config"] == {"world": "p0"}

    events = adapter.events()
    result = await adapter.command("say", {"text": "你好"})
    assert result["accepted"] is True
    assert result["task_id"]

    received = await collect_until(events, lambda event: event.type == "task_done")
    types = [event.type for event in received]
    assert "task_done" in types
    assert any(
        event.type == "chat" and event.payload.get("text") == "你好" for event in received
    )

    stopped = await adapter.stop_session()
    assert stopped["ok"] is True


@pytest.mark.asyncio
async def test_early_task_done_is_merged_after_command_returns():
    """D274：task_done 比 /command 响应先到时，任务不能永远停在 running。"""

    class FakeClient:
        async def command(self, action, args):  # noqa: ANN001
            return {"accepted": True, "task_id": "task-1"}

    session = GameSession(id="s1", friend_id="f1", game_kind="mock", state="running")
    session._client = FakeClient()  # type: ignore[assignment]
    session.completed_tasks["task-1"] = {
        "ok": True,
        "result": {"said": "早到的完成"},
    }
    service = GameService()
    service._session = session

    await service.command("say", {"text": "你好"}, origin="user")

    assert session.tasks["task-1"]["status"] == "done"
    assert session.tasks["task-1"]["result"] == {"said": "早到的完成"}
    assert session.tasks["task-1"]["origin"] == "user"
    assert "task-1" not in session.completed_tasks


@pytest.mark.asyncio
async def test_connection_error_marks_session_crashed():
    """适配器进程还活着、但 MC 连接断了：会话不能一直显示 running。"""

    service = GameService()
    session = GameSession(id="s1", friend_id="f1", game_kind="minecraft", state="running")
    service._session = session

    await service._handle_event(
        session,
        SimpleNamespace(
            type="error",
            payload={"message": "连接结束了：socketClosed"},
        ),
    )

    assert session.state == "crashed"
    assert "socketClosed" in (session.error or "")


@pytest.mark.asyncio
async def test_wrong_token_is_rejected(adapter):
    bad = AdapterClient(adapter.base_url, "wrong-token")
    try:
        with pytest.raises(AdapterError) as exc:
            await bad.health()
        assert exc.value.status == 401
    finally:
        await bad.close()


@pytest.mark.asyncio
async def test_unknown_action_returns_human_error(adapter):
    await adapter.start_session({})
    with pytest.raises(AdapterError) as exc:
        await adapter.command("fly_to_the_moon")
    assert exc.value.code == "UNKNOWN_ACTION"
    assert "没有这个动作" in str(exc.value)


@pytest.mark.asyncio
async def test_service_state_machine_and_task_tracking():
    service = GameService(heartbeat_timeout=10.0)
    session = await service.start(
        friend_id="friend-1",
        game_kind="mock",
        command=MOCK_ADAPTER,
        config={"world": "p0"},
    )
    try:
        assert session.state == "running"
        assert session.adapter_alive is True

        status = service.status()
        assert status["state"] == "running"
        assert isinstance(status["started_at"], float)
        assert status["capabilities"]["actions"] == [
            "goto",
            "follow",
            "mode",
            "mine",
            "say",
            "command",
            "place",
            "inspect_blocks",
            "knowledge_query",
            "configure_reflex",
            "plugin_list",
            "plugin_call",
        ]

        result = await service.command("say", {"text": "hello"})
        task_id = result["task_id"]
        assert service.session.tasks[task_id]["status"] == "running"

        for _ in range(100):
            if service.session.tasks[task_id]["status"] == "done":
                break
            await asyncio.sleep(0.02)
        assert service.session.tasks[task_id]["status"] == "done"

        # 事件流里也应该收到了 chat（可能比 task_done 晚一拍）
        for _ in range(100):
            if any(event.type == "chat" for event in service.session.events):
                break
            await asyncio.sleep(0.02)
        assert any(event.type == "chat" for event in service.session.events)
    finally:
        await service.stop()

    assert service.session.state == "ended"
    assert service.session.adapter_alive is False


@pytest.mark.asyncio
async def test_session_ready_callback_runs_before_event_consumer():
    """D280：会话行必须先落库，再开始消费事件（否则 player_near 会撞外键）。"""

    order: list[str] = []

    async def ready(_session):
        order.append("ready")

    service = GameService(heartbeat_timeout=10.0)

    async def fake_consume(_session):
        order.append("events")

    service._consume_events = fake_consume  # type: ignore[assignment]
    session = await service.start(
        friend_id="friend-1",
        game_kind="mock",
        command=MOCK_ADAPTER,
        config={},
        on_session_ready=ready,
    )
    try:
        await asyncio.sleep(0.05)
        assert order[:2] == ["ready", "events"]
    finally:
        await service.stop()
    assert service.status()["adapter_alive"] is False


@pytest.mark.asyncio
async def test_service_rejects_a_second_session():
    service = GameService(heartbeat_timeout=10.0)
    await service.start(friend_id="f", game_kind="mock", command=MOCK_ADAPTER)
    try:
        with pytest.raises(RuntimeError) as exc:
            await service.start(friend_id="f2", game_kind="mock", command=MOCK_ADAPTER)
        assert "只允许一局" in str(exc.value)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_service_command_without_session_is_rejected():
    service = GameService()
    with pytest.raises(RuntimeError) as exc:
        await service.command("say", {"text": "hi"})
    assert "没有进行中的游戏会话" in str(exc.value)
