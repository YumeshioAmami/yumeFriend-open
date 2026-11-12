"""唯一的 provider 实现：OpenAI 兼容端点（LLM协议§1.3）。

NanoGPT / Ollama / DeepSeek 都兼容 OpenAI 协议，只是 base_url 不同。
"""

import asyncio
import os
import socket
import sys
import uuid
from typing import Any, AsyncIterator

import httpx
from loguru import logger
from openai import AsyncOpenAI

from backend.llm.base import ILLMProvider, ProviderConfig
from backend.llm.errors import LLMCallError
from backend.services import usage_service

# 超时必须分开设（LLM协议§9.3）：连不上别等，读流可以等很久。
# ⚠️ connect 原来是 5s，实测**走梯子时 TLS 握手就要 5.4~5.7s** —— 正好卡在脖子上，
# 于是"梯子那条路"几乎永远失败（D54）。放宽到 8s。
DEFAULT_TIMEOUT = httpx.Timeout(connect=8.0, read=120.0, write=10.0, pool=5.0)

# "响应头护栏"：等**首个响应字节**超过 20 秒就放弃（多条路时换下一条）。
# 对所有路统一 20 秒（用户拍板，D58）——
# 实测依据：直连正常 1.2~4s、慢但能用 14~20s、真卡死 26s+；
#           梯子正常 1.3~19.5s。20s 是"两边都能过、卡死能掐掉"的那个值。
# 注意：护栏量的是**首个响应字节**（含上游排队），不是整段生成 ——
#       所以它不会掐掉"想得久但正常"的长回复。
FIRST_BYTE_TIMEOUT = 20.0


class FirstByteTimeoutError(Exception):
    """等响应头被护栏掐掉（是"卡住"了，不是上游报错）。消息要能看懂 + **能照着做**（D89）。"""

    def __init__(self, seconds: float, label: str, hint: str = "") -> None:
        tail = f" {hint}" if hint else ""
        super().__init__(
            f"等待响应头超过 {seconds:.0f} 秒（{label}这条路可能卡住了）。{tail}"
        )

# 强制直连的写法（小写比较）
_DIRECT_PROXY_VALUES = {"none", "direct", "off", "no", "-"}

# 自动模式：直连优先，连不上自动改走系统代理（给"梯子时开时关"的环境，D52）
AUTO_PROXY_VALUE = "auto"

# 连接池复用：HTTP 连接（含 TLS 握手）很贵，按 (proxy, trust_env) 缓存 client，
# 让同一个 provider 配置下的多次请求共用一条连接（实测能省 0.2~0.5s，冷启动时更多）。
_SHARED_CLIENTS: dict[tuple[str | None, bool], httpx.AsyncClient] = {}

# "哪条路现在好用"的记忆（D52）。没有它的话，直连断了之后**每次**都要先
# 白等一次直连超时（约 10s）才换线 —— 实测就是这个把 2s 拖成了 22s。
# key = (base_url, 尝试链)，value = (上次成功的下标, 记住的时间)。
# 带 TTL：别因为"五分钟前梯子好用"就**永远**走慢的那条 —— 到点再试一次直连。
_PREFERRED_PATH: dict[tuple[str, tuple[str, ...]], tuple[int, float]] = {}
_PREFERRED_TTL_SECONDS = 300.0

# 刚失败的路线，先晾一会儿（秒）。没有它的话，直连一断，**每次**都要重付一遍
# 连接超时（实测每次白等 ~10s）。窗口过了还会再试，不会永久放弃某条路。
# D89：120 → 300，跟 `_PREFERRED_TTL_SECONDS` 对齐（"记住哪条好用"的窗口一样长）。
_PATH_PENALTY_SECONDS = 300.0
_PATH_FAILED_AT: dict[tuple[str, tuple[str, ...], int], float] = {}


