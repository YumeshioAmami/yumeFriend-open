"""T2.3 验收：SSE 流式聊天端点（api.md §2 / LLM协议§3）。

全部用假 provider（mock 流式响应），不碰真实网络。
"""

import json
import time
import asyncio

import pytest
from fastapi.testclient import TestClient

import backend.config as config
import backend.db as db
from backend.api.deps import get_provider_factory
from backend.app import create_app
from backend.api.messages import _align_moods
from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.llm.errors import LLMCallError
from backend.models import Message
from backend.services import style_profile_service


class FakeProvider:
    """假 provider：按剧本吐归一化事件，并记下收到的 messages（给断言用）。"""

    def __init__(
        self,
        chunks=None,
        script=None,
        error: Exception | None = None,
        reasoning: bool = True,
        delay: float = 0.0,
    ):
        self._chunks = chunks or []
        # script = 每次调用各吐什么（工具循环会调用多次）；不传就每次吐同样的 chunks
        self._script = script
        self._error = error
        self._delay = delay
        self.config = ProviderConfig(
            name="fake",
            base_url="http://fake.local/v1",
            api_key="sk-fake",
            model="fake-model",
            capabilities=ProviderCapabilities(
                native_tool_calls=False, reasoning=reasoning, reasoning_streamable=True
            ),
        )
        self.calls: list[dict] = []

    async def chat_stream(self, messages, tools=None, **kwargs):
        index = len(self.calls)
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        if self._error is not None:
            raise self._error
        if self._script is not None:
            payload = self._script[min(index, len(self._script) - 1)]
        else:
            payload = self._chunks
        for chunk in payload:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield chunk

    async def health_check(self) -> bool:
        return True


def test_align_moods_matches_final_segments_by_offset():
    segments = ["第一泡。", "第二泡。", "第三泡。"]
    marks = [
        {"mood": "平静", "at": 0},
        {"mood": "调皮", "at": 4},
        {"mood": "认真", "at": 8},
    ]
    assert _align_moods(segments, marks) == ["平静", "调皮", "认真"]
    assert _align_moods(segments, [{"mood": "高兴", "at": 0}]) == [
        "高兴",
        "高兴",
        "高兴",
    ]


def test_multi_paragraph_moods_align_without_forcing_message_bubbles(app_client):
    app, client = app_client
    fid = _make_friend(client)
    _use(
        app,
        FakeProvider(
            chunks=[
                {
                    "type": "content",
                    "delta": (
                        "**调皮**：第一段。\n\n"
                        "**担心**：第二段。\n\n"
                        "**高兴**：第三段。"
                    ),
                },
                {"type": "done"},
            ]
        ),
    )
    response = client.post(f"/api/friends/{fid}/messages", json={"content": "测试"})
    assert response.status_code == 200, response.text
    with db.session_scope() as session:
        message = session.query(Message).filter(Message.role == "assistant").one()
        assert json.loads(message.moods) == ["调皮", "担心", "高兴"]
        assert not message.segments or json.loads(message.segments) == []
        assert "**" not in (message.content or "")
        assert "第一段" in (message.content or "")


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(db, "DB_PATH", db_file)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_session_factory", None)
    app = create_app()
    with TestClient(app) as client:
        yield app, client


def _make_friend(client, name="阿月", personality="话不多，爱用括号写动作。") -> str:
    r = client.post("/api/friends", json={"name": name, "personality": personality})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _use(app, provider: FakeProvider) -> None:
    app.dependency_overrides[get_provider_factory] = lambda: (lambda: provider)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name = None
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            events.append((name, json.loads(line[len("data: ") :])))
    return events


def names(events) -> list[str]:
    return [e[0] for e in events]


# ---------- 正常流 ----------


