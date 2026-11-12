"""P3：游戏工具门控 / prompt 注入 / 会话持久化 / 摘要落记忆。"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import backend.config as config
import backend.db as db
from backend.agent import tools as tools_module
from backend.agent.context import build_messages
from backend.app import create_app
from backend.game.manager import GameManager, _inventory_summary
import backend.game.manager as game_manager_module
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.models import Friend, Memory, Message
from backend.services import friend_service, game_store
from backend.services.actor import Actor


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    create_app()  # 跑迁移到 e4c7b1a9d2f6
    yield db_file


def test_inventory_summary_merges_all_stacks():
    summary = _inventory_summary(
        {
            "inventory": [
                {"item": "dirt", "count": 64},
                {"item": "cobblestone", "count": 64},
                {"item": "dirt", "count": 37},
                {"item": "cooked_mutton", "count": 12, "food": True},
            ]
        }
    )
    by_name = {item["item"]: item for item in summary}
    assert by_name["dirt"]["count"] == 101
    assert by_name["cobblestone"]["count"] == 64
    assert by_name["cooked_mutton"]["food"] is True


def test_game_tools_only_appear_when_active():
    inactive = "\n".join(tools_module.prompt_lines("normal", game_active=False))
    assert "game_status" not in inactive
    assert "game_do" not in inactive

    active = "\n".join(tools_module.prompt_lines("normal", game_active=True))
    assert "game_status" in active
    assert "game_do" in active
    assert "game_say" in active
    assert "game_cancel" in active
    assert "game_plan" in active
    assert "game_mode" in active

    names_inactive = {
        item["function"]["name"]
        for item in tools_module.native_tools("normal", game_active=False)
    }
    names_active = {
        item["function"]["name"]
        for item in tools_module.native_tools("normal", game_active=True)
    }
    assert "game_do" not in names_inactive
    assert "game_do" in names_active


def test_context_injects_game_blocks():
    friend = Friend(
        id="f1",
        name="汐音",
        personality="温柔",
        custom_instructions="",
    )
    messages = build_messages(
        friend,
        user_input="在吗",
        game_note="# 你正在和 TA 一起玩 Minecraft\n状态：running",
        recent_games_note="- 2026-10-18 · Minecraft · 一起挖了铁矿",
    )
    system = messages[0]["content"]
    assert "# 最近一起玩过" in system
    assert "一起挖了铁矿" in system
    assert "# 你正在和 TA 一起玩 Minecraft" in system


@pytest.mark.asyncio
async def test_game_tool_executes_through_tool_layer(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "摘要", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)

    with db.session_scope() as session:
        friend = friend_service.create(session, Actor.USER, name="汐音")
        friend_id = friend.id

    await manager.start(friend_id=friend_id, game_kind="mock", config={})
    try:
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "call_1",
                    "name": "game_do",
                    "arguments": {"action": "say", "args": {"text": "在游戏里说一句"}},
                },
            )
        assert record["ok"] is True
        assert record["result"]["task_id"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_manager_persists_session_task_event_and_summary(temp_db):
    async def fake_summarizer(_provider, _transcript):
        return (
            "2026-10-18 和梦汐一起在 mock 里试了任务。",
            [{"content": "她试过 mock 任务", "importance": 0.6}],
        )

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    with db.session_scope() as session:
        friend = friend_service.create(session, Actor.USER, name="汐音")
        friend_id = friend.id

    info = await manager.start(friend_id=friend_id, game_kind="mock", config={})
    session_id = manager.session_id
    assert info["active"] is True
    assert session_id

    result = await manager.command("say", {"text": "你好"})
    assert result["accepted"] is True

    events = []
    for _ in range(100):
        with db.session_scope() as session:
            events = game_store.recent_events(session, session_id, limit=50)
        if any(event.type == "task_done" for event in events):
            break
        await asyncio.sleep(0.02)
    assert any(event.type == "task_done" for event in events)
    # D262：say 不是一步行动，不触发续跑；但最近一步要记下来
    assert manager._last_task is not None
    assert manager._last_task["action"] == "say"
    assert manager._continuation_task is None

    note = await manager.note_for_prompt(friend_id)
    assert "# 你正在和 TA 一起玩" in note
    assert "game_do" in note or "follow" in note

    # P3.1 修复：status() 用的是契约事件（event_id），不能当成 DB 行去读 `.id`
    status = manager.status()
    assert status["recent_events"]
    assert status["recent_events"][0]["id"]

    await manager.stop()

    row = None
    for _ in range(100):
        with db.session_scope() as session:
            row = game_store.get_session(session, session_id)
            if row is not None and row.summary_state == "done":
                break
        await asyncio.sleep(0.05)

    with db.session_scope() as session:
        row = game_store.get_session(session, session_id)
        assert row is not None
        assert row.status == "ended"
        assert row.summary_state == "done"
        assert "mock" in (row.summary or "")
        tasks = session.execute(
            select(Memory).where(Memory.friend_id == friend_id)
        ).scalars().all()
        assert any("游戏·mock" in memory.content for memory in tasks)

    assert "mock" in manager.recent_games_note(friend_id)

    # event_id 去重：同一条再写一次不会多出一行
    with db.session_scope() as session:
        before = len(game_store.recent_events(session, session_id, limit=200))
        existing = game_store.recent_events(session, session_id, limit=1)
        if existing:
            duplicated = game_store.add_event(
                session,
                event_id=existing[0].id,
                session_id=session_id,
                friend_id=friend_id,
                event_type=existing[0].type,
                payload={},
            )
            assert duplicated is False
        after = len(game_store.recent_events(session, session_id, limit=200))
        assert after == before


class FakeGameProvider:
    def __init__(self, text: str):
        self._text = text
        self.config = ProviderConfig(
            name="fake",
            base_url="http://x",
            api_key="k",
            model="m",
            capabilities=ProviderCapabilities(),
        )

    async def chat_stream(self, _messages, **_kwargs):
        yield {"type": "content", "delta": self._text}
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}


class FakeGameSayProvider(FakeGameProvider):
    """第一轮显式调用 game_say，之后收尾。"""

    def __init__(self, text: str):
        super().__init__("")
        self._text = text
        self._round = 0

    async def chat_stream(self, _messages, **_kwargs):
        self._round += 1
        if self._round == 1:
            tool = {
                "name": "game_say",
                "arguments": {"text": self._text},
            }
            yield {
                "type": "content",
                "delta": f"<tool>{json.dumps(tool, ensure_ascii=False)}</tool>",
            }
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}


class FakeTwoGameSaysProvider(FakeGameProvider):
    """D330：先回一句过程，再回一句结果。"""

    def __init__(self):
        super().__init__("")
        self._round = 0

    async def chat_stream(self, _messages, **_kwargs):
        self._round += 1
        if self._round in (1, 2):
            text = "我先看看。" if self._round == 1 else "看完了，背包里有 12 个泥土。"
            tool = {"name": "game_say", "arguments": {"text": text}}
            yield {
                "type": "content",
                "delta": f"<tool>{json.dumps(tool, ensure_ascii=False)}</tool>",
            }
        yield {"type": "done", "usage": {}, "stop_reason": "stop"}


@pytest.mark.asyncio
async def test_game_chat_runs_the_tool_loop_and_speaks_in_game(temp_db, monkeypatch):
    """游戏里的玩家聊天 → 走完整 agent 循环；显式 game_say 才算说出口。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        provider_factory=lambda _db, _friend: FakeGameSayProvider("好，我跟着你"),
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id

        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        await manager._respond_to_game_chat(friend_id, {"from": "梦汐", "text": "跟着我走"})

        tasks = manager.status()["tasks"]
        assert any(
            task.get("action") == "say" and task.get("status") in ("running", "done")
            for task in tasks.values()
        )
        # 游戏里的对话要进聊天记录（用户拍板：不要"双线程"）
        with db.session_scope() as session:
            messages = session.execute(
                select(Message).where(Message.friend_id == friend_id)
            ).scalars().all()
        assert any("[游戏]" in (message.content or "") for message in messages)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_chat_allows_ack_and_result_in_one_turn(temp_db, monkeypatch):
    """D330：同一回合允许两段 game_say。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        provider_factory=lambda _db, _friend: FakeTwoGameSaysProvider(),
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id

        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        await manager._respond_to_game_chat(
            friend_id, {"from": "梦汐", "text": "你背包里有多少泥土？"}
        )

        with db.session_scope() as session:
            messages = session.execute(
                select(Message)
                .where(Message.friend_id == friend_id)
                .order_by(Message.created_at)
            ).scalars().all()
        assistant = [
            message.content
            for message in messages
            if message.role == "assistant" and "[游戏]" in (message.content or "")
        ]
        assert assistant == [
            "[游戏] 我先看看。",
            "[游戏] 看完了，背包里有 12 个泥土。",
        ]
        say_tasks = [
            task
            for task in manager.status()["tasks"].values()
            if task.get("action") == "say"
        ]
        assert len(say_tasks) >= 2
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_chat_without_game_say_does_not_fabricate_speech(temp_db, monkeypatch):
    """D318：零动作、零 game_say 的正文不能变成游戏发言。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        provider_factory=lambda _db, _friend: FakeGameProvider("我已经把房子盖好了"),
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id

        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        await manager._respond_to_game_chat(friend_id, {"from": "梦汐", "text": "建好了吗"})

        tasks = manager.status()["tasks"]
        assert not any(task.get("action") == "say" for task in tasks.values())
        with db.session_scope() as session:
            messages = session.execute(
                select(Message).where(Message.friend_id == friend_id)
            ).scalars().all()
        assert not any(
            message.role == "assistant" and "[游戏]" in (message.content or "")
            for message in messages
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_action_request_with_only_speech_sets_plan_nudge(
    temp_db, monkeypatch
):
    """D335：TA 交办动作，她只 game_say 没碰 plan/动作 → 外层补一轮。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        provider_factory=lambda _db, _friend: FakeGameSayProvider("我现在去挖。"),
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id

        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        await manager._run_game_turn(
            friend_id,
            {"from": "梦汐", "text": "帮我挖点泥土"},
            persist_user=True,
            hint=None,
        )

        assert manager._plan_nudge_pending == friend_id
        assert not manager._plans.active()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_stop_message_uses_quick_reply_and_locks_plan(temp_db, monkeypatch):
    """D336：明确叫停走快速回应，不进入计划复盘，也不能立刻恢复。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    calls: list[tuple[str, str]] = []

    async def fake_quick(_friend_id, *, fact, instruction):
        calls.append((fact, instruction))

    monkeypatch.setattr(manager, "_quick_game_line", fake_quick)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        plan = manager._plans.create(
            condition="有石头",
            expected="建房子",
            tasks=[{"action": "mine", "args": {"block": "stone", "count": 1}}],
        )
        manager._plans.mark_task(plan.id, 1, status="executing")
        await manager._respond_to_game_chat(
            friend_id, {"from": "梦汐", "text": "先不建了，停下"}
        )

        assert calls
        assert manager._plan_stop_hold == plan.id
        assert manager._plans.get(plan.id).status == "paused"
        with db.session_scope() as session:
            messages = session.execute(
                select(Message)
                .where(Message.friend_id == friend_id)
                .order_by(Message.created_at)
            ).scalars().all()
        assert any(
            message.role == "user" and "[游戏] 梦汐：先不建了" in (message.content or "")
            for message in messages
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_continuation_turn_is_silent(temp_db, monkeypatch):
    """D264：续跑轮的正文只是思考，不进聊天记录、不兜底发到游戏。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
        provider_factory=lambda _db, _friend: FakeGameProvider("我先去挖点木头"),
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id

        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        acted = await manager._run_game_turn(
            friend_id,
            {"text": "继续"},
            persist_user=False,
            hint="[游戏里] 继续推进计划，安静做事",
            quiet=True,
        )
        assert acted is False
        assert not any(
            task.get("action") == "say" for task in manager.status()["tasks"].values()
        )
        with db.session_scope() as session:
            messages = session.execute(
                select(Message).where(Message.friend_id == friend_id)
            ).scalars().all()
        assert messages == []
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_autonomy_starts_immediately_with_the_game(temp_db, monkeypatch):
    """D272：进游戏就启动自主循环，不再等 1 分钟空闲。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=True,
        provider_factory=lambda _db, _friend: FakeGameProvider("我先找点事做"),
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        await manager.set_mode("free", force=True)
        assert manager._autonomy_task is not None
        await asyncio.sleep(0.05)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_mode_defaults_to_follow_and_switches(temp_db, monkeypatch):
    """D276：默认跟随；聊天/自由可切；切换有 5 分钟冷却。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        assert manager.status()["mode"] == "follow"
        assert (await manager.snapshot())["mode"] == "follow"

        # 跟随模式不许自己定计划
        with pytest.raises(ValueError):
            manager.set_plan(friend_id, "自己找点事", ["挖石头"])

        assert (await manager.set_mode("chat"))["mode"] == "chat"
        assert (await manager.snapshot())["mode"] == "chat"
        with pytest.raises(ValueError):
            await manager.set_mode("free")  # 冷却中
        assert (await manager.set_mode("free", force=True))["mode"] == "free"
        assert manager.set_plan(friend_id, "自己找点事", ["挖石头"])["goal"] == "自己找点事"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_user_task_suspends_follow_and_restores_it(temp_db, monkeypatch):
    """D276：用户任务优先；做完自动回到跟随。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        assert (await manager.snapshot())["mode"] == "follow"

        await manager.command("mine", {"block": "dirt", "count": 1})
        # 任务开始时模式先被撤掉
        assert (await manager.snapshot())["mode"] == "none"

        restored = None
        for _ in range(200):
            restored = await manager.snapshot()
            if restored.get("mode") == "follow":
                break
            await asyncio.sleep(0.02)
        assert restored["mode"] == "follow"
    finally:
        await manager.stop()


def test_quantitative_partial_is_detected():
    """D278：挖 7/32 是"没做完"，不能当成任务完成。"""
    assert GameManager._remaining({"mined": 7, "requested": 32}) == 25
    assert GameManager._is_partial({"mined": 7, "requested": 32}) is True
    assert GameManager._is_partial({"mined": 32, "requested": 32}) is False
    assert GameManager._is_partial({"killed": 2, "requested": 5}) is True
    assert GameManager._is_partial({"error": "附近没找到"}) is False


def test_clean_game_text_strips_game_tag_and_mood():
    from backend.game.manager import _clean_game_text

    assert _clean_game_text("[游戏] [开心] 你好") == "你好"
    assert _clean_game_text("[游戏] [游戏] 8 块木头") == "8 块木头"


@pytest.mark.asyncio
async def test_game_message_stores_mood(temp_db, monkeypatch):
    """D282：游戏里说的话也要把情绪落库，不能只把标签摘掉。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        await manager._persist_game_message(
            friend_id, "assistant", "[游戏] 你好", mood="开心", moods=["开心"]
        )
        with db.session_scope() as session:
            row = session.execute(
                select(Message).where(Message.friend_id == friend_id)
            ).scalars().first()
        assert row is not None
        assert row.content == "[游戏] 你好"
        assert row.mood == "开心"
        assert "开心" in (row.moods or "")
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_user_task_reports_and_partial_is_analyzed(temp_db, monkeypatch):
    """D282：完成要回一句；只完成一部分交给 DeepSeek 分析，不默认重试。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        game = manager._service.session
        reports: list[dict] = []
        analyses: list[tuple[dict, bool, bool]] = []

        async def fake_report(_friend_id, task):
            reports.append(dict(task))

        async def fake_analyze(_friend_id, task, *, user_visible, partial=False):
            analyses.append((dict(task), user_visible, partial))

        manager._report_user_task = fake_report  # type: ignore[assignment]
        manager._analyze_failure = fake_analyze  # type: ignore[assignment]

        def add_task(task_id: str, origin: str, args: dict) -> None:
            game.tasks[task_id] = {
                "action": "mine",
                "args": args,
                "origin": origin,
                "status": "running",
            }
            with db.session_scope() as session:
                game_store.add_task(
                    session,
                    session_id=manager.session_id,
                    adapter_task_id=task_id,
                    action="mine",
                    args=args,
                )

        def done(task_id: str, result: dict):
            return SimpleNamespace(
                type="task_done",
                payload={"task_id": task_id, "ok": True, "result": result},
                event_id=f"ev-{task_id}",
                ts="",
            )

        # 用户任务：完成 → 回一句
        add_task("task-user", "user", {"block": "oak_log", "count": 32})
        await manager._on_event(
            game, done("task-user", {"mined": 32, "requested": 32, "remaining": 0})
        )
        await asyncio.sleep(0.02)
        assert len(reports) == 1
        assert analyses == []

        # 用户任务：只挖到 7/32 → 交给 DeepSeek 分析，不默认重试
        add_task("task-partial", "user", {"block": "oak_log", "count": 32})
        await manager._on_event(
            game, done("task-partial", {"mined": 7, "requested": 32, "remaining": 25})
        )
        await asyncio.sleep(0.02)
        assert len(reports) == 1
        assert analyses and analyses[0][1] is True and analyses[0][2] is True

        # 她自己的任务：完成不回话
        add_task("task-self", "auto", {"block": "stone", "count": 8})
        await manager._on_event(
            game, done("task-self", {"mined": 8, "requested": 8, "remaining": 0})
        )
        await asyncio.sleep(0.02)
        assert len(reports) == 1
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_fast_brain_executes_local_decision(temp_db, monkeypatch):
    """D279：配了 game_fast 时，快脑能直接执行一个本地选出的动作。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})

        manager._fast_provider = lambda: SimpleNamespace(  # type: ignore[assignment]
            config=SimpleNamespace(model="fake-local")
        )

        async def fake_decide(*_args, **_kwargs):
            return (
                {"action": "mine", "args": {"block": "dirt", "count": 1}, "say": ""},
                "",
                {"latency_ms": 12, "prompt_tokens": 10, "completion_tokens": 4},
            )

        monkeypatch.setattr(game_manager_module.fast_brain, "decide", fake_decide)
        assert await manager._run_fast_decision(friend_id) is True
        assert manager._last_brain_decision["lane"] == "local"
        assert manager._last_brain_decision["action"] == "mine"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_fast_brain_escalates_when_local_is_unsure(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._fast_provider = lambda: SimpleNamespace(  # type: ignore[assignment]
            config=SimpleNamespace(model="fake-local")
        )

        async def fake_decide(*_args, **_kwargs):
            return None, "", {"latency_ms": 9}

        monkeypatch.setattr(game_manager_module.fast_brain, "decide", fake_decide)
        assert await manager._run_fast_decision(friend_id) is False
        assert manager._last_brain_decision["lane"] == "local->deepseek"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_fast_brain_provider_error_disables_lane(temp_db, monkeypatch):
    """D281：本地模型 404/超时不能每 2 秒重撞；冷却后回退聊天模型。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._fast_provider = lambda: SimpleNamespace(  # type: ignore[assignment]
            config=SimpleNamespace(model="broken-local")
        )

        async def boom(*_args, **_kwargs):
            raise RuntimeError("404 model not found")

        monkeypatch.setattr(game_manager_module.fast_brain, "decide", boom)
        assert await manager._run_fast_decision(friend_id) is False
        assert manager._fast_brain_disabled_until > 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_fast_brain_reads_structured_plan(temp_db, monkeypatch):
    """D293：game_plan(action=create) 的结构化 plan 也要喂给快脑。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._plans.create(
            condition="背包里有镐子",
            expected="挖到 4 块石头",
            tasks=[
                {"action": "equip", "args": {"item": "stone_pickaxe"}},
                {"action": "mine", "args": {"block": "stone", "count": 4}},
            ],
            origin="user",
        )
        manager._fast_provider = lambda: SimpleNamespace(  # type: ignore[assignment]
            config=SimpleNamespace(model="fake-local")
        )
        captured: dict = {}

        async def fake_decide(*_args, **kwargs):
            captured.update(kwargs)
            return (
                {"action": "equip", "args": {"item": "stone_pickaxe"}, "say": ""},
                "",
                {"latency_ms": 5},
            )

        async def fake_command(*_args, **_kwargs):
            return {"accepted": True}

        monkeypatch.setattr(game_manager_module.fast_brain, "decide", fake_decide)
        manager.command = fake_command  # type: ignore[assignment]
        assert await manager._run_fast_decision(friend_id) is True
        assert "挖到 4 块石头" in captured["plan"]
        assert any("stone_pickaxe" in step for step in captured["plan_steps"])
        assert any("stone" in step for step in captured["plan_steps"])
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_autonomy_backoff_without_progress(temp_db, monkeypatch):
    """D293：没有计划也没有动作时指数退避，不能每 2 秒烧一次慢脑。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._mode = "free"
        manager._autonomy_task = None

        async def no_action(*_args, **_kwargs):
            return False

        manager._run_game_turn = no_action  # type: ignore[assignment]

        await manager._autonomy_turn(friend_id)
        first_due = manager._next_idle_think_at
        await manager._autonomy_turn(friend_id)
        assert manager._autonomy_no_progress == 2
        assert manager._next_idle_think_at > first_due
        assert manager._next_idle_think_at > time.monotonic() + 10.0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_autonomy_progress_resets_backoff(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._mode = "free"
        manager._autonomy_task = None
        manager._autonomy_no_progress = 3
        manager._next_idle_think_at = time.monotonic() + 60

        async def acted(*_args, **_kwargs):
            return True

        manager._run_game_turn = acted  # type: ignore[assignment]
        await manager._autonomy_turn(friend_id)
        assert manager._autonomy_no_progress == 0
        assert manager._next_idle_think_at > time.monotonic()
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_auto_failure_uses_single_deepseek_brain(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        calls: list[str] = []

        async def deep(*_args, **_kwargs):
            calls.append("deep")
            return True

        manager._run_game_turn = deep  # type: ignore[assignment]
        await manager._analyze_failure(
            friend_id,
            {
                "action": "mine",
                "args": {"block": "stone", "count": 4},
                "ok": False,
                "result": {"error": "需要木镐"},
            },
            user_visible=False,
        )
        assert calls == ["deep"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_failure_analysis_uses_deep_thinking_and_forbids_auto_retry(
    temp_db, monkeypatch
):
    """D282：失败先让 DeepSeek 分析；重试必须是分析后的决定。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        captured: dict = {}

        async def fake_run(_friend_id, payload, **kwargs):
            captured["text"] = payload["text"]
            captured.update(kwargs)
            return True

        manager._run_game_turn = fake_run  # type: ignore[assignment]
        await manager._analyze_failure(
            friend_id,
            {
                "action": "eat",
                "args": {},
                "ok": False,
                "result": {"error": "[条件不满足] 背包里没有能吃的食物"},
            },
            user_visible=True,
        )
        assert captured["deep_thinking_override"] is False
        assert captured["max_tool_rounds"] == 1
        assert "不要原样重试" in captured["text"]
        assert "条件不满足" in captured["text"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_continuation_without_action_retries_even_without_plan(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        continuation_delay=60.0,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        await manager.set_mode("free", force=True)

        async def fake_turn(*_args, **_kwargs):
            return False

        manager._run_game_turn = fake_turn  # type: ignore[assignment]
        manager._plan = ""
        manager._last_task = {"action": "mine", "ok": True, "result": {}}
        manager._continuation_retry = 0
        await manager._continue_plan(manager._continuation_generation, retry=False)
        assert manager._continuation_retry == 1
        assert manager._continuation_task is not None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_patrol_does_not_count_as_a_real_task(temp_db, monkeypatch):
    """D271：待机巡逻只是占位，不应该阻止空闲自启动。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        assert manager._service is not None
        manager._service.session.tasks["p1"] = {
            "action": "patrol",
            "args": {},
            "status": "running",
        }
        assert manager._has_running_task() is False
        assert manager._has_running_task(include_patrol=True) is True
        manager._service.session.tasks["m1"] = {
            "action": "mine",
            "args": {},
            "status": "running",
        }
        assert manager._has_running_task() is True
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "patrol_tool",
                    "name": "game_do",
                    "arguments": {"action": "patrol", "args": {"seconds": 5}},
                },
            )
        assert record["ok"] is False
        assert "patrol" in record["error"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_do_accepts_top_level_sequence_steps(temp_db, monkeypatch):
    """D273：模型把 steps 放顶层时也要兼容，不能空转失败。"""
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    calls: list[tuple[str, dict, str]] = []

    async def fake_command(action, args, *, source="user"):
        calls.append((action, dict(args), source))
        return {"accepted": True, "task_id": "task-x"}

    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._plans.create(
            condition="测试",
            expected="测试",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 1}}],
        )
        monkeypatch.setattr(manager, "command", fake_command)
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "seq_tool",
                    "name": "game_do",
                    "arguments": {
                        "action": "sequence",
                        "steps": [{"action": "mine", "args": {"block": "dirt", "count": 2}}],
                    },
                },
            )
        assert record["ok"] is True
        assert calls and calls[0][0] == "sequence"
        assert calls[0][1]["steps"][0]["action"] == "mine"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_do_accepts_json_string_steps(temp_db, monkeypatch):
    """D281：模型把 args / steps 写成 JSON 字符串时也要兼容。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    calls: list[tuple[str, dict, str]] = []

    async def fake_command(action, args, *, source="user"):
        calls.append((action, dict(args), source))
        return {"accepted": True, "task_id": "task-x"}

    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._plans.create(
            condition="测试",
            expected="测试",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 1}}],
        )
        monkeypatch.setattr(manager, "command", fake_command)
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "seq_string",
                    "name": "game_do",
                    "arguments": {
                        "action": "sequence",
                        "args": json.dumps(
                            {
                                "steps": [
                                    {
                                        "action": "mine",
                                        "args": {"block": "dirt", "count": 2},
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    },
                },
            )
        assert record["ok"] is True
        assert calls and calls[0][0] == "sequence"
        assert calls[0][1]["steps"][0]["args"]["count"] == 2
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_do_format_error_is_tagged(temp_db, monkeypatch):
    """D281：参数格式错误要带 [格式错误]，模型才知道该改格式而不是换目标。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        manager._plans.create(
            condition="测试",
            expected="测试",
            tasks=[{"action": "mine", "args": {"block": "dirt", "count": 1}}],
        )
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "bad_goto",
                    "name": "game_do",
                    "arguments": {"action": "goto", "args": {}},
                },
            )
        assert record["ok"] is False
        assert "[格式错误]" in record["error"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_do_requires_an_active_plan(temp_db, monkeypatch):
    """D335：世界动作不能再裸调 game_do，必须先有 active plan。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "bare_mine",
                    "name": "game_do",
                    "arguments": {"action": "mine", "args": {"block": "dirt"}},
                },
            )
        assert record["ok"] is False
        assert "世界动作必须放在 plan 里" in record["error"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_skill_requires_an_active_plan(temp_db, monkeypatch):
    """D335：game_skill 也执行世界动作，没有 plan 时直接拒绝。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "bare_skill",
                    "name": "game_skill",
                    "arguments": {"name": "gather_oak_logs", "args": {}},
                },
            )
        assert record["ok"] is False
        assert "技能执行也是世界动作" in record["error"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_game_inspect_invalid_center_falls_back_to_player(
    temp_db, monkeypatch
):
    """D335：center 写错不能再反复报错，直接回退到玩家脚下。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    captured: list[dict] = []

    async def fake_inspect_area(*, center, radius, height, include_air):
        captured.append(
            {
                "center": center,
                "radius": radius,
                "height": height,
                "include_air": include_air,
            }
        )
        return {"ok": True}

    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        monkeypatch.setattr(manager, "inspect_area", fake_inspect_area)
        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "inspect_fallback",
                    "name": "game_inspect",
                    "arguments": {"center": "not-a-coordinate", "radius": 3},
                },
            )
        assert record["ok"] is True
        assert captured[0]["center"] is None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_second_task_does_not_override_running_task(temp_db, monkeypatch):
    """D282：同一时间只跑一个真实任务，避免 pathfinder 的 goal 被抢来抢去。"""

    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        auto_continue=False,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id
        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        game = manager._service.session
        game.tasks["existing"] = {
            "action": "goto",
            "args": {},
            "origin": "auto",
            "status": "running",
        }
        with pytest.raises(RuntimeError) as exc:
            await manager.command("goto", {"x": 0, "y": 64, "z": 0}, source="auto")
        assert "[条件不满足]" in str(exc.value)
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_plan_is_visible_and_task_done_schedules_continuation(temp_db, monkeypatch):
    async def fake_summarizer(_provider, _transcript):
        return "", []

    manager = GameManager(
        summarizer=fake_summarizer,
        comment_throttle=10**9,
        autonomy_enabled=False,
        continuation_delay=60.0,
    )
    monkeypatch.setattr(game_manager_module, "_manager", manager)
    try:
        with db.session_scope() as session:
            friend = friend_service.create(session, Actor.USER, name="汐音")
            friend_id = friend.id

        await manager.start(friend_id=friend_id, game_kind="mock", config={})
        await manager.set_mode("free", force=True)
        plan = manager.set_plan(
            friend_id,
            "盖一间能住的小屋",
            ["收集木头", "合成木板", "建墙和屋顶"],
        )
        assert plan["goal"] == "盖一间能住的小屋"
        assert len(plan["steps"]) == 3

        with db.session_scope() as session:
            record = await tools_module.execute_async(
                session,
                friend_id,
                {
                    "id": "plan_1",
                    "name": "game_plan",
                    "arguments": {
                        "goal": "先挖 8 个石头",
                        "steps": ["找露出来的石头", "挖 8 个", "回来说一声"],
                    },
                },
            )
        assert record["ok"] is True
        assert record["result"]["goal"] == "先挖 8 个石头"

        note = await manager.note_for_prompt(friend_id)
        assert "先挖 8 个石头" in note
        assert "挖 8 个" in note
        assert "game_plan" in note

        await manager.command("mine", {"block": "dirt"})
        for _ in range(100):
            if manager._last_task and manager._last_task.get("action") == "mine":
                break
            await asyncio.sleep(0.02)
        assert manager._last_task["action"] == "mine"
        assert manager._continuation_task is not None

        # 用户下新指令 → 取消待续跑
        pending = manager._continuation_task
        await manager.command("say", {"text": "先别动了"}, source="user")
        assert manager._continuation_task is None
        assert pending.cancelled() or pending.done()
    finally:
        await manager.stop()
