r"""**后台跑完这一轮回复**（v3.14 / D182）。

## 为什么要它

以前"一轮回复"活在 **HTTP 请求里**：用户关掉窗口 / 切走页面 / 手机返回 → SSE 断开 →
Starlette 把 `GeneratorExit` 打进生成器 → 我们只能把已生成的部分落库标 `partial`，
**回复当场被截断**。用户实测抱怨的就是这个："她正说着呢，一关窗口就不说了。"

现在：真正干活的是这里的一个 **后台 task**，HTTP 那段只是"订阅者"。

```
POST /messages ──▶ reply_runs.start()  ──▶ task: 跑 loop.stream_reply（自己的 session）
                        │                        │
                        │                        └─▶ publish(每一帧) ──▶ 每个订阅者的 Queue
                        └─▶ 订阅者：SSE 生成器（前端连着的那个）

客户端断开 ──▶ 订阅者退订（task 继续跑）──▶ 跑完照常落库 completed + 推 WS
用户按停止 ──▶ reply_runs.stop() → cancel 那个 task → 落库 partial（跟以前一样）
```

一条设计红线：**task 自己开 session**（`session_scope()`），
绝不用请求级的那个 —— 请求一结束 FastAPI 就在**别的线程**里 commit/close 它，
跟后台 task 并发用同一个 Session 是自找麻烦。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable, Coroutine

from loguru import logger


class ReplyRun:
    """一轮正在跑的回复：一个后台 task + 若干订阅者。"""

    def __init__(self, *, assistant_id: str, friend_id: str, friend_name: str) -> None:
        self.assistant_id = assistant_id
        self.friend_id = friend_id
        self.friend_name = friend_name
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.finished = False
        self.stopped = False

    # -- 订阅（SSE 那一段） -------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def publish(self, frame: str) -> None:
        """把一帧 SSE 文本发给所有订阅者（没人订阅就丢掉，不报错）。"""
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:  # pragma: no cover —— 没设上限，理论上到不了
                logger.warning("订阅者队列满了，丢一帧")

    def _close_subscribers(self) -> None:
        """收工哨兵：让每个订阅者的 `await queue.get()` 醒过来并结束。"""
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:  # pragma: no cover
                pass
        self.subscribers.clear()

    # -- 控制 -------------------------------------------------------------

    def stop(self) -> bool:
        """用户按了停止：取消后台 task（它自己会把已生成的落库标 partial）。"""
        if self.finished or self.task is None:
            return False
        self.stopped = True
        self.task.cancel()
        return True


# assistant_id → ReplyRun（进程内；重启就没了，见 D182 的启动清理）
_runs: dict[str, ReplyRun] = {}


def get(assistant_id: str) -> ReplyRun | None:
    return _runs.get(assistant_id)


def running_ids() -> list[str]:
    return list(_runs)


def start(
    *,
    assistant_id: str,
    friend_id: str,
    friend_name: str,
    body: Callable[[ReplyRun, asyncio.Event], Coroutine[Any, Any, None]],
) -> ReplyRun:
    """起一个后台 task 跑这一轮。**必须在事件循环里调用**（端点函数里就行）。

    `body` 拿到 `(run, wake)`：自己开 session、自己把帧 `run.publish()` 出去；
    跑完（或被打断）由它负责落库。`wake` 在 task 真的结束后被 set（测试用）。
    """
    run = ReplyRun(assistant_id=assistant_id, friend_id=friend_id, friend_name=friend_name)
    wake = asyncio.Event()

    async def _wrapper() -> None:
        try:
            await body(run, wake)
        except asyncio.CancelledError:
            # 停止 / 关进程：`body` 里的 sse_stream 已经把它自己的收尾做完了
            logger.info("这一轮被取消（用户按停止或进程退出）：{}", assistant_id[:8])
            raise
        except Exception as exc:  # noqa: BLE001 —— 后台任务绝不能让异常逃出去
            logger.error("后台回复任务崩了（{}）：{}", assistant_id[:8], exc)
        finally:
            run.finished = True
            run._close_subscribers()
            _runs.pop(assistant_id, None)
            wake.set()

    run.task = asyncio.create_task(_wrapper(), name=f"reply-run-{assistant_id[:8]}")
    _runs[assistant_id] = run
    return run


async def stop(assistant_id: str) -> bool:
    run = _runs.get(assistant_id)
    return bool(run and run.stop())


async def shutdown() -> None:
    """进程退出时把还在跑的取消掉（它们会把已生成的部分落库）。"""
    runs = list(_runs.values())
    for run in runs:
        if run.task is not None:
            run.task.cancel()
    for run in runs:
        if run.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(run.task), timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass


def subscriber(run: ReplyRun, queue: asyncio.Queue) -> AsyncIterator[str]:
    """SSE 生成器（前端连着的那个）：只管把后台 task 发的帧转发出去。

    ⚠️ **队列必须在端点里就订阅好**（`run.subscribe()`）再交给它 —— 这是个真踩过的坑：
    异步生成器的函数体要等第一次 `__anext__()` 才跑，而那时后台 task 可能已经跑完了
    （fake provider / 很短的回复），`_close_subscribers()` 早就把订阅者表清了 →
    这个新队列永远等不到收工哨兵 → **响应挂死不结束**（几百秒的那种卡）。

    客户端断开 → Starlette 在这里抛 `GeneratorExit` → `finally` 退订就结束了，
    **不碰**后台 task（这就是"关掉窗口她继续说"的全部秘密）。
    """

    async def _frames() -> AsyncIterator[str]:
        try:
            while True:
                frame = await queue.get()
                if frame is None:
                    return
                yield frame
        finally:
            run.unsubscribe(queue)

    return _frames()
