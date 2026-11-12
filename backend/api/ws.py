"""WebSocket：**只用于主动消息推送**（api.md §7）。

聊天本身不用 WebSocket（SSE 足够，见 LLM协议§3.3）；这里只做一件事：
朋友主动冒泡、提醒到点时，把消息推给前端。
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

router = APIRouter(tags=["ws"])


class ConnectionManager:
    """进程内连接表。单人本地应用，不需要 Redis 那套。"""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    @property
    def count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        logger.info("WebSocket 已连接（当前 {} 个）", self.count)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)
        logger.info("WebSocket 断开（剩 {} 个）", self.count)

    async def broadcast(self, payload: dict) -> int:
        """发给所有连着的客户端；发不出去的直接踢掉。返回成功条数。"""
        sent = 0
        for websocket in list(self._connections):
            try:
                await websocket.send_json(payload)
                sent += 1
            except Exception:  # noqa: BLE001 —— 连着的对端没了，踢掉就行
                self.disconnect(websocket)
        return sent


manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            # 前端可以发心跳（内容不重要）；这里必须一直读，否则收不到断开事件
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:  # noqa: BLE001
        manager.disconnect(websocket)
