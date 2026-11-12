"""T2.1 验收：LLM provider 抽象（LLM协议§1）。

全部用假 client，不发真实网络请求 —— 这样测试不依赖网络和 API key。
"""

import json

import pytest

from backend.llm import openai_compat as _oc

from backend.llm.base import ProviderCapabilities, ProviderConfig
from backend.llm.errors import LLMCallError, LLMConfigError
from backend.llm.factory import create_provider, load_provider_configs, pick_provider_config
from backend.llm.openai_compat import (
    DEFAULT_TIMEOUT,
    OpenAICompatProvider,
    close_shared_clients,
    get_shared_client,
    proxy_chain,
    resolve_proxy,
)
from backend.services import usage_service

_REAL_SYSTEM_PROXY_ALIVE = _oc.system_proxy_alive


class _NS:
    """极简命名空间对象，模拟 openai SDK 的响应对象。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture(autouse=True)
def _clear_path_memory():
    """"哪条路好用"是模块级记忆，测试之间必须隔离，否则互相污染。"""
    import backend.llm.openai_compat as oc

    oc._PREFERRED_PATH.clear()
    oc._PATH_FAILED_AT.clear()


@pytest.fixture(autouse=True)
def _system_proxy_looks_alive(monkeypatch):
    """测试里默认假设"梯子开着"（这样 `auto` 才有两条路可测）。

    真实环境里梯子关着时会返回 False，`auto` 退化成单路 —— 这是 D56 的核心行为，
    由 `test_auto_chain_only_adds_proxy_when_it_is_really_listening` 单独钉住。
    """
    import backend.llm.openai_compat as oc

    monkeypatch.setattr(oc, "system_proxy_alive", lambda *a, **k: True)
    yield
    oc._PREFERRED_PATH.clear()
    oc._PATH_FAILED_AT.clear()


@pytest.fixture(autouse=True)
def _usage_log_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_service, "USAGE_LOG_PATH", tmp_path / "usage.jsonl")


def _chunk(content=None, reasoning=None, tool_calls=None, finish_reason=None, usage=None):
    delta = _NS(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    return _NS(choices=[_NS(delta=delta, finish_reason=finish_reason)], usage=usage)


class _FakeClient:
    def __init__(self, chunks=None, fail_call=False, fail_models=False):
        self._chunks = chunks or []
        self._fail_call = fail_call
        self._fail_models = fail_models
        self._hang: float | None = None  # 卡住不返回（测"握手超时换线"）
        self.calls: list[dict] = []
        self.chat = _NS(completions=_NS(create=self._create))
        self.models = _NS(list=self._list)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._hang is not None:
            import asyncio

            await asyncio.sleep(self._hang)
        if self._fail_call:
            if isinstance(self._fail_call, BaseException):
                raise self._fail_call
            raise RuntimeError("boom")

        async def gen():
            for c in self._chunks:
                yield c

        return gen()

    async def _list(self):
        if self._fail_models:
            raise RuntimeError("no models")
        return _NS(data=[])


def _cfg(**over) -> ProviderConfig:
    data = {
        "name": "nanogpt",
        "base_url": "https://nano-gpt.com/api/v1",
        "api_key": "sk-test",
        "model": "qwen/qwen3.8-27b-uncensored",
        "capabilities": {"native_tool_calls": True, "reasoning": True, "max_context": 512000},
    }
    data.update(over)
    return ProviderConfig.from_dict(data)


# ---------- factory ----------


def test_load_configs_rejects_empty_and_bad_json():
    with pytest.raises(LLMConfigError):
        load_provider_configs(None)
    with pytest.raises(LLMConfigError):
        load_provider_configs("[]")
    with pytest.raises(LLMConfigError):
        load_provider_configs("{not json")


def test_pick_config_by_active_name():
    a = _cfg(name="a")
    b = _cfg(name="b")
    assert pick_provider_config([a, b], "b").name == "b"
    assert pick_provider_config([a, b], None).name == "a"  # 没指定 → 第一个
    with pytest.raises(LLMConfigError):
        pick_provider_config([a, b], "c")


def test_create_provider_requires_model_and_base_url():
    bases = json.dumps([{"name": "x", "base_url": "http://x", "model": ""}])
    with pytest.raises(LLMConfigError):
        create_provider(bases, "x")
    bases = json.dumps([{"name": "x", "base_url": "", "model": "m"}])
    with pytest.raises(LLMConfigError):
        create_provider(bases, "x")


def test_create_provider_ok_and_capabilities():
    cfg = _cfg()
    bases = json.dumps(
        [
            {
                "name": cfg.name,
                "base_url": cfg.base_url,
                "api_key": cfg.api_key,
                "model": cfg.model,
                "capabilities": {
                    "native_tool_calls": True,
                    "reasoning": True,
                    "reasoning_streamable": False,
                    "max_context": 512000,
                },
            }
        ]
    )
    provider = create_provider(bases, "nanogpt")
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.config.model == "qwen/qwen3.8-27b-uncensored"
    assert provider.config.capabilities.native_tool_calls is True
    assert provider.config.capabilities.max_context == 512000
    # 没配 capabilities 时保守取值（LLM协议§1.2 第 3 条）
    assert ProviderCapabilities.from_dict(None) == ProviderCapabilities()


# ---------- 流式事件归一化 ----------


async def _collect(provider: OpenAICompatProvider, **kw):
    return [ev async for ev in provider.chat_stream([{"role": "user", "content": "hi"}], **kw)]


async def test_chat_stream_normalizes_events():
    tool = _NS(id="call_1", function=_NS(name="save_memory", arguments='{"content":"x"}'))
    client = _FakeClient(
        chunks=[
            _chunk(content="你"),
            _chunk(content="好"),
            _chunk(reasoning="thinking..."),
            _chunk(tool_calls=[tool]),
            _chunk(usage=_NS(model_dump=lambda: {"prompt_tokens": 5, "completion_tokens": 3})),
            _chunk(finish_reason="stop"),
        ]
    )
    provider = OpenAICompatProvider(_cfg(), client=client)
    events = await _collect(provider)

    assert events[:-1] == [
        {"type": "content", "delta": "你"},
        {"type": "content", "delta": "好"},
        {"type": "reasoning", "delta": "thinking..."},
        {"type": "tool_call", "id": "call_1", "name": "save_memory", "arguments": '{"content":"x"}'},
    ]
    done = events[-1]
    assert done["type"] == "done"
    assert done["usage"] == {
        "input_tokens": 5,
        "output_tokens": 3,
        "reasoning_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 5,
        "total_tokens": 8,
    }
    assert done["stop_reason"] == "stop"


async def test_native_tool_call_arguments_are_accumulated_across_chunks():
    """v3.8（D169 / 补 G1）：原生 function calling 的参数是**流式分片** —— 攒齐了才发一次完整调用。

    以前是"收到几片就 yield 几个 tool_call"，每个事件的 arguments 都是半截 JSON
    （`{"content` / `:"x"}`）→ 打开原生工具也执行不了。LLM协议§3.2：完整才发。
    """
    first = _NS(index=0, id="call_1", function=_NS(name="save_memory", arguments='{"content"'))
    second = _NS(index=0, function=_NS(name=None, arguments=':"x"}'))
    client = _FakeClient(
        chunks=[
            _chunk(tool_calls=[first]),
            _chunk(tool_calls=[second]),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    events = await _collect(OpenAICompatProvider(_cfg(), client=client))

    assert events[:-1] == [
        {"type": "tool_call", "id": "call_1", "name": "save_memory", "arguments": '{"content":"x"}'}
    ]
    assert events[-1]["type"] == "done"


async def test_native_tool_calls_are_emitted_in_index_order():
    """两个调用**交错**回来（index 0/1 各来一片）→ 各攒各的、按 index 顺序发。"""
    c0a = _NS(index=0, id="a", function=_NS(name="remember", arguments='{"x"'))
    c1a = _NS(index=1, id="b", function=_NS(name="remind", arguments='{"y"'))
    c0b = _NS(index=0, function=_NS(name=None, arguments=":1}"))
    c1b = _NS(index=1, function=_NS(name=None, arguments=":2}"))
    client = _FakeClient(
        chunks=[
            _chunk(tool_calls=[c0a, c1a]),
            _chunk(tool_calls=[c0b]),
            _chunk(tool_calls=[c1b]),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    events = await _collect(OpenAICompatProvider(_cfg(), client=client))

    assert [ev for ev in events if ev["type"] == "tool_call"] == [
        {"type": "tool_call", "id": "a", "name": "remember", "arguments": '{"x":1}'},
        {"type": "tool_call", "id": "b", "name": "remind", "arguments": '{"y":2}'},
    ]


async def test_native_tool_call_without_a_name_is_dropped():
    """光有参数分片、没有名字 → 不发（发出去也没法执行，只会白占一轮）。"""
    client = _FakeClient(
        chunks=[
            _chunk(tool_calls=[_NS(index=0, id=None, function=_NS(name=None, arguments="{"))]),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    events = await _collect(OpenAICompatProvider(_cfg(), client=client))

    assert [ev["type"] for ev in events] == ["done"]


async def test_tools_only_sent_when_native_tool_calls_enabled():
    tools = [{"type": "function", "function": {"name": "save_memory"}}]

    c1 = _FakeClient(chunks=[_chunk(content="hi")])
    await _collect(OpenAICompatProvider(_cfg(capabilities={"native_tool_calls": False}), client=c1), tools=tools)
    assert "tools" not in c1.calls[0], "capability 说不用原生工具，就不该把 tools 传上去"

    c2 = _FakeClient(chunks=[_chunk(content="hi")])
    await _collect(OpenAICompatProvider(_cfg(capabilities={"native_tool_calls": True}), client=c2), tools=tools)
    assert c2.calls[0]["tools"] == tools
    assert c2.calls[0]["stream"] is True


async def test_reasoning_effort_uses_extra_body_for_old_mobile_sdk():
    """D320：旧 SDK 的 create() 不认 reasoning_effort，必须塞进 extra_body。"""
    client = _FakeClient(chunks=[_chunk(content="hi")])
    await _collect(OpenAICompatProvider(_cfg(), client=client), reasoning_effort="high")

    sent = client.calls[0]
    assert "reasoning_effort" not in sent
    assert sent["extra_body"] == {"reasoning_effort": "high"}


async def test_chat_stream_wraps_failure():
    from backend.llm.errors import LLMCallError

    provider = OpenAICompatProvider(_cfg(), client=_FakeClient(fail_call=True))
    with pytest.raises(LLMCallError):
        await _collect(provider)


async def test_opencode_gets_stable_session_header():
    client = _FakeClient(chunks=[_chunk(content="ok")])
    provider = OpenAICompatProvider(
        _cfg(base_url="https://opencode.ai/inference/openai/v1"),
        client=client,
    )
    await _collect(provider, session_id="friend-123")
    sent = client.calls[0]
    assert sent["extra_headers"]["x-opencode-session"] == "friend-123"
    assert sent["extra_headers"]["User-Agent"].startswith("yumeFriend/")


async def test_health_check():
    assert await OpenAICompatProvider(_cfg(), client=_FakeClient()).health_check() is True
    assert await OpenAICompatProvider(_cfg(), client=_FakeClient(fail_models=True)).health_check() is False


# ---------- 健壮性：超时 / 代理 / 重试（LLM协议§9） ----------


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        ("", (None, True)),          # 没配 → 跟随环境变量 / Windows 系统代理
        (None, (None, True)),
        ("   ", (None, True)),
        ("none", (None, False)),     # 强制直连
        ("DIRECT", (None, False)),
        ("http://127.0.0.1:8080", ("http://127.0.0.1:8080", False)),
    ],
)
def test_resolve_proxy(proxy, expected):
    assert resolve_proxy(proxy) == expected


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        ("none", ["none"]),
        ("", [""]),
        ("http://127.0.0.1:8080", ["http://127.0.0.1:8080"]),
    ],
)
def test_proxy_chain(proxy, expected):
    assert proxy_chain(proxy) == expected


def test_auto_chain_only_adds_proxy_when_it_is_really_listening(monkeypatch):
    """梯子关着时，"" 其实还是直连 —— 不能把它当备选路，否则护栏会掐死慢直连（D56）。"""
    import backend.llm.openai_compat as oc

    monkeypatch.setattr(oc, "system_proxy_alive", lambda *a, **k: False)
    assert proxy_chain("auto") == ["none"], "系统代理没在监听 → 只有一条路"
    monkeypatch.setattr(oc, "system_proxy_alive", lambda *a, **k: True)
    assert proxy_chain("auto") == ["none", ""], "真的在监听才给第二条路"


def test_system_proxy_alive_false_when_port_closed(monkeypatch):
    import backend.llm.openai_compat as oc

    # 上面那个 autouse fixture 把它换成了假函数，这里换回真的来测
    monkeypatch.setattr(oc, "system_proxy_alive", _REAL_SYSTEM_PROXY_ALIVE)
    monkeypatch.setattr(oc, "_system_proxy_address", lambda: ("127.0.0.1", 9))  # 没人听
    assert oc.system_proxy_alive(timeout=0.2) is False


async def test_auto_mode_falls_back_when_direct_fails():
    """直连连不上 → 自动换下一条（系统代理）。就是"梯子不开也能用"的兜底。"""
    import httpx
    from openai import APIConnectionError

    direct = _FakeClient(fail_call=APIConnectionError(request=httpx.Request("POST", "http://x")))
    via_proxy = _FakeClient(chunks=[_chunk(content="走代理成功")])

    provider = OpenAICompatProvider(_cfg(proxy="auto"))
    provider._clients = [direct, via_proxy]  # 第一条直连，第二条走代理
    import backend.llm.openai_compat as oc

    oc._PREFERRED_PATH.clear()  # 别让别的测试的"记忆"影响这里
    try:
        events = await _collect(provider)
        assert events[0] == {"type": "content", "delta": "走代理成功"}
        assert len(direct.calls) == 1 and len(via_proxy.calls) == 1
        # 记住"代理那条好用"→ 下一次不用再白等直连超时（D52）
        assert oc._PREFERRED_PATH[provider._path_key()][0] == 1
        direct2 = _FakeClient(fail_call=True)
        provider._clients = [direct2, via_proxy]
        await _collect(provider)
        assert direct2.calls == [], "上一轮已经知道直连不通，这次不该再试直连"
    finally:
        oc._PREFERRED_PATH.clear()
        await provider.aclose()


async def test_non_connection_errors_do_not_trigger_fallback():
    """4xx / 业务错误不该被"换线路"掩盖掉。"""
    direct = _FakeClient(fail_call=RuntimeError("boom"))
    fallback = _FakeClient(chunks=[_chunk(content="不该走到这里")])

    provider = OpenAICompatProvider(_cfg(proxy="auto"))
    provider._clients = [direct, fallback]
    import backend.llm.openai_compat as oc

    oc._PREFERRED_PATH.clear()
    with pytest.raises(Exception):
        await _collect(provider)
    assert fallback.calls == []
    oc._PREFERRED_PATH.clear()


async def test_failed_path_gets_benched_for_a_while():
    """刚失败的路线先晾一会儿：别每次都在坏路上白等连接超时（D52）。"""
    import backend.llm.openai_compat as oc
    from openai import APIConnectionError
    import httpx

    oc._PREFERRED_PATH.clear()
    oc._PATH_FAILED_AT.clear()

    direct = _FakeClient(fail_call=APIConnectionError(request=httpx.Request("POST", "http://x")))
    ok = _FakeClient(chunks=[_chunk(content="好")])
    provider = OpenAICompatProvider(_cfg(proxy="auto"))
    provider._clients = [direct, ok]
    try:
        await _collect(provider)
        order = provider._attempt_order()  # noqa: SLF001
        assert order == [1, 0], "刚失败过的直连应该排到最后"
        # D89：失败时**顺手把偏好切到另一条**。所以哪怕惩罚窗口过了（这里直接清掉），
        # 下一次也不会再去碰那条坏路 —— "偶发一次抽风"不会连着放大成"每条都白等 20 秒"。
        assert oc._PREFERRED_PATH[provider._path_key()][0] == 1
        oc._PATH_FAILED_AT.clear()
        assert provider._attempt_order() == [1, 0], "偏好已经切过去了，不该再回头试坏路"
    finally:
        oc._PREFERRED_PATH.clear()
        oc._PATH_FAILED_AT.clear()
        await provider.aclose()


async def test_stalled_handshake_switches_path(monkeypatch):
    """握手太久（GFW 抖动实测能到 26s）→ 弃掉这条路换下一条（D53）。"""
    import backend.llm.openai_compat as oc

    slow = _FakeClient(chunks=[_chunk(content="不该等到我")])
    slow._hang = 3600
    fast = _FakeClient(chunks=[_chunk(content="换线成功")])

    monkeypatch.setattr(oc, "FIRST_BYTE_TIMEOUT", 0.05)
    provider = OpenAICompatProvider(_cfg(proxy="auto"))
    provider._clients = [slow, fast]
    try:
        events = await _collect(provider)
        assert events[0] == {"type": "content", "delta": "换线成功"}
        assert provider.last_path_label == "系统代理"
    finally:
        await provider.aclose()


async def test_provider_uses_spec_timeouts_and_retries():
    """超时分开设；connect 放宽到 8s 是因为走梯子 TLS 就要 5.4~5.7s（D54）。"""
    provider = OpenAICompatProvider(_cfg())  # 不传 client → 走真实构造（不发请求）
    try:
        timeout = provider._http_client.timeout
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (8.0, 120.0, 10.0, 5.0)
        assert timeout is not None and DEFAULT_TIMEOUT.connect == 8.0
        assert provider._client.max_retries == 1  # D45：最多试 2 次，别让用户干等
        assert provider._client.base_url.host == "nano-gpt.com"
    finally:
        await provider.aclose()


async def test_multi_path_uses_one_attempt_per_path():
    """多条路时每条只试一次：换路本身就是重试，别在坏路上连试两遍（D54）。"""
    provider = OpenAICompatProvider(_cfg(proxy="auto"))
    try:
        assert [c.max_retries for c in provider._clients] == [0, 0]
    finally:
        await provider.aclose()

    single = OpenAICompatProvider(_cfg(proxy="none"))
    try:
        assert [c.max_retries for c in single._clients] == [1]
    finally:
        await single.aclose()


async def test_first_byte_guard_is_per_path():
    """D58：护栏对所有路统一 20 秒（单路也掐 —— 宁可给明确报错，也别干等两分钟）。"""
    import backend.llm.openai_compat as oc

    auto = OpenAICompatProvider(_cfg(proxy="auto"))
    try:
        assert oc.FIRST_BYTE_TIMEOUT == 20.0
        assert auto._first_byte_timeout(0) == 20.0
        assert auto._first_byte_timeout(1) == 20.0
    finally:
        await auto.aclose()

    via_proxy = OpenAICompatProvider(_cfg(proxy="http://127.0.0.1:8080"))
    try:
        assert via_proxy._first_byte_timeout(0) == 20.0, "单路也要掐，但报错要说人话"
    finally:
        await via_proxy.aclose()


async def test_stalled_single_path_reports_readable_error(monkeypatch):
    """卡住时不许抛"空消息"——要给能看懂的报错（D58）。"""
    import backend.llm.openai_compat as oc

    slow = _FakeClient(chunks=[_chunk(content="不该等到我")])
    slow._hang = 3600
    monkeypatch.setattr(oc, "FIRST_BYTE_TIMEOUT", 0.05)

    provider = OpenAICompatProvider(_cfg(proxy="none"))  # 单路
    provider._clients = [slow]
    try:
        # chat_stream 会统一包成 LLMCallError（接口契约），但**消息必须能看懂**
        with pytest.raises(LLMCallError) as info:
            await _collect(provider)
        assert "等待响应头超过" in str(info.value)
        assert not str(info.value).rstrip().endswith("："), "报错不能是空的"
    finally:
        await provider.aclose()


async def test_timeout_hint_tells_the_user_what_to_do(monkeypatch):
    """D89：卡住时的报错要**能照着做** —— 单条直连路就提示"开梯子 / 改成走梯子"。"""
    import backend.llm.openai_compat as oc

    monkeypatch.setattr(oc, "FIRST_BYTE_TIMEOUT", 0.05)

    slow = _FakeClient(chunks=[_chunk(content="不该等到我")])
    slow._hang = 3600
    direct_only = OpenAICompatProvider(_cfg(proxy="none"))
    direct_only._clients = [slow]
    try:
        with pytest.raises(LLMCallError) as info:
            await _collect(direct_only)
        message = str(info.value)
        assert "只有直连这一条路" in message
        assert "走梯子" in message
    finally:
        await direct_only.aclose()

    slow2 = _FakeClient(chunks=[_chunk(content="不该等到我")])
    slow2._hang = 3600
    via_proxy = OpenAICompatProvider(_cfg(proxy="http://127.0.0.1:8080"))
    via_proxy._clients = [slow2]
    try:
        with pytest.raises(LLMCallError) as info:
            await _collect(via_proxy)
        message = str(info.value)
        assert "走梯子" not in message  # 已经在走代理了，别再教他"改成走梯子"
        assert "梯子/节点" in message
    finally:
        await via_proxy.aclose()


async def test_preferred_path_expires_so_we_retry_the_fast_one():
    """偏好带 TTL：别因为五分钟前梯子好用，就永远走慢的那条（D57）。"""
    import time as _time

    import backend.llm.openai_compat as oc

    provider = OpenAICompatProvider(_cfg(proxy="auto"))
    try:
        provider._remember_path(1)  # noqa: SLF001 —— 假装梯子刚成功过
        assert provider._attempt_order()[0] == 1
        # 把"记住的时间"改成 TTL 之前 → 偏好过期，回到配置顺序（直连优先）
        key = provider._path_key()
        oc._PREFERRED_PATH[key] = (1, _time.monotonic() - oc._PREFERRED_TTL_SECONDS - 1)
        assert provider._attempt_order()[0] == 0
    finally:
        await provider.aclose()


def test_provider_config_defaults_for_proxy_and_retries():
    cfg = _cfg()
    assert cfg.proxy == ""
    assert cfg.max_retries == 1
    assert _cfg(proxy="none", max_retries=1).proxy == "none"
    assert _cfg(proxy="none", max_retries=1).max_retries == 1


async def test_http_connections_are_reused_across_providers():
    """D45：同一个代理设置下必须复用同一条连接（不然每条消息都重新握手）。"""
    first = OpenAICompatProvider(_cfg())
    second = OpenAICompatProvider(_cfg())
    try:
        assert first._http_client is get_shared_client("")  # noqa: SLF001
        assert first._http_client is second._http_client
        # 关掉一个不能影响后面（连接池是共享的）
        await first.aclose()
        assert not second._http_client.is_closed
    finally:
        await close_shared_clients()


async def test_error_message_keeps_request_id():
    from backend.llm.errors import LLMCallError

    provider = OpenAICompatProvider(_cfg(), client=_FakeClient(fail_call=True))
    with pytest.raises(LLMCallError) as ei:
        await _collect(provider)
    assert "request_id=" in str(ei.value)
    assert "boom" in str(ei.value)
