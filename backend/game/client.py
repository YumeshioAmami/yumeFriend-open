"""适配器客户端（D250 契约 v1）。

主代码**只通过这个类**跟适配器说话：HTTP 走 httpx，事件走 WebSocket。
把"本地小服务"包装成几个 async 方法，别让业务层关心端口 / token / JSON。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Mapping
from urllib.parse import quote

import httpx
import websockets

from backend.game.contract import (
    Capabilities,
    ContractError,
    GameEvent,
    UnsupportedVersionError,
)


class AdapterError(RuntimeError):
    """适配器报错 / 连不上。`message` 是给人看的。"""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _error_detail(response: httpx.Response) -> Mapping[str, Any]:
    """兼容两种错误体：`{"code","message"}` 与 FastAPI 的 `{"detail": {...}}`。"""
    try:
        payload = response.json()
    except ValueError:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    detail = payload.get("detail")
    if isinstance(detail, Mapping):
        return detail
    return payload


class AdapterClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._timeout = timeout
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AdapterClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise AdapterError(f"连不上游戏适配器：{exc}") from exc

        if response.status_code >= 400:
            detail = _error_detail(response)
            code = str(detail.get("code") or "") or None
            message = str(detail.get("message") or "").strip() or (
                f"适配器返回 HTTP {response.status_code}"
            )
            if code == "UNSUPPORTED_VERSION":
                raise UnsupportedVersionError(message)
            raise AdapterError(message, code=code, status=response.status_code)

        try:
            payload = response.json()
        except ValueError as exc:
            raise AdapterError("适配器返回的不是 JSON") from exc
        if not isinstance(payload, dict):
            raise AdapterError("适配器返回的不是 JSON 对象")
        return payload

    # ---------- 契约端点 ----------

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def capabilities(self) -> Capabilities:
        return Capabilities.from_payload(await self._request("GET", "/capabilities"))

    async def start_session(self, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self._request(
            "POST", "/session/start", json={"config": dict(config or {})}
        )

    async def stop_session(self) -> dict[str, Any]:
        return await self._request("POST", "/session/stop", json={})

    async def command(
        self, action: str, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST", "/command", json={"action": action, "args": dict(args or {})}
        )

    async def cancel(self, task_id: str) -> dict[str, Any]:
        return await self._request("POST", "/cancel", json={"task_id": task_id})

    async def snapshot(self) -> dict[str, Any]:
        return await self._request("GET", "/snapshot")

    async def events(self) -> AsyncIterator[GameEvent]:
        """事件流。连接断开时迭代结束；调用方据此判断 crashed。"""
        ws_base = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{ws_base}/events?token={quote(self.token, safe='')}"
        async with websockets.connect(url, open_timeout=self._timeout) as socket:
            async for raw in socket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                try:
                    yield GameEvent.from_payload(payload)
                except ContractError:
                    # 单条坏事件不该掐掉整条流；跳过并继续。
                    continue
