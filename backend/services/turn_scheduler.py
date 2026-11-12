"""每个朋友一个聊天回合锁（v7.13 / D344）。

前端会先把连续输入防抖合并；这里再保证**同一朋友不会同时跑两条回复**，
避免手机/电脑或快速请求把两轮 LLM 叠在一起。
"""

from __future__ import annotations

import asyncio

_locks: dict[str, asyncio.Lock] = {}


def _lock(friend_id: str) -> asyncio.Lock:
    lock = _locks.get(friend_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[friend_id] = lock
    return lock


async def acquire(friend_id: str) -> None:
    await _lock(friend_id).acquire()


def release(friend_id: str) -> None:
    lock = _locks.get(friend_id)
    if lock is not None and lock.locked():
        lock.release()
