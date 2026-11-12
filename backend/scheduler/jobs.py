"""APScheduler 的活儿（architecture.md §8）。

现在有四条：
  · 停歇补漏检查：每 30 秒看一眼有没有"聊完没人整理"的对话（D24）
  · short_term 过期清理：每天硬删过期记忆
  · 日程扫描：每 30 秒（到点必响，不受在场限制 D35；到日 / 响完进纸条 D243）
  · 定时预热：每 60 秒问一次"该预热了吗"（D87，按设置里的间隔真探活）

看屏没有独立定时器了（D103）：它是**主动消息的第一步**，在 `proactive_service` 里。

**规矩**：每个任务都必须幂等、自己吞异常 —— 一个任务炸了绝不能把调度器带死
（architecture.md §8 的警告）。
"""

import asyncio
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from backend.config import CATCHUP_POLL_SECONDS, CLEANUP_EVERY_HOURS
from backend.db import session_scope
from backend.llm.factory import create_provider, create_provider_for_llm_base
from backend.llm.warmup import warmup_tick
from backend.services import (
    catchup_service,
    embedding_service,
    expression_service,
    friend_service,
    image_service,
    life_service,
    memory_service,
    proactive_service,
    reflection_service,
    schedule_service,
    settings_service,
    tts_service,
    voice_reply_service,
)
from backend.services.actor import Actor

# 软删的数据留多久（data_model §7.1 / §7.3：30 天内可恢复，之后物理删；宪法§9）
PURGE_AFTER_DAYS = 30

# v3.46（D231）：向量回填间隔（秒）。新记忆 / 改过内容的记忆，最晚 ~2 分钟能被向量召回。
EMBEDDING_BACKFILL_SECONDS = 120
# 每拍最多补几批（每批 64 条）—— 首次索引 500~600 条时，别等十几分钟（实测：一拍拍完）
EMBEDDING_BACKFILL_ROUNDS = 12


def _is_autonomy_owner() -> bool:
    """D303：主动消息 / 到点提醒 / 反思只由主设备执行。"""
    try:
        from backend.services import sync_service  # noqa: PLC0415

        with session_scope() as session:
            owner = str(settings_service.get(session).autonomy_owner_device_id or "")
            return bool(owner) and owner == sync_service.device_id(session)
    except Exception:  # noqa: BLE001
        return False


async def _catchup_job() -> None:
    if not _is_autonomy_owner():
        return
    try:
        await catchup_service.tick()
    except Exception as exc:  # noqa: BLE001
        logger.warning("停歇补漏任务异常（已吞掉，不让调度器死）：{}", exc)


async def _reflection_job() -> None:
    """V6.0-3（D297）：停歇后反思情节 / 理解 / 纠正。"""
    if not _is_autonomy_owner():
        return
    try:
        await reflection_service.tick()
    except Exception as exc:  # noqa: BLE001
        logger.warning("停歇反思任务异常（已吞掉，不让调度器死）：{}", exc)


