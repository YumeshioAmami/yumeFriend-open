"""FastAPI 应用实例。"""

import asyncio
import os
import socket
from contextlib import asynccontextmanager
from typing import AsyncIterator, Mapping

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from backend.api import documents as documents_api
from backend.api import embedding as embedding_api
from backend.api import features as features_api
from backend.api import friends as friends_api
from backend.api import game as game_api
from backend.api import approvals as approvals_api
from backend.api import life as life_api
from backend.api import live2d as live2d_api
from backend.api import live2d_models as live2d_models_api
from backend.api import logs as logs_api
from backend.api import portraits as portraits_api
from backend.api import portrait_packs as portrait_packs_api
from backend.api import memories as memories_api
from backend.api import mobile as mobile_api
from backend.api import messages as messages_api
from backend.api import schedules as schedules_api
from backend.api import screen as screen_api
from backend.api import server_sync as server_sync_api
from backend.api import settings as settings_api
from backend.api import space as space_api
from backend.api import speech as speech_api
from backend.api import style as style_api
from backend.api import sync as sync_api
from backend.api import thoughts as thoughts_api
from backend.api import update as update_api
from backend.api import uploads as uploads_api
from backend.api import usage as usage_api
from backend.api import voices as voices_api
from backend.api import ws as ws_api
from backend.config import (
    APP_NAME,
    APP_VERSION,
    CATCHUP_POLL_SECONDS,
    RESOURCE_DIR,
    ensure_dirs,
)
from backend.agent import reply_runs
from backend.db import session_scope
from backend.features import catalog as feature_catalog
from backend.features import flags as feature_flags
from backend.features import subscribers as feature_subscribers
from backend.features.catalog import feature_for_path
from backend.llm.errors import LLMConfigError
from backend.llm.warmup import bind_loop, cancel_warm_ups, spawn_warm_up
from backend.scheduler.jobs import build_scheduler
from backend.services import (
    default_friend,
    image_note_service,
    message_service,
    portrait_service,
    settings_service,
    speech_service,
    tts_service,
    voice_reply_service,
)
from backend.services import sync_service
from backend.services import server_sync
from backend.services.errors import ServiceError

# v3.14（D182 附带）：设成 1 = **不碰**用户正在跑的语音服务（验收/测试用）
NO_TTS_PREWARM_ENV = "YF_NO_TTS_PREWARM"


def tts_prewarm_enabled(env: Mapping[str, str] | None = None) -> bool:
    """要不要跑"冷启动预热"（默认要）。

    v3.14（D182 附带）：**起"验收实例"时别去碰用户正在跑的语音服务** ——
    预热会调上游的 `set_*_weights`，那是**写操作**（会改它自己的 `tts_infer.yaml`，
    D179 的教训）。而验收脚本（`scripts/check_packaged.ps1`）起的正是
    "带一份真数据副本"的完整实例 → 一启动就把用户的服务切到别人的音色去了
    （实测踩过一次：check 完发现服务被切成了甘雨）。
    所以给这类跑法留一个开关：`YF_NO_TTS_PREWARM=1`。
    """
    source = os.environ if env is None else env
    return (source.get(NO_TTS_PREWARM_ENV) or "").strip().lower() not in {"1", "true", "yes"}


async def _warm_tts_voice() -> None:
    """冷启动预热语音（v3.11 / D178）：best-effort，绝不抛。

    为什么：GPT-SoVITS 刚起来时第一次合成要**现加载模型**（实测能超 60 秒）——
    用户第一句就干等，甚至撞超时。这里在启动后偷偷把"最可能用到的那个音色"热一遍
    （服务没开 / 没配音色 → 什么都不做，最多等 30 秒就放弃）。
    """
    try:
        from sqlalchemy import desc, select

        from backend.models import Friend

        with session_scope() as session:
            friend = session.scalars(
                select(Friend)
                .where(Friend.voice_reply_enabled == 1, Friend.voice_id.is_not(None))
                .order_by(desc(Friend.updated_at))
                .limit(1)
            ).first()
            if friend is None:
                return
            voice_id = friend.voice_id
            conf = tts_service.tts_settings(
                settings_service.effective_purposes_from(settings_service.get(session))
            )
        for _ in range(30):  # 最多等 30 秒（用户可能刚点"启动语音服务"）
            if tts_service.status(conf["base_url"], force=True)["available"]:
                tts_service.spawn_prewarm(voice_id, conf["base_url"], conf["sample_steps"])
                logger.info("语音冷启动预热已排上（音色 {}）", str(voice_id)[:8])
                return
            await asyncio.sleep(1)
    except Exception as exc:  # noqa: BLE001 —— 预热失败绝不能影响启动
        logger.debug("语音冷启动预热跳过：{}", exc)