def resolve_proxy(proxy: str | None) -> tuple[str | None, bool]:
    """把配置里的 proxy 翻译成给 httpx 的参数。

    返回 (proxy, trust_env)：
      - ""/None      → (None, True)   跟随环境变量 / Windows 系统代理（httpx 自己读注册表）
      - "none"/"direct" 等 → (None, False) 强制直连（本机走系统代理反而慢时用这个）
      - 其它非空字符串 → (该 URL, False)  显式指定代理

    背景：本机开梯子时，httpx 默认会读注册表里的系统代理，实测比直连慢 5 倍。
    """
    value = (proxy or "").strip()
    if not value:
        return None, True
    if value.lower() in _DIRECT_PROXY_VALUES:
        return None, False
    return value, False


def proxy_chain(proxy: str | None) -> list[str]:
    """按配置给出**尝试顺序**（前面的优先）。

    · `"auto"` → 先直连；**只有系统代理真的在监听**时，才把"走系统代理"当作备选。
      （梯子关着的时候，"" 其实还是直连 —— 把它当备选会让"两条路"变成同一个东西，
      于是 12s 护栏会把"慢但能用"的直连掐死，用户看到的就是莫名失败。踩过一次。）
    · 其它值 → 就它自己（不做花活）
    """
    if (proxy or "").strip().lower() == AUTO_PROXY_VALUE:
        return ["none", ""] if system_proxy_alive() else ["none"]
    return [proxy or ""]


def _system_proxy_address() -> tuple[str, int] | None:
    """系统代理的 host:port（先看环境变量，再看 Windows 注册表）。"""
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name)
        if value:
            parsed = httpx.URL(value)
            if parsed.host:
                return parsed.host, parsed.port or 80
    if sys.platform != "win32":
        return None
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    first = str(server).split(";")[0]
    if "=" in first:
        first = first.split("=", 1)[1]
    if ":" not in first:
        return None
    host, port = first.rsplit(":", 1)
    try:
        return host, int(port)
    except ValueError:
        return None


def system_proxy_alive(timeout: float = 0.3) -> bool:
    """系统代理是不是真的有个东西在监听。梯子关了这里会返回 False。"""
    address = _system_proxy_address()
    if address is None:
        return False
    try:
        with socket.create_connection(address, timeout=timeout):
            return True
    except OSError:
        return False


def path_label(proxy_value: str | None) -> str:
    """给日志看的路径名（"慢在哪条路上"要一眼看出来）。"""
    value = (proxy_value or "").strip()
    if not value:
        return "系统代理"
    if value.lower() in _DIRECT_PROXY_VALUES:
        return "直连"
    return f"代理({value})"


def _is_connection_error(exc: BaseException) -> bool:
    """只对"根本没连上"的错误做切换。

    已经在流中途断掉的错误**不切换**——那时正文可能已经发给用户了，
    重来一遍会重复输出（LLM协议§4.3 的边界）。
    """
    import openai

    if isinstance(exc, (openai.APIConnectionError, httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return isinstance(exc, httpx.TransportError) and not isinstance(exc, httpx.ReadTimeout)


def get_shared_client(proxy: str | None) -> httpx.AsyncClient:
    """按代理设置取一个进程级共享的 httpx client（连接池在里面）。"""
    key = resolve_proxy(proxy)
    client = _SHARED_CLIENTS.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            proxy=key[0],
            trust_env=key[1],
            follow_redirects=True,
            # D87：httpx 默认**空闲 5 秒**就把连接从池里丢掉 → "隔几分钟说一句话"每次
            # 都要重新 TCP+TLS 握手，这正是"第一句特别慢"的一大来源。把空闲窗口拉长，
            # 再配合定时预热（D87），基本就一直是热的。
            limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=180.0),
        )
        _SHARED_CLIENTS[key] = client
    return client


async def close_shared_clients() -> None:
    """进程退出 / 测试收尾用。"""
    for client in list(_SHARED_CLIENTS.values()):
        await client.aclose()
    _SHARED_CLIENTS.clear()


