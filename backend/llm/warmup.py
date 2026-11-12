"""开机预热（D46）：进程一起来就把 DNS / TCP / TLS 和上游通道捂热。

为什么需要：进程刚起来时，**第一条**消息要多付一次冷启动代价
（实测 8.59s vs 之后 2.2s），其中一大块是 TLS 握手。
启动后偷偷发一次最小请求（列模型列表）——**不花 token**，
用户真正发第一条消息时连接已经是热的。

注意：预热只热"连接"，热不了上游模型的排队（那部分只有换 provider 才能动）。
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger

from backend.db import session_scope
from backend.llm.errors import LLMConfigError
from backend.llm.factory import create_provider
from backend.services import settings_service

WARM_UP_DELAY_SECONDS = 0.2

# 后台预热任务的引用（不持有引用的话可能被 GC 掉）
_BACKGROUND: set[asyncio.Task] = set()  # type: ignore[type-arg]

# 主事件循环（lifespan 启动时绑定）。同步端点跑在线程池里，没有 running loop，
# 必须靠 run_coroutine_threadsafe 把活儿丢回主循环。
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None

# 上次预热（或上次被"要不要预热"判定的）时间点 —— D87 的定时预热靠它算间隔。
# 用单调时钟：改系统时间不会让定时空转。
_last_warm_at: float = 0.0


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def reset_state() -> None:
    """测试用：清掉"上次预热时间"。"""
    global _last_warm_at
    _last_warm_at = 0.0


async def warm_up_provider(*, delay: float | None = None, quiet: bool = False) -> bool:
    """预热当前 provider。返回是否真的热了一下（没配 provider 就返回 False）。

    `quiet=True` 给**定时预热**用：日志降到 DEBUG，免得日志窗口每几分钟响一次。
    """
    await asyncio.sleep(WARM_UP_DELAY_SECONDS if delay is None else delay)

    try:
        with session_scope() as session:
            bases, active = settings_service.llm_config(session)
        provider = create_provider(bases, active)
    except LLMConfigError:
        (logger.debug if quiet else logger.info)("还没配置 provider，跳过预热")
        return False

    started = time.monotonic()
    try:
        await provider.probe()
    except Exception as exc:  # noqa: BLE001 —— 预热失败绝不能影响使用
        (logger.debug if quiet else logger.warning)("LLM 预热失败（不影响正常使用）：{}", exc)
        return False
    finally:
        closer = getattr(provider, "aclose", None)
        if closer is not None:
            await closer()

    if quiet:
        logger.debug("定时预热完成，用时 {:.2f}s", time.monotonic() - started)
    else:
        logger.info("LLM 预热完成，用时 {:.2f}s（连接已热）", time.monotonic() - started)
    return True


async def warmup_tick(*, enabled: bool, interval_minutes: int) -> bool:
    """**定时预热**（D87）：距上次够久了就探一次（`models.list()`，不花 token）。

    返回这次到底探了没有。判定用的是**内存里的上次时间**，所以：
    · 设置里改开关 / 间隔 → **立刻生效**，不用重启（调度器每分钟来问一次）
    · 探失败也记一次时间（不然会每分钟狂试）
    """
    global _last_warm_at
    if not enabled:
        return False
    interval = max(1, int(interval_minutes or 5)) * 60.0
    now = time.monotonic()
    if _last_warm_at and now - _last_warm_at < interval:
        return False
    _last_warm_at = now
    return await warm_up_provider(delay=0.0, quiet=True)


def spawn_warm_up(*, delay: float | None = None) -> None:
    """后台起一个预热任务（启动时 / 改完 provider 设置后调用）。

    两种调用场景都要支持：
    · 事件循环里（lifespan）→ 直接 create_task
    · 同步端点（FastAPI 线程池）→ run_coroutine_threadsafe 丢回主循环
    """
    global _last_warm_at
    _last_warm_at = time.monotonic()  # 这次也算"刚热过"，别让定时任务一分钟后又来一次
    if delay is None:
        delay = 0.0 if _MAIN_LOOP is not None else WARM_UP_DELAY_SECONDS

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        task = loop.create_task(warm_up_provider(delay=delay))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        return

    if _MAIN_LOOP is None or _MAIN_LOOP.is_closed():
        return  # 没有事件循环（纯单测 / 已关闭）→ 静默跳过
    asyncio.run_coroutine_threadsafe(warm_up_provider(delay=delay), _MAIN_LOOP)


async def cancel_warm_ups() -> None:
    """进程退出时收尾。"""
    for task in list(_BACKGROUND):
        task.cancel()
    for task in list(_BACKGROUND):
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _BACKGROUND.clear()