async def _cleanup_job() -> None:
    """每 `CLEANUP_EVERY_HOURS` 小时跑一次：过期短期记忆 + 没人要的上传图 +
    **30 天前的软删数据（含日程）** + **语音/缓存/孤儿图片**（都幂等，重复跑没事）。"""
    try:
        with session_scope() as session:
            removed = memory_service.purge_expired(session)
        if removed:
            logger.info("清理过期 short_term 记忆：{} 条", removed)
        # v2（T2.0-6）：传了却一直没发出去的图（24 小时）
        stale_images = image_service.cleanup_pending()
        if stale_images:
            logger.info("清理没人要的上传图片：{} 张", stale_images)
    except Exception as exc:  # noqa: BLE001
        logger.warning("过期记忆清理异常（已吞掉）：{}", exc)
    # v2.8（B1 / D137）：软删超过 30 天 → **物理删**（宪法§9 / data_model §7.1、§7.3）
    # 删朋友要清一串文件（头像/立绘/背景/资料原件）→ 扔线程里，别占着事件循环（D136 同款理由）
    try:
        friends, memories, schedules = await asyncio.to_thread(_purge_soft_deleted)
        if friends or memories or schedules:
            logger.info(
                "清理软删超过 {} 天的数据：朋友 {} 个、记忆 {} 条、日程 {} 条",
                PURGE_AFTER_DAYS,
                friends,
                memories,
                schedules,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("软删清理异常（已吞掉）：{}", exc)
    # v3.0（D141）：语音文件很占地方（每分钟 ≈ 5.6 MB）→ 每天清
    try:
        media = await asyncio.to_thread(_purge_media)
        if media["spoken"] or media["cache"]:
            logger.info(
                "清理语音文件：消息语音删 {} 项（{:.1f} MB）、合成缓存删 {} 个",
                media["spoken"],
                media["freed"] / 1024 / 1024,
                media["cache"],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("语音清理异常（已吞掉）：{}", exc)


def _purge_media() -> dict[str, int]:
    """清语音（消息语音 + 合成缓存）+ 孤儿图片。返回给日志用的数字。

    保留天数读设置里的 `voice_keep_days`（0 = 永久留）；合成缓存固定 3 天。
    """
    with session_scope() as session:
        keep_days = int(settings_service.get(session).voice_keep_days or 0)
        audio = voice_reply_service.purge_audio(session, keep_days=keep_days)
        image_service.purge_orphans(session)
    cache = tts_service.purge_cache()
    return {
        "spoken": audio["removed"],
        "freed": audio["freed"],
        "cache": cache["removed"],
    }


def _purge_soft_deleted(*, older_than_days: int = PURGE_AFTER_DAYS) -> tuple[int, int, int]:
    """物理删掉"软删超过 N 天"的朋友 / 记忆 / 日程。返回 `(朋友, 记忆, 日程)`。

    **幂等**（第二天再跑一次什么都不会删）、**一个失败不影响其他**（删到一半崩了，
    前面已经提交的不白干，后面的下次再说）。
    """
    friends = 0
    memories = 0
    schedules = 0
    with session_scope() as session:
        for friend in friend_service.list_purgeable(session, older_than_days=older_than_days):
            try:
                # 系统行为，按"用户"身份执行（purge 本身不校验 actor，只是不给自己留后门）
                friend_service.purge(session, Actor.USER, friend.id)
                session.commit()
                friends += 1
            except Exception as exc:  # noqa: BLE001 —— 一个朋友删不掉不能拖垮整轮清理
                session.rollback()
                logger.warning("清理软删的朋友失败（跳过，下次再试）：{} — {}", friend.id, exc)
        memories = memory_service.purge_deleted(session, older_than_days=older_than_days)
        # v4.0（D243）：日程软删超过 30 天 → 物理清掉（跟朋友软删期同一个数，D138）
        schedules = schedule_service.purge_deleted(session, older_than_days=older_than_days)
        session.commit()
    return friends, memories, schedules


def _provider_for(session, friend=None):  # noqa: ANN001, ANN202
    """拿 provider：朋友自己指定了模型端点就用它的（D151），否则全局「聊天」用途。

    传 `friend=None`（或一个没有 `llm_base` 的朋友）= 老行为，一行都没变。
    """
    name = (getattr(friend, "llm_base", None) or "").strip() if friend is not None else ""
    if name:
        row = settings_service.get(session)
        return create_provider_for_llm_base(row.llm_bases, name)
    bases, active = settings_service.llm_config(session)
    return create_provider(bases, active)


def _provider_resolver(session):  # noqa: ANN001, ANN202
    """给"一个 tick 里要招呼多个朋友"的活儿用：返回按朋友造 provider 的函数（D151）。"""

    def _make(friend=None):  # noqa: ANN001, ANN202
        return _provider_for(session, friend)

    return _make


async def _schedule_job() -> None:
    """日程扫描 —— 到点必响（不受在场限制，D35）；到日 / 响完进纸条（D243）。"""
    if not _is_autonomy_owner():
        return
    try:
        with session_scope() as session:
            # D151：按朋友给 provider（某个朋友的模型端点配坏了 → 那条提醒退回原文发，见 tick）
            await proactive_service.run_schedule_tick(session, _provider_resolver(session))
    except Exception as exc:  # noqa: BLE001
        logger.warning("日程扫描异常（已吞掉）：{}", exc)


class _FallbackProvider:
    """拿不到 provider 时的兜底：什么都不生成，让调用方用原文。"""

    async def chat_stream(self, *_a, **_k):  # noqa: ANN001, ANN201
        return
        yield  # pragma: no cover —— 让它是个 async generator


async def _heartbeat_job() -> None:
    """朋友主动冒泡（D21 / D22）。频率由设置决定，见 `build_scheduler`。

    生成失败**原地重试一次**（D89）：主动消息没有用户在等，**发出去**比"跳过等下个周期"
    重要；而且那次失败多半是网络抽风，重试时 `_attempt_order()` 会优先换另一条路。
    """
    if not _is_autonomy_owner():
        return
    for attempt in (1, 2):
        try:
            with session_scope() as session:
                provider = _provider_resolver(session)
                await proactive_service.run_heartbeat_tick(session, provider)
            return
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                logger.warning("主动消息心跳异常（已吞掉）：{}", exc)
                return
            logger.info("主动消息这次没生成出来（{}），3 秒后重试一次", exc)
            await asyncio.sleep(3)


async def _warmup_job() -> None:
    """定时预热（D87）：**每分钟问一次"该热了吗"**，间隔由设置决定。

    这样做的好处：设置里改开关 / 间隔立刻生效，不用重建调度器。
    """
    try:
        with session_scope() as session:
            row = settings_service.get(session)
            enabled = bool(row.warmup_enabled)
            interval = int(row.warmup_interval_minutes or 5)
        await warmup_tick(enabled=enabled, interval_minutes=interval)
    except Exception as exc:  # noqa: BLE001
        logger.warning("定时预热异常（已吞掉）：{}", exc)


async def _life_job() -> None:
    """v7.12（D341）：电脑端唯一的生活线 tick。手机端/服务器端会被 service 拒绝。"""
    try:
        result = await life_service.tick()
        if result.get("started") or result.get("finished"):
            logger.info(
                "生活线：开始 {} 个活动，结束 {} 个",
                result.get("started", 0),
                result.get("finished", 0),
            )
    except Exception as exc:  # noqa: BLE001 —— 生活线失败不能拖垮调度器
        logger.warning("生活线 tick 异常（已吞掉）：{}", exc)


async def _life_share_job() -> None:
    """v7.15（D351）：30 秒主动消息 tick，处理所有非 checkin 的真实候选。"""
    try:
        with session_scope() as session:
            provider = _provider_resolver(session)
            await proactive_service.run_life_share_tick(session, provider)
    except Exception as exc:  # noqa: BLE001
        logger.warning("主动消息 tick 异常（已吞掉）：{}", exc)


async def _expression_job() -> None:
    """v7.13（D344）：低频从 TA 最近说法里学短表达样本。"""
    try:
        count = await asyncio.to_thread(expression_service.tick)
        if count:
            logger.info("表达样本学习：{} 条", count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("表达样本学习异常（已吞掉）：{}", exc)


def _backfill_embeddings() -> int:
    added = 0
    with session_scope() as session:
        for _ in range(EMBEDDING_BACKFILL_ROUNDS):
            count = embedding_service.backfill(session)
            added += count
            if count == 0:
                break
    return added


async def _embedding_backfill_job() -> None:
    """v3.46（D231）：给缺失 / 内容变了的记忆补向量（后台、分批、幂等）。

    模型首次加载（含第一次下载）可能要几十秒 → 扔线程，绝不占事件循环、更不堵对话。
    """
    try:
        count = await asyncio.to_thread(_backfill_embeddings)
        if count:
            logger.info("记忆向量回填：{} 条", count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆向量回填异常（已吞掉）：{}", exc)


def _job_defs(heartbeat_minutes: int) -> list[dict]:
    """所有后台任务的静态定义（v7.20 / D373）。

    `feature` 是归属；关掉那个功能，这条任务就不再注册。
    加任务时只改这张表，`build_scheduler` / `sync_scheduler` 跟着走。
    """
    now = datetime.now(timezone.utc)
    heartbeat = max(1, heartbeat_minutes)
    return [
        {
            "id": "memory_catchup",
            "feature": "core",
            "func": _catchup_job,
            "trigger": {"trigger": "interval", "seconds": CATCHUP_POLL_SECONDS},
            "next_run_time": now + timedelta(seconds=CATCHUP_POLL_SECONDS),
        },
        {
            "id": "episode_reflection",
            "feature": "core",
            "func": _reflection_job,
            "trigger": {"trigger": "interval", "seconds": CATCHUP_POLL_SECONDS},
            "next_run_time": now + timedelta(seconds=CATCHUP_POLL_SECONDS),
        },
        {
            "id": "memory_cleanup",
            "feature": "core",
            "func": _cleanup_job,
            # ⚠️ 原来是"每天凌晨 4 点"，但桌面版晚上就关了 → 改成每 6 小时一次，
            # 只要应用开着就会追上（都幂等且便宜）。
            "trigger": {"trigger": "interval", "hours": CLEANUP_EVERY_HOURS},
            "next_run_time": None,
        },
        {
            "id": "schedule_scan",
            "feature": "schedule",
            "func": _schedule_job,
            "trigger": {"trigger": "interval", "seconds": CATCHUP_POLL_SECONDS},
            "next_run_time": now + timedelta(seconds=CATCHUP_POLL_SECONDS),
        },
        {
            "id": "proactive_heartbeat",
            "feature": "core",
            "func": _heartbeat_job,
            "trigger": {"trigger": "interval", "minutes": heartbeat},
            "next_run_time": now + timedelta(minutes=heartbeat),
        },
        {
            "id": "llm_warmup",
            "feature": "core",
            "func": _warmup_job,
            "trigger": {"trigger": "interval", "seconds": 60},
            "next_run_time": now + timedelta(seconds=60),
        },
        {
            "id": "life_tick",
            "feature": "life",
            "func": _life_job,
            "trigger": {"trigger": "interval", "seconds": 30},
            "next_run_time": now + timedelta(seconds=10),
        },
        {
            "id": "life_share_tick",
            "feature": "life",
            "func": _life_share_job,
            "trigger": {"trigger": "interval", "seconds": 30},
            "next_run_time": now + timedelta(seconds=35),
        },
        {
            "id": "expression_learning",
            "feature": "expression",
            "func": _expression_job,
            "trigger": {"trigger": "interval", "minutes": 15},
            "next_run_time": now + timedelta(minutes=1),
        },
        {
            "id": "memory_embedding_backfill",
            "feature": "core",
            "func": _embedding_backfill_job,
            "trigger": {
                "trigger": "interval",
                "seconds": EMBEDDING_BACKFILL_SECONDS,
            },
            # 启动 20 秒后先来一轮（把老库的存量补上），之后每 2 分钟看一次
            "next_run_time": now + timedelta(seconds=20),
        },
    ]


def _add_job(scheduler: AsyncIOScheduler, spec: dict) -> None:
    kwargs = dict(spec["trigger"])
    kwargs.update(id=spec["id"], coalesce=True, max_instances=1)
    # None = 不显式指定，让 trigger 自己算第一次；传 None 会被 APScheduler 当成"暂停"。
    if spec["next_run_time"] is not None:
        kwargs["next_run_time"] = spec["next_run_time"]
    scheduler.add_job(spec["func"], **kwargs)


def build_scheduler(
    *, heartbeat_minutes: int = 60, enabled_features: set[str] | None = None
) -> AsyncIOScheduler:
    """按功能开关注册后台任务。

    `enabled_features=None` = 全开（兼容老调用 / 测试）。
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
    allowed = None if enabled_features is None else set(enabled_features) | {"core"}
    for spec in _job_defs(heartbeat_minutes):
        if allowed is None or spec["feature"] in allowed:
            _add_job(scheduler, spec)
    return scheduler


def sync_scheduler(
    scheduler: AsyncIOScheduler,
    enabled_features: set[str],
    *,
    heartbeat_minutes: int | None = None,
) -> None:
    """功能开关变了 → 立刻增删后台任务，不用重启。"""
    allowed = set(enabled_features) | {"core"}
    desired = {spec["id"]: spec for spec in _job_defs(heartbeat_minutes or 60)}
    wanted = {
        job_id for job_id, spec in desired.items() if spec["feature"] in allowed
    }
    existing = {job.id for job in scheduler.get_jobs()}
    for job_id in existing & set(desired) - wanted:
        try:
            scheduler.remove_job(job_id)
            logger.info("功能关闭：已撤下后台任务 {}", job_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("撤下后台任务 {} 失败（已忽略）：{}", job_id, exc)
    for job_id in wanted - existing:
        spec = desired[job_id]
        _add_job(scheduler, spec)
        logger.info("功能开启：已挂上后台任务 {}", job_id)
    if heartbeat_minutes is not None:
        reschedule_heartbeat(scheduler, heartbeat_minutes)


def reschedule_heartbeat(scheduler: AsyncIOScheduler, minutes: int) -> None:
    """设置里改了频率 → 立刻改心跳间隔（D22 说这个数字是给用户调的）。"""
    for job_id in ("proactive_heartbeat", "mobile_heartbeat"):
        try:
            scheduler.reschedule_job(
                job_id, trigger="interval", minutes=max(1, minutes)
            )
        except Exception:  # noqa: BLE001 —— 这台设备不是对应的调度器
            pass
