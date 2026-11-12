"""适配器进程启动器（D250：适配器必须独立进程，崩了不拖垮聊天）。

负责三件事：
  ① 选一个本机空闲端口 + 生成一次性 token；
  ② 起子进程，等 `/health` 就绪；
  ③ 停止时把子进程收干净（终止 → 等 → 必要时 kill）。
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import time
from typing import Mapping, Sequence

from loguru import logger

from backend.game.client import AdapterClient, AdapterError


class AdapterStartError(RuntimeError):
    """适配器没起来。消息里带上它自己的 stderr，方便定位。"""


def free_port() -> int:
    """拿一个当前空闲的本机端口（绑 0 再放开；本机竞态可忽略）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class AdapterProcess:
    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        ready_timeout: float = 15.0,
    ) -> None:
        self.command = list(command)
        self._env = dict(os.environ, **(env or {}))
        # P4.1：让适配器知道"父进程是谁"——父进程被强杀后，它自己退出（避免孤儿）
        self._env["YF_PARENT_PID"] = str(os.getpid())
        self._ready_timeout = ready_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._client: AdapterClient | None = None
        self.port: int | None = None
        self.token: str | None = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    async def start(self) -> AdapterClient:
        if self._proc is not None:
            raise RuntimeError("这个 AdapterProcess 已经启动过了")

        self.port = free_port()
        # token 以字母开头：token_urlsafe 偶尔会以 '-' 开头，argparse 会把它当成选项
        self.token = f"yf_{secrets.token_urlsafe(18)}"
        args = [
            *self.command,
            "--port",
            str(self.port),
            "--token",
            self.token,
        ]

        kwargs: dict[str, object] = {
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.PIPE,
            "env": self._env,
        }
        if sys.platform == "win32":
            # 别弹黑窗（绿色版从托盘启动时尤其重要）
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            self._proc = await asyncio.create_subprocess_exec(*args, **kwargs)
        except OSError as exc:
            raise AdapterStartError(f"起不来适配器进程：{exc}") from exc

        client = AdapterClient(f"http://127.0.0.1:{self.port}", self.token)
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if self._proc.returncode is not None:
                stderr = await self._read_stderr()
                await client.close()
                await self.stop()
                raise AdapterStartError(
                    f"适配器进程退出了（code={self._proc.returncode if self._proc else '?'}）"
                    + (f"\n{stderr}" if stderr else "")
                )
            try:
                await client.health()
            except AdapterError:
                await asyncio.sleep(0.1)
                continue
            self._client = client
            logger.info("游戏适配器已就绪：port={} pid={}", self.port, self.pid)
            return client

        stderr = await self._read_stderr()
        await client.close()
        await self.stop()
        raise AdapterStartError(
            f"适配器 {self._ready_timeout:.0f}s 内没就绪" + (f"\n{stderr}" if stderr else "")
        )

    async def _read_stderr(self) -> str:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ""
        try:
            data = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
        except (asyncio.TimeoutError, ValueError):
            return ""
        return data.decode("utf-8", "replace").strip()[-4000:]

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("适配器进程没在 3s 内退出，强制 kill（pid={}）", proc.pid)
                proc.kill()
                await proc.wait()
        self._proc = None

    async def __aenter__(self) -> AdapterClient:
        return await self.start()

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()
