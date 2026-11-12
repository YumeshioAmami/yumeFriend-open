"""T5.4 验收：WebSocket 推送（api.md §7）。

只测连接表本身（广播 / 踢掉坏连接）——它才是"消息能不能推到前端"的关键。
"""

import pytest

from backend.api.ws import ConnectionManager


class FakeWebSocket:
    def __init__(self, broken: bool = False) -> None:
        self.accepted = False
        self.sent: list[dict] = []
        self._broken = broken

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        if self._broken:
            raise RuntimeError("对端没了")
        self.sent.append(payload)


async def test_broadcast_reaches_everyone():
    manager = ConnectionManager()
    a, b = FakeWebSocket(), FakeWebSocket()
    await manager.connect(a)
    await manager.connect(b)
    assert manager.count == 2

    sent = await manager.broadcast({"type": "new_message", "friend_name": "小敏"})
    assert sent == 2
    assert a.sent[0]["friend_name"] == "小敏"
    assert b.sent == a.sent


async def test_broken_connection_is_kicked_out():
    """前端关掉页面后不能拖累广播 —— 发失败的直接踢掉。"""
    manager = ConnectionManager()
    good, bad = FakeWebSocket(), FakeWebSocket(broken=True)
    await manager.connect(good)
    await manager.connect(bad)

    assert await manager.broadcast({"type": "new_message"}) == 1
    assert manager.count == 1
    assert good.sent


async def test_broadcast_with_no_clients_is_harmless():
    manager = ConnectionManager()
    assert await manager.broadcast({"type": "new_message"}) == 0