class OpenAICompatProvider(ILLMProvider):
    def __init__(self, config: ProviderConfig, client: AsyncOpenAI | None = None) -> None:
        self._config = config
        # 允许注入 client（测试用假 client；生产走真实端点）
        self._http_client: httpx.AsyncClient | None = None
        self._clients: list[AsyncOpenAI] = []
        if client is not None:
            self._client = client
            self._clients = [client]
            self._labels = ["注入的 client"]
        else:
            # 按 proxy_chain 的顺序准备候选连接（auto = 直连 → 系统代理）
            chain = proxy_chain(config.proxy)
            self._labels = [path_label(value) for value in chain]
            # 有多条路时**每条路只试一次**：换路本身就是重试，不必在坏路上连试两遍
            # （实测：路上失败时"每条路试两次"会把等待翻倍 —— D54）
            per_path_retries = config.max_retries if len(chain) == 1 else 0
            for proxy_value in chain:
                # 共享 client：请求之间复用连接，不每次都重新握手
                http_client = get_shared_client(proxy_value)
                if self._http_client is None:
                    self._http_client = http_client
                self._clients.append(
                    AsyncOpenAI(
                        api_key=config.api_key or "not-needed",
                        base_url=config.base_url or None,
                        max_retries=per_path_retries,
                        http_client=http_client,
                    )
                )
            self._client = self._clients[0]
        self.last_path_label = self._labels[0]

    async def _open_stream(
        self,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ):
        """建立流式连接；`auto` 模式下连不上就换下一个候选（并记住哪条好用）。"""
        import time

        last_error: BaseException | None = None
        order = self._attempt_order()
        for position, index in enumerate(order):
            client = self._clients[index]
            started = time.monotonic()
            try:
                limit = self._first_byte_timeout(index)
                if limit is not None:
                    stream = await asyncio.wait_for(
                        client.chat.completions.create(
                            **payload,
                            **({"extra_headers": extra_headers} if extra_headers else {}),
                        ),
                        limit,
                    )
                else:
                    stream = await client.chat.completions.create(
                        **payload,
                        **({"extra_headers": extra_headers} if extra_headers else {}),
                    )
                self._remember_path(index)
                self.last_path_label = self._labels[index]
                logger.info(
                    # 注意：这不是 TCP/TLS 握手（连接是复用连接池的），而是
                    # **等上游吐出第一个响应字节**的耗时（含上游排队 + 预填充）
                    "LLM 响应头到达：路径={} 等首字节={:.2f}s",
                    self._labels[index],
                    time.monotonic() - started,
                )
                return stream
            except Exception as exc:  # noqa: BLE001 —— 判断完不是连接问题就原样抛
                stalled = isinstance(exc, asyncio.TimeoutError)
                if position + 1 >= len(order):
                    # 最后一条路也没了：报错要说人话（别抛 str() 为空的 TimeoutError）
                    if stalled:
                        self._penalize_path(index)
                        raise FirstByteTimeoutError(
                            limit or FIRST_BYTE_TIMEOUT,
                            self._labels[index],
                            self._timeout_hint(index),
                        ) from exc
                    raise
                if not (stalled or _is_connection_error(exc)):
                    raise
                last_error = exc
                self._penalize_path(index)
                logger.warning(
                    "第 {} 条路{}（白等 {:.1f}s），自动换下一条",
                    index + 1,
                    "等响应头超时" if stalled else f"连不上：{type(exc).__name__}",
                    time.monotonic() - started,
                )
        raise last_error  # pragma: no cover —— 循环里必定 return 或 raise

    def _path_key(self) -> tuple[str, tuple[str, ...]]:
        return (self._config.base_url, tuple(proxy_chain(self._config.proxy)))

    def _attempt_order(self) -> list[int]:
        """尝试顺序：没被罚站的优先，其中"上次好用的那条"排最前。"""
        import time

        count = len(self._clients)
        key = self._path_key()
        now = time.monotonic()

        preferred = 0
        remembered = _PREFERRED_PATH.get(key)
        if remembered is not None:
            index, remembered_at = remembered
            # 过期的偏好不算数：重新从配置顺序开始，给直连一个翻身的机会
            if 0 <= index < count and now - remembered_at <= _PREFERRED_TTL_SECONDS:
                preferred = index

        def penalized(index: int) -> int:
            failed_at = _PATH_FAILED_AT.get((*key, index))
            return 1 if failed_at is not None and now - failed_at < _PATH_PENALTY_SECONDS else 0

        return sorted(
            range(count), key=lambda i: (penalized(i), 0 if i == preferred else 1, i)
        )

    def _remember_path(self, index: int) -> None:
        if len(self._clients) > 1:
            import time

            _PREFERRED_PATH[self._path_key()] = (index, time.monotonic())

    def _penalize_path(self, index: int) -> None:
        """记下"这条路刚失败过"，短时间内不再优先试它；**并且立刻把偏好切到另一条**（D89）。

        为什么顺手切偏好：失败一次就已经白等了 20 秒，如果只记"惩罚"而偏好还停在
        原地，**下一条消息**又会先试这条路、又白等 20 秒（实测就是这样把"偶发一次"
        放大成"连着几条都慢"）。切了偏好之后，后面的请求直接走另一条。
        """
        import time

        if len(self._clients) > 1:
            now = time.monotonic()
            _PATH_FAILED_AT[(*self._path_key(), index)] = now
            others = [i for i in range(len(self._clients)) if i != index]
            if others:
                _PREFERRED_PATH[self._path_key()] = (others[0], now)

    def _timeout_hint(self, index: int) -> str:
        """超时的时候告诉用户**接下来能做什么**（D89，别再只说"可以直接重试"）。"""
        label = self._labels[index] if 0 <= index < len(self._labels) else "这条"
        if len(self._clients) <= 1:
            if "直连" in label:
                return (
                    "现在只有直连这一条路 —— 如果你有梯子，打开它，"
                    "并在设置里把「网络走法」改成「走梯子」，再重试。"
                )
            return "检查一下梯子/节点是不是断了，或者在设置里换个「网络走法」再试。"
        return "两条路都卡住了 —— 等一会儿再试，或者换一个更快的节点。"

    def _first_byte_timeout(self, index: int) -> float | None:
        """这条路等响应头最多等多久。**所有路统一 20 秒**（D58）。

        单条路也掐：宁可 20 秒给个明确报错让人重试，也别干等两分钟。
        """
        return FIRST_BYTE_TIMEOUT

    @property
    def config(self) -> ProviderConfig:
        return self._config

    async def aclose(self) -> None:
        """**不关共享连接池**（关了下一个请求又要重新握手）。

        真正要释放连接用 `close_shared_clients()`（进程退出时）。
        保留这个方法是为了兼容调用方（请求结束时的收尾是无害的）。
        """

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        usage_purpose: str = "chat",
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        session_id = str(kwargs.pop("session_id", "") or "").strip()
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "stream": True,
        }
        if tools and self._config.capabilities.native_tool_calls:
            payload["tools"] = tools
        payload.update(kwargs)
        # D320：手机端 Chaquopy 固定的 openai==1.30.0 还不认 `reasoning_effort`
        # 这个显式参数（桌面端新 SDK 认）。`extra_body` 在旧 SDK 里就有，且会原样
        # 合并进请求 JSON 顶层，所以用它在所有 SDK 版本上发同一个字段。
        reasoning_effort = payload.pop("reasoning_effort", None)
        if reasoning_effort is not None:
            extra_body = dict(payload.get("extra_body") or {})
            extra_body["reasoning_effort"] = reasoning_effort
            payload["extra_body"] = extra_body

        request_id = uuid.uuid4().hex[:8]
        extra_headers: dict[str, str] | None = None
        if "opencode.ai" in (self._config.base_url or "") and session_id:
            # OpenCode Go / Zen 要求稳定会话 ID 做路由和提示缓存；它不是标准
            # OpenAI 字段，必须放在 HTTP header 里。
            extra_headers = {
                "x-opencode-session": session_id,
                "User-Agent": "yumeFriend/1.0",
            }

        try:
            stream = await self._open_stream(payload, extra_headers=extra_headers)
        except Exception as exc:  # noqa: BLE001 —— 统一包成 LLMCallError 交给上层
            # 有些异常（比如 asyncio.TimeoutError）的 str() 是空的，
            # 直接拼会得到"失败："这种没头没尾的提示 —— 兜底写类型名
            reason = str(exc) or type(exc).__name__
            raise LLMCallError(f"调用 LLM 失败 request_id={request_id}：{reason}") from exc

        usage: dict[str, Any] = {}
        stop_reason: str | None = None
        # v3.8（D169 / G1）：原生工具调用的参数分片 —— 按 index 攒，流结束再发（见下面的注释）
        pending_calls: dict[int, dict[str, Any]] = {}

        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage.model_dump() if hasattr(chunk.usage, "model_dump") else dict(chunk.usage)
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                if getattr(choice, "finish_reason", None):
                    stop_reason = choice.finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                # 正文
                content = getattr(delta, "content", None)
                if content:
                    yield {"type": "content", "delta": content}

                # 思考链（部分 provider 会单独给）
                reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if reasoning:
                    yield {"type": "reasoning", "delta": reasoning}

                # 原生 function calling
                #
                # v3.8（D169 / 补 G1）：**参数是流式分片** —— 官方语义是"每个 delta 带一小段
                # arguments，按 index 归到同一个调用上"。以前这里"收到几片就 yield 几个 tool_call"，
                # 每个事件的 arguments 都是半截 JSON → 打开原生工具也执行不了（G1 一直开放的原因）。
                # 按 LLM协议§3.2：**解析出一个完整调用才发** —— 所以先按 index 攒，
                # 到流结束（或下一个不同 index 出现）再一次性发出去。
                for position, call in enumerate(getattr(delta, "tool_calls", None) or []):
                    index = getattr(call, "index", None)
                    slot = pending_calls.setdefault(
                        position if index is None else int(index),
                        {"id": None, "name": "", "arguments": ""},
                    )
                    call_id = getattr(call, "id", None)
                    if call_id and not slot["id"]:
                        slot["id"] = call_id  # id 只在第一片里出现
                    fn = getattr(call, "function", None)
                    if fn is not None:
                        name = getattr(fn, "name", None)
                        if name and not slot["name"]:
                            slot["name"] = name  # name 也只在第一片里出现
                        fragment = getattr(fn, "arguments", None)
                        if fragment:
                            slot["arguments"] += fragment
        except Exception as exc:  # noqa: BLE001
            reason = str(exc) or type(exc).__name__
            raise LLMCallError(f"流式响应中断 request_id={request_id}：{reason}") from exc

        # v3.8（D169）：参数攒齐了才发（多个调用按 index 顺序）
        for slot in pending_calls.values():
            if slot["name"]:
                yield {
                    "type": "tool_call",
                    "id": slot["id"] or "tool_native",
                    "name": slot["name"],
                    "arguments": slot["arguments"],
                }
        usage_service.record_call(
            provider=self._config.name,
            model=self._config.model,
            purpose=usage_purpose,
            usage=usage,
            request_id=request_id,
        )
        yield {
            "type": "done",
            "usage": usage_service.normalize_usage(usage),
            "stop_reason": stop_reason,
        }

    async def probe(self) -> None:
        """最小请求 = 列模型列表；失败原样抛（让"测试连接"能显示原因）。"""
        last_error: BaseException | None = None
        probe_headers: dict[str, str] | None = None
        if "opencode.ai" in (self._config.base_url or ""):
            probe_headers = {
                "x-opencode-session": "yumefriend-probe",
                "User-Agent": "yumeFriend/1.0",
            }
        for position, index in enumerate(self._attempt_order()):
            client = self._clients[index]
            try:
                if probe_headers:
                    await client.models.list(extra_headers=probe_headers)
                else:
                    await client.models.list()
                self._remember_path(index)
                return
            except Exception as exc:  # noqa: BLE001
                order_size = len(self._clients)
                if position + 1 >= order_size or not _is_connection_error(exc):
                    raise
                last_error = exc
                logger.warning("probe 第 {} 条路连不上（{}），换下一条", index + 1, type(exc).__name__)
        if last_error is not None:  # pragma: no cover
            raise last_error