def run_migrations() -> None:
    """启动时自动 upgrade head（AGENTS.md §6）。

    打包（v3.0）后这两个都从 `RESOURCE_DIR`（解包目录）里找 —— spec 用 `datas` 塞进包；
    `script_location` 这里**显式指一遍**，免得 alembic 按 CWD 去猜（打包后 CWD 是 exe 的目录）。
    """
    cfg = Config(str(RESOURCE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(RESOURCE_DIR / "backend" / "migrations"))
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动时偷偷预热 LLM 连接（D46），退出时收尾。"""
    server_only = (os.environ.get("YF_SERVER_ONLY") or "").strip().lower() in {"1", "true", "yes"}
    with session_scope() as session:
        sync_service.configure_device(
            session,
            name=socket.gethostname() or "电脑",
            kind="server" if server_only else "desktop",
        )
        try:
            if (os.environ.get("YF_SKIP_DEFAULT_FRIEND") or "").strip() != "1":
                default_friend.ensure(session)
        except Exception as exc:  # noqa: BLE001 —— 默认朋友失败不能挡住启动
            logger.warning("默认朋友创建失败（不影响启动）：{}", exc)
        try:
            from sqlalchemy import select  # noqa: PLC0415

            from backend.models import Friend  # noqa: PLC0415

            friend_ids = list(
                session.scalars(
                    select(Friend.id).where(Friend.deleted_at.is_(None))
                )
            )
            created = portrait_service.ensure_all_defaults(friend_ids)
            if created:
                logger.info("已给 {} 位朋友补上默认立绘", created)
        except Exception as exc:  # noqa: BLE001 —— 立绘补齐失败不能挡住启动
            logger.warning("默认立绘补齐失败（不影响启动）：{}", exc)
    bind_loop(asyncio.get_running_loop())
    voice_reply_service.bind_loop(asyncio.get_running_loop())
    if not server_only:
        spawn_warm_up()
    with session_scope() as session:
        frequency = settings_service.get(session).active_message_frequency
        enabled_features = feature_flags.refresh(session)
    # 上次没合成完的 pending（进程重启后没人会再做）→ 标成 failed，前端给「重新合成」
    stale = voice_reply_service.reset_stale_pending()
    if stale:
        logger.info("有 {} 条语音没合成完（上次退出时中断），已标成可重试", stale)
    # v3.14（D182）：上次进程留下的"正在回复"（回复跑在后台 task 里，进程没了它就跟没了）
    # → 标成 partial（正文是边生成边落库的，说到哪就留到哪）
    interrupted = message_service.reset_stale_streaming()
    if interrupted:
        logger.info("有 {} 条回复没说完（上次退出时中断），已标成 partial", interrupted)
    # v5.0（P3）：上次没正常结束的游戏会话 → 标 crashed，并用事件日志补摘要
    try:
        from backend.game.manager import get_manager  # noqa: PLC0415

        await get_manager().recover_orphans()
    except Exception as exc:  # noqa: BLE001 —— 游戏恢复失败绝不能挡住启动
        logger.warning("游戏会话恢复失败（不影响启动）：{}", exc)
    # v2.1（D96）：上次被硬杀（崩溃 / 任务管理器）留下的语音服务孤儿，启动时收掉
    leftover = speech_service.cleanup_leftovers()
    if leftover:
        logger.info("收掉上次留下的语音服务：{}", "、".join(leftover))
    scheduler = None
    if not server_only:
        scheduler = build_scheduler(
            heartbeat_minutes=frequency, enabled_features=enabled_features
        )
        scheduler.start()
        _app.state.scheduler = scheduler
        logger.info(
            "定时任务已启动（停歇补漏 {}s、提醒扫描 {}s、主动消息心跳 {} 分钟）",
            CATCHUP_POLL_SECONDS,
            CATCHUP_POLL_SECONDS,
            frequency,
        )
        server_sync.start_loop()
    else:
        logger.info("服务器模式：跳过定时任务 / 生活线 / 主动消息 / TTS 预热")
    # v3.11（D178）：**冷启动预热** —— 语音服务刚起来时第一次合成要现加载模型
    #（实测能超过 60 秒，容易直接撞超时）。启动后偷偷把它热一遍，用户点开就有声。
    # v3.14（D182）：`YF_NO_TTS_PREWARM=1` 时跳过（验收脚本用，别去切用户的服务）
    if tts_prewarm_enabled() and not server_only:
        asyncio.create_task(_warm_tts_voice())
    try:
        yield
    finally:
        try:
            from backend.game.manager import get_manager  # noqa: PLC0415

            await get_manager().shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.warning("游戏会话收尾失败（已吞掉）：{}", exc)
        await server_sync.stop_loop()
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        # v2.1（D96）：**只收我们自己起的**语音服务进程（用户自己开的窗口别去动）
        speech_service.stop_all_ours()
        # v3.14（D182）：还在跑的后台回复先取消掉（它们会把已生成的部分落库）
        await reply_runs.shutdown()
        await cancel_warm_ups()
        await image_note_service.cancel_describes()


def create_app() -> FastAPI:
    ensure_dirs()
    run_migrations()

    app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)
    feature_subscribers.install()

    @app.middleware("http")
    async def _feature_gate(request: Request, call_next):
        """已关闭功能的 HTTP 入口统一拒绝（v7.20 / D373）。

        没登记归属的路径一律放行；宁可漏拦，也不能误拦核心聊天。
        """
        feature_id = feature_for_path(request.url.path)
        if feature_id and not feature_flags.enabled(feature_id):
            feature = feature_catalog.get_feature(feature_id)
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "FEATURE_DISABLED",
                        "message": f"「{feature.name if feature else feature_id}」这个功能现在是关着的 —— 去设置 → 功能里打开再用",
                    }
                },
            )
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        """业务错误 → api.md §0.1 的统一错误格式。"""
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(LLMConfigError)
    async def _llm_config_error_handler(_request: Request, exc: LLMConfigError) -> JSONResponse:
        """没配 provider / model → 400 LLM_NOT_CONFIGURED（api.md §9）。"""
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "LLM_NOT_CONFIGURED", "message": str(exc)}},
        )

    @app.exception_handler(ValueError)
    async def _value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        """service 层的参数校验（如"内容不能为空"）→ 422 INVALID_INPUT（api.md §9）。

        不这么做就会变成 500 —— 明明是我们能判断的输入问题，不该报"服务器错误"。
        """
        logger.warning("参数不合法：{}", exc)
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "INVALID_INPUT", "message": str(exc)}},
        )

    @app.get("/api/health")
    async def health() -> dict:
        """健康检查（api.md §0.4）。"""
        return {"ok": True, "app": APP_NAME, "version": APP_VERSION}

    app.include_router(documents_api.router)
    app.include_router(embedding_api.router)
    app.include_router(features_api.router)
    app.include_router(mobile_api.router)
    app.include_router(portraits_api.router)
    app.include_router(portrait_packs_api.router)
    app.include_router(approvals_api.router)
    app.include_router(friends_api.router)
    app.include_router(game_api.router)
    app.include_router(life_api.router)
    app.include_router(live2d_api.router)
    app.include_router(live2d_models_api.router)
    app.include_router(logs_api.router)
    app.include_router(memories_api.router)
    app.include_router(messages_api.router)
    app.include_router(schedules_api.router)
    app.include_router(screen_api.router)
    app.include_router(server_sync_api.router)
    app.include_router(settings_api.router)
    app.include_router(space_api.router)
    app.include_router(speech_api.router)
    app.include_router(style_api.router)
    app.include_router(sync_api.router)
    app.include_router(thoughts_api.router)
    app.include_router(update_api.router)
    app.include_router(uploads_api.router)
    app.include_router(usage_api.router)
    app.include_router(voices_api.router)
    app.include_router(voices_api.tts_router)
    app.include_router(ws_api.router)

    # 桌面外壳要加载页面：把前端构建产物挂到根路径（M5）。
    # ⚠️ 必须**最后**挂：普通路由优先匹配，不然 /api 会被静态文件抢走。
    dist_dir = RESOURCE_DIR / "frontend" / "dist"
    if dist_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="frontend")

    return app


app = create_app()