def test_stream_event_sequence_and_persistence(app_client):
    app, client = app_client
    fid = _make_friend(client)
    tool = '<tool>{"name":"save_memory","arguments":{"content":"用户生日 5/20"}}</tool>'
    _use(
        app,
        FakeProvider(
            script=[
                # 第一轮：说了句话 + 顺手记一笔
                [
                    {"type": "content", "delta": "好"},
                    {"type": "content", "delta": "呀。\n"},
                    {"type": "content", "delta": tool},
                    {"type": "done", "usage": {"completion_tokens": 7}, "stop_reason": "stop"},
                ],
                # 第二轮：工具执行完，接着把话说完
                [
                    {"type": "content", "delta": "（记下了）"},
                    {"type": "done", "usage": {"completion_tokens": 3}, "stop_reason": "stop"},
                ],
            ]
        ),
    )

    r = client.post(f"/api/friends/{fid}/messages", json={"content": "记住我生日"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)

    # 事件顺序：正文 → 工具调用 → 工具结果 → 接着说的正文 → done（LLM协议§3.2）
    # D110：流一开始先来一条 `start`（把两边消息 id 给前端——分段语音靠它对号入座）
    assert names(events) == [
        "start",
        "content",
        "content",
        "tool_call",
        "tool_result",
        "content",
        "done",
    ]
    assert events[0][1]["assistant_message_id"] == events[-1][1]["assistant_message_id"]
    assert "".join(d["delta"] for n, d in events if n == "content") == "好呀。（记下了）"
    tool_event = dict(next(d for n, d in events if n == "tool_call"))
    assert tool_event["name"] == "save_memory"
    assert tool_event["arguments"] == {"content": "用户生日 5/20"}
    result_event = dict(next(d for n, d in events if n == "tool_result"))
    assert result_event["ok"] is True
    assert result_event["name"] == "save_memory"
    assert events[-1][1]["stop_reason"] == "stop"
    assert events[-1][1]["usage"] == {
        "input_tokens": 0,
        "output_tokens": 10,
        "reasoning_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "total_tokens": 10,
        "provider_calls": 2,
    }
    assert events[-1][1]["user_message_id"] and events[-1][1]["assistant_message_id"]

    # 落库：正文干净、工具块已剥离、状态 completed
    with db.session_scope() as s:
        rows = s.query(Message).order_by(Message.created_at).all()
    assert [m.role for m in rows] == ["user", "assistant"]
    assert rows[0].content == "记住我生日"
    assert rows[1].content == "好呀。（记下了）"
    assert "<tool>" not in rows[1].content
    assert rows[1].stream_status == "completed"
    assert rows[1].mode_at_send == "normal"
    used = json.loads(rows[1].tools_used)
    assert used[0]["name"] == "save_memory"
    assert used[0]["ok"] is True
    assert used[0]["duration_ms"] >= 0
    assert used[0]["args"] == {"content": "用户生日 5/20"}


def test_personality_and_history_reach_the_model(app_client):
    app, client = app_client
    fid = _make_friend(client, personality="嘴上不饶人，其实很在意对方。")
    provider = FakeProvider(
        chunks=[
            {"type": "content", "delta": "收到。"},
            {"type": "done", "usage": {}, "stop_reason": "stop"},
        ]
    )
    _use(app, provider)

    client.post(f"/api/friends/{fid}/messages", json={"content": "第一句"})
    client.post(f"/api/friends/{fid}/messages", json={"content": "第二句"})

    assert len(provider.calls) == 2
    messages = provider.calls[1]["messages"]
    assert messages[0]["role"] == "system"
    assert "嘴上不饶人，其实很在意对方。" in messages[0]["content"]
    assert "【硬性规则】" in messages[0]["content"]
    # 历史：第一轮 user + 第一轮 assistant，然后是本轮情绪白名单（D440）、
    # 状态提示，最后才是本轮输入
    assert [m["role"] for m in messages[1:]] == [
        "user",
        "assistant",
        "system",
        "user",
        "user",
    ]
    # v3.31（D216）：历史正文前会拼一个本地时间戳 `[MM-DD HH:MM]`
    assert messages[1]["content"].endswith("第一句")
    assert messages[1]["content"].startswith("[")
    assert messages[-1]["content"] == "第二句"
    assert "你现在的模式是 普通（NORMAL）" in messages[-2]["content"]
    # 本轮输入不能被重复塞进历史
    assert [m["content"] for m in messages].count("第二句") == 1
    # M2 不传原生 tools（还没工具），也不该传
    assert provider.calls[0]["tools"] is None


def test_done_event_carries_moods_aligned_to_segments(app_client):
    app, client = app_client
    fid = _make_friend(client)
    provider = FakeProvider(
        chunks=[
            {
                "type": "content",
                "delta": "**平静**：第一句。<<<MSG>>>**调皮**：第二句。",
            },
            {"type": "done", "usage": {}, "stop_reason": "stop"},
        ]
    )
    _use(app, provider)
    response = client.post(
        f"/api/friends/{fid}/messages", json={"content": "在吗"}
    )
    events = parse_sse(response.text)
    done = [data for name, data in events if name == "done"][-1]
    assert done["segments"] == ["第一句。", "第二句。"]
    assert done["moods"] == ["平静", "调皮"]
    with db.session_scope() as session:
        message = session.query(Message).filter(Message.role == "assistant").one()
        assert json.loads(message.moods) == ["平静", "调皮"]


def test_explicit_feedback_falls_back_when_model_skips_tool(app_client):
    app, client = app_client
    fid = _make_friend(client)
    provider = FakeProvider(
        chunks=[
            {"type": "content", "delta": "好，我短一点。"},
            {"type": "done", "usage": {}, "stop_reason": "stop"},
        ]
    )
    _use(app, provider)
    response = client.post(
        f"/api/friends/{fid}/messages",
        json={"content": "你刚才太长了，像写报告，短一点"},
    )
    assert response.status_code == 200
    with db.session_scope() as session:
        profile = style_profile_service.get_profile(session, fid)
        assert profile["sections"]["length"]
        assert profile["version"] >= 1


def test_switching_mode_tells_the_model_to_switch_immediately(app_client):
    """D49：用户点了 mode 按钮 → 下一轮 prompt 里必须出现"立刻切换/出戏"的提示。"""
    app, client = app_client
    fid = _make_friend(client)
    provider = FakeProvider(
        chunks=[{"type": "content", "delta": "嗯。"}, {"type": "done", "stop_reason": "stop"}]
    )
    _use(app, provider)

    client.post(f"/api/friends/{fid}/messages", json={"content": "第一句"})
    assert "刚刚把你切到" not in provider.calls[0]["messages"][0]["content"]

    # 切到入戏，再说话
    client.patch(f"/api/friends/{fid}/mode", json={"mode": "roleplay", "role": "女朋友"})
    client.post(f"/api/friends/{fid}/messages", json={"content": "第二句"})
    switched = provider.calls[1]["messages"][0]["content"]
    assert "用户刚刚把你切到了 入戏 模式（上一轮是 普通）" in switched
    assert "不用问、不用确认" in switched

    # 第三轮：mode 没再变，提示就不该出现
    client.post(f"/api/friends/{fid}/messages", json={"content": "第三句"})
    assert "刚刚把你切到" not in provider.calls[2]["messages"][0]["content"]

    # 切回普通 → 必须提示出戏
    client.patch(f"/api/friends/{fid}/mode", json={"mode": "normal"})
    client.post(f"/api/friends/{fid}/messages", json={"content": "第四句"})
    back = provider.calls[3]["messages"][0]["content"]
    assert "用户刚刚把你切到了 普通 模式（上一轮是 入戏）" in back
    assert "现在就出戏" in back


def test_empty_assistant_messages_are_skipped_in_history(app_client):
    """D43：内容为空的失败回复不进历史；有内容的 partial 照常带。"""
    app, client = app_client
    fid = _make_friend(client)
    provider = FakeProvider(chunks=[{"type": "done", "usage": {}, "stop_reason": "stop"}])
    _use(app, provider)

    with db.session_scope() as s:
        from backend.services import message_service

        message_service.create(s, fid, role="user", content="上一句")
        message_service.create(
            s, fid, role="assistant", content="", stream_status=message_service.ERROR
        )
        message_service.create(
            s, fid, role="assistant", content="说到一半就断了", stream_status=message_service.PARTIAL
        )

    client.post(f"/api/friends/{fid}/messages", json={"content": "这一句"})
    sent = provider.calls[0]["messages"]
    contents = [m["content"] for m in sent]
    # v3.31（D216）：历史正文前会拼本地时间戳 → 用"包含"判断，不是完全相等
    assert any("上一句" in c for c in contents)
    assert any("说到一半就断了" in c for c in contents)
    assert "" not in contents, "空 assistant 消息不该进历史"


def test_reasoning_events_are_forwarded_and_not_stored(app_client):
    app, client = app_client
    fid = _make_friend(client)
    _use(
        app,
        FakeProvider(
            chunks=[
                {"type": "reasoning", "delta": "先想想…"},
                {"type": "content", "delta": "嗯。"},
                {"type": "done", "usage": {}, "stop_reason": "stop"},
            ]
        ),
    )
    events = parse_sse(client.post(f"/api/friends/{fid}/messages", json={"content": "在吗"}).text)
    assert "reasoning" in names(events)
    assert events[1] == ("reasoning", {"delta": "先想想…"})

    with db.session_scope() as s:
        assistant = s.query(Message).filter_by(role="assistant").one()
    assert assistant.content == "嗯。"
    assert "先想想" not in assistant.content  # 思考链不落库（D23）


def test_deep_thinking_setting_passes_reasoning_effort(app_client):
    app, client = app_client
    fid = _make_friend(client)
    # v4.1（D246）：给一句正文，免得触发"空回复兜底重试"多打几次 provider
    provider = FakeProvider(
        chunks=[
            {"type": "content", "delta": "好。"},
            {"type": "done", "usage": {}, "stop_reason": "stop"},
        ]
    )
    _use(app, provider)

    client.post(f"/api/friends/{fid}/messages", json={"content": "hi"})
    assert "reasoning_effort" not in provider.calls[0]["kwargs"]  # 默认关闭（D23）

    with db.session_scope() as s:
        from backend.services import settings_service

        settings_service.get(s).deep_thinking = 1
    client.post(f"/api/friends/{fid}/messages", json={"content": "hi again"})
    assert provider.calls[1]["kwargs"]["reasoning_effort"] == "high"


def test_model_without_reasoning_ignores_the_switch(app_client):
    app, client = app_client
    fid = _make_friend(client)
    provider = FakeProvider(chunks=[{"type": "done"}], reasoning=False)
    _use(app, provider)
    with db.session_scope() as s:
        from backend.services import settings_service

        settings_service.get(s).deep_thinking = 1
    client.post(f"/api/friends/{fid}/messages", json={"content": "hi"})
    assert "reasoning_effort" not in provider.calls[0]["kwargs"]


# ---------- 错误路径 ----------


def test_llm_failure_emits_error_event_and_marks_message(app_client):
    app, client = app_client
    fid = _make_friend(client)
    _use(
        app,
        FakeProvider(
            chunks=[{"type": "content", "delta": "开头"}],
            error=LLMCallError("调用 LLM 失败 request_id=abc123：503 all_fallbacks_failed"),
        )
    )
    r = client.post(f"/api/friends/{fid}/messages", json={"content": "在吗"})
    assert r.status_code == 200  # 流已经开始，错误走 error 事件
    events = parse_sse(r.text)
    assert names(events)[-1] == "error"
    assert events[-1][1]["code"] == "LLM_CALL_FAILED"
    assert "request_id=abc123" in events[-1][1]["message"]  # 原始信息要保留（LLM协议§9）
    assert "done" not in names(events), "失败不许假装成功"

    with db.session_scope() as s:
        assistant = s.query(Message).filter_by(role="assistant").one()
    assert assistant.stream_status == "error"
    assert assistant.content == ""  # 一个 chunk 都没吐出来


def test_missing_provider_config_returns_llm_not_configured(app_client):
    _app, client = app_client
    fid = _make_friend(client)
    r = client.post(f"/api/friends/{fid}/messages", json={"content": "在吗"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "LLM_NOT_CONFIGURED"


def test_friend_not_found(app_client):
    _app, client = app_client
    r = client.post("/api/friends/nope/messages", json={"content": "在吗"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "FRIEND_NOT_FOUND"


def test_deleted_friend_cannot_chat(app_client):
    app, client = app_client
    fid = _make_friend(client)
    _use(app, FakeProvider(chunks=[{"type": "done"}]))
    client.delete(f"/api/friends/{fid}")
    r = client.post(f"/api/friends/{fid}/messages", json={"content": "在吗"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "FRIEND_DELETED"


def test_empty_content_rejected(app_client):
    app, client = app_client
    fid = _make_friend(client)
    _use(app, FakeProvider(chunks=[{"type": "done"}]))
    assert client.post(f"/api/friends/{fid}/messages", json={"content": ""}).status_code == 422


# ---------- 断开连接 ----------


async def test_cancelled_reply_keeps_what_was_generated(app_client):
    """**取消**（= 用户按停止）→ 已生成的部分照样落库，标 partial（api.md §1.6）。

    ⚠️ v3.14（D182）改了语义：**客户端断开不再等于取消**（她继续说，见
    `test_reply_keeps_going_after_the_subscriber_leaves`）。这里直接 `aclose()` 这条
    生成器 —— 它抛出的 `GeneratorExit` 正是"这一轮被 cancel"时打进生成器里的那个异常
    （`reply_runs.stop()` 走的就是这条路）。
    """
    from backend.api import messages as messages_api
    from backend.services import friend_service, message_service
    from backend.services.actor import Actor

    app, client = app_client
    fid = _make_friend(client)
    slow = [{"type": "content", "delta": f"第{i}段"} for i in range(50)]
    provider = FakeProvider(chunks=slow, delay=0.01)

    with db.session_scope() as s:
        friend = friend_service.get(s, Actor.USER, fid)
        user_message = message_service.create(s, fid, role="user", content="说话")
        assistant = message_service.create(
            s, fid, role="assistant", content="", stream_status=message_service.STREAMING
        )
        user_id, assistant_id = user_message.id, assistant.id

    stream = messages_api.sse_stream(
        friend=friend,
        user_input="说话",
        provider=provider,
        history=[],
        memories=[],
        user_message_id=user_id,
        assistant_id=assistant_id,
        deep_thinking=False,
        session=None,
    )
    first = await stream.__anext__()
    assert first.startswith("event: start")
    second = await stream.__anext__()
    assert second.startswith("event: content")
    await stream.aclose()  # ← 模拟客户端断开

    with db.session_scope() as s:
        stored = s.get(Message, assistant_id)
    assert stored.stream_status == "partial"
    assert stored.content == "第0段", "断开前已生成的内容必须留下"


async def test_reply_keeps_going_after_the_subscriber_leaves(app_client):
    """v3.14（D182）：**看的人走了，她照样把这轮说完**。

    用户实测的 bug："她正发着消息呢，一关窗口/一切页面就不说了。"
    做法：真正干活的是后台 task（`reply_runs`），SSE 只是个订阅者 ——
    订阅者退订只等于"这一屏不看了"，task 继续跑到 `completed`。
    """
    from backend.agent import reply_runs
    from backend.api import messages as messages_api
    from backend.services import friend_service, message_service
    from backend.services.actor import Actor

    app, client = app_client
    fid = _make_friend(client)
    provider = FakeProvider(
        chunks=[{"type": "content", "delta": f"第{i}段"} for i in range(6)], delay=0.01
    )

    with db.session_scope() as s:
        friend = friend_service.get(s, Actor.USER, fid)
        user_message = message_service.create(s, fid, role="user", content="说话")
        assistant = message_service.create(
            s, fid, role="assistant", content="", stream_status=message_service.STREAMING
        )
        user_id, assistant_id, friend_name = user_message.id, assistant.id, friend.name

    run = reply_runs.start(
        assistant_id=assistant_id,
        friend_id=fid,
        friend_name=friend_name,
        body=lambda run, _wake: messages_api._run_reply(
            run, user_input="说话", user_message_id=user_id, provider=provider
        ),
    )
    queue = run.subscribe()
    frames = reply_runs.subscriber(run, queue)
    first = await frames.__anext__()
    assert first.startswith("event: start")
    await frames.aclose()  # ← 相当于"关掉窗口 / 切走页面"

    assert run.task is not None
    for _ in range(200):  # 她继续说 —— 等后台 task 自己收工
        if run.finished:
            break
        await asyncio.sleep(0.02)
    assert run.finished, "订阅者走了之后，后台这一轮必须继续跑完"

    with db.session_scope() as s:
        stored = s.get(Message, assistant_id)
    assert stored.stream_status == "completed", "跑完了就是 completed（不是 partial）"
    assert stored.content == "".join(f"第{i}段" for i in range(6)), "一个字都不能少"


async def test_stop_cancels_the_background_reply(app_client):
    """v3.14（D182）：用户按停止 → 后台那一轮被真的掐掉，已生成的落库标 partial。"""
    from backend.agent import reply_runs
    from backend.api import messages as messages_api
    from backend.services import friend_service, message_service
    from backend.services.actor import Actor

    app, client = app_client
    fid = _make_friend(client)
    provider = FakeProvider(
        chunks=[{"type": "content", "delta": f"第{i}段"} for i in range(50)], delay=0.02
    )

    with db.session_scope() as s:
        friend = friend_service.get(s, Actor.USER, fid)
        user_message = message_service.create(s, fid, role="user", content="说话")
        assistant = message_service.create(
            s, fid, role="assistant", content="", stream_status=message_service.STREAMING
        )
        user_id, assistant_id = user_message.id, assistant.id
        friend_name = friend.name

    run = reply_runs.start(
        assistant_id=assistant_id,
        friend_id=fid,
        friend_name=friend_name,
        body=lambda run, _wake: messages_api._run_reply(
            run, user_input="说话", user_message_id=user_id, provider=provider
        ),
    )
    queue = run.subscribe()
    frames = reply_runs.subscriber(run, queue)
    await frames.__anext__()  # start
    await frames.__anext__()  # 至少生成一段
    assert await reply_runs.stop(assistant_id) is True
    await frames.aclose()

    for _ in range(200):
        if run.finished:
            break
        await asyncio.sleep(0.02)
    assert run.finished

    with db.session_scope() as s:
        stored = s.get(Message, assistant_id)
    assert stored.stream_status == "partial"
    assert stored.content, "已经说出来的部分要留下"
    assert "第5段" not in stored.content, "停之后不该再往下说"


def test_stop_endpoint_is_wired(app_client):
    """停止端点：朋友/消息都校验过；没在跑的那条返回 stopped=False（不报错）。"""
    app, client = app_client
    fid = _make_friend(client)
    _use(app, FakeProvider(chunks=[{"type": "content", "delta": "在的。"}]))
    r = client.post(f"/api/friends/{fid}/messages", json={"content": "在吗"})
    assert r.status_code == 200
    # 从库里挑这条助手消息（流跑完了）
    with db.session_scope() as s:
        rows = (
            s.query(Message)
            .filter(Message.friend_id == fid, Message.role == "assistant")
            .order_by(Message.created_at.desc())
            .all()
        )
        mid = rows[0].id
    stopped = client.post(f"/api/friends/{fid}/messages/{mid}/stop")
    assert stopped.status_code == 200
    assert stopped.json() == {"ok": True, "stopped": False}


def test_reset_stale_streaming_marks_partial(app_client):
    """v3.14（D182）：进程重启后，上次留下的 `streaming` 要标成 `partial`（能留多少留多少）。"""
    from backend.services import message_service

    app, client = app_client
    fid = _make_friend(client)
    with db.session_scope() as s:
        stuck = message_service.create(
            s, fid, role="assistant", content="说到一半", stream_status=message_service.STREAMING
        )
        mid = stuck.id
    assert message_service.reset_stale_streaming() == 1
    with db.session_scope() as s:
        stored = s.get(Message, mid)
    assert stored.stream_status == message_service.PARTIAL
    assert stored.content == "说到一半", "边生成边落库的部分要留下"


def test_tool_list_follows_the_mode(app_client):
    """T2.0-8：清单按 mode 分组 —— 入戏看不见搜网（一搜就出戏），工具模式才全给。"""
    app, client = app_client
    fid = _make_friend(client)
    provider = FakeProvider(chunks=[{"type": "content", "delta": "在。"}, {"type": "done"}])
    _use(app, provider)

    client.patch(f"/api/friends/{fid}/mode", json={"mode": "roleplay", "role": "女老板"})
    client.post(f"/api/friends/{fid}/messages", json={"content": "在吗"})
    system = provider.calls[-1]["messages"][0]["content"]
    assert "web_search" not in system, "入戏模式不该看见搜网"
    assert "save_memory" in system
    assert "look_image" in system

    client.patch(f"/api/friends/{fid}/mode", json={"mode": "tool"})
    client.post(f"/api/friends/{fid}/messages", json={"content": "在吗"})
    system2 = provider.calls[-1]["messages"][0]["content"]
    assert "web_search" in system2, "工具模式应该全给"
    assert "add_schedule" in system2


def test_chat_pacing_passes_near_generation_shape_and_budget(app_client):
    """D350：形态提醒必须真的进入本轮 provider 请求，且轻聊预算被收紧。"""
    app, client = app_client
    fid = _make_friend(client)
    assert client.patch(
        f"/api/friends/{fid}", json={"chat_pacing_enabled": True}
    ).status_code == 200
    provider = FakeProvider(
        chunks=[{"type": "content", "delta": "在看视频。"}, {"type": "done"}]
    )
    _use(app, provider)

    response = client.post(f"/api/friends/{fid}/messages", json={"content": "在干嘛"})
    assert response.status_code == 200
    call = provider.calls[0]
    joined = "\n".join(
        str(item.get("content"))
        for item in call["messages"]
        if isinstance(item.get("content"), str)
    )
    assert "【这一轮先按这个形态回】" in joined
    assert "只回 1 个气泡" in joined
    assert call["kwargs"]["max_tokens"] == 700
