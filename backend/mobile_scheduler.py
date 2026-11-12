"""手机端自主行为调度（D303）：只有主设备运行。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from backend import notify
from backend.config import CATCHUP_POLL_SECONDS, CLEANUP_EVERY_HOURS
from backend.db import session_scope
from backend.scheduler.jobs import (
    _catchup_job,
    _cleanup_job,
    _embedding_backfill_job,
    _expression_job,
    _heartbeat_job,
    _reflection_job,
    _schedule_job,
    EMBEDDING_BACKFILL_SECONDS,
)
from backend.services import settings_service, sync_service


def is_autonomy_owner() -> bool:
    try:
        with session_scope() as session:
            row = settings_service.get(session)
            owner = str(row.autonomy_owner_device_id or "")
            return bool(owner) and owner == sync_service.device_id(session)
    except Exception:  # noqa: BLE001
        return False


async def _guarded(name: str, job) -> None:
    if not is_autonomy_owner():
        return
    try:
        await job()
    except Exception as exc:  # noqa: BLE001
        logger.warning("手机自主任务 {} 异常（已吞掉）：{}", name, exc)


async def _catchup_guard() -> None:
    await _guarded("catchup", _catchup_job)


async def _reflection_guard() -> None:
    await _guarded("reflection", _reflection_job)


async def _expression_guard() -> None:
    await _guarded("expression", _expression_job)


async def _embedding_guard() -> None:
    await _guarded("embedding", _embedding_backfill_job)


async def _schedule_guard() -> None:
    await _guarded("schedule", _schedule_job)


async def _heartbeat_guard() -> None:
    await _guarded("heartbeat", _heartbeat_job)


async def _cleanup_guard() -> None:
    await _guarded("cleanup", _cleanup_job)


def build_mobile_scheduler(*, heartbeat_minutes: int = 60) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _catchup_guard,
        "interval",
        seconds=CATCHUP_POLL_SECONDS,
        id="mobile_catchup",
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=CATCHUP_POLL_SECONDS),
    )
    scheduler.add_job(
        _reflection_guard,
        "interval",
        seconds=CATCHUP_POLL_SECONDS,
        id="mobile_reflection",
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=CATCHUP_POLL_SECONDS),
    )
    # 与桌面同一套表达学习 / 向量回填；手机也是独立 Agent，不能只做关键词记忆。
    scheduler.add_job(
        _expression_guard,
        "interval",
        minutes=15,
        id="mobile_expression_learning",
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    scheduler.add_job(
        _embedding_guard,
        "interval",
        seconds=EMBEDDING_BACKFILL_SECONDS,
        id="mobile_embedding_backfill",
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20),
    )
    scheduler.add_job(
        _schedule_guard,
        "interval",
        seconds=CATCHUP_POLL_SECONDS,
        id="mobile_schedule",
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=CATCHUP_POLL_SECONDS),
    )
    scheduler.add_job(
        _heartbeat_guard,
        "interval",
        minutes=max(1, heartbeat_minutes),
        id="mobile_heartbeat",
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=max(1, heartbeat_minutes)),
    )
    scheduler.add_job(
        _cleanup_guard,
        "interval",
        hours=CLEANUP_EVERY_HOURS,
        id="mobile_cleanup",
        coalesce=True,
        max_instances=1,
    )
    return scheduler


def register_notifications() -> None:
    from backend import mobile_notify  # noqa: PLC0415

    notify.register(mobile_notify.emit)
