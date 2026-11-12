"""单实例锁（Q18 定为 **Windows 命名互斥体**）。

**为什么必须有**（architecture.md §9 的原话）：
开两个实例 = 两个 APScheduler，朋友会**重复主动找你**。

为什么选命名互斥体而不是文件锁：
· 进程被强杀时，互斥体由内核自动释放（文件锁可能留下脏锁文件）
· 不需要管"锁文件放哪"，也不会被用户误删
代价：Windows 专属 —— v1 本来就只做电脑端（D6），可以接受。
"""

from __future__ import annotations

import ctypes

ERROR_ALREADY_EXISTS = 183
DEFAULT_NAME = "yumeFriend.SingleInstance.v1"


class SingleInstance:
    """拿到锁才算"我是那个唯一实例"。"""

    def __init__(self, name: str = DEFAULT_NAME) -> None:
        self._name = name
        self._handle: int | None = None

    def acquire(self) -> bool:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # ⚠️ 必须先 SetLastError(0)：CreateMutex 成功时**不会清掉**上次的错误码，
        # 不这么做的话，之前任何一次 ERROR_ALREADY_EXISTS 都会让后面这次也被误判成
        # "已经有实例在跑"（测试抓到的真 bug）。
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            raise OSError("创建互斥体失败")
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.ReleaseMutex(self._handle)
            kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "SingleInstance":
        if not self.acquire():
            raise RuntimeError("已经有一个实例在跑了")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
