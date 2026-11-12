"""把桌面 Live2D 渲染成前端可用的帧（v7.30 / D428）。

沉浸模式是 WebView 里的 React，不是原生分层窗口。为了避免再引一套
Cubism Web SDK，这里复用桌面端已经调好的 `Live2DRenderer`：

* 单进程、单工作线程、单 OpenGL 上下文；
* 前端按约 10fps 请求透明 WebP 帧，情绪 / 口型走同一套参数；
* 请求串行排队，不会在多个线程里同时碰 OpenGL。
"""

from __future__ import annotations

import io
import queue
import threading
import time
from dataclasses import dataclass, field

from backend.desktop.live2d_renderer import Live2DRenderer


@dataclass
class _FrameJob:
    friend_id: str | None
    model_id: str | None
    mood: str
    speaking: bool
    width: int
    height: int
    done: threading.Event = field(default_factory=threading.Event)
    data: bytes | None = None
    error: str | None = None


class Live2DWebService:
    """一个进程只保留一个 Web 帧渲染器。"""

    _singleton: "Live2DWebService | None" = None
    _singleton_lock = threading.Lock()

    def __init__(self) -> None:
        self._queue: queue.Queue[_FrameJob] = queue.Queue(maxsize=8)
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    @classmethod
    def shared(cls) -> "Live2DWebService":
        with cls._singleton_lock:
            if cls._singleton is None:
                cls._singleton = cls()
            return cls._singleton

    def _ensure_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._thread_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._worker,
                name="yumefriend-live2d-web",
                daemon=True,
            )
            self._thread.start()

    def _worker(self) -> None:
        renderer = Live2DRenderer()
        try:
            while True:
                job = self._queue.get()
                try:
                    image = renderer.render(
                        friend_id=job.friend_id,
                        model_id=job.model_id,
                        mood=job.mood,
                        speaking=job.speaking,
                        area_width=job.width,
                        area_height=job.height,
                        premultiplied=False,
                    )
                    if image is None:
                        job.error = "live2d_unavailable"
                    else:
                        buffer = io.BytesIO()
                        # WebP 带 alpha，本地/局域网都够小；比 PNG 每帧快很多。
                        image.save(buffer, format="WEBP", quality=88, method=0)
                        job.data = buffer.getvalue()
                except Exception as exc:  # noqa: BLE001 —— 坏模型不能把工作线程带走
                    job.error = f"{type(exc).__name__}: {exc}"
                finally:
                    job.done.set()
                    self._queue.task_done()
        finally:
            try:
                renderer.close()
            except Exception:  # noqa: BLE001
                pass

    def render(
        self,
        *,
        friend_id: str | None,
        model_id: str | None = None,
        mood: str,
        speaking: bool,
        width: int,
        height: int,
        timeout: float = 6.0,
    ) -> bytes:
        self._ensure_worker()
        job = _FrameJob(
            friend_id=friend_id,
            model_id=model_id,
            mood=mood,
            speaking=speaking,
            width=max(64, min(1024, int(width))),
            height=max(64, min(1536, int(height))),
        )
        started = time.monotonic()
        try:
            self._queue.put(job, timeout=max(0.2, timeout))
        except queue.Full as exc:
            raise TimeoutError("Live2D 渲染队列已满") from exc
        remaining = max(0.2, timeout - (time.monotonic() - started))
        if not job.done.wait(remaining):
            raise TimeoutError("Live2D 渲染超时")
        if job.error:
            raise RuntimeError(job.error)
        if not job.data:
            raise RuntimeError("Live2D 没有返回画面")
        return job.data
